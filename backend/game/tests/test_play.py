import json
import threading
from unittest.mock import patch

from django.db.models import Sum
from django.http import HttpResponse
from django.test import Client, TransactionTestCase

from game import llm
from game.llm import Verdict
from game.models import Answer, Player, Room, Round
from game.tests.support import answer_chunks, drain, read_frames, wait_for
from game.views import _beat, _inflight, _presence, room_state, settle_round


class PlayTest(TransactionTestCase):
    """The play phase, and the stream the answer comes back on.

    `TransactionTestCase` and not `TestCase`, and the reason is the endpoint's shape rather than
    a preference: the three billed calls now run in a WORKER THREAD, so the row is written on a
    connection of its own. A `TestCase` holds its fixture in an uncommitted transaction on the
    test's connection, which that thread can neither see nor write — every answer would end in
    `database is locked`, and the failure would look like a bug in the endpoint. Real commits are
    what a second connection needs, so they are what the test has to give up.

    Every `/answer` call is therefore read to its end (`drain`): the work outlives the client by
    design, and a test that returned while the thread was still writing would race the flush
    that tears the fixture down.
    """

    def setUp(self) -> None:
        self.c = Client()
        self.room = Room.objects.create(code="ABCD", phase=Room.Phase.WAITING)
        Round.objects.create(room=self.room, order=0, target="42")
        Round.objects.create(room=self.room, order=1, target="Nantes")
        # The presence registry is process-local, so a rollback of the test transaction does NOT
        # roll it back: without this, a heartbeat from an earlier test marks a same-named player
        # online in a later one.
        _presence.clear()
        _inflight.clear()

    def _play(self) -> None:
        self.room.phase = Room.Phase.PLAYING
        self.room.save()

    def _join(self, name: str) -> tuple[HttpResponse, str | None]:
        r = self.c.post(
            "/api/rooms/ABCD/join",
            data=json.dumps({"name": name}),
            content_type="application/json",
        )
        return r, json.loads(r.content).get("token")

    def _online(self, *names: str) -> None:
        """Give each named player a live heartbeat, as their own SSE stream would.

        `maybe_advance_phase` now counts online players, and a test client that never opens a
        stream has no beats, so without this every round would be stuck with online_count 0.
        """
        for name in names:
            _beat("ABCD", Player.objects.get(room=self.room, name=name).pk)

    def _post_answer(self, token: str, prompt: str) -> HttpResponse:
        return self.c.post(
            "/api/rooms/ABCD/answer",
            data=json.dumps({"token": token, "prompt": prompt}),
            content_type="application/json",
        )

    def _answer(self, token: str, prompt: str) -> HttpResponse:
        """One submit, read to the end: the status is all this returns, and the work behind it
        has to have finished before the next line asserts on what it wrote."""
        r = self._post_answer(token, prompt)
        drain(r)
        return r

    def _answer_frames(self, token: str, prompt: str) -> list[tuple[str, dict]]:
        return read_frames(self._post_answer(token, prompt))

    def _answered_state(self) -> dict:
        """Room state mid-round: ana answered round 0, bob has not. Bob is the spectator that
        keeps the round open, so ana's answer is still the current round's and stays visible."""
        _, t = self._join("ana")
        self._join("bob")
        self._play()
        with patch("game.llm.answer_stream", new=answer_chunks("quarante-deux")), patch(
            "game.llm.score", return_value=Verdict(8, "tres proche")
        ):
            self._answer(t, "repete 42")
        return room_state("ABCD")

    def test_join_creates_player(self) -> None:
        r, token = self._join("ana")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Player.objects.filter(room=self.room, name="ana", token=token).exists())

    def test_join_refused_on_malformed_body(self) -> None:
        # Unauthenticated, so a raw json.loads would 500 with a debug traceback for any
        # garbage POST. 400 is the answer a client can act on.
        r = self.c.post("/api/rooms/ABCD/join", data="{not json", content_type="application/json")
        self.assertEqual(r.status_code, 400)
        # Parses, but is not an object: `.get` would blow up on the list.
        r = self.c.post("/api/rooms/ABCD/join", data='"ana"', content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Player.objects.count(), 0)

    def test_join_refused_for_taken_name(self) -> None:
        self._join("ana")
        r, token = self._join("ana")
        self.assertEqual(r.status_code, 409)
        self.assertIsNone(token)

    def test_join_refused_once_game_left_waiting(self) -> None:
        self._play()
        r, token = self._join("bob")
        self.assertEqual(r.status_code, 409)
        self.assertIsNone(token)

    @patch("game.llm.grade_prompt", return_value=Verdict(6, "methode"))
    @patch("game.llm.score", return_value=Verdict(8, "tres proche"))
    @patch("game.llm.answer_stream", new=answer_chunks("quarante-deux"))
    def test_answer_scores_and_advances_when_everyone_answered(self, _m_score, _m_grade) -> None:
        _, ta = self._join("ana")
        _, tb = self._join("bob")
        self._play()
        self._online("ana", "bob")
        # What the old `{"score": …, "comment": …, "output": …}` body carried is the `done` frame
        # and the `chunk` frames before it, so it is asserted where it now lives.
        self.assertEqual(
            self._answer_frames(ta, "repete 42"),
            [
                ("chunk", {"delta": "quarante-deux"}),
                ("scoring", {}),
                ("done", {"score": 8, "comment": "tres proche", "round": 0, "truncated": False}),
            ],
        )
        self.room.refresh_from_db()
        self.assertEqual((self.room.current, self.room.phase), (0, "playing"))  # bob still pending

        self._answer(tb, "Nantes")
        self.room.refresh_from_db()
        # Not `(1, "playing")`. When the last player answers, the round STOPS being written and
        # opens the rating; the next round is not created until the class has rated this one.
        # A player who is told "manche 2" before they have seen a single prompt to rate has
        # skipped the whole point of the game.
        self.assertEqual((self.room.current, self.room.phase), (0, "rating"))

    @patch("game.llm.grade_prompt", return_value=Verdict(7, "prompt correct"))
    @patch("game.llm.score", return_value=Verdict(8, "tres proche"))
    @patch("game.llm.answer_stream", new=answer_chunks("quarante-", "deux"))
    def test_the_frames_arrive_in_order_and_say_what_they_are(self, m_score, _m_grade) -> None:
        """The whole wire contract in one assertion: four frame names, in this order.

        The ORDER is the contract, not a detail. `scoring` exists so the client can say the jury
        is working during two more billed calls, and it is only meaningful before the verdict: a
        `done` frame ahead of the text would leave the client rendering a score over a verdict
        whose text it had not drawn yet. Whole-list equality, so a frame that stops being sent,
        starts being sent twice, or arrives out of order all fail here.
        """
        _, ta = self._join("ana")
        self._play()
        self._online("ana")
        r = self._post_answer(ta, "repete 42")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "text/event-stream")
        self.assertEqual(r["Cache-Control"], "no-cache")
        self.assertEqual(r["X-Accel-Buffering"], "no")
        self.assertEqual(
            read_frames(r),
            [
                ("chunk", {"delta": "quarante-"}),
                ("chunk", {"delta": "deux"}),
                ("scoring", {}),
                ("done", {"score": 8, "comment": "tres proche", "round": 0, "truncated": False}),
            ],
        )
        # The judge scored the text the player just watched, joined from the same deltas: one
        # truth, not a streamed text and a scored text that can differ. And it is that same text
        # the row keeps, so the summary cannot contradict the verdict either.
        m_score.assert_called_once_with("repete 42", "quarante-deux", "42")
        self.assertEqual(Answer.objects.get().output, "quarante-deux")

    @patch("game.llm.score", return_value=Verdict(8, "tres proche"))
    @patch("game.llm.answer_stream", new=answer_chunks("quarante-", "deux", truncated=True))
    def test_a_generation_cut_by_the_token_cap_says_so_in_the_done_frame(self, _m_score) -> None:
        # The text of a truncated answer is indistinguishable from a complete one, so the flag is
        # the only way a player learns their recipe stopped mid-sentence. It rides on `done`
        # because that is the frame the client resolves on, and a missing flag would be read as
        # "false" by every client anyway.
        _, ta = self._join("ana")
        self._play()
        self._online("ana")
        frames = self._answer_frames(ta, "repete 42")
        self.assertEqual(frames[-1], ("done", {"score": 8, "comment": "tres proche", "round": 0, "truncated": True}))

    @patch("game.llm.grade_prompt", return_value=Verdict(6, "methode"))
    @patch("game.llm.score", return_value=Verdict(8, "tres proche"))
    def test_the_no_key_path_streams_the_local_answer_in_pieces(self, _m_grade, _m_score) -> None:
        # The only test that runs the play call UNMOCKED, because with no key `llm.answer_stream`
        # is the local stub — and that fallback is what every classroom without a key will
        # actually run, so the endpoint has to work on it rather than beside it. The frames are
        # the stub's own chunks: a fallback that answered in one lump would fail here, which is
        # the point of the stub streaming at all.
        _, ta = self._join("ana")
        self._play()
        self._online("ana")
        prompt = "repete 42 en trois phrases"
        with patch("game.llm._has_key", return_value=False):
            frames = self._answer_frames(ta, prompt)
        deltas = [payload["delta"] for name, payload in frames if name == "chunk"]
        self.assertEqual("".join(deltas), llm._stub_answer(prompt))
        self.assertEqual([name for name, _ in frames], ["chunk", "chunk", "scoring", "done"])
        self.assertEqual(Answer.objects.get().output, llm._stub_answer(prompt).strip())

    @patch("game.llm.grade_prompt", return_value=Verdict(7, "prompt correct"))
    @patch("game.llm.score", side_effect=RuntimeError("le juge est tombe"))
    @patch("game.llm.answer_stream", new=answer_chunks("quarante-deux"))
    def test_a_failed_jury_streams_an_error_and_saves_nothing(self, _m_score, _m_grade) -> None:
        # A generator cannot `yield` inside an `except`, which is the whole reason the work is
        # not a generator: without a thread the client would see the text and then a closed
        # stream, and could not tell a crash from a phone that went to sleep.
        _, ta = self._join("ana")
        self._play()
        self._online("ana")
        player = Player.objects.get(room=self.room, name="ana")
        with self.assertLogs("game.views", level="ERROR"):
            frames = self._answer_frames(ta, "repete 42")
        self.assertEqual(frames[-1], ("error", {"error": "La partie a planté côté serveur. Préviens l'animateur."}))
        # `scoring` IS sent before the jury runs, so the crash arrives after it: the client has to
        # be able to hear "the jury is working" and then hear it failed, and the whole frame list
        # is pinned so neither of those two frames can quietly stop being sent.
        self.assertEqual([name for name, _ in frames], ["chunk", "scoring", "error"])
        # Nothing persisted: the player is not credited with an answer nobody judged, and the
        # round must not close on it. Their composer stays usable on the client precisely because
        # the row does not exist.
        self.assertEqual(Answer.objects.count(), 0)
        self.room.refresh_from_db()
        self.assertEqual((self.room.current, self.room.phase), (0, "playing"))
        # And the claim is released, or the player is locked out of a round they may still answer.
        self.assertNotIn((Round.objects.get(room=self.room, order=0).pk, player.pk), _inflight)

    @patch("game.llm.grade_prompt", return_value=Verdict(7, "prompt correct"))
    def test_a_client_that_walks_away_does_not_cancel_the_answer(self, _m_grade) -> None:
        """A phone that locks its screen mid-generation, which is the reason for the thread.

        The double blocks on the second delta, so the stream is closed while the work is provably
        mid-flight: had the calls run inline in the SSE generator — the obvious way to write this
        — `close()` would have stopped them there, nothing would have been stored, and the round
        would wait for a player who is asleep. Without this test the thread is an assertion in a
        comment rather than a design.
        """
        gate = threading.Event()

        class StallsAfterTheFirstDelta:
            """One delta, then nothing until the gate opens — a phone that locks its screen the
            moment the text starts arriving, which is the moment this whole design is about.

            Carries `truncated` because that is part of what `llm.answer_stream` returns: a
            double without it would fail on the `done` frame for a reason that has nothing to do
            with what this test is about.
            """

            truncated = False

            def __iter__(self):
                yield "quarante-"
                gate.wait(5.0)
                yield "deux"

        _, ta = self._join("ana")
        self._play()
        self._online("ana")
        player = Player.objects.get(room=self.room, name="ana")
        with patch("game.llm.answer_stream", new=lambda *_a, **_kw: StallsAfterTheFirstDelta()), patch(
            "game.llm.score", return_value=Verdict(8, "tres proche")
        ):
            r = self._post_answer(ta, "repete 42")
            self.assertIn(b"event: chunk", next(r.streaming_content))  # the phone is watching here
            # `response.close()` is the close the WSGI server itself calls when the client goes
            # away, and it is what raises GeneratorExit inside the SSE generator.
            r.close()  # the phone locks its screen here, before the jury has been called
            gate.set()  # the second delta, from a client nobody is there to receive
            # The work is finished when the claim is released: it is released in the `finally`
            # that runs after the write, so waiting on the claim is waiting on the write.
            claim = (Round.objects.get(room=self.room, order=0).pk, player.pk)
            self.assertTrue(wait_for(lambda: claim not in _inflight), "the work thread never finished")
        answer = Answer.objects.get(player=player)
        self.assertEqual((answer.output, answer.score), ("quarante-deux", 8))
        self.room.refresh_from_db()
        # The phase moved too, not just the row: the work ran to its end after the client was
        # gone, jury and critic included. `rating` because ana is the only player and the round
        # closes on the players who are there.
        self.assertEqual((self.room.current, self.room.phase), (0, "rating"))

    @patch("game.llm.score", return_value=Verdict(5, "moyen"))
    @patch("game.llm.answer_stream", new=answer_chunks("x"))
    def test_cannot_answer_twice(self, _m_score) -> None:
        _, t = self._join("ana")
        self._join("bob")  # bob never answers, so round 0 stays open and the second submit hits the same round
        self._play()
        self.assertEqual(self._answer(t, "a").status_code, 200)
        # The SECOND one is the refusal under test, and it is still JSON with a 409: decided
        # before the stream starts, which is the only way a client can read a status it has not
        # spent yet.
        second = self._answer(t, "b")
        self.assertEqual(second.status_code, 409)
        self.assertEqual(json.loads(second.content), {"error": "already answered"})

    def test_answer_refused_with_bad_token(self) -> None:
        self._play()
        self.assertEqual(self._answer("nope", "a").status_code, 403)

    def test_answer_refused_when_prompt_is_blank(self) -> None:
        # The client disables the button, but the client is not the boundary. Without this
        # guard a blank submit costs two billed calls and a 0 in the ranking.
        play = answer_chunks("x")
        _, t = self._join("ana")
        self._play()
        with patch("game.llm.answer_stream", new=play), patch("game.llm.score", return_value=Verdict(5, "moyen")):
            r = self._answer(t, "   \n ")
        self.assertEqual(r.status_code, 400)
        play.assert_not_called()
        self.assertEqual(Answer.objects.count(), 0)

    def test_token_is_scoped_to_its_room(self) -> None:
        # The lookup filters on `room` as well as `token`; dropping `room=room` lets a room-A
        # player submit into room B under a token the other room's state never shows.
        _, t = self._join("ana")
        other = Room.objects.create(code="WXYZ", phase=Room.Phase.PLAYING)
        Round.objects.create(room=other, order=0, target="42")
        r = self.c.post(
            "/api/rooms/WXYZ/answer",
            data=json.dumps({"token": t, "prompt": "repete 42"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(Answer.objects.count(), 0)

    def test_answer_refused_when_not_playing(self) -> None:
        _, t = self._join("ana")
        self.assertEqual(self._answer(t, "a").status_code, 409)

    def test_concurrent_resubmit_is_refused_before_the_billed_calls(self) -> None:
        # The duplicate check runs before the ~5s the two model calls take, so a reload in
        # that window used to be billed twice and lose the unique_together race. The claim
        # makes the second copy 409 without touching the model, which is the whole point:
        # `play` being called once is the assertion that matters, not the status code,
        # since the IntegrityError backstop reports the same way now.
        play = answer_chunks("quarante-deux")
        _, t = self._join("ana")
        self._join("bob")  # bob never answers, so round 0 stays open for the reentrant submit
        self._play()
        second: list[HttpResponse] = []
        fired = False

        def resubmit_while_in_flight(*_args) -> Verdict:
            nonlocal fired
            if not fired:
                # Set before the nested call, not after: `second.append(self._answer(...))`
                # evaluates the request first, so guarding on `second` recurses forever.
                fired = True
                second.append(self._answer(t, "meme reponse"))
            return Verdict(8, "tres proche")

        with patch("game.llm.answer_stream", new=play), patch("game.llm.score", side_effect=resubmit_while_in_flight):
            first = self._answer(t, "repete 42")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second[0].status_code, 409)
        self.assertEqual(json.loads(second[0].content), {"error": "already answered"})
        self.assertEqual(play.call_count, 1)  # the loser never reached the model
        self.assertEqual(Answer.objects.count(), 1)
        self.assertEqual(len(_inflight), 0)  # claim released

    def test_a_row_committed_mid_flight_is_reported_in_band(self) -> None:
        # The durable backstop the process-local claim cannot see: another process wins
        # unique_together between the duplicate check and the create. Without the
        # IntegrityError handling this is an unhandled error in a thread, and no test elsewhere
        # would notice. It can no longer be a 409 — the status was spent with the first chunk —
        # so the same refused answer reaches the player as the error frame.
        play = answer_chunks("quarante-deux")
        _, t = self._join("ana")
        self._join("bob")
        self._play()
        player = Player.objects.get(room=self.room, name="ana")
        rnd = Round.objects.get(room=self.room, order=0)

        def win_the_race(*_args) -> Verdict:
            Answer.objects.create(player=player, round=rnd, prompt="aile", output="42", score=3, comment="bof")
            return Verdict(8, "tres proche")

        with patch("game.llm.answer_stream", new=play), patch("game.llm.score", side_effect=win_the_race):
            with self.assertLogs("game.views", level="ERROR"):
                frames = self._answer_frames(t, "repete 42")
        self.assertEqual(frames[-1][0], "error")
        self.assertEqual(Answer.objects.count(), 1)  # the winner's row, not ours
        self.assertEqual(Answer.objects.get().score, 3)
        self.room.refresh_from_db()
        self.assertEqual((self.room.current, self.room.phase), (0, "playing"))  # not advanced
        self.assertEqual(play.call_count, 1)

    def test_state_shape_and_totals(self) -> None:
        _, t = self._join("ana")
        self._play()
        self._online("ana")
        with patch("game.llm.answer_stream", new=answer_chunks("42")), patch("game.llm.score", return_value=Verdict(10, "parfait")):
            self._answer(t, "repete 42")
        st = room_state("ABCD")
        # The FULL top-level key set, all eleven. This is the assertion that catches a key
        # appearing by accident and a key going missing, which is the whole reason it is a set
        # equality rather than a lookup: the four phase blocks are in every snapshot from here on,
        # and a subset check would be blind to that either way. `ready` joined them when the class
        # got its own advance, on the same unconditional-key rule as the three before it.
        self.assertEqual(
            set(st),
            {
                "code", "phase", "current", "total", "target", "target_image", "timer", "anonymous",
                "players", "online_count", "quiz", "reveal", "summary", "ready",
            },
        )
        # Still round 0, on its own target. The last player's answer opened the RATING, it did not
        # advance to round 1 — which is why `phase` below is `rating` and every block is null.
        self.assertEqual((st["code"], st["current"], st["total"], st["target"]), ("ABCD", 0, 2, "42"))
        # The didacticiel block is null outside the quiz, and so is the summary. `reveal` is NOT
        # null: the room is in the rating phase, so the prompt on screen is exactly what a client
        # must find there. A test that asserted all three null would have been asserting the shape
        # of a `playing` room while the fixture sat in `rating`.
        self.assertIsNone(st["quiz"])
        self.assertIsNone(st["summary"])
        # The reveal itself: one prompt to rate, the first player in join order, and no votes yet.
        # `author` is None under the room's default anonymity while `author_label` is always a
        # display string — a screen must never have to decide whether it has a label to draw.
        self.assertEqual(
            st["reveal"],
            {"index": 0, "count": 1, "prompt": "repete 42", "author": None,
             "author_label": "Joueur 1", "rated": 0},
        )
        self.assertIsNone(st["summary"])
        p = st["players"][0]
        # Likewise the FULL player row: nine keys, and no `prompt`, `output` or `comment`. Those
        # three are gone in EVERY phase rather than blanked in the rating one, so a screen coding
        # against this type finds exactly what the frozen `Player` promises.
        self.assertEqual(
            set(p),
            {
                "name", "score", "answered", "round_score", "online",
                "proximity_total", "quality_total", "quiz_score", "quiz_done",
            },
        )
        # `None`, not a number: the room is anonymous and in the rating phase, so the score is
        # blanked on purpose. A player who reads their own score, sees it missing from the column,
        # and works out which revealed line is theirs has defeated the anonymity the feature
        # exists to provide. The name survives — the roster is not a leak.
        self.assertIsNone(p["score"])
        # `answered` still True: the player DID answer round 0, and the mask is on the score, not
        # on the fact of having answered. Blanking it would tell a returning player they never
        # played a round they did play.
        self.assertTrue(p["answered"])
        # The room was created without a bank in this fixture, so `quiz_done` is False rather than
        # vacuously True: `count() == 0` is true of every player in an unmaterialised bank.
        self.assertFalse(p["quiz_done"])

    def test_state_pins_every_field_of_an_answered_player(self) -> None:
        st = self._answered_state()
        self.assertEqual((st["current"], st["target"]), (0, "42"))
        ana = next(p for p in st["players"] if p["name"] == "ana")
        # Whole-dict equality, so a renamed, dropped, nulled or ADDED key all fail here. This is
        # the assertion that would have caught `prompt`/`output`/`comment` outliving the contract,
        # so it stays a whole dict and is never narrowed to the keys it happens to mention.
        self.assertEqual(
            ana,
            {
                "name": "ana",
                "score": 8,
                "answered": True,
                "round_score": 8,
                "proximity_total": None,  # her round is not settled yet, so there is no total
                "quality_total": None,
                "quiz_score": 0,
                "quiz_done": False,  # this room has no bank, so nothing to be done
                "online": False,  # no stream in this test, so no heartbeat
            },
        )

    def test_state_gives_an_unanswered_player_int_zero(self) -> None:
        st = self._answered_state()
        bob = next(p for p in st["players"] if p["name"] == "bob")
        # `or 0` in room_state is what makes this an int. Task 5 types score as `number`,
        # so a null here is a type error in the client, not a cosmetic null.
        self.assertIsInstance(bob["score"], int)
        self.assertEqual(bob["score"], 0)
        # He wrote nothing in this round, so there is no round score to read — and no dimension
        # totals either, which are summed over SETTLED rounds only.
        self.assertIsNone(bob["round_score"])
        self.assertIsNone(bob["proximity_total"])
        self.assertIsNone(bob["quality_total"])
        # `quiz_score` is an int even for a player who never opened the didacticiel, so the
        # client formats "0 sur 8" rather than carrying a null branch.
        self.assertIsInstance(bob["quiz_score"], int)
        self.assertEqual(bob["quiz_score"], 0)

    def test_the_dimension_totals_are_sums_over_settled_rounds(self) -> None:
        # THE TWO NUMBERS THE FINAL SCREEN IS WAITING FOR. The final ranking is a single total,
        # and it is the columns beside it that make the total mean something: a player who ends on
        # "proximité 42, qualité 58, total 50" learns that their prompts WORK and are badly BUILT.
        # That lesson is invisible in the total alone, which reads like a grade — so the two halves
        # travel in the payload rather than being left to a client that cannot derive them.
        ana = Player.objects.create(room=self.room, name="ana")
        Player.objects.create(room=self.room, name="bob")  # bob never answers, so every round stays his to write
        self._play()
        # Written on the ORM and settled one round at a time, because the subject here is the SUM
        # `room_state` reads and not the three admin clicks that would get here over HTTP.
        for order, proximity in ((0, 8), (1, 4)):
            rnd = Round.objects.get(room=self.room, order=order)
            rnd.answers.create(player=ana, prompt="p", output="o", score=proximity, quality_llm=proximity)
            self.room.current = order
            self.room.save(update_fields=["current"])
            settle_round(self.room)  # nobody rated, so `quality` stands on the critic's own score
        view = next(p for p in room_state("ABCD")["players"] if p["name"] == "ana")
        # `score` is the sum of `round_score` over the settled rounds, each of which is
        # `(proximité + qualité) / 2` exactly: (8 + 8) / 2 == 8 and (4 + 4) / 2 == 4, so 12 here.
        # The three numbers still describe the SAME two rounds, which is the property that makes
        # them readable as one row.
        self.assertEqual(view["proximity_total"], 12)
        self.assertEqual(view["quality_total"], 12)
        self.assertEqual(view["score"], (8 + 8) / 2 + (4 + 4) / 2)
        self.assertTrue(view["answered"])  # the cursor is on round 1, the last one she wrote
        # The two halves are not forced to be equal by the payload: a room that agreed with the
        # critic on one round and not the other still gets two distinct columns, and that gap is
        # the whole lesson. They are equal in this fixture only because the critic's score was set
        # equal to the proximity on purpose.
        self.assertEqual(
            Answer.objects.filter(player=ana).aggregate(s=Sum("score"))["s"],
            Answer.objects.filter(player=ana).aggregate(s=Sum("quality"))["s"],
        )

    def test_score_is_the_sum_of_the_settled_round_scores(self) -> None:
        # The ranking sums the SETTLED `round_score`, which is `(proximité + qualité) / 2` — §7 of
        # the cahier says the final ranking "additionne les dix tours", and `models.py` says the
        # same in its comment on `round_score`. It does NOT sum `Answer.score`, which is the
        # judge's proximity on the output alone.
        #
        # The two totals are built so they cannot be confused: a round of proximity 10 with a
        # quality of 10 settles to 10, and a round of proximity 10 with a quality of 2 settles to
        # 6. The spec's total is therefore 16, while the proximities alone add up to 20. A player
        # whose prompts are badly written but whose output happens to land would otherwise tie
        # with a player who writes carefully — in a game whose only question is whether the prompt
        # was any good.
        ana = Player.objects.create(room=self.room, name="ana")
        self._play()
        for order, (proximity, quality_llm) in enumerate(((10, 10), (10, 2))):
            rnd = Round.objects.get(room=self.room, order=order)
            rnd.answers.create(player=ana, prompt="p", output="o", score=proximity, quality_llm=quality_llm)
            self.room.current = order
            self.room.save(update_fields=["current"])
            settle_round(self.room)
        view = next(p for p in room_state("ABCD")["players"] if p["name"] == "ana")
        # Nobody rated, so each round's `quality` stands on the critic's own score: 10 and 2.
        self.assertEqual(view["proximity_total"], 20)
        self.assertEqual(view["quality_total"], 12)
        # The two dimensions are visible next to the total, and the total is not their sum. The
        # spec's own arithmetic, not a floor of it: (10 + 10) / 2 == 10 and (10 + 2) / 2 == 6.
        spec_total = (10 + 10) / 2 + (10 + 2) / 2
        self.assertEqual(Answer.objects.filter(player=ana).aggregate(s=Sum("round_score"))["s"], spec_total)
        self.assertEqual(view["score"], spec_total)

    def test_an_unsettled_round_counts_provisionally_then_gets_corrected(self) -> None:
        # `round_score` is NULL until the class has rated the round, and a `Sum` skips NULLs. So
        # if the total were settled-rounds-only, the board would sit frozen through the whole
        # writing phase — and the spec asks for the opposite: the ranking is "mis à jour en direct
        # pendant la rédaction". Therefore an answered-but-unsettled round contributes its
        # PROXIMITY, provisionally, and is corrected when the round settles.
        #
        # The correction is the teaching moment, and it can go DOWN: a player whose prompt worked
        # but was badly written sees their total drop at the summary. That is the price of a weak
        # prompt made visible, which is the entire point of rating prompts.
        ana = Player.objects.create(room=self.room, name="ana")
        self._play()
        rnd = Round.objects.get(room=self.room, order=0)
        rnd.answers.create(player=ana, prompt="p", output="o", score=9, quality_llm=2)
        self.assertIsNone(rnd.answers.get(player=ana).round_score)

        # Provisional: the proximity alone, while the round is still open.
        view = next(p for p in room_state("ABCD")["players"] if p["name"] == "ana")
        self.assertEqual(view["score"], 9)
        # The dimension breakdown stays NULL until settlement — a silent 0 would read as a real
        # and terrible score rather than as "nothing decided yet", and the final screen prints an
        # em dash until these arrive.
        self.assertIsNone(view["proximity_total"])
        self.assertIsNone(view["quality_total"])

        # Settled: (9 + 2) / 2 = 5.5, so the total moves DOWN by 3.5 once the class has rated.
        # The half is the spec's arithmetic and not a rounding decision: `note_tour` is a mean, and
        # a floor here would quietly take half a point off every odd round of a game that is still
        # presented as « sur 100 ».
        settle_round(self.room)
        view = next(p for p in room_state("ABCD")["players"] if p["name"] == "ana")
        self.assertEqual(view["score"], 5.5)
        self.assertEqual(view["proximity_total"], 9)
        self.assertEqual(view["quality_total"], 2)

    def test_the_dimension_totals_are_null_before_a_player_is_settled(self) -> None:
        # The other half, and the reason they are `number | null` rather than `number`: a player
        # who has not finished a round yet has no totals, and a silent 0 would read as a real and
        # terrible score rather than as "nothing yet". A `Sum` over no settled rows is NULL, and
        # the final screen prints an em dash until these arrive.
        _, t = self._join("ana")
        self._play()
        with patch("game.llm.answer_stream", new=answer_chunks("42")), patch("game.llm.score", return_value=Verdict(9, "nickel")):
            self._answer(t, "repete 42")
        ana = room_state("ABCD")["players"][0]
        self.assertEqual(ana["score"], 9)  # the proximity it was just told
        self.assertIsNone(ana["proximity_total"])
        self.assertIsNone(ana["quality_total"])

    def test_state_ranks_by_score_desc(self) -> None:
        self._join("ana")
        self._join("bob")
        self._play()
        self._online("ana", "bob")
        # Deliberately NOT through `answer`: with both of them connected, the second submit
        # closes the writing phase and opens the rating, and the rating blanks every score by
        # design — at which point the board shows join order and there is nothing to rank. This
        # test is about the SORT, so it needs scores that exist, which means settled rounds.
        rnd = Round.objects.get(room=self.room, order=0)
        # `_join` hands back a token; the row is what `Answer` needs.
        ana = Player.objects.get(room=self.room, name="ana")
        bob = Player.objects.get(room=self.room, name="bob")
        rnd.answers.create(player=ana, prompt="p", output="o", score=3, quality_llm=3)
        rnd.answers.create(player=bob, prompt="p", output="o", score=9, quality_llm=9)
        settle_round(self.room)
        self.room.refresh_from_db()
        # The score is now the fused (proximité + qualité) / 2 of the settled round, so bob's 9
        # and ana's 3 are what the sort reads — and the rating phase has passed, so they are
        # visible again.
        self.assertEqual([p["name"] for p in room_state("ABCD")["players"]], ["bob", "ana"])

    def test_state_breaks_a_tie_by_join_order(self) -> None:
        # Names chosen so the two candidate tiebreaks disagree: a Python name sort puts "ana"
        # before "zoe", join order does not. Equal scores, so the join order decides.
        _, ta = self._join("zoe")
        _, tb = self._join("ana")
        self._play()
        self._online("zoe", "ana")
        for t in (ta, tb):
            with patch("game.llm.answer_stream", new=answer_chunks("42")), patch("game.llm.score", return_value=Verdict(5, "moyen")):
                self._answer(t, "repete 42")
        self.room.refresh_from_db()
        self.room.current = 0  # keep both answers in the same round for the ranking assertion
        self.room.save()
        self.assertEqual([p["name"] for p in room_state("ABCD")["players"]], ["zoe", "ana"])

    def test_events_endpoint_streams_json(self) -> None:
        self._answered_state()  # non-empty players list, so the frame's per-player keys are exercised
        r = self.c.get("/api/rooms/ABCD/events")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "text/event-stream")
        self.assertEqual(r["Cache-Control"], "no-cache")
        self.assertEqual(r["X-Accel-Buffering"], "no")
        # Exactly one frame, never two. The first is produced before the generator reaches
        # time.sleep, so this returns immediately; a second next() would block for 0.5s.
        frame = next(r.streaming_content)
        self.assertTrue(frame.startswith(b"data: "), frame)
        # EventSource only dispatches onmessage after a blank line, so the terminator is the
        # endpoint's whole reason to work. Never parse this frame through strip().
        self.assertTrue(frame.endswith(b"\n\n"), frame)
        payload = json.loads(frame[len(b"data: ") : -2])
        # The key set is the one from `test_state_shape_and_totals`, and the WHOLE payload is
        # pinned below it. Both are here because this is the bytes the phones actually parse: a
        # key that never reaches the wire is invisible to every other test in the suite.
        self.assertEqual(
            set(payload),
            {
                "code", "phase", "current", "total", "target", "target_image", "timer", "anonymous",
                "players", "online_count", "quiz", "reveal", "summary", "ready",
            },
        )
        self.assertEqual(
            payload,
            {
                "code": "ABCD",
                "phase": "playing",
                "current": 0,
                "total": 2,
                "target": "42",
                "target_image": None,
                "timer": None,
                "anonymous": True,  # the `Room` default, read through the snapshot
                "players": [
                    {
                        "name": "ana",
                        "score": 8,
                        "answered": True,
                        "round_score": 8,
                        "proximity_total": None,
                        "quality_total": None,
                        "quiz_score": 0,
                        "quiz_done": False,
                        "online": False,
                    },
                    {
                        "name": "bob",
                        "score": 0,
                        "answered": False,
                        "round_score": None,
                        "proximity_total": None,
                        "quality_total": None,
                        "quiz_score": 0,
                        "quiz_done": False,
                        "online": False,
                    },
                ],
                "online_count": 0,
                # All four present and null, which is the shape every phase carries. `ready` is
                # null twice over in this one: `playing` is not a discussion, and this stream
                # carries no token, so there is no viewer whose `mine` it could be.
                "quiz": None,
                "reveal": None,
                "summary": None,
                "ready": None,
            },
        )
        r.close()

    def test_anonymous_is_read_from_the_room_not_inferred(self) -> None:
        # The flag is FIXED for the whole game, so a screen reads it once instead of inferring
        # anonymity from whether `reveal.author` happens to be null — which is indistinguishable
        # from a round with no author. Pinned on both values because only the `True` half of a
        # boolean is otherwise tested.
        for flag in (True, False):
            with self.subTest(anonymous=flag):
                Room.objects.filter(code="ABCD").update(anonymous=flag)
                self.assertIs(room_state("ABCD")["anonymous"], flag)
