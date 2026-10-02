import json
import time
from datetime import timedelta
from unittest import mock

from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from game.llm import Verdict
from game.models import Player, QuizQuestion, Ready, Room
from game.prompts import majority
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


class ReadyModelTests(TestCase):
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


class VoteBase(TestCase):
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

    def _create(self) -> Room:
        """A room through the ADMIN ENDPOINT, not `Room.objects.create`.

        The bank is materialised in `admin_create_room` and nowhere else, so a room built on the
        ORM has no `QuizQuestion` rows at all — and an empty bank makes `_next_quiz` return False,
        which is the documented fall-through to opening the game. Every advance below would then
        land on `playing` instead of the next question, and the tests would be measuring the empty
        gate rather than the ready bar. Same reason, in the same words, as `test_quiz._create`.
        """
        self._login()
        code = self._post("/api/admin/rooms", {"tutorial": True}).json()["code"]
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
        with mock.patch("game.llm.answer", return_value="la reponse"), mock.patch(
            "game.llm.score", return_value=Verdict(8, "tres proche")
        ), mock.patch("game.llm.grade_prompt", return_value=Verdict(6, "methode")):
            r = self._post(
                f"/api/rooms/{room.code}/answer",
                {"token": player.token, "prompt": text},
            )
        self.assertEqual(r.status_code, 200, r.content)

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
