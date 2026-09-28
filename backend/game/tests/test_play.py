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
        r = self.c.get("/api/rooms/ABCD/events")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "text/event-stream")
        first = next(r.streaming_content)
        payload = json.loads(first.decode().removeprefix("data: ").strip())
        self.assertEqual(payload["code"], "ABCD")
        r.close()
