"""Cross-rating: the endpoint that takes a score, the reveal it is read against, the anonymity
that makes it fair, and the bound that keeps the result a judgement rather than a popularity
contest.

The two halves of the feature are tested apart on purpose. `PeerBoundTests` is a `SimpleTestCase`
and proves `prompts.quality` on its own, with no room and no request — the rule has to be right
before it can be applied correctly anywhere. `SettlementBoundTests` then proves the SAME rule
end to end, through the critic's billed call, the three players' taps and the settlement write,
because a bound that is right in isolation and wrong in the view is still wrong, and the two
failures look nothing alike.
"""

import json
import time
from unittest import mock

from django.test import Client, SimpleTestCase, TransactionTestCase, override_settings

from game import llm
from game.llm import Verdict
from game.models import Answer, Player, Rating, Room, Round
from game.prompts import QUALITY_BAND, SCALE_MAX, SCALE_MIN, half_up, quality
from game.tests.support import answer_chunks, drain
from game.views import (
    PRESENCE_TTL,
    _beat,
    _presence,
    _presence_lock,
    maybe_advance_phase,
    room_state,
    settle_round,
)


class PeerBoundTests(SimpleTestCase):
    """`prompts.quality`, the whole policy of the rating feature, with nothing around it."""

    def test_peers_cannot_push_more_than_three_points_above_the_llm(self) -> None:
        self.assertEqual(quality(llm=4, peers=[10, 10, 10]), 7)

    def test_peers_cannot_push_more_than_three_points_below(self) -> None:
        self.assertEqual(quality(llm=8, peers=[1, 1, 1]), 5)

    def test_a_sane_group_moves_the_score_within_the_band(self) -> None:
        self.assertEqual(quality(llm=5, peers=[7, 7, 8]), 7)

    def test_a_unanimous_group_inside_the_band_is_heard(self) -> None:
        # A group that judges correctly and disagrees with the critic by two points is not
        # silenced: the bound is a rail, not a veto. A rule that returned the critic's score
        # whenever it did not have to clamp — averaging the two, or clamping the critic to the
        # group — passes the two bound tests above and fails this one, which is why it exists.
        self.assertEqual(quality(llm=5, peers=[7, 7, 7]), 7)

    def test_no_peers_falls_back_to_the_llm(self) -> None:
        self.assertEqual(quality(llm=6, peers=[]), 6)

    def test_no_llm_falls_back_to_the_peers(self) -> None:
        # Offline, the critic is a stub. The stub must not silently become the score.
        self.assertEqual(quality(llm=None, peers=[8, 8]), 8)

    def test_neither_a_critic_nor_a_peer_is_zero_rather_than_a_crash(self) -> None:
        # The two empty cases meet in one room: a player alone in a lobby wrote a prompt nobody
        # rated, and there is no model either. Returning 0 is a bad round for that player; a
        # `ZeroDivisionError` in `settle_round` is a 500 on somebody else's screen, thrown while
        # the last rating of the round is landing.
        self.assertEqual(quality(llm=None, peers=[]), 0)

    def test_the_result_never_leaves_the_band_or_the_scale(self) -> None:
        # The property behind every one of the cases above, so a later rewrite of the clamp
        # cannot keep all six and break the rest of the range. Exhaustive on the critic and
        # exhaustive on the peers, which is cheap: the domain is 11 x 11.
        for llm in range(0, 11):
            for peers in ([], [1], [10], [1, 10], [7, 8, 6]):
                with self.subTest(llm=llm, peers=peers):
                    got = quality(llm=llm, peers=peers)
                    if not peers:
                        self.assertEqual(got, llm)
                    elif llm is None:
                        self.assertIn(got, range(SCALE_MIN, SCALE_MAX + 1))
                    else:
                        self.assertIn(got, range(llm - QUALITY_BAND, llm + QUALITY_BAND + 1))
                        self.assertIn(got, range(0, 11))

    def test_a_tie_breaks_half_up_and_not_to_the_even_number(self) -> None:
        # THE TIE, and the reason `quality` calls `half_up` rather than `round()`. Python's
        # `round()` is round-half-to-EVEN, so a peer mean of exactly 7.5 settles to 8 and one of
        # exactly 6.5 settles to 6 — two rooms that judged a prompt identically, one point apart,
        # for a reason no player could see and no player could argue with. Half-up is what a
        # classroom expects of a mean, and it is MONOTONE: every tie in the range falls the same
        # way, which is the property the second assertion pins.
        #
        # This test has no predecessor, and that is the point of adding it: every other case in
        # this class was written with its votes deliberately OFF the tie, so a change of rounding
        # mode passed all of them and the tie was never checked by anything.
        self.assertEqual(quality(llm=7, peers=[7, 8]), 8)  # 7.5 -> 8, not the even 8 by luck
        self.assertEqual(quality(llm=6, peers=[6, 7]), 7)  # 6.5 -> 7, which `round` would not give
        self.assertEqual(quality(llm=None, peers=[7, 8]), 8)  # the unanchored branch, same tie
        self.assertEqual(quality(llm=None, peers=[6, 7]), 7)
        # Three votes, so the mean is not a .5 at all and must be untouched by the change: 7.33
        # still rounds to 7, and 7.67 to 8. This is the case that would catch `half_up` being
        # wrong in the other direction — a banker's-rounding-like floor of everything above the
        # mean would move these two.
        self.assertEqual(quality(llm=5, peers=[7, 7, 8]), 7)  # 7.33, not a tie
        self.assertEqual(quality(llm=5, peers=[7, 8, 8]), 8)  # 7.67, not a tie
        # Monotone across the whole tie: for every integer mean-plus-a-half there is one answer,
        # and it is never below the floor.
        for lo in range(1, 10):
            with self.subTest(lo=lo):
                self.assertEqual(half_up(lo + 0.5), lo + 1)
                self.assertEqual(quality(llm=lo, peers=[lo, lo + 1]), lo + 1)

    def test_a_tie_falls_inside_the_band_the_same_way(self) -> None:
        # The tie has to be settled the same way on BOTH branches, and that it is not automatic:
        # the anchored branch clamps to the band first and rounds second, so the clamp could land
        # on a tie of its own. It does not — the band's ends are integers — and this pins it, since
        # a rewrite that rounded before clamping would still pass every other case here.
        self.assertEqual(quality(llm=5, peers=[8, 9]), 8)  # 8.5 clamped to 8 by the band, exactly
        self.assertEqual(quality(llm=8, peers=[4, 5]), 5)  # 4.5 clamped up to 5 by the band
        # And the two ends of the band are integers, so nothing rounds INTO it or out of it.
        self.assertEqual(quality(llm=4, peers=[10, 10, 10]), 7)
        self.assertEqual(quality(llm=8, peers=[1, 1, 1]), 5)

    def test_the_band_is_three(self) -> None:
        # Pinned because the number is the feature and it is otherwise a bare constant: a later
        # "let's make it four" would move every ranking in the game and no test of the function
        # alone would notice, because the function would be self-consistent.
        self.assertEqual(QUALITY_BAND, 3)

    def test_the_scale_is_one_to_ten(self) -> None:
        # The same argument for the scale, and it is the one the endpoint's 400, the `Rating`
        # check constraint and the visual scale all have to agree on.
        self.assertEqual((SCALE_MIN, SCALE_MAX), (1, 10))


@override_settings(ADMIN_PASSWORD="s3cret")
class RatingPlayTests(TransactionTestCase):
    """The endpoint, the reveal it is read against, and the anonymity that makes it fair."""

    def setUp(self) -> None:
        self.c = Client()
        self.c.post("/api/admin/login", data=json.dumps({"password": "s3cret"}), content_type="application/json")
        # Process-local, so a test rollback does not clear it: a heartbeat from an earlier test
        # marks a same-numbered player online in a later one, and every advance assertion here is
        # then measuring the previous test's leftovers.
        _presence.clear()

    # --- fixtures ---------------------------------------------------------------------------

    def _post(self, url: str, data: dict | None = None):
        return self.c.post(url, data=json.dumps(data or {}), content_type="application/json")

    def _rating_room(
        self,
        *,
        answers: int = 2,
        anonymous: bool = True,
        names: tuple[str, ...] = ("ana", "bob", "clea"),
        proximity: int = 5,
        quality_llm: int = 6,
        code: str = "RATE",
    ) -> tuple[Room, list[Player], list[Answer]]:
        """A room parked in `rating` on round 0, with `answers` answers already on it.

        Built on the ORM, in the style of `test_admin._in_phase`: these tests are about what the
        rating phase does with prompts that exist, and a fixture that walked there through
        `start` + two `next`s would be billing three LLM calls per answer and testing the
        transitions again on every single assertion.

        The names are in JOIN order and deliberately not alphabetical, so "Joueur N" cannot pass by
        accident on a sort. Two rounds, for the same reason as `test_admin`: with one round,
        `next_round()` from the summary goes straight to `done` and a summary test would be testing
        the end of the game.

        The answers are created in player order, so the answer at reveal index `i` was written by
        `players[i]` — which is what makes "the author of the prompt on screen" checkable without
        re-deriving the ordering in every test.
        """
        room = Room.objects.create(code=code, tutorial=False, anonymous=anonymous)
        for order in range(2):
            Round.objects.create(room=room, order=order, target=f"cible {order}")
        rnd = room.rounds.get(order=0)
        players = [Player.objects.create(room=room, name=name) for name in names]
        rows = [
            rnd.answers.create(
                player=p,
                prompt=f"prompt {i}",
                output=f"reponse {i}",
                score=proximity,
                quality_llm=quality_llm,
            )
            for i, p in enumerate(players[:answers])
        ]
        room.current = 0
        room.phase = Room.Phase.RATING
        room.save(update_fields=["current", "phase"])
        return room, players, rows

    def _online(self, room: Room, *players: Player) -> None:
        """Give these players a live heartbeat, as their own SSE stream would. The advance counts
        presence, and a test client that never opens a stream has no beats."""
        for p in players:
            _beat(room.code, p.pk)

    def _rate(self, room: Room, rater: Player, score):
        """A vote. Note what is NOT an argument: which prompt.

        The fixture knows the answer at the current reveal index, the view derives it, and this
        helper never says. That is the endpoint's whole security property, and a helper taking an
        `answer` would let a test look like it were checking it while actually only checking
        whatever the cursor happened to be.
        """
        return self._post(
            f"/api/rooms/{room.code}/rate",
            {"token": rater.token, "score": score},
        )

    def test_playing_a_round_actually_opens_the_rating(self) -> None:
        """The regression that matters most, because it is the one nobody would notice.

        Every other test in this class builds its room parked in `rating` on the ORM. So the
        whole file passed while the real path was broken: `maybe_advance_phase` called
        `room.next_round()` when the last player answered, which skips `rating` AND `summary`
        entirely and goes straight to the next round. A class playing normally — last student
        taps send, round closes, next round opens — would never have seen the cross-rating at
        all, and the teacher would have had to press "open the rating" by hand every round to
        get the feature this whole project exists for.

        It is here, and it walks `POST /answer` twice, because a test that hand-builds the phase
        it is testing cannot catch a bug in the transition INTO that phase.
        """
        self._post("/api/admin/rooms", {"tutorial": False})
        code = Room.objects.get().code
        for name in ("ana", "bob"):
            self._post(f"/api/rooms/{code}/join", {"name": name})
        self._post(f"/api/admin/rooms/{code}/start")
        room = Room.objects.get(code=code)
        players = list(room.players.order_by("created_at"))
        self._online(room, *players)

        with (
            mock.patch("game.llm.answer_stream", new=answer_chunks("le resultat")),
            mock.patch("game.llm.score", return_value=Verdict(8, "bien")),
            mock.patch("game.llm.grade_prompt", return_value=Verdict(6, "moyen")),
        ):
            for p in players:
                drain(self._post(f"/api/rooms/{code}/answer", {"token": p.token, "prompt": "mon prompt"}))
                room.refresh_from_db()

        self.assertEqual(room.phase, Room.Phase.RATING, "the last answer must open the rating")
        self.assertEqual(room.current, 0, "and it must not have skipped to the next round")
        self.assertEqual(room.rounds.get(order=0).revealed, 0, "on the FIRST prompt")

    def _age_out(self, player: Player) -> None:
        """Push a player's last beat into the past, as a sleeping phone does, without sleeping.
        The registry's own lock is taken, so a test writing it is not doing what the SSE threads
        are protected from doing."""
        with _presence_lock:
            _presence[(player.room_id, player.pk)] = time.monotonic() - PRESENCE_TTL - 1

    def _assert_phase(self, room: Room, phase: str) -> None:
        room.refresh_from_db()
        self.assertEqual(room.phase, phase)

    def _revealed(self, room: Room) -> int:
        return room.rounds.get(order=room.current).revealed

    # --- the endpoint -----------------------------------------------------------------------

    def test_a_rating_is_accepted_and_stores_the_score(self) -> None:
        room, players, rows = self._rating_room(answers=2)
        self._online(room, *players)
        r = self._rate(room, players[1], 7)
        self.assertEqual(r.status_code, 200, r.content)
        # `{"ok": true}` and nothing else. The screen's progress comes from the next SSE frame's
        # `reveal` block, and a second copy of that number here is a second truth to keep in step.
        self.assertEqual(r.json(), {"ok": True})
        self.assertEqual(Rating.objects.get(answer=rows[0], rater=players[1]).score, 7)

    def test_rating_outside_the_rating_phase_is_refused(self) -> None:
        # The other two phases players are in are the two the button is reachable from, and the
        # endpoint has to be shut in both: a note accepted outside the phase would sit in the
        # table and be counted by a settlement that was never meant to include it.
        room, players, rows = self._rating_room(answers=2)
        for phase in (Room.Phase.PLAYING, Room.Phase.SUMMARY, Room.Phase.WAITING, Room.Phase.DONE):
            with self.subTest(phase=phase):
                room.phase = phase
                room.save(update_fields=["phase"])
                r = self._rate(room, players[1], 7)
                self.assertEqual(r.status_code, 409)
                self.assertEqual(r.json()["error"], "not rating")
        self.assertEqual(Rating.objects.count(), 0)

    def test_a_blank_or_foreign_token_is_refused(self) -> None:
        # 403, not 400 or 404: the token is the authentication, and it is checked against THIS
        # room, so a token from another room must not vote in it.
        room, players, rows = self._rating_room(answers=2)
        other = Room.objects.create(code="OTHR")
        stranger = Player.objects.create(room=other, name="zoe")
        for token in ("", "nope", stranger.token):
            with self.subTest(token=token):
                r = self._post(f"/api/rooms/{room.code}/rate", {"token": token, "score": 7})
                self.assertEqual(r.status_code, 403)
                self.assertEqual(r.json()["error"], "bad token")
        self.assertEqual(Rating.objects.count(), 0)

    def test_a_player_cannot_rate_their_own_prompt(self) -> None:
        # The one rule the client cannot be trusted to enforce, and the reason the target is
        # derived here rather than named in the body: this refusal happens for a body that asks
        # for nothing but a score.
        room, players, rows = self._rating_room(answers=2)
        self._online(room, *players)
        r = self._rate(room, players[0], 7)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "your own prompt")
        self.assertEqual(Rating.objects.count(), 0)
        # And it did not move the reveal: a self-rating is not a third of the way to advancing.
        self.assertEqual(self._revealed(room), 0)

    def test_a_score_outside_the_scale_is_refused(self) -> None:
        # Every one of these would be accepted by a bare `1 <= score <= 10` check, or by a type
        # check alone: a bool IS an int in Python, and a float is not an int. The last one is the
        # nastiest, because a 7.5 that reached the `Rating` check constraint is a 500 on a screen
        # the player is holding, not a 400 they can act on.
        room, players, rows = self._rating_room(answers=2)
        for score in (0, 11, -1, 3.5, "7", None, True, False, [7], {}):
            with self.subTest(score=score):
                r = self._rate(room, players[1], score)
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json()["error"], "score 1-10")
        self.assertEqual(Rating.objects.count(), 0)
        # The ends of the scale are legal, so a 400 above is about the range and not the endpoint.
        self._online(room, *players)
        self.assertEqual(self._rate(room, players[1], 1).status_code, 200)
        self.assertEqual(self._rate(room, players[2], 10).status_code, 200)

    def test_a_player_cannot_rate_the_same_prompt_twice(self) -> None:
        room, players, rows = self._rating_room(answers=2)
        self._online(room, *players)
        self.assertEqual(self._rate(room, players[1], 7).status_code, 200)
        r = self._rate(room, players[1], 3)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "already rated")
        # The stored score is the FIRST one: a retry that lands after the write must not rewrite
        # what the player said, only tell them they already said it.
        self.assertEqual(Rating.objects.get(answer=rows[0], rater=players[1]).score, 7)

    def test_already_rated_is_per_prompt_not_per_player(self) -> None:
        # The mirror, and the reason the two tests above do not merge: rating is one note per
        # PROMPT. A player who voted on prompt 1 has to be able to vote on prompt 2, and the only
        # thing that changes between the two taps is the reveal cursor moving under them. A check
        # written as "has this player rated anything this round" would 409 here and lock a player
        # out of every prompt after the first — a room that would never close.
        # Four players, three prompts: dan wrote nothing, so dan is the only player who may rate
        # BOTH of the first two prompts, and is therefore the one whose second vote is the test.
        room, players, rows = self._rating_room(answers=3, names=("ana", "bob", "clea", "dan"))
        self._online(room, *players)
        self._rate(room, players[3], 7)
        self._rate(room, players[1], 7)  # closes prompt 1
        self._rate(room, players[2], 7)
        self.assertEqual(self._revealed(room), 1)
        self.assertEqual(self._rate(room, players[3], 9).status_code, 200)  # same player, next prompt
        self.assertEqual(Rating.objects.filter(rater=players[3]).count(), 2)

    def test_a_duplicate_row_committed_mid_flight_returns_409(self) -> None:
        # The durable backstop the view's own check cannot see: two taps on a phone that answered
        # twice before either request came back both find no rating, and `unique_together` is the
        # source of truth. Without the `IntegrityError` handling this is a 500, and the test that
        # would catch it — the one below — is the only thing in the suite that can reach it.
        room, players, rows = self._rating_room(answers=2)
        winner = Rating.objects.create(answer=rows[0], rater=players[1], score=4)

        class _BeforeTheRace:
            def exists(self) -> bool:
                return False

        with mock.patch.object(Rating.objects, "filter", return_value=_BeforeTheRace()):
            r = self._rate(room, players[1], 10)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "already rated")
        # The row that survived is the winner's, score and all: a lost race must not overwrite it.
        self.assertEqual(Rating.objects.get().pk, winner.pk)
        self.assertEqual(Rating.objects.get().score, 4)
        self.assertEqual(Rating.objects.count(), 1)

    def test_a_rating_on_a_round_nobody_answered_is_refused(self) -> None:
        # The rating phase of a round with no prompt in it. There is no index to read, so the
        # endpoint answers 409 rather than raising IndexError — a 500 on the one phone in the room
        # that is awake, while the other twenty are asleep.
        room = Room.objects.create(code="RATE", tutorial=False)
        Round.objects.create(room=room, order=0, target="cible")
        player = Player.objects.create(room=room, name="ana")
        room.phase = Room.Phase.RATING
        room.save(update_fields=["phase"])
        r = self._rate(room, player, 7)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "not rating")

    # --- the reveal advancing ---------------------------------------------------------------

    def test_rating_a_prompt_advances_the_reveal_for_everyone(self) -> None:
        # The phase closing on its own is the whole point of rating on the reveal: three prompts,
        # every one rated by the two players who did not write it, and the room walks itself
        # forward with nobody's finger on a button.
        room, players, rows = self._rating_room(answers=3)  # each of them wrote one prompt
        self._online(room, *players)
        self._rate(room, players[1], 7)
        self.assertEqual(self._revealed(room), 0)  # clea has not voted yet
        self._rate(room, players[2], 8)
        self.assertEqual(self._revealed(room), 1)
        # Prompt 1 is bob's, so bob is the one refused now — the exclusion follows the prompt, not
        # the player.
        self.assertEqual(self._rate(room, players[1], 5).status_code, 409)
        self._rate(room, players[0], 6)
        self.assertEqual(self._revealed(room), 1)  # clea is still owed a vote
        self._rate(room, players[2], 6)
        self.assertEqual(self._revealed(room), 2)

    def test_the_author_of_the_prompt_on_screen_is_never_waited_for(self) -> None:
        # The exclusion, from the only side it can be observed: the author is online, in the
        # roster, and forbidden to vote — so counting them would leave the room on prompt 1
        # forever. A room of two that rated one prompt is finished with that prompt.
        room, players, rows = self._rating_room(answers=2, names=("ana", "bob"))
        self._online(room, *players)
        self._rate(room, players[1], 7)
        self.assertEqual(self._revealed(room), 1)
        self._assert_phase(room, Room.Phase.RATING)

    def test_the_reveal_does_not_advance_when_only_the_author_is_online(self) -> None:
        # `all([])` is True. The author is connected and is the only person who may not vote, so
        # the set of raters this phase is waiting on is EMPTY — and an empty set is everybody
        # having voted. The room must sit on its prompt rather than walk itself forward on the
        # votes of nobody. The one vote that does land is from a player the game is not waiting
        # for, which is exactly the case that makes the empty set reachable.
        room, players, rows = self._rating_room(answers=2, names=("ana", "bob"))
        self._online(room, players[0])  # only the author
        self._rate(room, players[1], 7)  # bob votes from a phone that is not streaming
        self.assertEqual(self._revealed(room), 0)
        self._assert_phase(room, Room.Phase.RATING)

    def test_nothing_advances_when_no_player_is_online(self) -> None:
        # The same vacuous truth one level up: with every phone dropped there is no evidence
        # anyone is here, so a phase is only ever closed by a real submit made by a real stream.
        room, players, rows = self._rating_room(answers=2)
        self._rate(room, players[1], 7)
        self._rate(room, players[2], 7)
        self.assertEqual(self._revealed(room), 0)
        self._assert_phase(room, Room.Phase.RATING)

    def test_an_offline_rater_stops_holding_the_reveal(self) -> None:
        # Presence, live in both directions, for the rating phase as for the writing one. Bob's
        # phone sleeps between the two taps: the room is not held on his vote, and when his beats
        # come back he is back in the bar and his vote counts again.
        room, players, rows = self._rating_room(answers=2)  # clea wrote nothing
        self._online(room, *players)
        self._age_out(players[2])  # clea's phone slept
        # Prompt 0 is ana's, so the player the bar waits on is bob alone now, and his one vote
        # closes it. A bar built on `room.players` would sit on prompt 1 forever.
        self._rate(room, players[1], 7)
        self.assertEqual(self._revealed(room), 1)
        # And the round closes with two of three phones awake: prompt 1 is bob's, so the one player
        # left who may rate it is ana, and she is alone in the bar by the same reasoning.
        self._rate(room, players[0], 6)
        self._assert_phase(room, Room.Phase.SUMMARY)

    def test_the_last_prompt_settles_the_round_and_opens_the_summary(self) -> None:
        # The two things that happen on one tap: the reveal runs out, and the round is settled
        # before the summary is ever read. A summary that arrived before the settlement would show
        # a column of nulls for a fifth of a second and then fix itself.
        #
        # WHO RATES WHICH PROMPT is the whole mechanic and is worth spelling out: a player is only
        # excluded from the prompt THEY wrote, so on prompt 1 it is the two players who did not
        # write it, not the two who rated prompt 0. Getting that wrong in a fixture produces a
        # test that waits for a vote the game will never accept.
        room, players, rows = self._rating_room(answers=2)  # ana wrote 0, bob wrote 1, clea sat out
        self._online(room, *players)
        self._rate(room, players[1], 7)  # bob on ana's prompt
        self._rate(room, players[2], 9)  # clea on ana's prompt -> prompt 1
        self.assertEqual(self._revealed(room), 1)
        self._rate(room, players[0], 5)  # ana on BOB's prompt: allowed, she did not write it
        self._rate(room, players[2], 9)  # clea, the last rater standing
        self._assert_phase(room, Room.Phase.SUMMARY)
        # Every answer of the round is written, not just the one revealed last: a settlement that
        # only reached the last prompt would leave the summary a mixture of numbers and nulls.
        settled = Answer.objects.filter(round__room=room)
        self.assertEqual(settled.filter(quality__isnull=True).count(), 0)
        # Prompt 0 drew {7, 9} and prompt 1 drew {5, 9}: means 8 and 7, both inside the band
        # around the critic's 6, so the two qualities are 8 and 7. The round's note is then the
        # spec's mean of the two dimensions, and 5 + 8 is an odd sum, so the two rows no longer
        # collapse onto one number: 6.5 and 6. A `// 2` used to make both 6, which is the whole
        # reason §7's `/` is worth a FloatField.
        self.assertEqual({a.quality_peers for a in settled}, {7, 8})
        self.assertEqual({a.quality for a in settled}, {7, 8})
        self.assertEqual({a.round_score for a in settled}, {(5 + 8) / 2, (5 + 7) / 2})

    def test_the_summary_phase_never_moves_on_its_own(self) -> None:
        # A round gets commented on, so nothing in this file may advance out of it. Every submit
        # that could have called the advance function is closed by the phase gate above, and the
        # function itself refuses the phase, which is what this pins.
        room, players, rows = self._rating_room(answers=2)
        self._online(room, *players)
        room.phase = Room.Phase.SUMMARY
        room.save(update_fields=["phase"])
        maybe_advance_phase(room)
        self._assert_phase(room, Room.Phase.SUMMARY)

    # --- the snapshot: the reveal -----------------------------------------------------------

    def test_prompts_are_revealed_one_at_a_time(self) -> None:
        room, _players, rows = self._rating_room(answers=3)
        state = room_state(room.code)
        self.assertEqual(state["phase"], "rating")
        self.assertEqual(
            {k: state["reveal"][k] for k in ("index", "count", "prompt")},
            {"index": 0, "count": 3, "prompt": "prompt 0"},
        )
        # And the cursor moves the block, not just the number: after the advance, the SECOND
        # prompt is the one in the payload.
        rnd = room.rounds.get(order=0)
        rnd.revealed = 1
        rnd.save(update_fields=["revealed"])
        self.assertEqual(room_state(room.code)["reveal"]["prompt"], "prompt 1")

    def test_the_reveal_counts_the_raters_of_the_prompt_on_screen(self) -> None:
        # `rated` is the numerator of the "3 notés" the player is waiting on, so it counts the
        # CURRENT prompt's raters. A number that summed the whole round's ratings would jump from
        # 2 to 6 as the reveal walks the prompts and would be a progress bar for something else.
        room, players, rows = self._rating_room(answers=3)
        self._online(room, *players)
        self.assertEqual(room_state(room.code)["reveal"]["rated"], 0)
        self._rate(room, players[1], 7)
        self.assertEqual(room_state(room.code)["reveal"]["rated"], 1)
        self._rate(room, players[2], 8)
        self.assertEqual(self._revealed(room), 1)
        # Prompt 2 has been revealed and NOBODY has rated it: the count went back to 0, which is
        # the difference between this and a round-wide total.
        self.assertEqual(room_state(room.code)["reveal"]["rated"], 0)

    def test_each_phase_block_is_gated_to_its_own_phase(self) -> None:
        # The three blocks are UNCONDITIONAL KEYS — present in every snapshot, `None` outside the
        # phase that fills them — so what this pins is WHICH PHASE FILLS WHICH KEY, not which keys
        # exist. A summary screen that also found a `reveal` would have two blocks to reconcile, and
        # a rating screen that found a `summary` would show a round nobody has read yet. Asserted
        # on the VALUE (`None` or not) rather than on `in`, because the null IS the contract: a key
        # that went missing again would pass a membership check and break every screen.
        phases = (
            Room.Phase.WAITING,
            Room.Phase.QUIZ,
            Room.Phase.PLAYING,
            Room.Phase.RATING,
            Room.Phase.SUMMARY,
            Room.Phase.DONE,
        )
        for i, phase in enumerate(phases):
            with self.subTest(phase=phase):
                # A room per phase, because the code is the primary key and the same one twice is
                # an IntegrityError rather than a second room.
                room, _players, _rows = self._rating_room(answers=2, code=f"RAT{i}")
                room.phase = phase
                room.save(update_fields=["phase"])
                state = room_state(room.code)
                for block in ("quiz", "reveal", "summary"):
                    self.assertIn(block, state)  # always there, in all six phases
                self.assertEqual(state["quiz"] is not None, phase == Room.Phase.QUIZ)
                self.assertEqual(state["reveal"] is not None, phase == Room.Phase.RATING)
                self.assertEqual(state["summary"] is not None, phase == Room.Phase.SUMMARY)

    def test_the_key_set_is_identical_in_all_six_phases(self) -> None:
        # The property the three unconditional keys exist FOR, and the one that cannot be checked
        # one phase at a time: a client writes its branches once against a shape that does not move
        # under it. This compares the key SETS across all six phases, so a block that is added to
        # the snapshot in one phase only — the bug this contract exists to remove — fails here even
        # though every other test in the file passes.
        shapes: set[frozenset[str]] = set()
        for i, phase in enumerate(Room.Phase.values):
            with self.subTest(phase=phase):
                room, _players, _rows = self._rating_room(answers=2, code=f"KEY{i}")
                room.phase = phase
                room.save(update_fields=["phase"])
                shapes.add(frozenset(room_state(room.code)))
        # ONE shape for all six. Written as a set of key sets rather than a loop of pairwise
        # comparisons because the failure this catches is a phase adding a key: six pairwise
        # assertions all pass while a seventh key exists in one phase, and only the count of
        # DISTINCT shapes drops.
        self.assertEqual(len(shapes), 1, f"six phases produced {len(shapes)} different key sets")
        self.assertEqual(
            shapes.pop(),
            frozenset(
                {
                    "code", "phase", "current", "total", "target", "anonymous",
                    "players", "online_count", "quiz", "reveal", "summary", "ready",
                }
            ),
        )

    def test_a_rating_phase_with_nothing_to_reveal_carries_a_null_reveal(self) -> None:
        # The admin can open the rating on a round nobody answered. There is no prompt, so there
        # is no reveal to describe — and the key is NULL rather than absent, which is the frozen
        # `RevealState | null` the client null-checks. An empty `prompt` string would be a blank
        # card on a player's screen instead of a phase with nothing in it.
        room = Room.objects.create(code="RATE", tutorial=False)
        Round.objects.create(room=room, order=0, target="cible")
        room.phase = Room.Phase.RATING
        room.save(update_fields=["phase"])
        self.assertIsNone(room_state(room.code)["reveal"])
        # And the admin's button still gets them out of it, with a summary of nothing.
        self.assertEqual(self._post("/api/admin/rooms/RATE/next").status_code, 200)
        self._assert_phase(room, Room.Phase.SUMMARY)
        self.assertEqual(room_state(room.code)["summary"]["rows"], [])

    # --- the snapshot: the anonymity ---------------------------------------------------------

    def test_anonymous_rating_hides_the_author(self) -> None:
        room, _players, _rows = self._rating_room(answers=2, anonymous=True)
        reveal = room_state(room.code)["reveal"]
        self.assertIsNone(reveal["author"])
        # The label is ALWAYS a string to draw, so a screen never branches on anonymity to decide
        # whether it has something to put on the card.
        self.assertEqual(reveal["author_label"], "Joueur 1")

    def test_named_rating_shows_the_author(self) -> None:
        room, players, _rows = self._rating_room(answers=2, anonymous=False)
        reveal = room_state(room.code)["reveal"]
        self.assertEqual(reveal["author"], players[0].name)
        self.assertEqual(reveal["author_label"], players[0].name)

    def test_the_pseudonym_counts_arrival_order_not_the_alphabet(self) -> None:
        # "Joueur N" is a stable handle, so N has to come from something the room agrees on: who
        # arrived first. A name sort would number "ana" second in a room where she joined first,
        # and the same prompt would be two different players depending on the snapshot.
        room, players, _rows = self._rating_room(answers=3, names=("ana", "bob", "clea"), anonymous=True)
        self.assertEqual([p.name for p in players], ["ana", "bob", "clea"])
        rnd = room.rounds.get(order=0)
        for index, expected in enumerate(["Joueur 1", "Joueur 2", "Joueur 3"]):
            with self.subTest(index=index):
                rnd.revealed = index
                rnd.save(update_fields=["revealed"])
                self.assertEqual(room_state(room.code)["reveal"]["author_label"], expected)

    def test_the_pseudonym_does_not_move_when_the_room_changes(self) -> None:
        # STABLE FOR THE WHOLE GAME, and this is the test that holds it: a handle derived from a
        # row id, from the online players, or from the reveal cursor would all be stable in the
        # first test above and all wrong here. A player who watched their own handle change between
        # two prompts of the same game would recognise their own writing in a second costume, and
        # the anonymity would be defeated twice — once on the prompt and once in the ranking under
        # it. So: a late joiner arrives, a phone sleeps, a vote lands, and the label holds.
        room, players, rows = self._rating_room(answers=2, names=("ana", "bob"), anonymous=True)
        before = room_state(room.code)["reveal"]["author_label"]
        self.assertEqual(before, "Joueur 1")
        self._online(room, *players)
        self._rate(room, players[1], 7)  # prompt 0 closes, prompt 1 (bob's) is on screen
        self._age_out(players[0])  # ana's phone sleeps
        Player.objects.create(room=room, name="zoe")  # and a late joiner arrives
        self.assertEqual(self._revealed(room), 1)
        self.assertEqual(room_state(room.code)["reveal"]["author_label"], "Joueur 2")
        # The author of the FIRST prompt is still "Joueur 1" after a vote, a sleep and a joiner,
        # and the roster grew by one without renumbering anybody.
        rnd = room.rounds.get(order=0)
        rnd.revealed = 0
        rnd.save(update_fields=["revealed"])
        self.assertEqual(room_state(room.code)["reveal"]["author_label"], before)

    def test_anonymity_hides_the_score_and_the_round_score_during_rating(self) -> None:
        # The leak the spec names: if the payload still carries each player's score, a player
        # reads their own round score — the one `answer` just handed them — sees it missing from
        # the column, and works out WHICH revealed line is theirs. That defeats the whole feature.
        #
        # FOUR keys, not two and not seven. `prompt`, `output` and `comment` are not blanked here
        # any more because they are not SENT here either, in any phase: they are not in the frozen
        # `Player` type, no screen reads them, and the prompt and output a player needs to read the
        # round arrive in the `summary` rows. A key that is a string in one phase and null in
        # another is a key every consumer has to null-check for a reason that no longer exists.
        # `proximity_total` and `quality_total` ARE blanked, and `test_the_rating_phase_hides_the
        # dimension_totals` below is why: they are scores.
        room, _players, _rows = self._rating_room(answers=3, anonymous=True)
        state = room_state(room.code)
        for p in state["players"]:
            with self.subTest(player=p["name"]):
                for key in ("score", "round_score", "proximity_total", "quality_total"):
                    self.assertIsNone(p[key])
                for key in ("prompt", "output", "comment"):
                    self.assertNotIn(key, p)
        # The roster itself is NOT a leak: knowing who is in the room says nothing about who wrote
        # which prompt. It is the scores that cross-reference, and they are gone.
        self.assertEqual([p["name"] for p in state["players"]], ["ana", "bob", "clea"])
        self.assertEqual([p["answered"] for p in state["players"]], [True, True, True])

    def test_the_rating_phase_hides_the_dimension_totals(self) -> None:
        # THE LEAK UNDER THE OTHER ONE, and the reason the phase masks a number the summary is
        # about to show in full. A player in the rating phase reads their own round score, adds
        # the two halves of their own row, and gets their own total: 8 + 8 is their 16. Every
        # other player's `proximity_total` and `quality_total` sat next to it UNMASKED, so the
        # sum was readable straight off the board — the exact cross-reference §4 closes by hiding
        # the ranking, which the two totals defeated from underneath.
        #
        # The fixture is a real state of the game, not a contrivance: rounds 0..N-1 are settled
        # while round N is being rated, so the totals are non-null and the phase is `rating` at the
        # same time. Every other test in this class sits in `rating` on round 0 with nothing
        # settled, where these two keys are null for the boring reason that the round is not over
        # yet — which is why the leak survived a suite that has always asserted on them.
        room, _players, rows = self._rating_room(answers=2, anonymous=True)
        settle_round(room)  # one settled round behind the reveal, as in round 2 onwards
        masked = room_state(room.code)
        self.assertEqual(masked["phase"], "rating")
        # First prove the fixture HAS the numbers to leak, by reading them with the mask off. A
        # mask test over nulls passes for the wrong reason, so the same rows are read twice and
        # compared: settled, in the rating, and then settled, out of it. The fixture's critic is
        # 6 and nobody rated, so quality stands on the critic's own score.
        room.phase = Room.Phase.SUMMARY
        room.save(update_fields=["phase"])
        revealed = {p["name"]: (p["proximity_total"], p["quality_total"]) for p in room_state(room.code)["players"]}
        self.assertEqual(revealed, {"ana": (5, 6), "bob": (5, 6), "clea": (None, None)})
        self.assertEqual(
            {p["name"]: (p["proximity_total"], p["quality_total"]) for p in masked["players"]},
            {name: (None, None) for name in revealed},
        )
        # The names are still there, which is the part of the board the spec keeps: it is the
        # scores that cross-reference, not the roster.
        self.assertEqual([p["name"] for p in masked["players"]], [r.player.name for r in rows] + ["clea"])
        self.assertEqual([p["answered"] for p in masked["players"]], [True, True, False])
        # `quiz_score` and `online` are not scores, and stay.
        self.assertEqual([p["quiz_score"] for p in masked["players"]], [0, 0, 0])

    def test_the_summary_gives_the_dimension_totals_back(self) -> None:
        # The mask is scoped to the rating phase, not to anonymity and not to the game: the
        # summary is where the round is read out loud, and the final screen is where the two
        # halves are the lesson. So the same rows that were null a moment ago carry their numbers
        # again. Proximity 5 and the critic's unrated 6, so the total is 5.5 — a half, which is
        # the spec's arithmetic and the reason the column is a float.
        room, _players, _rows = self._rating_room(answers=2, anonymous=True)
        settle_round(room)
        room.phase = Room.Phase.SUMMARY
        room.save(update_fields=["phase"])
        view = [p for p in room_state(room.code)["players"] if p["answered"]]
        for p in view:
            self.assertEqual((p["proximity_total"], p["quality_total"]), (5, 6))
            self.assertEqual(p["score"], (5 + 6) / 2)

    def test_the_snapshot_still_sorts_with_every_score_blanked(self) -> None:
        # THE CRASH. `room_state` sorts on `-(score)`, and `-None` is a TypeError: the whole
        # snapshot dies, `events` dies with it, and the rating phase never starts for anybody. The
        # bug is invisible in every other phase, so it is invisible until a class of twenty sits in
        # front of a blank screen and nothing loads. This asserts the snapshot BUILDS, and that
        # it is the sort specifically: three players, all blanked, ordered by arrival.
        room, _players, _rows = self._rating_room(answers=3, anonymous=True)
        state = room_state(room.code)  # would raise, not return, on `-None`
        self.assertEqual([p["name"] for p in state["players"]], ["ana", "bob", "clea"])
        # And it still reaches the wire: the SSE thread `json.dumps` this payload, so anything
        # unserialisable here would kill the stream just as dead as the TypeError did.
        self.assertEqual(json.loads(json.dumps(state, ensure_ascii=False)), state)

    def test_the_prompt_and_the_score_come_back_after_the_rating(self) -> None:
        # The blanking is scoped to the rating phase, not to anonymity as such: the summary is
        # where the round gets read out loud, and there a player has to be able to see their own
        # score against the others'. The PROMPT comes back on the summary ROW rather than on the
        # player, which is where it is read from — the final `Player` type has no such key, so
        # putting it back here would contradict the contract the screens are coded against.
        room, players, rows = self._rating_room(answers=2, anonymous=True)
        self._online(room, *players)
        room.phase = Room.Phase.SUMMARY
        room.save(update_fields=["phase"])
        state = room_state(room.code)
        self.assertEqual(state["players"][0]["score"], 5)
        self.assertEqual([r["prompt"] for r in state["summary"]["rows"]], ["prompt 0", "prompt 1"])

    def test_the_player_row_carries_no_prompt_output_or_comment_in_any_phase(self) -> None:
        # NOT BLANKED IN THE RATING PHASE — ABSENT IN ALL SIX. The judge verdict reaches a player
        # through the `answer` POST response, which is where it was always delivered, and the
        # prompt and output they need to read the round arrive in the `summary` rows. So the three
        # keys are gone rather than nulled here, which is the difference between one branch a
        # client writes once and a branch per phase it writes and then cannot delete.
        phases = (
            Room.Phase.WAITING,
            Room.Phase.QUIZ,
            Room.Phase.PLAYING,
            Room.Phase.RATING,
            Room.Phase.SUMMARY,
            Room.Phase.DONE,
        )
        for i, phase in enumerate(phases):
            with self.subTest(phase=phase):
                room, _players, _rows = self._rating_room(answers=2, code=f"PRM{i}")
                room.phase = phase
                room.save(update_fields=["phase"])
                for p in room_state(room.code)["players"]:
                    self.assertEqual(
                        set(p),
                        {
                            "name", "score", "answered", "round_score", "online",
                            "proximity_total", "quality_total", "quiz_score", "quiz_done",
                        },
                    )

    # --- the snapshot: the summary ----------------------------------------------------------

    def test_the_summary_lifts_anonymity_and_shows_the_breakdown(self) -> None:
        # The four numbers, all of them, on every row: the summary screen's columns are wired and
        # the bound is the lesson, so a player can see a 7 that used to be a 10 and a 7 that used
        # to be a 3 and work out which way the critic and the room disagreed.
        room, players, rows = self._rating_room(answers=2, anonymous=True)
        self._online(room, *players)
        self._rate(room, players[1], 7)  # bob and clea on ana's prompt
        self._rate(room, players[2], 7)
        self._rate(room, players[0], 5)  # ana and clea on bob's
        self._rate(room, players[2], 5)
        state = room_state(room.code)
        self.assertEqual(state["phase"], "summary")
        self.assertEqual(state["summary"]["round"], 0)
        self.assertEqual(len(state["summary"]["rows"]), 2)
        first = state["summary"]["rows"][0]
        self.assertEqual(first["author"], players[0].name)  # the name the rating phase withheld
        # The bound at work, in numbers: two peers at 7, a critic at 6, and the mean is inside the
        # band, so the group's 7 is the quality. A player reading this row learns that their
        # prompt was heard, which is what the feature is for.
        self.assertEqual(
            {k: first[k] for k in ("proximity", "quality_llm", "quality_peers", "quality")},
            {"proximity": 5, "quality_llm": 6, "quality_peers": 7, "quality": 7},
        )
        for key in ("proximity", "quality_llm", "quality_peers", "quality", "round_score", "quality_comment"):
            self.assertIn(key, first)
        # The four judgements are INTEGERS, because the whole scale is: a float among them would
        # be a display bug the plan's constraints forbid, and only the round's note — a mean of
        # two of them — is allowed to carry a half.
        for key in ("proximity", "quality_llm", "quality_peers", "quality"):
            self.assertIsInstance(first[key], int)
        # `(5 + 7) / 2` exactly. This is 6.0, and it says nothing about the floor — what pins that
        # is the odd sums elsewhere in this file. What it does show is that the round's note is a
        # MEAN of the two dimensions and not their sum, a floor of their sum, or a quality of 7.
        self.assertEqual(first["round_score"], (5 + 7) / 2)
        # The critic's REASON, stored beside its score. The fixture built its answers on the ORM
        # with no comment, so it is empty here — the offline case is
        # `test_an_offline_settlement_carries_the_critics_own_words` in the class below. What this
        # pins is that the key EXISTS on the row: `api.ts` types it `string`, and a summary
        # payload that stopped carrying it would break a screen that is coded against it.
        self.assertEqual(first["quality_comment"], "")
        self.assertIsInstance(first["quality_comment"], str)
        # No stray key and none missing: the whole-dict key set is the shape check, and it is
        # the shape a finished UI codes against.
        self.assertEqual(
            set(first),
            {"author", "prompt", "output", "proximity", "quality_llm", "quality_comment", "quality_peers", "quality", "round_score"},
        )

    def test_the_summary_rows_follow_the_order_the_reveal_walked(self) -> None:
        # Same order as the reveal, so the summary is the transcript of what the players just
        # saw. Sorted by score it would not be: a summary that reorders the prompts re-hides the
        # ranking the players just voted through, in a different order.
        room, _players, rows = self._rating_room(answers=3, proximity=4)
        room.phase = Room.Phase.SUMMARY
        room.save(update_fields=["phase"])
        state = room_state(room.code)
        self.assertEqual([r["prompt"] for r in state["summary"]["rows"]], ["prompt 0", "prompt 1", "prompt 2"])
        # Not the reversal either, which is what an order-by would have produced from an unordered
        # queryset and been invisible until the ids stopped being sequential.
        self.assertNotEqual(
            [r["prompt"] for r in state["summary"]["rows"]], [r.prompt for r in reversed(rows)]
        )


@override_settings(ADMIN_PASSWORD="s3cret")
class SettlementBoundTests(TransactionTestCase):
    """`prompts.quality` again, this time through the whole game: the critic's billed call, three
    players' taps, and the write that settles the round.

    `PeerBoundTests` already proved the rule. This proves the rule is APPLIED — that the critic's
    score really is stored on the row, that the peers' mean is the raw one, that the bound is what
    the ranking is built from, and that a settlement is a write and not a computation. Each of
    those can be wrong while the pure function is perfect.
    """

    def setUp(self) -> None:
        self.c = Client()
        self.c.post(
            "/api/admin/login", data=json.dumps({"password": "s3cret"}), content_type="application/json"
        )
        _presence.clear()

    def _played_round(self, critic: int, *, peers: tuple[int, ...] = (10, 10, 10)) -> Answer:
        """A room of four that played and rated one round, end to end over HTTP.

        Ana WRITES (so the critic's score is stored by the real `answer` view, the one that calls
        it), and the other three RATE. The three votes are unanimous in `peers`, because a
        unanimous room is what makes the bound visible: the peers' mean is the number every one of
        them typed, so any difference from it is the band and nothing else.
        """
        code = "BND1"
        room = Room.objects.create(code=code, tutorial=False)
        for order in range(2):
            Round.objects.create(room=room, order=order, target=f"cible {order}")
        author = Player.objects.create(room=room, name="ana")
        raters = [Player.objects.create(room=room, name=n) for n in ("bob", "clea", "dan")]
        room.current = 0
        room.phase = Room.Phase.PLAYING
        room.save(update_fields=["current", "phase"])
        for p in (author, *raters):
            _beat(code, p.pk)
        with (
            mock.patch("game.llm.answer_stream", new=answer_chunks("la reponse")),
            mock.patch("game.llm.score", return_value=Verdict(8, "tres proche")),
            mock.patch("game.llm.grade_prompt", return_value=Verdict(critic, "methode")) as critic_call,
        ):
            r = self.c.post(
                f"/api/rooms/{code}/answer",
                data=json.dumps({"token": author.token, "prompt": "mon prompt"}),
                content_type="application/json",
            )
            drain(r)  # the work runs in a thread: the next line must not race it
        self.assertEqual(r.status_code, 200)
        # The critic was called on the SUBMIT, by the player who paid for it, and on the prompt
        # alone — never inside the settlement, which is why twenty phones do not queue up behind
        # one request.
        self.assertEqual(critic_call.call_count, 1)
        self.c.post(f"/api/admin/rooms/{code}/next")  # playing -> rating
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.RATING)
        for rater, score in zip(raters, peers, strict=True):
            vote = self.c.post(
                f"/api/rooms/{code}/rate",
                data=json.dumps({"token": rater.token, "score": score}),
                content_type="application/json",
            )
            self.assertEqual(vote.status_code, 200, vote.content)
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.SUMMARY)  # one prompt, three raters: settled
        return Answer.objects.get()

    def test_a_unanimous_room_ten_is_capped_three_above_a_critic_four(self) -> None:
        # THE FEATURE, end to end. Twenty players who all love a prompt cannot float it past the
        # critic's score plus three, and the column the ranking is built from says 7.
        answer = self._played_round(critic=4)
        self.assertEqual(answer.quality_llm, 4)
        self.assertEqual(answer.quality_peers, 10)  # the raw mean, before the bound
        self.assertEqual(answer.quality, 7)
        # The round's note is the spec's mean, not a floor of it: (8 + 7) / 2 == 7.5, and an
        # integer column would have awarded 7 and lost half a point of a « sur 100 » game.
        self.assertEqual(answer.round_score, (answer.score + 7) / 2)
        self.assertEqual(answer.round_score, 7.5)

    def test_a_unanimous_room_one_is_capped_three_below_a_critic_eight(self) -> None:
        # The mirror, and the one that protects the other end: a room that hates a prompt cannot
        # bury it, whatever they all agree on.
        answer = self._played_round(critic=8, peers=(1, 1, 1))
        self.assertEqual(answer.quality_llm, 8)
        self.assertEqual(answer.quality_peers, 1)
        self.assertEqual(answer.quality, 5)
        self.assertEqual(answer.round_score, (answer.score + 5) / 2)  # (8 + 5) / 2 == 6.5

    def test_a_unanimous_room_inside_the_band_is_heard_exactly(self) -> None:
        # The third leg, and the one a bound implemented the wrong way round fails: a group that
        # disagrees with the critic by two points is not silenced, and the settled number is the
        # group's, not the critic's and not the average of the two.
        answer = self._played_round(critic=5, peers=(7, 7, 7))
        self.assertEqual(answer.quality, 7)
        self.assertEqual(answer.quality_peers, 7)

    def test_the_summary_reports_the_bound_rather_than_the_raw_scores(self) -> None:
        # The same settlement as the first test, read off the wire this time: the summary is what
        # the players are shown, and a settled column the payload does not carry would be a
        # ranking nobody can see the working of.
        self._played_round(critic=4)
        row = room_state("BND1")["summary"]["rows"][0]
        self.assertEqual(
            {k: row[k] for k in ("proximity", "quality_llm", "quality_peers", "quality", "round_score")},
            {"proximity": 8, "quality_llm": 4, "quality_peers": 10, "quality": 7, "round_score": 7.5},
        )

    def test_a_rating_that_lands_after_the_settlement_never_rewrites_it(self) -> None:
        # WRITTEN, NOT RECOMPUTED — from the side that matters. The settlement is a write, so the
        # summary is a read of the column; a rating that turns up afterwards (a data fix, a shell,
        # a phone that had already sent its vote) leaves the settled round exactly as it was
        # awarded. Recomputing at read time would move a score a player was already told.
        answer = self._played_round(critic=8, peers=(1, 1, 1))
        self.assertEqual(answer.quality, 5)
        stray = Player.objects.create(room=answer.player.room, name="eve")
        Rating.objects.create(answer=answer, rater=stray, score=10)
        answer.refresh_from_db()
        self.assertEqual(answer.quality, 5)
        self.assertEqual(answer.quality_peers, 1)
        self.assertEqual(room_state("BND1")["summary"]["rows"][0]["quality"], 5)

    def test_a_prompt_nobody_rated_settles_on_the_critic_alone(self) -> None:
        # A room of one, or a round where every other phone was asleep: no rating at all. The peer
        # mean is left NULL rather than a division by zero thrown while the last tap of the round
        # is landing, and the critic's score stands alone — the one case where the bound has
        # nothing to hold.
        room = Room.objects.create(code="ALON", tutorial=False)
        rnd = Round.objects.create(room=room, order=0, target="cible")
        player = Player.objects.create(room=room, name="ana")
        room.phase = Room.Phase.RATING
        room.save(update_fields=["phase"])
        answer = rnd.answers.create(player=player, prompt="p", output="o", score=6, quality_llm=7)
        room._go(Room.Phase.SUMMARY)
        settle_round(room)
        answer.refresh_from_db()
        self.assertIsNone(answer.quality_peers)
        self.assertEqual(answer.quality, 7)
        self.assertEqual(answer.round_score, (6 + 7) / 2)

    def test_a_player_who_wrote_nothing_is_not_settled(self) -> None:
        # No row, no round counted. A player who sat out the round keeps the rounds they played
        # and nothing else: `settle_round` walks the round's ANSWERS, so there is nothing here to
        # invent a row out of, and the summary has no line for them either.
        room = Room.objects.create(code="ALON", tutorial=False)
        rnd = Round.objects.create(room=room, order=0, target="cible")
        author = Player.objects.create(room=room, name="ana")
        sat_out = Player.objects.create(room=room, name="bob")
        rnd.answers.create(player=author, prompt="p", output="o", score=6, quality_llm=7)
        settle_round(room)
        self.assertEqual(Answer.objects.filter(player=sat_out).count(), 0)
        self.assertEqual(Answer.objects.filter(round=rnd, round_score__isnull=False).count(), 1)
        # And in the payload: the player who wrote nothing has no round score to read.
        room.phase = Room.Phase.SUMMARY
        room.save(update_fields=["phase"])
        view = next(p for p in room_state(room.code)["players"] if p["name"] == "bob")
        self.assertFalse(view["answered"])
        self.assertIsNone(view["round_score"])

    def test_the_admin_settles_a_round_the_room_gave_up_on(self) -> None:
        # The admin's button, not a rating, has to be able to close a stuck round — and the
        # summary it lands on has to be a real summary. A round whose players walked away mid-reveal
        # would otherwise show a column of nulls, with no way for the admin to tell that apart from
        # a bug and no second way to fix it.
        code = "STCK"
        room = Room.objects.create(code=code, tutorial=False)
        Round.objects.create(room=room, order=0, target="cible")
        Round.objects.create(room=room, order=1, target="cible 1")
        author = Player.objects.create(room=room, name="ana")
        others = [Player.objects.create(room=room, name=n) for n in ("bob", "clea")]
        rnd = room.rounds.get(order=0)
        rnd.answers.create(player=author, prompt="p", output="o", score=4, quality_llm=6)
        for other in others:
            rnd.answers.create(player=other, prompt="q", output="o", score=4, quality_llm=6)
        room.phase = Room.Phase.RATING
        room.save(update_fields=["phase"])
        # Nobody rated anything, so only the admin can move this round. Three taps for three
        # prompts, and the third one settles on the way to the summary.
        url = f"/api/admin/rooms/{code}/next"
        for expected_revealed in (1, 2):
            self.assertEqual(self.c.post(url).status_code, 200)
            room.refresh_from_db()
            self.assertEqual(room.phase, Room.Phase.RATING)
            self.assertEqual(room.rounds.get(order=0).revealed, expected_revealed)
        self.assertEqual(self.c.post(url).status_code, 200)
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.SUMMARY)
        # Settled, not null: the players who left mid-round still get the critic's score they were
        # owed, and the two dimensions are averaged like any other row.
        self.assertEqual({a.round_score for a in rnd.answers.all()}, {(4 + 6) / 2})
        self.assertEqual({a.quality_peers for a in rnd.answers.all()}, {None})
        self.assertEqual(room_state(code)["summary"]["rows"][0]["quality"], 6)

    def test_both_summary_columns_round_an_exact_half_the_same_way(self) -> None:
        # THE SECOND ROUNDING, and the one nobody can see in a number until it is on the wall. The
        # peers' mean of exactly 6.5 was settled by `round()` here while `prompts.quality` — reading
        # the same rows one line below — settled it by `half_up`, so the summary printed
        # « moyenne des pairs 6 » directly above « qualité retenue 7 », under a formula the class
        # is invited to check, and `Game.tsx` tells them to check exactly that. The two columns
        # must be the same rounding of the same votes, or the table contradicts its own footnote.
        #
        # TWO raters, on purpose: a mean of an ODD number of integers is never a half, so the
        # three-vote fixtures in this class could never have caught it however they voted. A tie is
        # only reachable on an even count, which is exactly why no test found this.
        room = Room.objects.create(code="HALF", tutorial=False)
        rnd = Round.objects.create(room=room, order=0, target="cible")
        author = Player.objects.create(room=room, name="ana")
        raters = [Player.objects.create(room=room, name=name) for name in ("bob", "clea")]
        answer = rnd.answers.create(player=author, prompt="p", output="o", score=8, quality_llm=7)
        for rater, vote in zip(raters, (6, 7), strict=True):
            Rating.objects.create(answer=answer, rater=rater, score=vote)

        settle_round(room)
        answer.refresh_from_db()
        # (6 + 7) / 2 == 6.5, and half-up is 7 on both sides of the line.
        self.assertEqual(answer.quality_peers, 7)
        self.assertEqual(answer.quality, 7)
        # Read off the wire as the class reads it, where the two numbers are printed side by side.
        room.phase = Room.Phase.SUMMARY
        room.save(update_fields=["phase"])
        row = room_state(room.code)["summary"]["rows"][0]
        self.assertEqual((row["quality_llm"], row["quality_peers"], row["quality"]), (7, 7, 7))
        # The critic's 7 with a band of ±3 does not move a 6.5, so both columns are 7 and the
        # bound is not what produced the agreement — the rounding is, and this is the tie it has
        # to settle the same way in the function and at the call site.
        self.assertEqual(row["round_score"], (8 + 7) / 2)

    def test_an_offline_settlement_carries_the_critics_own_words(self) -> None:
        # THE REASON THE COMMENT IS KEPT, and the spec rule it satisfies: « Hors ligne : le modèle
        # devient un écho, le jury devient un chevauchement de mots, et l'écran le dit
        # explicitement. » With no key, `llm.grade_prompt` is `_stub_grade`: a count of keyword
        # markers. The comment used to be dropped on the floor on the reasoning that the summary's
        # four numbers told the story — and they do not, because with nothing else on the row the
        # screen has only a bare integer to put under the heading « note du modèle ».
        #
        # The stub's own comment says what it is (« regle locale, pas un vrai jury »), so storing
        # it is what lets the summary tell the class the number is a heuristic rather than a
        # model's opinion. `_has_key` is pinned instead of relying on the environment, the same
        # way `test_llm` does it, so this test cannot bill a real critic on a developer machine
        # that exports a key.
        with mock.patch("game.llm._has_key", return_value=False), mock.patch(
            "game.llm.answer_stream", new=answer_chunks("la reponse")
        ), mock.patch("game.llm.score", return_value=Verdict(8, "tres proche")):
            code = "OFFL"
            room = Room.objects.create(code=code, tutorial=False)
            rnd = Round.objects.create(room=room, order=0, target="cible")
            author = Player.objects.create(room=room, name="ana")
            Player.objects.create(room=room, name="bob")
            room.phase = Room.Phase.PLAYING
            room.save(update_fields=["phase"])
            r = self.c.post(
                f"/api/rooms/{code}/answer",
                data=json.dumps({"token": author.token, "prompt": "Tu es un guide, pour un public, contexte, en 3 lignes"}),
                content_type="application/json",
            )
            drain(r)  # the work runs in a thread: the row below must not race it
            self.assertEqual(r.status_code, 200)
            # Stored on the row, not just in the response: the `answer` response is gone by the
            # time the summary is read, and the summary is where the number is judged.
            answer = Answer.objects.get()
            self.assertEqual(answer.quality_comment, llm.STUB_COMMENT)
            self.assertTrue(answer.quality_comment)
            settle_round(room)
            room.phase = Room.Phase.SUMMARY
            room.save(update_fields=["phase"])
        row = room_state(code)["summary"]["rows"][0]
        self.assertEqual(row["quality_comment"], llm.STUB_COMMENT)
        # The offline heuristic's score is on the row beside its own honesty string, so the two can
        # never be separated on screen. It is still a number, and the game is still playable.
        self.assertIsInstance(row["quality_llm"], int)
        self.assertEqual(row["round_score"], (8 + row["quality"]) / 2)

    def test_the_critics_comment_is_the_one_the_critic_returned(self) -> None:
        # The keyed path, so the string on the row is a real critic's prose and not the stub's
        # fallback: `answer` writes `critique.comment`, not `verdict.comment` and not a constant.
        answer = self._played_round(critic=4)
        self.assertEqual(answer.quality_comment, "methode")
        row = room_state("BND1")["summary"]["rows"][0]
        self.assertEqual(row["quality_comment"], "methode")
        # And it is not the JUDGE's comment: `Verdict(8, "tres proche")` sits on the row too, and
        # the two say different things about the same submit — one about the output, one about the
        # method. Conflating them would put « tres proche » under « note du modèle », and the
        # summary carries no key for the judge's verdict at all.
        self.assertNotIn("comment", row)
        self.assertNotEqual(row["quality_comment"], "tres proche")
