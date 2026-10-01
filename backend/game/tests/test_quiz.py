import json
import random
import time
from contextlib import contextmanager
from datetime import timedelta
from unittest import mock

from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from game.models import Player, QuizAttempt, QuizQuestion, Room
from game.quiz import QUESTIONS, QUIZ_LENGTH, QUIZ_SECONDS, pick
from game.views import (
    PRESENCE_TTL,
    _beat,
    _presence,
    _presence_lock,
    maybe_advance_phase,
    room_state,
)


class QuizPickTests(SimpleTestCase):
    def test_pick_returns_a_permutation_of_the_whole_bank(self):
        picked = pick()
        self.assertEqual(len(picked), QUIZ_LENGTH)
        self.assertEqual({q.prompt for q in picked}, {q.prompt for q in QUESTIONS})

    def test_pick_never_repeats_a_question(self):
        picked = pick()
        self.assertEqual(len({q.prompt for q in picked}), len(picked))

    def test_pick_actually_shuffles_rather_than_returning_the_bank_in_file_order(self):
        # The bank is exactly one quiz long, so the only variety between two rooms is the
        # ORDER. A `pick` that quietly returned QUESTIONS untouched would pass every other
        # test here and make every room ask its questions in the same sequence, which is
        # exactly the thing this function exists to prevent.
        with mock.patch("game.quiz.random.sample", side_effect=lambda pool, n: tuple(pool)):
            self.assertEqual([q.prompt for q in pick()], [q.prompt for q in QUESTIONS])

    def test_pick_is_reproducible_under_a_seed(self):
        # Real seeding, not a patched-out `random.seed` — mocking the seeding function
        # proves nothing about reproducibility, it only proves the mock was called. This
        # also pins that the order comes from the stdlib RNG rather than a hardcoded
        # rotation, which is what makes a bug report from a classroom reproducible.
        state = random.getstate()
        try:
            random.seed(0)
            first = pick(3)
            random.seed(0)
            self.assertEqual(pick(3), first)
        finally:
            random.setstate(state)


class QuizBankTests(SimpleTestCase):
    def test_eight_questions(self):
        self.assertEqual(len(QUESTIONS), QUIZ_LENGTH)

    def test_every_question_has_four_distinct_choices(self):
        for i, q in enumerate(QUESTIONS):
            with self.subTest(question=i):
                self.assertEqual(len(q.choices), 4)
                self.assertEqual(len(set(q.choices)), 4)

    def test_every_correct_index_is_in_range(self):
        for i, q in enumerate(QUESTIONS):
            with self.subTest(question=i):
                self.assertIn(q.correct, range(4))

    def test_every_question_explains_its_answer(self):
        for i, q in enumerate(QUESTIONS):
            with self.subTest(question=i):
                self.assertGreater(len(q.explanation), 20)

    def test_a_question_is_never_trivially_guessable_by_position(self):
        # A Kahoot where the right answer is always first teaches position, not prompt
        # engineering. Require the correct index to be spread, not uniform-by-accident.
        positions = {q.correct for q in QUESTIONS}
        self.assertGreaterEqual(len(positions), 3)

    def test_no_correct_answer_is_the_longest_choice(self):
        # The mirror trap of the one above: a bank where the right answer is systematically the
        # most developed sentence is won by a reader who spots the careful wording, which is
        # exactly the reflex a quiz should not train. Equality is allowed, dominance is not.
        for i, q in enumerate(QUESTIONS):
            with self.subTest(question=i):
                self.assertLessEqual(
                    len(q.choices[q.correct]),
                    max(len(c) for j, c in enumerate(q.choices) if j != q.correct),
                )


@override_settings(ADMIN_PASSWORD="s3cret")
class QuizPlayTests(TestCase):
    """The gate itself: eight questions, two endpoints, and a room that will not start until they
    are answered. The bank tests above can all stay green while the game is unplayable, so every
    test here goes through HTTP from a real room, except where the ORM is the shorter way to the
    same state."""

    def setUp(self) -> None:
        self.c = Client()
        self.c.post("/api/admin/login", data=json.dumps({"password": "s3cret"}), content_type="application/json")
        # Process-local registry, so a test rollback does not clear it: without this a heartbeat
        # from an earlier test marks a same-numbered player online in a later one, and the advance
        # tests measure somebody else's presence.
        _presence.clear()

    # --- fixtures ---------------------------------------------------------------------------

    def _post(self, url: str, data: dict | None = None):
        return self.c.post(url, data=json.dumps(data or {}), content_type="application/json")

    def _create(self, tutorial: bool = True) -> Room:
        """A room through the admin endpoint, because that is where the bank is materialised — a
        room built on the ORM would have no questions and every assertion below would be about an
        empty gate."""
        code = json.loads(self._post("/api/admin/rooms", {"tutorial": tutorial}).content)["code"]
        return Room.objects.get(code=code)

    def _join(self, room: Room, name: str) -> Player:
        r = self._post(f"/api/rooms/{room.code}/join", {"name": name})
        self.assertEqual(r.status_code, 200, r.content)
        return room.players.get(name=name)

    def _start(self, room: Room) -> None:
        self.assertEqual(self._post(f"/api/admin/rooms/{room.code}/start").status_code, 200)
        room.refresh_from_db()

    def _joined(self, tutorial: bool = True, two: bool = False) -> tuple[Room, list[Player]]:
        """A room with players in it and the didacticiel open.

        Join before start, which is the order the real game has: the lobby fills first, and the
        admin launches when the room is ready. Joining after the start is a 409, pinned below."""
        room = self._create(tutorial=tutorial)
        names = ("ana", "bob") if two else ("ana",)
        players = [self._join(room, name) for name in names]
        self._start(room)
        return room, players

    def _online(self, room: Room, *players: Player) -> None:
        """Give these players a live heartbeat, as their own SSE stream would.
        `maybe_advance_phase` counts presence, and a test client that never opens a stream has no
        beats."""
        for p in players:
            _beat(room.code, p.pk)

    def _answer(self, room: Room, player: Player, question: int, choice: int):
        return self._post(
            f"/api/rooms/{room.code}/quiz",
            {"token": player.token, "question": question, "choice": choice},
        )

    def _next(self, room: Room):
        """The admin's ONE advance button — which is now what opens each question, and what opens
        the game after the eighth. Every fixture below that moves through the bank goes through
        here, because nothing else moves the room any more: the gate is not a gate that opens
        itself."""
        return self._post(f"/api/admin/rooms/{room.code}/next")

    def _walk_to(self, room: Room, index: int) -> None:
        """Put question `index` on screen with its window open, the way the admin's button does.

        FORWARD ONLY, which is the whole shape of the new flow: there is no going back to a
        question the room has left, so a test that needs question 3 on screen walks the room to 3
        rather than answering 0-2 first and hoping the cursor agrees.
        """
        while room.quiz_index < index:
            self.assertEqual(self._next(room).status_code, 200)
            room.refresh_from_db()
        self.assertEqual(room.quiz_index, index)
        self._open_window(room)

    def _move_deadline(self, room: Room, seconds: float) -> None:
        """Put the room's answering window `seconds` from now, as the passage of time would.

        THE SERVER'S CLOCK, never the test's: the deadline is stored, so a test moves it rather
        than sleeping. Fifteen seconds of real sleeping would be fifteen seconds of real sleeping
        per assertion, and a test suite is the wrong place to spend that.
        """
        Room.objects.filter(code=room.code).update(
            quiz_deadline=timezone.now() + timedelta(seconds=seconds)
        )
        room.refresh_from_db()

    def _open_window(self, room: Room) -> None:
        """The window as `admin_start` and the advance button leave it: fifteen seconds."""
        self._move_deadline(room, QUIZ_SECONDS)

    def _close_window(self, room: Room) -> None:
        """The window as the fifteen seconds leave it: already past."""
        self._elapse(room, QUIZ_SECONDS)

    def _elapse(self, room: Room, seconds: float) -> None:
        """Run the clock: `seconds` of the shared window have gone by.

        The deadline is an ABSOLUTE instant, so "sixteen seconds into the window" is a deadline of
        one second ago, not one of sixteen seconds ahead. Writing it the other way round — moving a
        deadline forward to express a lateness — produces a test that passes the exact case it
        exists to fail, which is why this is a helper with a name that says what it means and not a
        raw `_move_deadline(room, 16)` at every call site.
        """
        self._move_deadline(room, QUIZ_SECONDS - seconds)

    def _bank(self, room: Room) -> list[QuizQuestion]:
        """This room's eight, in `order`.

        Every read of the bank in this file goes through here. A bare `QuizQuestion.objects.get
        (order=i)` used to name the same row in every room — one bank, shared — and it silently
        stopped meaning "the question this player was shown" the moment a second room existed,
        which is the bug this file now pins shut.
        """
        return list(room.questions.order_by("order"))

    @contextmanager
    def _seeded(self, seed: int = 0):
        """Seed the stdlib RNG for the block, then put it back.

        `quiz.pick` draws from the global `random`, and `admin_create_room` calls it once per
        room, so ONE seed here makes a run of room creations a reproducible sequence of draws —
        which is what lets a test assert two rooms got *different* orders without being a
        1-in-40320 flake that passes by luck. Re-seeding before EACH room would pin them
        IDENTICAL, which is the bug; seeding once before the run is the fix. State is restored so
        a test does not decide the draw some later test gets.
        """
        state = random.getstate()
        try:
            random.seed(seed)
            yield
        finally:
            random.setstate(state)

    def _two_rooms(self) -> tuple[Room, Player, Room, Player]:
        """Two rooms, each with a player and its didacticiel open, drawn from one seeded run.

        One seed for the pair, so the two banks are two draws of the same distribution rather
        than one shuffled room and one untouched — that is what isolates "each room draws" from
        "the draw differs".
        """
        with self._seeded():
            first, first_players = self._joined()
            second, second_players = self._joined()
        return first, first_players[0], second, second_players[0]

    def _answer_all(self, room: Room, player: Player, up_to: int = QUIZ_LENGTH) -> None:
        """Answer questions 0..`up_to`-1, opening each one the way the admin does.

        It walks the room forward as it goes, which is not a convenience: under one shared clock
        question 2 cannot be answered while the room is still on question 0, so a helper that
        answered 0-7 against a frozen cursor would be testing an endpoint that no longer exists.
        The room is left ON the last question answered, not past it — moving on is the admin's
        button, and one of the tests below is about that button.
        """
        for i in range(up_to):
            self._walk_to(room, i)
            self.assertEqual(self._answer(room, player, i, 0).status_code, 200)

    def _answer_up_to_wrong(self, room: Room, player: Player, up_to: int = QUIZ_LENGTH, start: int = 0) -> None:
        """Answer questions `start`..`up_to`-1 with a choice that is never the key, so a score of 0
        in the snapshot is this test's doing and not a coincidence of which index the bank put its
        keys on. The room is walked to `start` first, for the reason `_answer_all` walks."""
        for i in range(start, up_to):
            self._walk_to(room, i)
            key = self._bank(room)[i].correct
            self.assertEqual(self._answer(room, player, i, (key + 1) % 4).status_code, 200)

    def _age_out(self, player: Player) -> None:
        """Push a player's last beat into the past, as a sleeping phone does, without sleeping.
        The registry's own lock is taken, so a test writing it is not doing what the SSE threads
        are protected from doing."""
        with _presence_lock:
            _presence[(player.room_id, player.pk)] = time.monotonic() - PRESENCE_TTL - 1

    def _assert_phase(self, room: Room, phase: str) -> None:
        room.refresh_from_db()
        self.assertEqual(room.phase, phase)

    def _assert_ok(self, response) -> None:
        self.assertEqual(response.status_code, 200, response.content)

    # --- the bank in the database ------------------------------------------------------------

    def test_creating_a_room_materialises_the_bank_in_the_order_pick_gave(self):
        # Patched where it is USED: `views` does `from game.quiz import pick as pick_questions`,
        # so `game.quiz.pick` is not the name `admin_create_room` calls. Asserting the order
        # against the bank as it happens to be shuffled would pin nothing — any order would pass.
        rotated = tuple(QUESTIONS[3:]) + tuple(QUESTIONS[:3])
        with mock.patch("game.views.pick_questions", return_value=rotated):
            room = self._create()
        stored = self._bank(room)
        self.assertEqual([q.order for q in stored], list(range(QUIZ_LENGTH)))
        self.assertEqual([q.prompt for q in stored], [q.prompt for q in rotated])
        # Every field is copied, not just the prompt: `correct` is the key and `explanation` is
        # what the reveal sends back, so a partial copy is a game nobody can finish honestly.
        for src, row in zip(rotated, stored, strict=True):
            self.assertEqual(
                (row.choices, row.correct, row.explanation),
                (list(src.choices), src.correct, src.explanation),
            )
        # And the rows belong to THIS room: the bank is materialised per room, so nothing is
        # shared between two of them and nothing is left over from a previous one.
        self.assertEqual({q.room_id for q in stored}, {room.code})

    def test_a_second_room_gets_its_own_bank_in_its_own_order(self):
        # THE OPPOSITE of what this used to assert. `QuizQuestion.order` was globally UNIQUE with
        # no room FK, so only one bank of eight could exist per database and the second room was
        # handed the first room's — the exact opposite of what `pick()`'s docstring promises
        # ("the order ONE ROOM asks its questions in"), and it made `pick()` dead code after the
        # first room ever created.
        first, _, second, _ = self._two_rooms()
        self.assertNotEqual(first.code, second.code)
        first_order = [q.prompt for q in self._bank(first)]
        second_order = [q.prompt for q in self._bank(second)]
        self.assertEqual(len(first_order), QUIZ_LENGTH)
        self.assertEqual(len(second_order), QUIZ_LENGTH)
        # The same eight questions, both times: the bank is one quiz long, so...
        self.assertEqual(set(first_order), {q.prompt for q in QUESTIONS})
        self.assertEqual(set(second_order), {q.prompt for q in QUESTIONS})
        # ...asked in DIFFERENT orders, which is the entire variety `pick()` offers.
        self.assertNotEqual(first_order, second_order)

    def test_each_room_reads_back_only_its_own_questions(self):
        # The per-room scoping of `_quiz_get`, end to end. With a global read the second room's
        # endpoint would answer with whichever eight the database returned first — this room's
        # player graded against questions they were never shown.
        first, _, second, _ = self._two_rooms()
        for room in (first, second):
            payload = self.c.get(f"/api/rooms/{room.code}/quiz").json()
            self.assertEqual(
                [q["prompt"] for q in payload["questions"]],
                [q.prompt for q in self._bank(room)],
            )
        self.assertNotEqual(
            [q.prompt for q in self._bank(first)],
            [q.prompt for q in self._bank(second)],
        )

    def test_a_room_only_answers_against_its_own_bank(self):
        # The POST half of the same scoping: `(room, order)` is the address, and a global read
        # would grade this player against another room's row — wrong key, wrong explanation, wrong
        # `QuizAttempt`. Order 0 exists in BOTH rooms, which is exactly the case a global
        # `filter(order=...)` gets wrong.
        first, first_player, second, second_player = self._two_rooms()
        here, there = self._bank(first)[0], self._bank(second)[0]
        self.assertEqual(here.order, there.order)
        self.assertNotEqual(here.pk, there.pk)
        # Answering THIS room's order 0 with THIS room's key reveals THIS room's explanation, and
        # the attempt names THIS room's row. Under a global read the endpoint would have found
        # whichever room's order-0 row came first — the wrong key, the wrong explanation, and an
        # attempt against a question that was never on the player's screen.
        r = self._answer(first, first_player, 0, here.correct)
        self.assertEqual(r.json()["correct"], True)
        self.assertEqual(r.json()["explanation"], here.explanation)
        self.assertEqual(QuizAttempt.objects.get().question_id, here.pk)
        # The other room's bank is untouched: same order, still unanswered, and its own key is what
        # it will be graded against.
        self.assertEqual(QuizAttempt.objects.filter(player=second_player).count(), 0)
        other = self._answer(second, second_player, 0, there.correct)
        self.assertEqual(other.json()["explanation"], there.explanation)
        self.assertEqual(QuizAttempt.objects.filter(player=second_player).get().question_id, there.pk)

    def test_the_bank_survives_a_restart(self):
        # The bank is not the game's: a relaunch re-asks the same eight in the same order rather
        # than re-rolling the questions under the players who have already answered them. Now
        # that the bank hangs off the room, this is a cascade boundary being respected —
        # `admin_restart` deletes rounds, never the room, so the room's questions stay.
        room, players = self._joined()
        self._online(room, players[0])
        self._answer_all(room, players[0])
        before = [q.prompt for q in self._bank(room)]
        self.assertEqual(self._post(f"/api/admin/rooms/{room.code}/restart").status_code, 200)
        self.assertEqual(room.questions.count(), QUIZ_LENGTH)
        # The same questions in the SAME order, so an attempt already made still means something.
        self.assertEqual([q.prompt for q in self._bank(room)], before)
        # The attempts are gone, and the questions they pointed at are not.
        self.assertEqual(QuizAttempt.objects.count(), 0)

    # --- reading the questions ---------------------------------------------------------------

    def test_get_returns_the_questions_without_the_key(self):
        # The whole reason this endpoint exists in this shape. The payload is JSON on the wire: a
        # `correct` field here is an answer key in devtools, and a QCM with its key in the payload
        # is a form with a lookup table.
        room, players = self._joined()
        r = self.c.get(f"/api/rooms/{room.code}/quiz")
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        self.assertEqual(len(payload["questions"]), QUIZ_LENGTH)
        for q in payload["questions"]:
            self.assertEqual(set(q), {"order", "prompt", "choices"})
            self.assertEqual(len(q["choices"]), 4)
        self.assertEqual([q["order"] for q in payload["questions"]], list(range(QUIZ_LENGTH)))
        # And not under another name either: no explanation from the bank appears in the raw body.
        raw = r.content.decode()
        for src in QUESTIONS:
            with self.subTest(prompt=src.prompt[:20]):
                self.assertNotIn(src.explanation, raw)

    def test_get_accepts_a_token_and_answers_with_the_callers_own_orders(self):
        # A `?token=` on the GET is accepted, and all it can resolve to is the caller's OWN
        # progress. It carries no key, so there is nothing for it to leak, and a 403 here would
        # break a screen that has a token in hand.
        room, players = self._joined()
        r = self.c.get(f"/api/rooms/{room.code}/quiz?token={players[0].token}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["questions"]), QUIZ_LENGTH)
        self.assertEqual(r.json()["answered_orders"], [])

    def test_get_reports_the_orders_this_player_has_answered(self):
        # THE LOCK, AND WHY IT IS A LIST. The question on screen belongs to the ROOM, so "have I
        # done THIS one" is a membership question and not a count: a player who answered question
        # 0 and then looked away has one answered and is looking at question 4, which they have not
        # done. A client that resumed on a count would re-ask the four it had skipped past, or lock
        # a player out of a question they never saw — both are the same bug wearing two hats.
        room, players = self._joined()
        player = players[0]
        self._answer_up_to_wrong(room, player, up_to=3)
        r = self.c.get(f"/api/rooms/{room.code}/quiz?token={player.token}")
        self.assertEqual(r.status_code, 200)
        # The ORDERS, in order, and nothing else: no count, and never the key.
        self.assertEqual(r.json()["answered_orders"], [0, 1, 2])
        # It reports ATTEMPTS, not right answers: the didacticiel's score is a separate number and
        # a player who got them all wrong has still been asked them all. A client that locked on
        # the score would re-ask the three it already sat.
        self.assertEqual(QuizAttempt.objects.filter(player=player, correct=True).count(), 0)
        # Top level, next to the questions, and a list of ints — the client declares it as
        # `number[]`, so a nested object or a string would be a missed lock rather than an error.
        self.assertEqual(set(r.json()), {"questions", "answered_orders"})
        for order in r.json()["answered_orders"]:
            self.assertIsInstance(order, int)
        # And every order is one this room actually asked, which is what lets the client test
        # membership without defending itself against a number the server produced.
        self.assertLessEqual(max(r.json()["answered_orders"]), QUIZ_LENGTH - 1)

    def test_get_reports_an_empty_list_for_a_player_who_has_answered_nothing(self) -> None:
        room, players = self._joined(two=True)
        self._answer_up_to_wrong(room, players[0], up_to=4)
        # Somebody else's progress is not the caller's: bob has answered nothing and is told
        # nothing about ana, or about the room.
        for player in players:
            with self.subTest(player=player.name):
                r = self.c.get(f"/api/rooms/{room.code}/quiz?token={player.token}")
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["answered_orders"], [] if player.name == "bob" else [0, 1, 2, 3])

    def test_get_reports_an_empty_list_for_a_missing_a_wrong_or_a_foreign_token(self) -> None:
        # THE THREE NON-TOKENS, and none of them is an error. The client degrades to the old
        # behaviour rather than to a blank screen, so an empty list is the correct answer for a
        # token that identifies nobody — and a 403 would take away a payload the caller is already
        # entitled to.
        first, foreign, second, owner = self._two_rooms()
        self._answer_up_to_wrong(first, foreign, up_to=2)
        for label, query in (
            ("absent", ""),
            ("empty", "?token="),
            ("unknown", "?token=nope"),
            # THE FOREIGN TOKEN, and the one worth pinning: this is the first place on this
            # endpoint where a token is read at all, and the lookup is scoped to the room exactly
            # as `answer` and `rate` scope theirs. Without that scope, bob's two answers in `first`
            # would be listed for a player of `second` — told he has done questions he has never
            # seen, on a client that then locks him out of them.
            ("another room's", f"?token={foreign.token}"),
        ):
            with self.subTest(token=label):
                r = self.c.get(f"/api/rooms/{second.code}/quiz{query}")
                self.assertEqual(r.status_code, 200, r.content)
                self.assertEqual(r.json()["answered_orders"], [])
                # The questions are still all there in every case: a caller the server cannot
                # identify gets the bank it always got, not a refusal.
                self.assertEqual(len(r.json()["questions"]), QUIZ_LENGTH)
        # The mirror: the same token in its OWN room resolves to its own orders, so the scope costs
        # nothing to the player it belongs to.
        r = self.c.get(f"/api/rooms/{first.code}/quiz?token={foreign.token}")
        self.assertEqual(r.json()["answered_orders"], [0, 1])
        r = self.c.get(f"/api/rooms/{second.code}/quiz?token={owner.token}")
        self.assertEqual(r.json()["answered_orders"], [])
        self._answer_up_to_wrong(second, owner, up_to=5)
        r = self.c.get(f"/api/rooms/{second.code}/quiz?token={owner.token}")
        self.assertEqual(r.json()["answered_orders"], [0, 1, 2, 3, 4])
        # Five answers in `second` do not leak into `first`, where the same foreign token is
        # pinned at its own two.
        self.assertEqual(
            self.c.get(f"/api/rooms/{first.code}/quiz?token={foreign.token}").json()["answered_orders"],
            [0, 1],
        )

    def test_answered_orders_still_holds_after_a_reconnect_mid_quiz(self) -> None:
        # §2: « Quelqu'un qui se reconnecte n'est pas un nouvel arrivant : il est retrouvé par son
        # jeton […] Il retombe sur la question en cours, verrouillée s'il y a déjà répondu. » The
        # lock is what makes that sentence true, and it is read from this list: the phone comes
        # back on question 4, finds 4 in the list, and renders it answered rather than offering it
        # again. Asserted from a second `GET` with the same token rather than from the first
        # response, because the reconnect is a fresh screen.
        room, players = self._joined()
        player = players[0]
        self._answer_up_to_wrong(room, player, up_to=4)
        self._walk_to(room, 4)
        payload = self.c.get(f"/api/rooms/{room.code}/quiz?token={player.token}").json()
        self.assertEqual(payload["answered_orders"], [0, 1, 2, 3])
        # Question 4 is on screen, unanswered, and still answerable.
        self.assertNotIn(4, payload["answered_orders"])
        self.assertEqual(self._answer(room, player, 4, 0).status_code, 200)
        # And the retry after the lock is the ordinary 409, not a re-score.
        self.assertEqual(self._answer(room, player, 4, 1).status_code, 409)

    def test_get_on_an_unknown_room_is_404(self):
        self.assertEqual(self.c.get("/api/rooms/NOPE/quiz").status_code, 404)

    # --- answering ---------------------------------------------------------------------------

    def test_answering_reveals_the_answer_the_explanation_and_the_score(self):
        room, players = self._joined()
        player = players[0]
        key = self._bank(room)[0]
        right = self._answer(room, player, 0, key.correct)
        self.assertEqual(right.status_code, 200)
        self.assertEqual(
            set(right.json()), {"correct", "correct_index", "explanation", "score"}
        )
        self.assertEqual(right.json()["correct"], True)
        self.assertEqual(right.json()["explanation"], key.explanation)
        self.assertEqual(right.json()["score"], 1)

    def test_correct_index_is_the_key_on_a_right_answer_and_on_a_wrong_one(self):
        # `correct` (did I get it right) and `correct_index` (which one was right) answer different
        # questions, and the screen needs both. The index cannot come from the client, which never
        # holds the bank key — so it has to be in the response.
        room, players = self._joined()
        player = players[0]
        key = self._bank(room)[0]

        # On a RIGHT answer the two fields coincide, so this alone cannot tell the key's position
        # from the player's own choice echoed back...
        right = self._answer(room, player, 0, key.correct)
        self.assertEqual(right.json()["correct"], True)
        self.assertEqual(right.json()["correct_index"], key.correct)

        # ...which is exactly why the WRONG answer is the one that matters: it is the only case
        # where `correct` is False and `correct_index` is not the choice that was sent. Here
        # `correct_index` is still the key of the question that was ANSWERED (order 1, not
        # order 0), and the choice the player picked is not it.
        second = self._bank(room)[1]
        wrong_choice = (second.correct + 1) % 4
        # The room is on question 1 now: under one shared clock the player cannot be answering
        # order 1 while the room still shows order 0, so the admin's button moves it.
        self._next(room)
        wrong = self._answer(room, player, 1, wrong_choice)
        self.assertEqual(wrong.json()["correct"], False)
        self.assertEqual(wrong.json()["correct_index"], second.correct)
        self.assertNotEqual(wrong_choice, wrong.json()["correct_index"])
        # The wrong answer scored nothing and the right one still stands.
        self.assertEqual(wrong.json()["score"], 1)

    def test_a_wrong_answer_is_wrong_and_scores_nothing(self):
        room, players = self._joined()
        player = players[0]
        key = self._bank(room)[0]
        wrong = (key.correct + 1) % 4
        r = self._answer(room, player, 0, wrong)
        self.assertEqual((r.json()["correct"], r.json()["score"]), (False, 0))
        # The attempt records what was chosen, not just whether it was right: an admin reading a
        # bad game needs to see WHICH distractor the room fell for.
        self.assertEqual(QuizAttempt.objects.get().choice, wrong)

    def test_the_score_counts_right_answers_only(self):
        # Three answers, one right. The bank is drawn per room, so every index here is read off the row
        # rather than assumed: a test that hard-coded "question 2's key is 0" would pass or fail on
        # the draw.
        room, players = self._joined()
        player = players[0]
        bank = self._bank(room)
        right = self._answer(room, player, 0, bank[0].correct)
        self._walk_to(room, 1)
        wrong = self._answer(room, player, 1, (bank[1].correct + 1) % 4)
        self.assertEqual((right.json()["score"], wrong.json()["score"]), (1, 1))
        self.assertEqual(player.quiz_score(), 1)
        self._walk_to(room, 2)
        self._answer(room, player, 2, (bank[2].correct + 1) % 4)
        self.assertEqual(player.quiz_score(), 1)

    def test_a_question_can_only_be_answered_once(self):
        room, players = self._joined()
        player = players[0]
        self.assertEqual(self._answer(room, player, 0, 0).status_code, 200)
        again = self._answer(room, player, 0, 1)
        self.assertEqual(again.status_code, 409)
        # The stored answer is the FIRST one: a retry that lands after the write must not rewrite
        # what the player said, only tell them they already said it.
        self.assertEqual(QuizAttempt.objects.get().choice, 0)
        self.assertEqual(QuizAttempt.objects.count(), 1)

    def test_a_duplicate_row_committed_mid_flight_returns_409(self):
        # The durable backstop the view's own check cannot see: two submits from one phone can both
        # find no attempt before either writes, and `unique_together` is the source of truth. Same
        # race `answer` has, same story — a retry on a flaky phone is indistinguishable from a
        # second answer. The `IntegrityError` path is what turns a lost race into a 409 rather than
        # a 500, so it needs a test of its own: nothing else here can reach it.
        room, players = self._joined()
        player = players[0]
        key = self._bank(room)[0]
        winner = QuizAttempt.objects.create(player=player, question=key, choice=1, correct=False)

        class _BeforeTheRace:
            """The duplicate check answering from before the winner's row landed."""

            def exists(self) -> bool:
                return False

        with mock.patch.object(QuizAttempt.objects, "filter", return_value=_BeforeTheRace()):
            r = self._answer(room, player, 0, 0)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(json.loads(r.content), {"error": "already answered"})
        # The row that survived is the winner's, choice and all: a lost race must not overwrite it.
        self.assertEqual(QuizAttempt.objects.get().pk, winner.pk)
        self.assertEqual(QuizAttempt.objects.get().choice, 1)
        self.assertEqual(QuizAttempt.objects.count(), 1)

    def test_out_of_range_choice_is_refused(self):
        room, players = self._joined()
        for choice in (-1, 4, "0", True, None):
            with self.subTest(choice=choice):
                r = self._answer(room, players[0], 0, choice)
                self.assertEqual(r.status_code, 400)
        # Nothing was written, so the question is still answerable afterwards.
        self.assertEqual(QuizAttempt.objects.count(), 0)
        self.assertEqual(self._answer(room, players[0], 0, 0).status_code, 200)

    def test_an_unknown_question_index_is_refused(self):
        room, players = self._joined()
        # NOT AN INT: 400, a malformed body rather than a closed question. `isinstance(True, int)`
        # is True, so the boolean is in this list too — it is the guard the view has to make
        # before `order` is compared with the room's cursor.
        for order in ("0", None, True, 1.0, [0]):
            with self.subTest(question=order):
                r = self._answer(room, players[0], order, 0)
                self.assertEqual(r.status_code, 400, r.content)
        # AN INT THAT IS NOT THE QUESTION ON SCREEN: 409, because the room is on question 0 and
        # there is no other question in play. Eight and 99 are outside the bank, -1 is below it,
        # and 3 is a real question of this room that nobody has been asked yet — which is the case
        # worth having here, since it is the one a client with a stale cursor actually sends.
        for order in (QUIZ_LENGTH, 99, -1, 3):
            with self.subTest(question=order):
                r = self._answer(room, players[0], order, 0)
                self.assertEqual(r.status_code, 409, r.content)
                self.assertEqual(r.json()["error"], "not the current question")
        self.assertEqual(QuizAttempt.objects.count(), 0)

    def test_a_question_the_room_has_no_row_for_is_refused(self):
        # `no such question` is a 400 about the BANK rather than about the clock, and it needs its
        # own fixture: the cursor has to point at an order this room never materialised, which no
        # normal game produces and which no client can send on purpose. Unreachable over HTTP in
        # normal play is exactly why it needs a test — it is the last thing standing between a
        # stale row and a KeyError.
        room, players = self._joined()
        Room.objects.filter(code=room.code).update(quiz_index=42)
        r = self._answer(room, players[0], 42, 0)
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.json()["error"], "no such question")
        self.assertEqual(QuizAttempt.objects.count(), 0)

    def test_a_blank_or_foreign_token_is_refused(self):
        # 403 and not 404 or 400: the token is the authentication, and it is checked against this
        # room, so a token from another room must not score a question here.
        room, players = self._joined()
        other = self._join(self._create(), "zoe")
        for token in ("", "nope", other.token):
            with self.subTest(token=token):
                r = self._post(f"/api/rooms/{room.code}/quiz", {"token": token, "question": 0, "choice": 0})
                self.assertEqual(r.status_code, 403)
        self.assertEqual(QuizAttempt.objects.count(), 0)

    def test_answering_outside_the_quiz_phase_is_refused(self):
        # The gate, from the other side. The admin can open the game early — one button per
        # question, and the eighth one opens the game — and after that the endpoint has to be
        # closed, otherwise a late submit would write an attempt against a game that is already
        # being played. Seven advances from question 0 gets there, which is also the shortest
        # statement of "the button opens the next question seven times and then the game".
        room, players = self._joined()
        player = players[0]
        for _ in range(QUIZ_LENGTH - 1):
            self.assertEqual(self._next(room).status_code, 200)
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, QUIZ_LENGTH - 1)
        self.assertEqual(self._next(room).status_code, 200)
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.PLAYING)
        self.assertEqual(self._answer(room, player, 0, 0).status_code, 409)
        self.assertEqual(QuizAttempt.objects.count(), 0)

    def test_join_is_refused_once_the_quiz_is_open(self):
        # The roster is fixed at the gate: a player who joins halfway through has not done the
        # eight questions, and letting them in would put them in the running with none of them.
        room, _ = self._joined()
        r = self._post(f"/api/rooms/{room.code}/join", {"name": "zoe"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(room.players.count(), 1)

    # --- the shared window -------------------------------------------------------------------

    def test_start_puts_question_zero_on_screen_with_fifteen_seconds(self):
        # THE WINDOW OPENS WITH THE QUESTION. An admin who presses start and leaves a class on a
        # blank screen for fifteen seconds of them wondering whether something is wrong has spent
        # the first question on nothing, and the fixture every other test in this file builds on
        # would not notice.
        room, players = self._joined()
        self.assertEqual((room.phase, room.quiz_index), (Room.Phase.QUIZ, 0))
        self.assertIsNotNone(room.quiz_deadline)
        seconds = (room.quiz_deadline - timezone.now()).total_seconds()
        self.assertLessEqual(seconds, QUIZ_SECONDS)
        self.assertGreater(seconds, QUIZ_SECONDS - 5)
        # And the snapshot says so, in the client's own unit.
        quiz = room_state(room.code)["quiz"]
        self.assertEqual(quiz["index"], 0)
        self.assertEqual(quiz["total"], QUIZ_LENGTH)
        # Fifteen seconds, within the millisecond the snapshot's own `server_ms` truncates and the
        # few milliseconds between the deadline being written and the snapshot being built.
        self.assertAlmostEqual(quiz["deadline_ms"] - quiz["server_ms"], QUIZ_SECONDS * 1000, delta=100)

    def test_an_answer_one_second_before_the_deadline_is_accepted(self):
        # FOURTEEN SECONDS IN. The window is fifteen, so this is the last moment it is legal, and
        # the request itself costs a millisecond or two — which is why the margin below is one
        # second and not one millisecond.
        room, players = self._joined()
        self._elapse(room, QUIZ_SECONDS - 1)
        r = self._answer(room, players[0], 0, 0)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(QuizAttempt.objects.count(), 1)

    def test_an_answer_one_second_past_the_deadline_is_refused(self):
        # SIXTEEN SECONDS IN, and the refusal is THE DEADLINE and nothing else. Every other reason
        # the endpoint can say no is held still across this pair of tests: the phase is `quiz`,
        # the token is this player's, the order is the one on screen, the choice is in range, and
        # there is no attempt yet — so the clock is the only variable, and the error names it.
        room, players = self._joined()
        self._elapse(room, QUIZ_SECONDS + 1)
        r = self._answer(room, players[0], 0, 0)
        self.assertEqual(r.status_code, 409, r.content)
        self.assertEqual(r.json(), {"error": "time's up"})
        self.assertEqual(QuizAttempt.objects.count(), 0)
        # A second attempt at the same question is refused by the deadline too, and not by the
        # duplicate check: a player who was late twice is late, not already answered.
        self.assertEqual(self._answer(room, players[0], 0, 1).json()["error"], "time's up")
        self.assertEqual(QuizAttempt.objects.count(), 0)

    def test_a_player_who_did_not_tap_blocks_nobody(self):
        # §2: « Le joueur qui n'a pas cliqué n'a rien, et c'est tout : il n'est pas bloqué, et il
        # ne bloque personne. » Ana taps, bob never does, the deadline passes, and bob's nothing is
        # a private loss: his screen shows the reveal for a question he never answered, and the
        # room moves on when the admin presses the button.
        room, players = self._joined(two=True)
        ana, bob = players
        self._online(room, ana, bob)
        self._close_window(room)
        # Bob is the player the rule is about, so he is asserted on directly: no attempt of his
        # own, and the server still answers him like everybody else.
        self.assertEqual(self._answer(room, bob, 0, 0).status_code, 409)
        self.assertEqual(QuizAttempt.objects.filter(player=bob).count(), 0)
        # The room's `done` counts ana only, so a bar of 1/2 is an honest reading of the room and
        # not one stuck at zero because a phone slept.
        self.assertEqual(room_state(room.code)["quiz"]["done"], 0)
        self.assertEqual(self._next(room).status_code, 200)
        room.refresh_from_db()
        self.assertEqual((room.phase, room.quiz_index), (Room.Phase.QUIZ, 1))

    def test_an_expired_window_is_closed_until_the_admin_opens_the_next_one(self):
        # The expiry does NOT advance anything: it reveals, and only the admin moves. Which is why
        # this is 409 here and 200 one button later — the player is not locked out of the
        # didacticiel, they are locked out of THIS question, and the next one is fifteen seconds
        # away whenever the teacher decides.
        room, players = self._joined()
        self._close_window(room)
        self.assertEqual(self._answer(room, players[0], 0, 0).status_code, 409)
        self._assert_phase(room, Room.Phase.QUIZ)
        self._next(room)
        room.refresh_from_db()
        self._open_window(room)
        self.assertEqual(self._answer(room, players[0], 1, 0).status_code, 200)

    def test_a_room_with_no_open_window_refuses_rather_than_asking_forever(self):
        # `quiz_deadline` NULL means NO WINDOW, not an open one, and it has to read that way. A
        # room left in the quiz phase with a null deadline — a restart that has not been started
        # again, a room an admin parked on the ORM — would otherwise accept answers into a
        # question nobody can be shown the reveal for, and the snapshot's `deadline_ms` of 0 would
        # have every client counting down from 1970.
        room, players = self._joined()
        Room.objects.filter(code=room.code).update(quiz_deadline=None)
        r = self._answer(room, players[0], 0, 0)
        self.assertEqual(r.status_code, 409, r.content)
        self.assertEqual(r.json(), {"error": "time's up"})
        self.assertEqual(QuizAttempt.objects.count(), 0)
        quiz = room_state(room.code)["quiz"]
        self.assertFalse(quiz["revealed"])
        self.assertIsNone(quiz["correct"])
        self.assertEqual(quiz["deadline_ms"], 0)

    # --- the admin's button ------------------------------------------------------------------

    def test_nothing_opens_the_game_by_itself(self):
        # THE REMOVAL, pinned. The gate used to open the game when the last online player answered
        # the eighth question, and there is a test for it in this file's history. Nothing does that
        # now, and the way to prove a negative is to do the whole quiz and look: two players, both
        # online, both answering every question, and the room still in `quiz` with the answer to
        # every one of them stored. §2 hands the advance to a person.
        #
        # The two players cannot both answer the SAME question — one question is for the room, and
        # the second answer to it is the 409 below — so they are walked together, question by
        # question, and that is the shape of a real class anyway: one question, everybody in it.
        room, players = self._joined(two=True)
        self._online(room, *players)
        for i in range(QUIZ_LENGTH):
            self._walk_to(room, i)
            self.assertEqual(self._answer(room, players[0], i, 0).status_code, 200)
            self.assertEqual(self._answer(room, players[1], i, 0).status_code, 200)
        self._assert_phase(room, Room.Phase.QUIZ)
        self.assertEqual(QuizAttempt.objects.count(), QUIZ_LENGTH * 2)
        self.assertEqual(room.quiz_index, QUIZ_LENGTH - 1)
        # And there is no code left that could have done it: the phase is not in the tuple the
        # auto-advance dispatches on, so even a direct call is a no-op.
        maybe_advance_phase(room)
        self._assert_phase(room, Room.Phase.QUIZ)

    def test_a_room_nobody_is_connected_to_does_not_open_itself_either(self):
        # The vacuous-truth case the old gate had to be defended against, kept for the same reason
        # and now covering a stronger claim: with every phone dropped, the room does not open on
        # the last answer, does not open on an auto-advance call, and does not open on a stream
        # nobody is watching. Only the admin's button moves it.
        room, players = self._joined()  # no heartbeat: nobody is streaming
        self._answer_all(room, players[0])
        self._assert_phase(room, Room.Phase.QUIZ)
        self.assertEqual(room_state(room.code)["quiz"]["done"], 0)
        maybe_advance_phase(room)
        self._assert_phase(room, Room.Phase.QUIZ)
        # The whole quiz has been sat, so the button's next press is the one that opens the game —
        # which is the eighth, not the first, and that difference is the claim above.
        self.assertEqual(self._next(room).status_code, 200)
        self._assert_phase(room, Room.Phase.PLAYING)

    def test_the_button_opens_the_next_question_and_stays_in_the_quiz(self):
        # ONE BUTTON, SEVEN TIMES. It moves the room's cursor and opens a fresh fifteen-second
        # window, and it does not touch the phase — the gate is not a gate that closes itself
        # either.
        room, players = self._joined()
        for index in range(1, QUIZ_LENGTH):
            self.assertEqual(self._next(room).status_code, 200)
            room.refresh_from_db()
            self.assertEqual((room.phase, room.quiz_index), (Room.Phase.QUIZ, index))
            self.assertGreater(room.quiz_deadline, timezone.now())
        # Nothing was scored by pressing it: the button moves the game, it does not answer for the
        # class.
        self.assertEqual(QuizAttempt.objects.count(), 0)

    def test_the_button_opens_the_game_after_the_eighth_reveal(self):
        # « Après la huitième révélation, le même bouton ouvre la partie. » The eighth press, from
        # question 7, has nowhere left to go in the bank, so it opens the writing phase. That is
        # the ONLY thing that opens the game now, and it takes an admin.
        room, players = self._joined()
        self._answer_all(room, players[0])
        self._assert_phase(room, Room.Phase.QUIZ)
        r = self._next(room)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "phase": "playing"})
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.PLAYING)
        # The cursor is left where the eighth question left it rather than wrapped: a relaunch
        # resets it, and nothing else should quietly move it.
        self.assertEqual(room.quiz_index, QUIZ_LENGTH - 1)

    def test_the_button_pressed_before_the_deadline_closes_the_question_without_revealing_it(self):
        # THE HURRY-UP PATH, and it is deliberate: « Le même bouton referme la question en cours
        # quand il est pressé avant l'échéance, pour passer plus vite quand la classe a compris. »
        # Two things follow and both are asserted. The next question opens immediately, with its
        # own fifteen seconds — not zero, and not the old deadline. And the question that was
        # skipped is NEVER revealed: the key is gone with the question, because the reveal is read
        # off the deadline of whatever is on screen and nothing writes a deadline for a question
        # the room has left.
        room, players = self._joined()
        player = players[0]
        skipped = self._bank(room)[0]
        self.assertFalse(room_state(room.code)["quiz"]["revealed"])
        self.assertEqual(self._next(room).status_code, 200)
        room.refresh_from_db()
        self.assertEqual((room.phase, room.quiz_index), (Room.Phase.QUIZ, 1))
        # A fresh window, on the new question, and the new key is still withheld.
        quiz = room_state(room.code)["quiz"]
        self.assertEqual(quiz["index"], 1)
        self.assertFalse(quiz["revealed"])
        self.assertIsNone(quiz["correct"])
        self.assertGreater(quiz["deadline_ms"], quiz["server_ms"])
        # The skipped question's row is untouched — it has no reveal of its own to leak, and the
        # player never answered it either.
        self.assertEqual(QuizAttempt.objects.count(), 0)
        self.assertFalse(QuizAttempt.objects.filter(player=player, question=skipped).exists())
        # And the room can still be walked from here to the end: a skipped question is skipped,
        # not fatal.
        self.assertEqual(self._answer(room, player, 1, 0).status_code, 200)

    def test_an_unmaterialised_bank_does_not_trap_the_room_in_the_quiz(self):
        # `Room._next_quiz` counts THIS room's questions, so a bank that was never materialised
        # (count 0) falls straight through to opening the game. A gate with nothing in it must not
        # be a gate that cannot be left: the admin would be pressing a button that does nothing,
        # with no error to tell them so.
        room = self._create(tutorial=True)
        self._join(room, "ana")
        self._start(room)
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.QUIZ)
        room.questions.all().delete()
        self.assertEqual(self._next(room).status_code, 200)
        self._assert_phase(room, Room.Phase.PLAYING)

    def test_another_rooms_questions_do_not_end_this_rooms_didacticiel(self):
        # The per-room scoping of the cursor, from the other side. `_next_quiz` compares the cursor
        # with `self.questions.count()` — this room's eight. A global count would read sixteen and
        # let the room's button open eight phantom questions that exist in nobody's bank, each one
        # a question on screen that no phone can render.
        room, players = self._joined()
        self._create()  # a second room, with its own eight
        self.assertEqual(room_state(room.code)["quiz"]["total"], QUIZ_LENGTH)
        for _ in range(QUIZ_LENGTH - 1):
            self._next(room)
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, QUIZ_LENGTH - 1)
        # The eighth press opens the game, and it does it on the eighth — not the sixteenth.
        self.assertEqual(self._next(room).status_code, 200)
        self._assert_phase(room, Room.Phase.PLAYING)

    def test_the_quiz_score_does_not_reach_the_final_ranking(self):
        # A gate you must pass must not also be points you can farm: eight out of eight is
        # `score: 0` on the board, and the final ranking is built from the rounds alone. The board
        # only means anything once the game is open, so the admin's button opens it.
        room, players = self._joined()
        player = players[0]
        self._online(room, player)
        bank = self._bank(room)
        for order in range(QUIZ_LENGTH):
            self._walk_to(room, order)
            self.assertEqual(self._answer(room, player, order, bank[order].correct).status_code, 200)
        self.assertEqual(player.quiz_score(), QUIZ_LENGTH)
        self._next(room)
        self._assert_phase(room, Room.Phase.PLAYING)
        self.assertEqual(room_state(room.code)["players"][0]["score"], 0)

    # --- the snapshot ------------------------------------------------------------------------

    def test_the_snapshot_carries_the_whole_quiz_block(self):
        # Room-level, not per-viewer: `room_state(code)` takes no token, so it cannot know who is
        # reading. This is the block every phone and the admin screen draw the same question from,
        # and it is pinned WHOLE — a key that went missing, or turned into a null, would break the
        # countdown on every screen at once, and a subset check would not notice either.
        room, players = self._joined(two=True)
        ana, _bob = players
        self._online(room, ana, _bob)
        self._assert_phase(room, Room.Phase.QUIZ)
        quiz = room_state(room.code)["quiz"]
        self.assertEqual(
            set(quiz),
            {
                "index", "total", "deadline_ms", "server_ms", "revealed", "correct",
                "explanation", "done", "online_count", "score",
            },
        )
        self.assertEqual((quiz["index"], quiz["total"]), (0, QUIZ_LENGTH))
        # THE THREE CLOCKS, in the unit the client reads them in: two epoch MILLISECONDS, and their
        # difference is the window. A phone corrects its skew with `localNow - server_ms` and counts
        # down on `deadline_ms`, so a phone whose own clock is two minutes out still locks on the
        # right side of the question.
        self.assertIsInstance(quiz["server_ms"], int)
        self.assertIsInstance(quiz["deadline_ms"], int)
        self.assertGreater(quiz["server_ms"], 1_600_000_000_000)  # after 2020, in MILLISECONDS
        self.assertAlmostEqual(quiz["deadline_ms"] - quiz["server_ms"], QUIZ_SECONDS * 1000, delta=100)
        # And nothing is revealed yet, which is the other half of the block.
        self.assertFalse(quiz["revealed"])
        self.assertIsNone(quiz["correct"])
        self.assertIsNone(quiz["explanation"])
        self.assertEqual((quiz["done"], quiz["online_count"], quiz["score"]), (0, 2, 0))
        # Ana answers the question on screen: `done` counts the players who answered THIS one.
        self._assert_ok(self._answer(room, ana, 0, 0))
        quiz = room_state(room.code)["quiz"]
        self.assertEqual((quiz["done"], quiz["online_count"]), (1, 2))
        # And `done` is per question, not per game: the moment the room moves on it is back to
        # zero, which is what makes it a live answer meter rather than a scoreboard.
        self._next(room)
        self.assertEqual(room_state(room.code)["quiz"]["done"], 0)

    def test_the_key_and_the_explanation_are_null_until_the_reveal_on_every_question(self):
        # THE SECURITY PROPERTY, ON ALL EIGHT. One question proves nothing: a gate written against
        # `room.quiz_index == 0`, or a window only the first question ever opens, would leak the key
        # for questions 1 to 7 into a payload every phone in the room holds and devtools prints —
        # a QCM the class reads instead of plays. So the whole bank is walked, and both fields are
        # asserted null on the way in, before the deadline, from a room of players who have answered
        # nothing.
        room, players = self._joined()
        for index in range(QUIZ_LENGTH):
            with self.subTest(question=index):
                self._walk_to(room, index)
                key = self._bank(room)[index]
                quiz = room_state(room.code)["quiz"]
                self.assertEqual(quiz["index"], index)
                self.assertFalse(quiz["revealed"])
                self.assertIsNone(quiz["correct"])
                self.assertIsNone(quiz["explanation"])
                # And not under another name either: no explanation from the bank appears anywhere
                # in the serialised snapshot, which is the bytes the phones actually parse.
                self.assertNotIn(key.explanation, json.dumps(room_state(room.code)))

    def test_the_reveal_carries_the_key_and_the_explanation_on_every_question(self):
        # AND THE OTHER HALF, ON ALL EIGHT, because the two halves fail independently: a reveal
        # wired to the wrong row hands every player question 2's answer on question 5. `correct` is
        # read off THE ROW rather than off the bank file, so this catches a reveal that returns a
        # real key for the wrong question. The expiry is the SERVER's clock, moved by the fixture
        # rather than slept through.
        room, players = self._joined()
        for index in range(QUIZ_LENGTH):
            with self.subTest(question=index):
                self._walk_to(room, index)
                self._close_window(room)
                key = self._bank(room)[index]
                quiz = room_state(room.code)["quiz"]
                self.assertEqual(quiz["index"], index)
                self.assertTrue(quiz["revealed"])
                self.assertEqual(quiz["correct"], key.correct)
                self.assertEqual(quiz["explanation"], key.explanation)
                # The answers are refused AFTER the reveal, and this is the reason: the key is
                # public, the WINDOW is not. A phone that unlocked on `revealed` and then posted
                # would let every player who took a moment to read the explanation score late.
                self.assertEqual(self._answer(room, players[0], index, 0).status_code, 409)

    def test_a_tapped_question_is_refused_after_the_reveal_too(self):
        # The same rule from the other side of the deadline, and the one a client is most likely to
        # get wrong: the reveal unlocks the KEY, not the question. The player answered inside the
        # window, the window has since closed, and the retry from a flaky phone is refused by the
        # clock rather than scoring twice.
        room, players = self._joined()
        player = players[0]
        self.assertEqual(self._answer(room, player, 0, 0).status_code, 200)
        self._close_window(room)
        self.assertEqual(self._answer(room, player, 0, 1).json()["error"], "time's up")
        self.assertEqual(QuizAttempt.objects.count(), 1)
        self.assertEqual(QuizAttempt.objects.get().choice, 0)  # the first answer, not the retry

    def test_the_snapshot_follows_presence_and_best_score(self):
        # `done` and `score` are the room's, over the ONLINE players only, and `online_count` is the
        # denominator the bar is drawn against — so the three have to agree. Bob got it wrong and
        # Ana got it right, both on the question on screen; when his phone sleeps, his half of the
        # bar goes, because a phone that slept did not answer this question and must not appear to
        # have.
        room, players = self._joined(two=True)
        ana, bob = players
        self._online(room, ana, bob)
        key = self._bank(room)[0].correct
        self._assert_ok(self._answer(room, ana, 0, key))
        self._assert_ok(self._answer(room, bob, 0, (key + 1) % 4))
        quiz = room_state(room.code)["quiz"]
        self.assertEqual((quiz["done"], quiz["online_count"], quiz["score"]), (2, 2, 1))
        self._age_out(bob)
        quiz = room_state(room.code)["quiz"]
        self.assertEqual((quiz["done"], quiz["online_count"], quiz["score"]), (1, 1, 1))
        # Ana's point is hers and stays while SHE is connected: the room's best is the best of the
        # present, not of the roster.
        self._age_out(ana)
        quiz = room_state(room.code)["quiz"]
        self.assertEqual((quiz["done"], quiz["online_count"], quiz["score"]), (0, 0, 0))
        # And the phase never moved for any of it: nothing about presence opens the game now.
        self._assert_phase(room, Room.Phase.QUIZ)

    def test_the_quiz_block_is_present_and_null_outside_the_quiz_phase(self):
        # PRESENT AND NULL, not absent. The frozen `State` types `quiz` as `QuizState | null`
        # rather than an optional key, and a client that null-checks one shape is more robust than
        # one testing for the key's existence — one branch instead of a branch per screen. A room
        # with no tutorial never opens the gate at all, so this is the case that has to keep
        # returning a null rather than dropping the key.
        room = self._create(tutorial=False)
        player = self._join(room, "ana")
        self._start(room)
        self._online(room, player)
        self.assertEqual(room.phase, Room.Phase.PLAYING)
        state = room_state(room.code)
        self.assertIn("quiz", state)
        self.assertIsNone(state["quiz"])
        # The other two blocks too, so this also pins that `quiz` is not alone in being unconditional.
        self.assertIn("reveal", state)
        self.assertIn("summary", state)

    def test_the_quiz_block_is_zeroed_with_nobody_online(self):
        # 0, not None: an empty lobby is the state the admin screen looks at before anyone joins,
        # and a null here is a null branch in the client that only that state would take. The
        # question, the clock and the key are still real — the room HAS asked something, and that is
        # what the admin screen shows while it waits.
        room, players = self._joined()
        quiz = room_state(room.code)["quiz"]
        self.assertEqual((quiz["index"], quiz["total"]), (0, QUIZ_LENGTH))
        self.assertFalse(quiz["revealed"])
        self.assertIsNone(quiz["correct"])
        self.assertEqual((quiz["done"], quiz["online_count"], quiz["score"]), (0, 0, 0))
