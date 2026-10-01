import json
from unittest.mock import patch

from django.db.models import Sum
from django.http import HttpResponse
from django.test import Client, TestCase

from game.llm import Verdict
from game.models import Answer, Player, Room, Round
from game.views import _beat, _inflight, _presence, room_state, settle_round


class PlayTest(TestCase):
    def setUp(self) -> None:
        self.c = Client()
        self.room = Room.objects.create(code="ABCD", phase=Room.Phase.WAITING)
        Round.objects.create(room=self.room, order=0, target="42")
        Round.objects.create(room=self.room, order=1, target="Nantes")
        # The presence registry is process-local, so a rollback of the test transaction does NOT
        # roll it back: without this, a heartbeat from an earlier test marks a same-named player
        # online in a later one.
        _presence.clear()

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

    def _answer(self, token: str, prompt: str) -> HttpResponse:
        return self.c.post(
            "/api/rooms/ABCD/answer",
            data=json.dumps({"token": token, "prompt": prompt}),
            content_type="application/json",
        )

    def _answered_state(self) -> dict:
        """Room state mid-round: ana answered round 0, bob has not. Bob is the spectator that
        keeps the round open, so ana's answer is still the current round's and stays visible."""
        _, t = self._join("ana")
        self._join("bob")
        self._play()
        with patch("game.llm.answer", return_value="quarante-deux"), patch(
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

    @patch("game.llm.score", return_value=Verdict(8, "tres proche"))
    @patch("game.llm.answer", return_value="quarante-deux")
    def test_answer_scores_and_advances_when_everyone_answered(self, m_answer, m_score) -> None:
        _, ta = self._join("ana")
        _, tb = self._join("bob")
        self._play()
        self._online("ana", "bob")
        r = self._answer(ta, "repete 42")
        self.assertEqual(json.loads(r.content), {"score": 8, "comment": "tres proche", "output": "quarante-deux"})
        self.room.refresh_from_db()
        self.assertEqual((self.room.current, self.room.phase), (0, "playing"))  # bob still pending

        self._answer(tb, "Nantes")
        self.room.refresh_from_db()
        self.assertEqual((self.room.current, self.room.phase), (1, "playing"))

    @patch("game.llm.score", return_value=Verdict(5, "moyen"))
    @patch("game.llm.answer", return_value="x")
    def test_cannot_answer_twice(self, _m_answer, _m_score) -> None:
        _, t = self._join("ana")
        self._join("bob")  # bob never answers, so round 0 stays open and the second submit hits the same round
        self._play()
        self.assertEqual(self._answer(t, "a").status_code, 200)
        self.assertEqual(self._answer(t, "b").status_code, 409)

    def test_answer_refused_with_bad_token(self) -> None:
        self._play()
        self.assertEqual(self._answer("nope", "a").status_code, 403)

    @patch("game.llm.score", return_value=Verdict(5, "moyen"))
    @patch("game.llm.answer", return_value="x")
    def test_answer_refused_when_prompt_is_blank(self, m_answer, m_score) -> None:
        # The client disables the button, but the client is not the boundary. Without this
        # guard a blank submit costs two billed calls and a 0 in the ranking.
        _, t = self._join("ana")
        self._play()
        self.assertEqual(self._answer(t, "   \n ").status_code, 400)
        m_answer.assert_not_called()
        m_score.assert_not_called()
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

    @patch("game.llm.score", return_value=Verdict(8, "tres proche"))
    @patch("game.llm.answer", return_value="quarante-deux")
    def test_concurrent_resubmit_is_refused_before_the_billed_calls(self, m_answer, _m_score) -> None:
        # The duplicate check runs before the ~5s the two model calls take, so a reload in
        # that window used to be billed twice and lose the unique_together race. The claim
        # makes the second copy 409 without touching the model, which is the whole point:
        # `m_answer` being called once is the assertion that matters, not the status code,
        # since the IntegrityError backstop returns 409 too.
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

        with patch("game.llm.score", side_effect=resubmit_while_in_flight):
            first = self._answer(t, "repete 42")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second[0].status_code, 409)
        self.assertEqual(json.loads(second[0].content), {"error": "already answered"})
        self.assertEqual(m_answer.call_count, 1)  # the loser never reached the model
        self.assertEqual(Answer.objects.count(), 1)
        self.assertEqual(len(_inflight), 0)  # claim released

    @patch("game.llm.answer", return_value="quarante-deux")
    def test_duplicate_row_committed_mid_flight_returns_409(self, m_answer) -> None:
        # The durable backstop the process-local claim cannot see: another process wins
        # unique_together between the duplicate check and the create. Without the
        # IntegrityError handling this is a 500, and no test elsewhere would notice.
        _, t = self._join("ana")
        self._join("bob")
        self._play()
        player = Player.objects.get(room=self.room, name="ana")
        rnd = Round.objects.get(room=self.room, order=0)

        def win_the_race(*_args) -> Verdict:
            Answer.objects.create(player=player, round=rnd, prompt="aile", output="42", score=3, comment="bof")
            return Verdict(8, "tres proche")

        with patch("game.llm.score", side_effect=win_the_race):
            r = self._answer(t, "repete 42")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(json.loads(r.content), {"error": "already answered"})
        self.assertEqual(Answer.objects.count(), 1)  # the winner's row, not ours
        self.assertEqual(Answer.objects.get().score, 3)
        self.room.refresh_from_db()
        self.assertEqual((self.room.current, self.room.phase), (0, "playing"))  # not advanced
        self.assertEqual(m_answer.call_count, 1)

    def test_state_shape_and_totals(self) -> None:
        _, t = self._join("ana")
        self._play()
        self._online("ana")
        with patch("game.llm.answer", return_value="42"), patch("game.llm.score", return_value=Verdict(10, "parfait")):
            self._answer(t, "repete 42")
        st = room_state("ABCD")
        # The FULL top-level key set, all ten. This is the assertion that catches a key appearing
        # by accident and a key going missing, which is the whole reason it is a set equality
        # rather than a lookup: the three phase blocks are in every snapshot from here on, and a
        # subset check would be blind to that either way.
        self.assertEqual(
            set(st),
            {
                "code", "phase", "current", "total", "target", "anonymous",
                "players", "online_count", "quiz", "reveal", "summary",
            },
        )
        self.assertEqual((st["code"], st["current"], st["total"], st["target"]), ("ABCD", 1, 2, "Nantes"))
        # And all three phase blocks are present and null in `playing`, which is the other half of
        # the contract: present, not absent.
        self.assertIsNone(st["quiz"])
        self.assertIsNone(st["reveal"])
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
        self.assertEqual(p["score"], 10)
        self.assertFalse(p["answered"])
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
        # 8 + 4 of proximity, and 8 + 4 of quality. NOTE: `score` is the sum of PROXIMITY, not of the
        # settled `round_score` — see the discrepancy this task reports, and `test_score_is_not_the
        # sum_of_round_score` which pins the behaviour as it stands rather than as §7 of the spec
        # describes it. The three numbers still describe the SAME two rounds, which is the property
        # that makes them readable as one row.
        self.assertEqual(view["proximity_total"], 12)
        self.assertEqual(view["quality_total"], 12)
        self.assertEqual(view["score"], 12)  # == proximity_total here, and only because prox == qual
        self.assertTrue(view["answered"])  # the cursor is on round 1, the last one she wrote
        # The two halves are not forced to be equal by the payload: a room that agreed with the
        # critic on one round and not the other still gets two distinct columns, and that gap is
        # the whole lesson. They are equal in this fixture only because the critic's score was set
        # equal to the proximity on purpose.
        self.assertEqual(
            Answer.objects.filter(player=ana).aggregate(s=Sum("score"))["s"],
            Answer.objects.filter(player=ana).aggregate(s=Sum("quality"))["s"],
        )

    def test_score_is_not_the_sum_of_round_score(self) -> None:
        # PINS THE DISCREPANCY, rather than the spec. §7 of the cahier says the final ranking
        # "additionne les dix tours" where a tour is `(proximité + qualité) / 2`, and `models.py`
        # says the same in its comment on `round_score`. `room_state` sums `Answer.score` — the
        # proximity — instead. These two rounds are built so the two totals cannot be confused:
        # 10+10 settles to 10 and 10+2 settles to 6, so the spec's total is 16 and what the payload
        # sends is 20.
        #
        # This is left as a test rather than silently corrected because it is a behaviour change
        # with a visible consequence — the leaderboard would move on every settlement, so the
        # number a player has watched all game would change under them — and that call belongs to
        # whoever owns the final ranking screen. What this pins is the CURRENT number, so whoever
        # changes it has to change this test on purpose.
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
        # 16 is what §7 calls the total, and it is the sum of the settled `round_score`s.
        self.assertEqual(Answer.objects.filter(player=ana).aggregate(s=Sum("round_score"))["s"], 16)
        # What the leaderboard actually sends is 20.
        self.assertEqual(view["score"], 20)

    def test_the_dimension_totals_are_null_before_a_player_is_settled(self) -> None:
        # The other half, and the reason they are `number | null` rather than `number`: a player
        # who has not finished a round yet has no totals, and a silent 0 would read as a real and
        # terrible score rather than as "nothing yet". A `Sum` over no settled rows is NULL, and
        # the final screen prints an em dash until these arrive.
        _, t = self._join("ana")
        self._play()
        with patch("game.llm.answer", return_value="42"), patch("game.llm.score", return_value=Verdict(9, "nickel")):
            self._answer(t, "repete 42")
        ana = room_state("ABCD")["players"][0]
        self.assertEqual(ana["score"], 9)  # the proximity it was just told
        self.assertIsNone(ana["proximity_total"])
        self.assertIsNone(ana["quality_total"])

    def test_state_ranks_by_score_desc(self) -> None:
        _, ta = self._join("ana")
        _, tb = self._join("bob")
        self._play()
        self._online("ana", "bob")
        with patch("game.llm.answer", return_value="42"), patch("game.llm.score", return_value=Verdict(3, "bof")):
            self._answer(ta, "repete 42")
        with patch("game.llm.answer", return_value="42"), patch("game.llm.score", return_value=Verdict(9, "nickel")):
            self._answer(tb, "repete 42")
        self.room.refresh_from_db()
        self.room.current = 0  # keep both answers in the same round for the ranking assertion
        self.room.save()
        self.assertEqual([p["name"] for p in room_state("ABCD")["players"]], ["bob", "ana"])

    def test_state_breaks_a_tie_by_join_order(self) -> None:
        # Names chosen so the two candidate tiebreaks disagree: a Python name sort puts "ana"
        # before "zoe", join order does not. Equal scores, so the join order decides.
        _, ta = self._join("zoe")
        _, tb = self._join("ana")
        self._play()
        self._online("zoe", "ana")
        for t in (ta, tb):
            with patch("game.llm.answer", return_value="42"), patch("game.llm.score", return_value=Verdict(5, "moyen")):
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
                "code", "phase", "current", "total", "target", "anonymous",
                "players", "online_count", "quiz", "reveal", "summary",
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
                # All three present and null, which is the shape every phase carries.
                "quiz": None,
                "reveal": None,
                "summary": None,
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
