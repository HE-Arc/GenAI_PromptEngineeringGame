import json
import time
from datetime import timedelta
from unittest import mock

from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone

from game.llm import Verdict
from game.models import Player, QuizQuestion, Ready, Room
from game.prompts import LOBBY_STEP, majority, threshold
from game.tests.support import answer_chunks, drain
from game.quiz import pick
from game.views import (
    PRESENCE_TTL,
    _presence,
    _presence_lock,
    current_step,
    maybe_advance_by_votes,
    room_state,
)


def _one_player_room(tutorial: bool = True):
    """A room with one joined player, its didacticiel open on question 0. The fixtures the other
    test files use are private to those files, so this one is rebuilt here — it is four lines and
    copying a shared helper across test modules would couple them to each other's edits."""
    from game.quiz import pick

    room = Room.objects.create(code="RDY1", tutorial=tutorial)
    from game.models import QuizQuestion

    QuizQuestion.objects.bulk_create(
        QuizQuestion(room=room, order=i, prompt=q.prompt, choices=list(q.choices), correct=q.correct, explanation=q.explanation)
        for i, q in enumerate(pick())
    )
    player = Player.objects.create(room=room, name="Ana", token="t")
    room._go(Room.Phase.QUIZ)
    room._open_quiz(0)
    # The window ELAPSED, not merely opened: `current_step` withholds the didacticiel step until
    # the reveal, so a fixture that left thirty seconds on the clock would give a room with no
    # discussion step and `current_step` would return None for every question below.
    room._close_quiz()
    room.refresh_from_db()
    return room, player


class MajorityTests(SimpleTestCase):
    def test_the_threshold_is_a_strict_majority(self):
        for n, expected in ((1, 1), (2, 2), (3, 2), (4, 3), (5, 3), (20, 11), (21, 11)):
            with self.subTest(players=n):
                self.assertEqual(majority(n), expected)

    def test_a_room_with_nobody_connected_has_no_threshold(self):
        # 0 // 2 + 1 is 1, which would be a threshold one player could never reach in a room
        # with no players — and the caller that used it would advance a game nobody is in.
        self.assertEqual(majority(0), 0)

    def test_the_threshold_is_never_more_than_the_players(self):
        for n in range(1, 30):
            with self.subTest(players=n):
                self.assertLessEqual(majority(n), n)

    def test_the_threshold_is_always_reachable(self):
        # The property that makes the game autonomous: with every player ready, the room always
        # crosses the line. A threshold of n+1 would deadlock a unanimous class forever.
        for n in range(1, 30):
            with self.subTest(players=n):
                self.assertLessEqual(majority(n), n)


class ThresholdTests(SimpleTestCase):
    """The one function that decides which rule a step is under, tested without a room.

    `SimpleTestCase` for the same reason `MajorityTests` is: `prompts` is the module of pure rules
    with no Django in it, and the case that matters here — which rule a STEP NAME lands on — is a
    decision about a string, not about anybody's presence.
    """

    def test_the_lobby_is_the_only_step_that_asks_for_everybody(self):
        # THE TWO RULES, AND THEY DIFFER ON PURPOSE. The lobby is unanimity: everybody in it just
        # arrived and is looking at the same screen, so waiting for the last phone costs nothing.
        # A discussion is a majority: twenty minutes in, one phone sleeps, and unanimity would park
        # the whole class on a bar that will never move. Written as two separate assertions rather
        # than a table so the difference is readable, and so a rule that swallowed the other fails
        # on the case it broke instead of on a diff nobody reads.
        for n in (1, 2, 3, 5, 20, 21):
            with self.subTest(players=n):
                self.assertEqual(threshold(LOBBY_STEP, n), n)

    def test_every_discussion_step_is_a_majority(self):
        # THE THREE STEP FAMILIES, named as `current_step` names them. They are steps, not phases:
        # the cursor moves inside them (`quiz:3` is question four) and the threshold must not care
        # which one it is, which is why this passes real step names rather than a phase constant.
        for step in ("quiz:0", "quiz:3", "quiz:7", "rating:0:0", "rating:2:0", "summary:0", "summary:2"):
            for n in (1, 2, 3, 20):
                with self.subTest(step=step, players=n):
                    self.assertEqual(threshold(step, n), majority(n))

    def test_the_step_name_is_not_a_phase_name(self):
        # `waiting` IS the phase and `lobby` IS its step. A threshold keyed on the phase would
        # have made every discussion unanimous the day someone renamed a step, and this is the
        # assertion that says the lobby's string is its own.
        self.assertEqual(threshold(LOBBY_STEP, 4), 4)
        self.assertEqual(threshold("waiting", 4), majority(4))

    def test_a_room_with_nobody_connected_has_no_threshold_under_either_rule(self):
        # `0` and not `1`, for both branches: a caller that got 1 for an empty lobby would compare
        # a room with no players against a bar of one, which is the `0 == 0` this module refuses
        # everywhere else in a costume.
        for step in (LOBBY_STEP, "quiz:0", "rating:2:0", "summary:2"):
            with self.subTest(step=step):
                self.assertEqual(threshold(step, 0), 0)

    def test_every_threshold_is_reachable_and_never_over_the_population(self):
        # THE PROPERTY THE AUTONOMOUS GAME STANDS ON, both rules: with every connected player
        # ready, the room always crosses the line. Unanimity is exactly `n` and a majority is at
        # most `n`, so there is no class size at which the bar is a number nobody can reach — which
        # is a dead game at the lobby and a dead step mid-round.
        for step in (LOBBY_STEP, "quiz:3", "rating:2:0", "summary:2"):
            for n in range(1, 30):
                with self.subTest(step=step, players=n):
                    self.assertLessEqual(threshold(step, n), n)


class ReadyModelTests(TransactionTestCase):
    def test_one_vote_per_player_per_step(self):
        room, player = _one_player_room()
        Ready.objects.create(room=room, player=player, step="quiz:0")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Ready.objects.create(room=room, player=player, step="quiz:0")

    def test_the_same_player_may_vote_on_two_steps(self):
        # The step is in the key, not derived: a player who is ready on question 0 and again on
        # question 1 has two rows, and the question-0 row is garbage the moment the cursor moves.
        room, player = _one_player_room()
        Ready.objects.create(room=room, player=player, step="quiz:0")
        Ready.objects.create(room=room, player=player, step="quiz:1")
        self.assertEqual(Ready.objects.filter(player=player).count(), 2)

    def test_a_vote_on_a_previous_step_is_never_counted_again(self):
        # THE INVARIANT THAT MAKES THE WHOLE THING SAFE, and the reason nothing in this codebase
        # clears votes when the room moves on. `step` is part of the key, so a vote on `quiz:0`
        # cannot match the count for `quiz:1` — there is no window in which a stale vote is
        # visible. Clearing instead would add three call sites and a failure mode (a forgotten
        # clear is a room that opens its next question already holding enough votes to skip it)
        # to solve a problem the key already solved.
        room, player = _one_player_room()
        Ready.objects.create(room=room, player=player, step="quiz:0")
        room._open_quiz(1)
        room._close_quiz()
        room.refresh_from_db()
        from game.views import current_step
        self.assertEqual(current_step(room), "quiz:1")
        self.assertEqual(Ready.objects.filter(room=room, step=current_step(room)).count(), 0)
        # The row is still there, and that is deliberate: it is inert, not gone.
        self.assertEqual(Ready.objects.filter(room=room, step="quiz:0").count(), 1)


class VoteBase(TransactionTestCase):
    """The room a vote happens in: players joined, all online, the didacticiel open.

    COPIED, NOT SHARED, and the reason is worth stating because it will look like duplication
    to whoever reads it next: `test_quiz.py` holds these as private methods, so sharing them
    means either making them public in another test file or extracting a fixtures module. The
    first is a worse coupling than the copy, and the second refactors the longest test file in
    the repo to serve one new one.
    """

    def setUp(self) -> None:
        # The presence registry is PROCESS-LOCAL, so a test rollback does not clear it: without
        # this, a heartbeat from an earlier test marks a same-numbered player online in a later
        # one, and these tests would be measuring somebody else's presence. `test_quiz.py` clears
        # it for exactly the same reason, and the two rooms in this file share the id space.
        _presence.clear()

    def _post(self, url: str, data: dict | None = None):
        return self.client.post(url, data=json.dumps(data or {}), content_type="application/json")

    def _login(self) -> None:
        """The admin session, which is a COOKIE and not a password in the body.

        `require_admin` reads `request.session["admin"]`, so a test that posts `password` to an
        admin endpoint is refused with a 403 it would then have to explain. This is the two
        lines `test_admin.py` opens its class with, for the same reason.
        """
        self._post("/api/admin/login", {"password": "genai"})

    def _create(self, tutorial: bool = True) -> Room:
        """A room through the ADMIN ENDPOINT, not `Room.objects.create`.

        The bank is materialised in `admin_create_room` and nowhere else, so a room built on the
        ORM has no `QuizQuestion` rows at all — and an empty bank makes `_next_quiz` return False,
        which is the documented fall-through to opening the game. Every advance below would then
        land on `playing` instead of the next question, and the tests would be measuring the empty
        gate rather than the ready bar. Same reason, in the same words, as `test_quiz._create`.

        `tutorial` is a parameter because the lobby is where the two openings differ: a class that
        votes its own game into existence gets the didacticiel, and a room created without one has
        to land in `playing` instead — the vote path and the admin's button agreeing on that is
        half of what these tests are for.
        """
        self._login()
        code = self._post("/api/admin/rooms", {"tutorial": tutorial}).json()["code"]
        return Room.objects.get(code=code)

    def _join(self, room: Room, name: str) -> Player:
        resp = self._post(f"/api/rooms/{room.code}/join", {"name": name})
        # BY THE TOKEN THE ENDPOINT RETURNED, and not by a primary key: `join` answers
        # `{"token", "name"}` and no `id`, so reading `resp.json()["id"]` is a KeyError. The
        # token is also the credential every later helper passes back, so matching on it proves
        # the POST produced the row the rest of the test is about to use.
        return Player.objects.get(token=resp.json()["token"])

    def _start(self, room: Room) -> None:
        self._login()
        self._post(f"/api/admin/rooms/{room.code}/start")
        room.refresh_from_db()

    def _reveal_now(self, room: Room) -> None:
        """Put the room in its REVEALED state: question asked, window elapsed, key on screen.

        THE FIXTURE HAS TO DO THIS, because the ready bar is deliberately withheld until the
        reveal: a room whose thirty-second window is still open has no discussion to vote on, so
        every test in this file that votes on a didacticiel step has to be past the reveal. The
        alternative — a fixture that leaves the window open — measures the withholding, which is
        `test_a_vote_during_the_answering_window_is_refused`'s job and not anybody else's.

        The deadline is an ABSOLUTE instant, so "the window has closed" is a deadline in the
        past, never thirty seconds ahead of one. Moving it the other way round would pass the
        exact case this exists to set up.
        """
        Room.objects.filter(code=room.code).update(quiz_deadline=timezone.now() - timedelta(seconds=1))
        room.refresh_from_db()

    def _online(self, room: Room, *players: Player) -> None:
        for p in players:
            with _presence_lock:
                _presence[(room.code, p.pk)] = time.monotonic()

    def _room_with(self, players: int) -> tuple[Room, list[Player]]:
        """A started room, its didacticiel REVEALED on question 0, `players` joined and online."""
        room = self._create()
        joined = [self._join(room, f"p{i}") for i in range(players)]
        self._start(room)
        self._reveal_now(room)
        self._online(room, *joined)
        room.refresh_from_db()
        return room, joined

    def _lobby(self, players: int = 1, tutorial: bool = True) -> tuple[Room, list[Player]]:
        """A room in its lobby: `players` joined, streaming, and the game NOT started.

        THE SAME ROOM `_room_with` BUILDS WITH ONE STEP LEFT OUT, and the step left out is the
        admin's press: `_room_with` starts the game, a lobby is the state before that press, and a
        fixture that started the room to get players into it would have nothing left to test. The
        phase is asserted rather than assumed because every test below is about whether the room
        MOVED, and a fixture that quietly started would make all of them pass on a room that never
        was in its lobby.
        """
        room = self._create(tutorial=tutorial)
        joined = [self._join(room, f"p{i}") for i in range(players)]
        self._online(room, *joined)
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.WAITING)
        self.assertEqual(room.rounds.count(), 0)
        return room, joined

    def _age_out(self, player: Player) -> None:
        """Push a player's last beat into the past, as a sleeping phone does, without sleeping."""
        with _presence_lock:
            _presence[(player.room_id, player.pk)] = time.monotonic() - PRESENCE_TTL - 1

    def _drop_everyone(self, room: Room) -> None:
        for p in room.players.all():
            self._age_out(p)

    def _ready(self, room: Room, player: Player, step: str | None = None) -> None:
        """One player's vote, recorded the way the game records it: through the endpoint.

        `step` is only ever passed when the test is about a step the room is not in — it writes
        the row directly, because no endpoint can produce a vote for a step that has not started
        and pretending otherwise would test a path the game does not have.
        """
        if step is not None:
            Ready.objects.create(room=room, player=player, step=step)
            return
        self._post(f"/api/rooms/{room.code}/ready", {"token": player.token})

    def _open_game(self, room: Room) -> None:
        """Press the admin's button until the didacticiel is over and the room is `playing`.

        THROUGH THE BUTTON, in a loop bounded by the bank, and not by writing the phase: the gate
        is only openable that way in real play, and a fixture that set `room.phase = "playing"`
        directly would build a state the game cannot reach from a started room. The loop is
        bounded by `room.questions.count()` so an extra press cannot walk the cursor off the end
        of the bank.
        """
        for _ in range(room.questions.count()):
            if room.phase != Room.Phase.QUIZ:
                break
            self._post(f"/api/admin/rooms/{room.code}/next")
            room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.PLAYING)

    def _answer(self, room: Room, player: Player, text: str) -> None:
        """One player writes a prompt, with the model's three calls stubbed out.

        THE STUB IS AT `game.llm.<name>`, PATCHED WHERE IT IS USED, which is the convention every
        other test file in this repo follows (`test_rating.py:240`). It is also what keeps these
        tests off the network: with no `OPENAI_API_KEY` the calls fall back to the local stub
        rather than failing, so a test that did not patch them would still pass — silently, and
        several seconds slower per answer, on a code path that is not what any of these tests are
        about. The ready bar never reads a prompt's text.
        """
        with mock.patch("game.llm.answer_stream", new=answer_chunks("la reponse")), mock.patch(
            "game.llm.score", return_value=Verdict(8, "tres proche")
        ), mock.patch("game.llm.grade_prompt", return_value=Verdict(6, "methode")):
            r = self._post(
                f"/api/rooms/{room.code}/answer",
                {"token": player.token, "prompt": text},
            )
            drain(r)  # the work runs in a thread: the next line must not race it
        self.assertEqual(r.status_code, 200)

    def _to_rating(self, room: Room, players: list[Player]) -> None:
        """Drive the room to `rating` on the first prompt, the honest way.

        THE GATE IS CLOSED FIRST, because this room came out of `_room_with` sitting in the
        didacticiel and `answer` answers 409 outside `playing`. Then every player writes a prompt
        — through the endpoint, because a test that inserts `Answer` rows is testing a state the
        game cannot reach — and THE LAST ANSWER OPENS THE RATING BY ITSELF, through
        `maybe_advance_phase`.

        NO ADMIN `next` AFTER THE ANSWERS, and its absence is the correction. The plan had one,
        on the reasoning that the admin opens the rating; but `maybe_advance_phase` already calls
        `_open_rating` the moment the last online player has answered, so that extra press
        advanced the reveal to prompt 1 before the test had rated anything. Every rating helper
        below then operated on the wrong prompt and the counts were off by one for the rest of
        the run. With N players the room lands in `rating` on the FIRST prompt, revealed 0.
        """
        self._open_game(room)
        for i, p in enumerate(players):
            self._answer(room, p, f"un prompt {i}")
        room.refresh_from_db()
        self.assertEqual((room.phase, room.rounds.get(order=0).revealed), (Room.Phase.RATING, 0))

    def _rate_all(self, room: Room, players: list[Player], target: Player, score: int = 7) -> None:
        """Every player who MAY rate the prompt on screen has rated it. The author may not, and
        `_rate_all` does not try: the author being absent from the count is the rule under test
        in `test_the_rating_phase_advances_on_a_majority_of_ready_players`."""
        rnd = room.current_round()
        answer = rnd.answers.order_by("id")[room.rounds.get(order=room.current).revealed]
        for p in players:
            if p.pk == answer.player_id:
                continue
            self._post(f"/api/rooms/{room.code}/rate", {"token": p.token, "score": score})

    def _skip_to_summary(self, room: Room, players: list[Player]) -> None:
        """Rating through every prompt and landed on the summary, rating each one as it appears."""
        self._to_rating(room, players)
        while room.phase == Room.Phase.RATING:
            self._rate_all(room, players, players[0])
            self._post(f"/api/admin/rooms/{room.code}/next")
            room.refresh_from_db()


class VoteAdvanceTests(VoteBase):
    def test_nothing_moves_on_an_empty_room(self):
        # The 0 == 0 guard, in the place where the threshold is computed. A room whose phones
        # all dropped has nobody to press anything, and a bar of zero would move it on.
        room, _ = self._room_with(1)
        self._drop_everyone(room)
        maybe_advance_by_votes(room)
        room.refresh_from_db()
        self.assertEqual((room.phase, room.quiz_index), (Room.Phase.QUIZ, 0))

    def test_one_ready_player_out_of_three_is_not_enough(self):
        room, players = self._room_with(3)
        self._ready(room, players[0])
        maybe_advance_by_votes(room)
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, 0)

    def test_two_ready_players_out_of_three_move_the_room_on(self):
        # 3 // 2 + 1 == 2. This is the whole feature: the room moves on itself.
        room, players = self._room_with(3)
        self._ready(room, players[0])
        self._ready(room, players[1])
        maybe_advance_by_votes(room)
        room.refresh_from_db()
        self.assertEqual((room.phase, room.quiz_index), (Room.Phase.QUIZ, 1))
        self.assertGreater(room.quiz_deadline, timezone.now())

    def test_the_votes_of_the_step_that_just_closed_are_inert(self):
        # NOT DELETED, INERT — and the assertion is on the count for the NEW step, not on the
        # table. `quiz:0` votes cannot be counted against `quiz:1`, so a room that moved on is
        # not a room holding a stale majority. A test asserting `Ready.objects.count() == 0` here
        # would pin a deletion the design no longer does.
        room, players = self._room_with(3)
        for p in players[:2]:
            self._ready(room, p)
        maybe_advance_by_votes(room)
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, 1)
        # The room MOVED, so the votes that carried it are behind the cursor. The step name is
        # asserted on the row rather than through `current_step`, because advancing opens a fresh
        # thirty-second window on question 1 and a question still being asked is deliberately not
        # a discussion — so `current_step` is None here, which is the withholding rule and not a
        # claim about these rows. What is under test is that the old votes cannot match the new
        # step, and that is a property of the table whatever the room is doing.
        self._reveal_now(room)
        self.assertEqual(current_step(room), "quiz:1")
        self.assertEqual(Ready.objects.filter(room=room, step="quiz:1").count(), 0)

    def test_a_disconnected_player_does_not_count_in_the_bar(self):
        # Three connected, one of them sleeps: the bar is 2 of 2 among the two awake, not 2 of 3.
        # This is why the rule counts `online` and not `room.players` — a phone in a bag would
        # otherwise hold the class on every question for the rest of the game.
        room, players = self._room_with(3)
        self._age_out(players[2])
        self._ready(room, players[0])
        self._ready(room, players[1])
        maybe_advance_by_votes(room)
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, 1)

    def test_a_vote_from_a_disconnected_player_does_not_count_either(self):
        room, players = self._room_with(3)
        self._ready(room, players[0])
        self._age_out(players[0])
        self._ready(room, players[1])
        maybe_advance_by_votes(room)
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, 0)

    def test_the_rating_phase_advances_on_a_majority_of_ready_players(self):
        # The prompt on screen is rated, then discussed. The bar is the players who may RATE it:
        # the author cannot, so counting them would make the bar unreachable.
        room, players = self._room_with(3)
        self._to_rating(room, players)
        self._rate_all(room, players, players[0])
        self._ready(room, players[1], step="rating:0:0")
        maybe_advance_by_votes(room)
        room.refresh_from_db()
        self.assertEqual(room.rounds.get(order=0).revealed, 1)

    def test_the_summary_advances_the_round_on_a_majority(self):
        room, players = self._room_with(3)
        self._skip_to_summary(room, players)
        for p in players[:2]:
            self._ready(room, p, step="summary:0")
        maybe_advance_by_votes(room)
        room.refresh_from_db()
        self.assertEqual((room.phase, room.current), (Room.Phase.PLAYING, 1))

    def test_the_admin_button_still_moves_the_room_on(self):
        # The filet. A class with one player who walked out and one phone that died is a class
        # the teacher can still finish, and that path is the one that existed before this feature.
        room, players = self._room_with(3)
        self._post(f"/api/admin/rooms/{room.code}/next")
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, 1)

    def test_a_vote_moves_the_room_on_without_waiting_for_the_next_snapshot(self):
        # The endpoint calls the same function the snapshot does, so a room that crosses the bar
        # on a tap advances on that tap. Polling for it would make the last vote's effect land up
        # to half a second late for no reason.
        room, players = self._room_with(3)
        for p in players[:2]:
            self.assertEqual(self._post(f"/api/rooms/{room.code}/ready", {"token": p.token}).status_code, 200)
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, 1)


class ReadyEndpointTests(VoteBase):
    def test_a_vote_is_recorded_and_reported_back(self):
        room, players = self._room_with(2)
        r = self._post(f"/api/rooms/{room.code}/ready", {"token": players[0].token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["ready"]["count"], 1)

    def test_voting_twice_is_refused(self):
        room, players = self._room_with(2)
        self._post(f"/api/rooms/{room.code}/ready", {"token": players[0].token})
        self.assertEqual(
            self._post(f"/api/rooms/{room.code}/ready", {"token": players[0].token}).status_code, 409
        )

    def test_a_bad_token_is_refused(self):
        room, _ = self._room_with(2)
        self.assertEqual(self._post(f"/api/rooms/{room.code}/ready", {"token": "nope"}).status_code, 403)

    def test_a_vote_outside_a_discussion_is_refused(self):
        # In `waiting` there is nothing to discuss, and in `playing` the round closes on the
        # answers themselves. A vote accepted there would sit in the table until a step that
        # never matches its name, which is the shape of a bug that only shows up in a class.
        room, players = self._room_with(2)
        # `playing`, reached by the admin's button carrying the room out of the gate. The plan
        # walked this through `_to_rating` and one further `next`, which does NOT leave the
        # discussion: in `rating` the advance moves the REVEAL to the next prompt, so the room is
        # still rating a prompt and still has a discussion — a vote there is legitimate and the
        # endpoint rightly takes it. `playing` is the phase the comment above names, and it is
        # where a vote has nothing to attach itself to.
        self._open_game(room)
        self.assertEqual(room.phase, Room.Phase.PLAYING)
        r = self._post(f"/api/rooms/{room.code}/ready", {"token": players[0].token})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "nothing to discuss")
        # And nothing was recorded, so the room is not carrying a vote on a step it never had.
        self.assertEqual(Ready.objects.filter(room=room).count(), 0)

    def test_the_snapshot_reports_the_bar_and_whether_this_player_is_ready(self):
        room, players = self._room_with(3)
        self._ready(room, players[0])
        ready = room_state(room.code)["ready"]
        self.assertEqual((ready["count"], ready["needed"], ready["mine"]), (1, 2, False))

class ReadyEdgeCaseTests(VoteBase):
    """The three ways the ready bar used to be wrong, each pinned so the bug cannot come back."""

    def test_a_vote_during_the_answering_window_is_refused(self):
        # THE BUG THIS WHOLE CLASS EXISTS FOR. `current_step` used to name a `quiz:<n>` step for
        # the whole quiz phase, so the ready bar was on screen DURING the thirty seconds the
        # question was being asked. Two players out of three could then press « prêt » on
        # question 3 and move the room on to question 4 before anybody had read question 3's
        # answer — a question skipped, never seen, with the reveal to explain it happening to a
        # class that had already left it.
        #
        # Asking and discussing are two different moments. The step exists only once the window
        # has closed, which is the comparison `_quiz_state` already makes and which
        # `_quiz_is_revealed` now shares, so the bar and the snapshot's `revealed` flag cannot
        # disagree about whether the answer is out.
        room, players = self._room_with(3)
        Room.objects.filter(code=room.code).update(
            quiz_deadline=timezone.now() + timedelta(seconds=30)
        )
        room.refresh_from_db()
        # The window is open: the room is in `quiz`, on a question nobody has been told the answer
        # to, and `quiz.revealed` says so independently of the ready bar.
        self.assertFalse(room_state(room.code)["quiz"]["revealed"])
        self.assertIsNone(current_step(room))
        # No bar for a phase that is not discussing anything.
        self.assertIsNone(room_state(room.code)["ready"])
        # And a majority pressing anyway is refused outright, twice over — by the phase check and
        # by the fact that nothing was recorded.
        for p in players[:2]:
            self.assertEqual(self._post(f"/api/rooms/{room.code}/ready", {"token": p.token}).status_code, 409)
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, 0)
        self.assertEqual(Ready.objects.filter(room=room).count(), 0)
        # Once the window closes, the same room is ready to discuss and the same bar works.
        self._reveal_now(room)
        self.assertEqual(current_step(room), "quiz:0")
        self.assertIsNotNone(room_state(room.code)["ready"])
        for p in players[:2]:
            self.assertEqual(self._post(f"/api/rooms/{room.code}/ready", {"token": p.token}).status_code, 200)
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, 1)

    def test_the_bar_in_a_two_player_room_counts_the_players_who_may_vote(self):
        # THE BAR AND THE COUNT WERE MEASURED OVER DIFFERENT POPULATIONS, and in a room of two
        # during the rating that is a dead game. The author of the prompt on screen cannot rate
        # it, so they are not a voter: `voters` is one player. But `needed` was computed from
        # `len(online)`, which is two — so the screen read « 1 sur 2 prêts » on every frame and
        # the room could never move on. The lone player pressed, the number never reached the
        # bar, and nothing said why.
        #
        # `needed` is `majority(len(voters))` now, over the same population `count` counts.
        room, players = self._room_with(2)
        self._to_rating(room, players)
        # players[0] wrote the prompt on screen, so the only voter is players[1].
        author, voter = players[0], players[1]
        ready = room_state(room.code)["ready"]
        self.assertEqual((ready["count"], ready["needed"]), (0, 1))
        # One vote — the one the game allows — is the whole bar, and it moves the room.
        self.assertEqual(self._post(f"/api/rooms/{room.code}/ready", {"token": voter.token}).status_code, 200)
        room.refresh_from_db()
        self.assertEqual(room.rounds.get(order=0).revealed, 1)

    def test_the_author_cannot_vote_on_their_own_prompt(self):
        # THE AUTHOR'S VOTE WAS ACCEPTED AND SILENTLY DISCARDED. `_ready_state` excluded the
        # author from `count` and the endpoint took their vote anyway, so the button showed « Prêt »,
        # the press returned 200, and the bar did not move — the one outcome with no explanation
        # anywhere on the phone. The bar is unreachable for them, exactly as the rating is.
        #
        # REFUSED WITH ITS OWN MESSAGE rather than quietly hidden on the client: the frontend
        # already surfaces a refusal, so the author reads why, and nothing extra has to travel in
        # the payload for one case. And the other players are unaffected — this is one player's
        # vote refused, not the step closing on everyone.
        room, players = self._room_with(3)
        self._to_rating(room, players)
        author, other = players[0], players[1]
        r = self._post(f"/api/rooms/{room.code}/ready", {"token": author.token})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "you wrote this one")
        # Nothing recorded, so a refused vote cannot be counted by a later call.
        self.assertEqual(Ready.objects.filter(room=room, player=author).count(), 0)
        # The author is not shown a bar either: `mine` is False and the step is not theirs.
        self.assertFalse(room_state(room.code)["ready"]["mine"])
        # And the other two can still vote, and still cross the bar.
        for p in players[1:]:
            self.assertEqual(self._post(f"/api/rooms/{room.code}/ready", {"token": p.token}).status_code, 200)
        room.refresh_from_db()
        self.assertEqual(room.rounds.get(order=0).revealed, 1)

    def test_a_restart_wipes_the_votes_of_the_previous_game(self):
        # The one place a wipe IS needed: a restart replays `quiz:0`, so this is the single case
        # where a stale step name comes back round and would otherwise count.
        #
        # It goes through the endpoint, so it needs the room helpers, and that is why it lives
        # here rather than beside the other model tests: a test that deletes the rows itself and
        # then finds them gone has asserted that `delete()` works.
        #
        # THE REPLAY IS WHAT MAKES IT NECESSARY. Everywhere else the votes survive the room moving
        # on, because `step` is part of their key. A restart is the one transition that brings a
        # step name BACK, so without the wipe the two players' votes from the previous game would
        # still be sitting on `quiz:0` and the relaunched room would open its first question
        # already holding enough votes to skip it.
        room, players = self._room_with(3)
        for p in players[:2]:
            self._ready(room, p)
        self.assertEqual(Ready.objects.filter(room=room, step="quiz:0").count(), 2)

        self._post(f"/api/admin/rooms/{room.code}/restart", {"tutorial": True})
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.WAITING)
        # The players and their tokens survive the restart, so the assertion above is about the
        # votes and nothing else.
        self.assertEqual(room.players.count(), 3)
        self.assertEqual(Ready.objects.filter(room=room).count(), 0)


class LobbyVoteTests(VoteBase):
    """The lobby is a step too, and it is the one step that asks for EVERYBODY.

    The class could not open its own game before this: `waiting` named no step, so the only thing
    that moved the room out of it was `admin_start`. Every test here is the vote path doing what the
    admin's button does, through the same function, and the two thresholds are asserted apart
    because a lobby on the discussion's majority is the one change that would break the feature
    silently — the bar would read « 2 sur 3 prêts » and start a game without the third phone.
    """

    def test_the_lobby_is_a_step_and_its_bar_asks_for_everybody(self):
        room, players = self._lobby(3)
        self.assertEqual(current_step(room), LOBBY_STEP)
        ready = room_state(room.code, players[0])["ready"]
        self.assertEqual((ready["step"], ready["count"], ready["needed"]), (LOBBY_STEP, 0, 3))
        # Three connected, three needed: a majority rule here would say 2 and let the class start
        # without the phone that has not been picked up yet.
        self.assertEqual(ready["needed"], len(players))

    def test_one_player_alone_in_the_lobby_starts_the_game_with_their_own_vote(self):
        # A THRESHOLD OF ONE, AND THE CLASS'S OWN VOTE IS THE ONE THAT CROSSES IT. Not a footgun,
        # and the reasoning is the same as the rule's: the bar counts CONNECTED players, so the only
        # player this can start a game for is the player pressing, in a room nobody else is in. It
        # cannot carry a group into a game they did not agree to — there is no group to carry. What
        # it CAN do is leave a one-student room unplayable, which is a real classroom (the teacher
        # trying it out, one group left after the others went home) and was the state this feature
        # existed to end.
        room, players = self._lobby(1)
        self.assertEqual(room_state(room.code, players[0])["ready"]["needed"], 1)
        r = self._post(f"/api/rooms/{room.code}/ready", {"token": players[0].token})
        self.assertEqual(r.status_code, 200)
        room.refresh_from_db()
        self.assertEqual((room.phase, room.quiz_index), (Room.Phase.QUIZ, 0))
        # And the ten rounds, drawn by the same line the admin's button runs.
        self.assertEqual(room.rounds.count(), 10)

    def test_one_of_two_ready_players_does_not_start_the_game(self):
        # IN THE LOBBY, ONE IS NOT ENOUGH. This is the line the discussion's rule would cross: at
        # two connected players a majority happens to be two as well, so this room is the SMALLEST
        # one that can tell the two rules apart, and the assertion is on the bar the phone shows
        # rather than only on the phase, because a lobby that started early would leave the second
        # player on a game nobody asked them to join.
        room, players = self._lobby(2)
        self.assertEqual(self._post(f"/api/rooms/{room.code}/ready", {"token": players[0].token}).status_code, 200)
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.WAITING)
        ready = room_state(room.code, players[1])["ready"]
        self.assertEqual((ready["count"], ready["needed"], ready["mine"]), (1, 2, False))
        # Nothing was drawn, so the room is exactly as it was: a lobby with a round nobody asked for
        # would break every caller of `current_round`, which is None in a lobby on purpose.
        self.assertEqual(room.rounds.count(), 0)

    def test_two_of_two_ready_players_start_the_game(self):
        room, players = self._lobby(2)
        self._post(f"/api/rooms/{room.code}/ready", {"token": players[0].token})
        # The reported bar is the acted one: the number on the phone is 2, which is what
        # `maybe_advance_by_votes` compares against. A `needed` from another rule would be a bar
        # nobody can trust, and `test_the_lobby_is_a_step` pins the rule separately.
        self.assertEqual(room_state(room.code, players[1])["ready"]["needed"], 2)
        r = self._post(f"/api/rooms/{room.code}/ready", {"token": players[1].token})
        self.assertEqual(r.status_code, 200)
        room.refresh_from_db()
        self.assertEqual((room.phase, room.quiz_index), (Room.Phase.QUIZ, 0))
        self.assertEqual(room.rounds.count(), 10)

    def test_the_bar_shrinks_when_one_of_the_ready_players_disconnects(self):
        # THE BAR IS THE CONNECTED PLAYERS, AND THAT WORKS IN THE LOBBY TOO — with one difference
        # from a discussion that is worth being explicit about. Three connected, two ready: the
        # lobby needs three, so nothing starts. Then one of the two READY phones sleeps, and the
        # bar falls to 1 of 2 — two connected players, one of them voted — which is still short, so
        # the room holds. A bar that counted `room.players` would have stayed at 3 of 3 in reach
        # forever on a phone in a bag, and the lobby would never open for anybody.
        room, players = self._lobby(3)
        for p in players[:2]:
            self._post(f"/api/rooms/{room.code}/ready", {"token": p.token})
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.WAITING)
        self.assertEqual(
            (room_state(room.code)["ready"]["count"], room_state(room.code)["ready"]["needed"]), (2, 3)
        )
        self._age_out(players[1])
        ready = room_state(room.code)["ready"]
        self.assertEqual((ready["count"], ready["needed"]), (1, 2))
        maybe_advance_by_votes(room)
        room.refresh_from_db()
        self.assertEqual((room.phase, room.rounds.count()), (Room.Phase.WAITING, 0))

    def test_a_player_joining_after_the_others_have_voted_grows_the_bar(self):
        # A LATE ARRIVAL PUTS THEMSELF BACK ON THE BAR, and their own vote is not the one that
        # vanishes: the two earlier votes stay on the `lobby` step because `step` is part of the
        # key, and the bar grows from 2 of 2 to 2 of 3. This is the cost of asking for everybody in
        # the lobby, and it is the right trade — the alternative is a game that starts while someone
        # is still walking into the room with their phone out.
        #
        # The join goes through the endpoint, so it is a real arrival: a `Player.objects.create`
        # would not put the newcomer in the presence registry, and `needed` counts presence.
        #
        # THREE IN THE ROOM AND TWO OF THEM VOTE, because a two-player room cannot show this: at 2
        # connected the lobby needs 2, so the second vote starts the game and there is no lobby
        # left for a fourth phone to walk into.
        room, players = self._lobby(3)
        for p in players[:2]:
            self._post(f"/api/rooms/{room.code}/ready", {"token": p.token})
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.WAITING)
        self.assertEqual(
            (room_state(room.code)["ready"]["count"], room_state(room.code)["ready"]["needed"]), (2, 3)
        )
        late = self._join(room, "late")
        self._online(room, late)
        ready = room_state(room.code)["ready"]
        self.assertEqual((ready["count"], ready["needed"]), (2, 4))
        # The earlier votes are still counted, on the step the room is still in.
        self.assertEqual(Ready.objects.filter(room=room, step=LOBBY_STEP).count(), 2)
        maybe_advance_by_votes(room)
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.WAITING)

    def test_the_vote_opens_the_didacticiel_on_question_zero_with_its_window(self):
        # EXACTLY WHAT `admin_start` LEAVES BEHIND, which is the whole point of the two writers
        # sharing one function: a class that votes itself into the game must not land somewhere the
        # admin's button would not have put it. Question 0 on screen, thirty seconds of window, ten
        # rounds behind the gate.
        room, players = self._lobby(2)
        for p in players:
            self._post(f"/api/rooms/{room.code}/ready", {"token": p.token})
        room.refresh_from_db()
        self.assertEqual((room.phase, room.quiz_index), (Room.Phase.QUIZ, 0))
        self.assertGreater(room.quiz_deadline, timezone.now())
        self.assertEqual(room.rounds.count(), 10)

    def test_a_room_without_the_didacticiel_goes_straight_to_playing(self):
        # THE OTHER BRANCH OF THE SAME FUNCTION. A teacher who created the room « sans
        # didacticiel » gets no gate from a vote either: the lobby's last vote lands in `playing`,
        # with no question and no window opened behind it, because `_start_game` reads the flag
        # exactly as the admin's button does.
        room, players = self._lobby(1, tutorial=False)
        self._post(f"/api/rooms/{room.code}/ready", {"token": players[0].token})
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.PLAYING)
        self.assertIsNone(room.quiz_deadline)
        self.assertEqual(room.rounds.count(), 10)

    def test_playing_and_done_still_have_no_step(self):
        # THE LOBBY IS NOT A PHASE-WIDE LOOSENING. `playing` and `done` name nothing, so a vote
        # there is still a 409 with nothing recorded — the two cases that were already refused, and
        # the reason the lobby needed its own branch rather than `waiting` being loosened into
        # everything that is not a discussion.
        room = self._create(tutorial=False)
        players = [self._join(room, f"p{i}") for i in range(2)]
        self._start(room)
        self._online(room, *players)
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.PLAYING)
        self.assertIsNone(current_step(room))
        self.assertIsNone(room_state(room.code)["ready"])
        self.assertEqual(self._post(f"/api/rooms/{room.code}/ready", {"token": players[0].token}).status_code, 409)
        self.assertEqual(Ready.objects.filter(room=room).count(), 0)
        room.phase = Room.Phase.DONE
        room.save(update_fields=["phase"])
        self.assertIsNone(current_step(room))
        self.assertIsNone(room_state(room.code)["ready"])

    def test_joining_after_a_vote_driven_start_is_still_refused(self):
        # THE LOBBY HAS NOT STAYED OPEN BY ACCIDENT. Once the class has started its own game the
        # room is `quiz`, and `join` still answers 409 — otherwise the third player would walk into
        # a game that had already asked everybody for a vote and start it with nobody.
        room, players = self._lobby(2)
        for p in players:
            self._post(f"/api/rooms/{room.code}/ready", {"token": p.token})
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.QUIZ)
        r = self._post(f"/api/rooms/{room.code}/join", {"name": "late"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "game already started")

    def test_a_lobby_with_nobody_connected_changes_nothing(self):
        # THE `0 == 0` GUARD, IN THE LOBBY, WITH THE VOTES ALREADY WRITTEN. This is the case that
        # would be worst to get wrong: a room whose phones all slept, holding a full set of ready
        # votes from before they dropped. Threshold of zero would cross on `0 >= 0` and open a game
        # in an empty room — twenty students' worth of votes firing a transition nobody is there to
        # see. The rows are written directly because the endpoint is not what is under test, and
        # they are written the way a real run leaves them: on the lobby step, one per player.
        room, players = self._lobby(2)
        self._drop_everyone(room)
        for p in players:
            Ready.objects.create(room=room, player=p, step=LOBBY_STEP)
        maybe_advance_by_votes(room)
        room.refresh_from_db()
        self.assertEqual((room.phase, room.rounds.count()), (Room.Phase.WAITING, 0))
