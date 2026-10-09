"""The two time limits: writing a prompt (60 s by default) and rating one (20 s by default).

The admin sets both when creating the room. When one runs out, `expire_phase` moves the room on:
the writing phase opens the rating, and a prompt being rated gives way to the next one.
"""

import json
import time
from datetime import timedelta
from unittest import mock

from django.test import TransactionTestCase
from django.utils import timezone

from game.llm import Verdict
from game.models import RATE_SECONDS, ROUND_SECONDS, Player, Room
from game.tests.support import answer_chunks, drain
from game.views import _inflight, _presence, _presence_lock, expire_phase, room_state


class TimerTests(TransactionTestCase):
    def setUp(self) -> None:
        _presence.clear()

    def _post(self, url: str, data: dict | None = None):
        return self.client.post(url, data=json.dumps(data or {}), content_type="application/json")

    def _create(self, **limits) -> Room:
        self._post("/api/admin/login", {"password": "genai"})
        code = self._post("/api/admin/rooms", {"tutorial": False, **limits}).json()["code"]
        return Room.objects.get(code=code)

    def _game(self, players: int = 3, **limits) -> tuple[Room, list[Player]]:
        """A room without the didacticiel, `players` joined and online, game started: `playing`."""
        room = self._create(**limits)
        joined = []
        for i in range(players):
            token = self._post(f"/api/rooms/{room.code}/join", {"name": f"p{i}"}).json()["token"]
            joined.append(Player.objects.get(token=token))
        with _presence_lock:
            for p in joined:
                _presence[(room.code, p.pk)] = time.monotonic()
        self._post(f"/api/admin/rooms/{room.code}/start")
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.PLAYING)
        return room, joined

    def _answer(self, room: Room, player: Player):
        with mock.patch("game.llm.answer_stream", new=answer_chunks("la reponse")), mock.patch(
            "game.llm.score", return_value=Verdict(8, "proche")
        ), mock.patch("game.llm.grade_prompt", return_value=Verdict(6, "methode")):
            r = self._post(f"/api/rooms/{room.code}/answer", {"token": player.token, "prompt": "un prompt"})
            drain(r)
        return r

    def _run_out(self, room: Room) -> None:
        """The clock of the current phase, already past."""
        Room.objects.filter(code=room.code).update(phase_deadline=timezone.now() - timedelta(seconds=1))
        expire_phase(room.code)
        room.refresh_from_db()

    def _seconds_left(self, room: Room) -> float:
        return (room.phase_deadline - timezone.now()).total_seconds()

    def test_the_defaults_are_sixty_and_twenty_seconds(self):
        room = self._create()
        self.assertEqual((room.round_seconds, room.rate_seconds), (ROUND_SECONDS, RATE_SECONDS))
        self.assertEqual((ROUND_SECONDS, RATE_SECONDS), (60, 20))

    def test_the_admin_sets_both_limits_and_they_are_bounded(self):
        room = self._create(round_seconds=90, rate_seconds=15)
        self.assertEqual((room.round_seconds, room.rate_seconds), (90, 15))
        room = self._create(round_seconds=1, rate_seconds=100000)
        self.assertEqual((room.round_seconds, room.rate_seconds), (10, 300))
        room = self._create(round_seconds="abc", rate_seconds=None)
        self.assertEqual((room.round_seconds, room.rate_seconds), (60, 20))

    def test_a_round_is_armed_with_the_round_time_and_shown_to_the_phones(self):
        room, _ = self._game(round_seconds=45)
        self.assertAlmostEqual(self._seconds_left(room), 45, delta=2)
        timer = room_state(room.code)["timer"]
        self.assertEqual(set(timer), {"deadline_ms", "server_ms"})
        self.assertGreater(timer["deadline_ms"], timer["server_ms"])

    def test_nothing_happens_before_the_deadline(self):
        room, players = self._game()
        self._answer(room, players[0])
        expire_phase(room.code)
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.PLAYING)

    def test_the_round_running_out_opens_the_rating_with_the_rating_time(self):
        room, players = self._game(rate_seconds=25)
        self._answer(room, players[0])
        self._run_out(room)
        self.assertEqual(room.phase, Room.Phase.RATING)
        self.assertAlmostEqual(self._seconds_left(room), 25, delta=2)

    def test_a_round_nobody_answered_goes_to_the_summary(self):
        room, _ = self._game()
        self._run_out(room)
        self.assertEqual(room.phase, Room.Phase.SUMMARY)
        self.assertIsNone(room.phase_deadline)
        self.assertIsNone(room_state(room.code)["timer"])

    def test_a_prompt_still_generating_holds_the_round_open(self):
        room, players = self._game()
        rnd = room.current_round()
        _inflight.add((rnd.pk, players[0].pk))
        try:
            self._run_out(room)
            self.assertEqual(room.phase, Room.Phase.PLAYING)
        finally:
            _inflight.discard((rnd.pk, players[0].pk))
        expire_phase(room.code)
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.SUMMARY)

    def test_a_prompt_sent_after_the_deadline_is_refused(self):
        room, players = self._game()
        Room.objects.filter(code=room.code).update(phase_deadline=timezone.now() - timedelta(seconds=1))
        r = self._answer(room, players[0])
        self.assertEqual(r.status_code, 409)
        self.assertFalse(room.current_round().answers.exists())

    def test_a_rating_running_out_shows_the_next_prompt_then_the_summary(self):
        room, players = self._game(rate_seconds=12)
        for p in players[:2]:
            self._answer(room, p)
        self._run_out(room)
        self.assertEqual((room.phase, room.current_round().revealed), (Room.Phase.RATING, 0))
        self._run_out(room)
        self.assertEqual((room.phase, room.current_round().revealed), (Room.Phase.RATING, 1))
        self.assertAlmostEqual(self._seconds_left(room), 12, delta=2)
        self._run_out(room)
        self.assertEqual(room.phase, Room.Phase.SUMMARY)
        # Settled on the way, like any other summary.
        self.assertTrue(all(a.round_score is not None for a in room.current_round().answers.all()))

    def test_the_admin_next_round_arms_a_fresh_round_clock(self):
        room, _ = self._game(round_seconds=30)
        self._run_out(room)
        self.assertEqual(room.phase, Room.Phase.SUMMARY)
        self._post(f"/api/admin/rooms/{room.code}/next")
        room.refresh_from_db()
        self.assertEqual((room.phase, room.current), (Room.Phase.PLAYING, 1))
        self.assertAlmostEqual(self._seconds_left(room), 30, delta=2)

    def test_a_restart_clears_the_clock(self):
        room, _ = self._game()
        self._post(f"/api/admin/rooms/{room.code}/restart", {"tutorial": False})
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.WAITING)
        self.assertIsNone(room.phase_deadline)
