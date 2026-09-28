import json
from unittest.mock import patch

from django.http import HttpResponse
from django.test import Client, TestCase

from game.llm import Verdict
from game.models import Player, Room, Round
from game.views import room_state


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

    def test_answer_refused_when_not_playing(self) -> None:
        _, t = self._join("ana")
        self.assertEqual(self._answer(t, "a").status_code, 409)

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
