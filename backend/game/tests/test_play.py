import json
from unittest.mock import patch

from django.http import HttpResponse
from django.test import Client, TestCase

from game.llm import Verdict
from game.models import Answer, Player, Room, Round
from game.views import _inflight, room_state


class PlayTest(TestCase):
    def setUp(self) -> None:
        self.c = Client()
        self.room = Room.objects.create(code="ABCD", status=Room.Status.WAITING)
        Round.objects.create(room=self.room, order=0, target="42")
        Round.objects.create(room=self.room, order=1, target="Nantes")

    def _play(self) -> None:
        self.room.status = Room.Status.PLAYING
        self.room.save()

    def _join(self, name: str) -> tuple[HttpResponse, str | None]:
        r = self.c.post(
            "/api/rooms/ABCD/join",
            data=json.dumps({"name": name}),
            content_type="application/json",
        )
        return r, json.loads(r.content).get("token")

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
        r = self._answer(ta, "repete 42")
        self.assertEqual(json.loads(r.content), {"score": 8, "comment": "tres proche", "output": "quarante-deux"})
        self.room.refresh_from_db()
        self.assertEqual((self.room.current, self.room.status), (0, "playing"))  # bob still pending

        self._answer(tb, "Nantes")
        self.room.refresh_from_db()
        self.assertEqual((self.room.current, self.room.status), (1, "playing"))

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
        other = Room.objects.create(code="WXYZ", status=Room.Status.PLAYING)
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
        self.assertEqual((self.room.current, self.room.status), (0, "playing"))  # not advanced
        self.assertEqual(m_answer.call_count, 1)

    def test_state_shape_and_totals(self) -> None:
        _, t = self._join("ana")
        self._play()
        with patch("game.llm.answer", return_value="42"), patch("game.llm.score", return_value=Verdict(10, "parfait")):
            self._answer(t, "repete 42")
        st = room_state("ABCD")
        self.assertEqual(set(st), {"code", "status", "current", "total", "target", "players"})
        self.assertEqual((st["code"], st["current"], st["total"], st["target"]), ("ABCD", 1, 2, "Nantes"))
        p = st["players"][0]
        self.assertEqual(set(p), {"name", "score", "answered", "prompt", "output", "comment", "round_score"})
        self.assertEqual(p["score"], 10)
        self.assertIsNone(p["prompt"])
        self.assertFalse(p["answered"])

    def test_state_pins_every_field_of_an_answered_player(self) -> None:
        st = self._answered_state()
        self.assertEqual((st["current"], st["target"]), (0, "42"))
        ana = next(p for p in st["players"] if p["name"] == "ana")
        # Whole-dict equality, so a renamed, dropped or nulled key all fail here.
        self.assertEqual(
            ana,
            {
                "name": "ana",
                "score": 8,
                "answered": True,
                "prompt": "repete 42",
                "output": "quarante-deux",
                "comment": "tres proche",
                "round_score": 8,
            },
        )

    def test_state_gives_an_unanswered_player_int_zero(self) -> None:
        st = self._answered_state()
        bob = next(p for p in st["players"] if p["name"] == "bob")
        # `or 0` in room_state is what makes this an int. Task 5 types score as `number`,
        # so a null here is a type error in the client, not a cosmetic null.
        self.assertIsInstance(bob["score"], int)
        self.assertEqual(bob["score"], 0)
        self.assertEqual(
            {k: bob[k] for k in ("prompt", "output", "comment", "round_score")},
            {"prompt": None, "output": None, "comment": None, "round_score": None},
        )

    def test_state_ranks_by_score_desc(self) -> None:
        _, ta = self._join("ana")
        _, tb = self._join("bob")
        self._play()
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
        self.assertEqual(set(payload), {"code", "status", "current", "total", "target", "players"})
        self.assertEqual(
            payload,
            {
                "code": "ABCD",
                "status": "playing",
                "current": 0,
                "total": 2,
                "target": "42",
                "players": [
                    {
                        "name": "ana",
                        "score": 8,
                        "answered": True,
                        "prompt": "repete 42",
                        "output": "quarante-deux",
                        "comment": "tres proche",
                        "round_score": 8,
                    },
                    {
                        "name": "bob",
                        "score": 0,
                        "answered": False,
                        "prompt": None,
                        "output": None,
                        "comment": None,
                        "round_score": None,
                    },
                ],
            },
        )
        r.close()
