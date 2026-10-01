import json
import random
import time
from contextlib import contextmanager
from unittest import mock

from django.test import Client, SimpleTestCase, TestCase, override_settings

from game.models import Player, QuizAttempt, QuizQuestion, Room
from game.quiz import QUESTIONS, QUIZ_LENGTH, pick
from game.views import PRESENCE_TTL, _beat, _presence, _presence_lock, maybe_advance, room_state


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
        """Give these players a live heartbeat, as their own SSE stream would. `maybe_advance`
        counts presence, and a test client that never opens a stream has no beats."""
        for p in players:
            _beat(room.code, p.pk)

    def _answer(self, room: Room, player: Player, question: int, choice: int):
        return self._post(
            f"/api/rooms/{room.code}/quiz",
            {"token": player.token, "question": question, "choice": choice},
        )

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
        for i in range(up_to):
            self.assertEqual(self._answer(room, player, i, 0).status_code, 200)

    def _answer_up_to_wrong(self, room: Room, player: Player, up_to: int = QUIZ_LENGTH, start: int = 0) -> None:
        """Answer questions `start`..`up_to`-1 with a choice that is never the key, so a score of 0
        in the snapshot is this test's doing and not a coincidence of which index the bank put its
        keys on."""
        for i in range(start, up_to):
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

    def test_get_accepts_a_token_without_using_it(self):
        # A `?token=` on the GET is accepted and ignored. It carries no key, so there is nothing
        # for it to leak, and a 403 here would break a screen that has a token in hand.
        room, players = self._joined()
        r = self.c.get(f"/api/rooms/{room.code}/quiz?token={players[0].token}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["questions"]), QUIZ_LENGTH)

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
        wrong = self._answer(room, player, 1, (bank[1].correct + 1) % 4)
        self.assertEqual((right.json()["score"], wrong.json()["score"]), (1, 1))
        self.assertEqual(player.quiz_score(), 1)
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
        for order in (QUIZ_LENGTH, 99, -1, "0", None, True):
            with self.subTest(question=order):
                self.assertEqual(self._answer(room, players[0], order, 0).status_code, 400)
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
        # The gate, from the other side. The admin can open the game early, and after the gate has
        # opened itself the endpoint has to be closed again — otherwise a late submit would write
        # an attempt against a game that is already being played.
        room, players = self._joined()
        player = players[0]
        self.assertEqual(self._post(f"/api/admin/rooms/{room.code}/next").status_code, 200)
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

    # --- the gate ----------------------------------------------------------------------------

    def test_the_gate_opens_the_game_on_the_last_answer(self):
        room, players = self._joined()
        player = players[0]
        self._online(room, player)
        self._answer_all(room, player, QUIZ_LENGTH - 1)
        self.assertEqual(room.phase, Room.Phase.QUIZ)  # one question short, still shut
        self._answer(room, player, QUIZ_LENGTH - 1, 0)
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.PLAYING)
        # And it is closed behind them, not merely bypassed.
        self.assertEqual(self._answer(room, player, 0, 0).status_code, 409)

    def test_the_gate_waits_for_an_online_player_who_has_one_left(self):
        # Presence, not rows, in both directions: ana being done is not the gate opening over
        # bob, and bob coming back into the count is not bob being locked out of it.
        room, players = self._joined(two=True)
        ana, bob = players
        self._online(room, ana, bob)
        self._answer_all(room, ana)
        self._assert_phase(room, Room.Phase.QUIZ)
        self._answer_all(room, bob, QUIZ_LENGTH - 1)
        self._assert_phase(room, Room.Phase.QUIZ)
        self._answer(room, bob, QUIZ_LENGTH - 1, 0)
        self._assert_phase(room, Room.Phase.PLAYING)

    def test_a_player_stuck_in_the_quiz_does_not_hold_the_room(self):
        # Bob's phone slept. He is a row forever, so a comparison against `room.players` would
        # sit on the gate until he came back, or forever.
        room, players = self._joined(two=True)
        ana, _bob = players
        self._online(room, ana)
        self._answer_all(room, ana)
        self._assert_phase(room, Room.Phase.PLAYING)

    def test_a_room_nobody_is_connected_to_does_not_open_itself(self):
        # `0 == 0` is true. With every phone dropped, "every online player has answered" holds
        # vacuously, and the gate would open on a room nobody is in — the quiz phase ending by
        # itself is exactly what a real submit is supposed to prevent.
        room, players = self._joined()
        self._answer_all(room, players[0])  # no heartbeat: nobody is streaming
        self._assert_phase(room, Room.Phase.QUIZ)

    def test_an_empty_bank_does_not_open_the_gate(self):
        # A room whose bank was never materialised has nothing to pass, and `count() == 0` is true
        # of every player, so the naive comparison would open the gate on a room with no questions
        # in it. `maybe_advance` is called directly because it is unreachable over HTTP here: with
        # no row to name, the endpoint refuses the answer and never gets as far as advancing.
        room = self._create(tutorial=True)
        player = self._join(room, "ana")
        self._start(room)
        self._online(room, player)
        room.questions.all().delete()
        maybe_advance(room)
        self._assert_phase(room, Room.Phase.QUIZ)

    def test_another_rooms_questions_do_not_hold_this_gate_shut(self):
        # The per-room scoping of the gate's own count, from the other side of the fix. Under a
        # global `QuizQuestion.objects.count()` a second room's eight questions would raise this
        # room's total to sixteen, and no player could ever reach it — the gate would be shut
        # forever on a room whose eight questions they had all answered. `total` is this room's
        # eight, which is the number the players can actually reach.
        room, players = self._joined()
        player = players[0]
        self._online(room, player)
        self._create()  # a second room, with its own eight
        # The headline is this room's eight even though the database now holds sixteen.
        self.assertEqual(room_state(room.code)["quiz"], {"total": QUIZ_LENGTH, "done": 0, "score": 0})
        self._answer_all(room, player)
        self._assert_phase(room, Room.Phase.PLAYING)

    def test_the_quiz_score_does_not_reach_the_final_ranking(self):
        # A gate you must pass must not also be points you can farm: eight out of eight is
        # `score: 0` on the board, and the final ranking is built from the rounds alone.
        room, players = self._joined()
        player = players[0]
        self._online(room, player)
        bank = self._bank(room)
        for order in range(QUIZ_LENGTH):
            self._answer(room, player, order, bank[order].correct)
        self.assertEqual(player.quiz_score(), QUIZ_LENGTH)
        self._assert_phase(room, Room.Phase.PLAYING)
        self.assertEqual(room_state(room.code)["players"][0]["score"], 0)

    # --- the snapshot ------------------------------------------------------------------------

    def test_the_snapshot_carries_the_quiz_headline(self):
        # Room-level, not per-viewer: `room_state(code)` takes no token, so it cannot know who is
        # reading. This is the number the lobby shows — how many of the players who are actually
        # there are through the gate, and the best score among them.
        room, players = self._joined(two=True)
        ana, _bob = players
        self._online(room, ana, _bob)
        self._assert_phase(room, Room.Phase.QUIZ)
        self.assertEqual(
            room_state(room.code)["quiz"], {"total": QUIZ_LENGTH, "done": 0, "score": 0}
        )
        self._answer_up_to_wrong(room, ana)
        self.assertEqual(
            room_state(room.code)["quiz"], {"total": QUIZ_LENGTH, "done": 1, "score": 0}
        )

    def test_the_quiz_headline_follows_presence_and_best_score(self):
        # Bob is through the gate and is the only player with a point; both of those count for the
        # room only while he is connected. When his phone sleeps they stop — the same presence bar
        # the gate itself uses, so the screen cannot promise a "c'est parti" the phase machine is
        # not going to honour. Ana is online with one question left, so the room stays shut either
        # way, which is what isolates `done` and `score` from the phase.
        room, players = self._joined(two=True)
        ana, bob = players
        self._online(room, ana, bob)
        self._answer_up_to_wrong(room, ana, QUIZ_LENGTH - 1)
        self._answer_up_to_wrong(room, bob, QUIZ_LENGTH, start=1)
        self._assert_ok(self._answer(room, bob, 0, self._bank(room)[0].correct))
        self.assertEqual(room_state(room.code)["quiz"], {"total": QUIZ_LENGTH, "done": 1, "score": 1})
        self._age_out(bob)
        self.assertEqual(
            room_state(room.code)["quiz"], {"total": QUIZ_LENGTH, "done": 0, "score": 0}
        )
        self._assert_phase(room, Room.Phase.QUIZ)

    def test_the_quiz_block_is_absent_outside_the_quiz_phase(self):
        # Pins the CURRENT shape, which is not the final contract: the finished one puts `quiz` in
        # every snapshot, `None` outside the phase, so the client tests one nullable field instead
        # of the key's presence. A room with no tutorial never opens the gate at all.
        room = self._create(tutorial=False)
        player = self._join(room, "ana")
        self._start(room)
        self._online(room, player)
        self.assertEqual(room.phase, Room.Phase.PLAYING)
        self.assertNotIn("quiz", room_state(room.code))

    def test_the_quiz_headline_is_zero_with_nobody_online(self):
        # 0, not None: an empty lobby is the state the admin screen looks at before anyone joins,
        # and a null here is a null branch in the client that only that state would take.
        room, players = self._joined()
        self.assertEqual(
            room_state(room.code)["quiz"], {"total": QUIZ_LENGTH, "done": 0, "score": 0}
        )
