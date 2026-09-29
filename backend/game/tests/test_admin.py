import json
from unittest.mock import patch

from django.http import HttpResponse
from django.test import Client, TestCase, override_settings

from game.llm import Verdict
from game.models import Answer, Player, Room, Round
from game.views import room_state


@override_settings(ADMIN_PASSWORD="s3cret")
class AdminTest(TestCase):
    # This module is also the regression guard for Task 1's MIDDLEWARE: `admin_login` writes
    # `request.session["admin"]`, which raises AttributeError without `SessionMiddleware` in the
    # chain. If every test here 500s, check that middleware before anything else.
    def setUp(self) -> None:
        self.c = Client()

    def _login(self, pwd: str = "s3cret") -> HttpResponse:
        return self.c.post("/api/admin/login", data=json.dumps({"password": pwd}), content_type="application/json")

    def test_login_rejects_bad_password(self) -> None:
        self.assertEqual(self._login("nope").status_code, 403)

    def test_login_accepts_good_password(self) -> None:
        r = self._login()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.content), {"ok": True})
        # Do not delete this line as redundant. It is the only regression guard for Task 1's
        # `SessionMiddleware`: without that middleware `admin_login` raises on the session write
        # (caught above) and `process_response` never sets the cookie (caught here).
        self.assertIn("sessionid", self.c.cookies)

    def test_create_room_requires_login(self) -> None:
        self.assertEqual(self.c.post("/api/admin/rooms", content_type="application/json").status_code, 403)

    def test_create_room_returns_unique_code(self) -> None:
        self._login()
        a = json.loads(self.c.post("/api/admin/rooms", content_type="application/json").content)
        b = json.loads(self.c.post("/api/admin/rooms", content_type="application/json").content)
        self.assertNotEqual(a["code"], b["code"])
        self.assertEqual(len(a["code"]), 4)

    def test_start_requires_login(self) -> None:
        # Both halves of the auth spine, in one test: `require_admin` on `admin_start` and on
        # `admin_next`. Dropping either leaves every other test here green.
        Room.objects.create(code="WXYZ")
        self.assertEqual(self.c.post("/api/admin/rooms/WXYZ/start", content_type="application/json").status_code, 403)
        self.assertEqual(Round.objects.count(), 0)
        self.assertEqual(self.c.post("/api/admin/rooms/WXYZ/next", content_type="application/json").status_code, 403)
        Room.objects.get(code="WXYZ").refresh_from_db()
        self.assertEqual(Room.objects.get(code="WXYZ").status, "waiting")

    def test_start_creates_ten_rounds(self) -> None:
        self._login()
        code = json.loads(self.c.post("/api/admin/rooms", content_type="application/json").content)["code"]
        r = self.c.post(f"/api/admin/rooms/{code}/start", content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.content)["total"], 10)
        self.assertEqual(Round.objects.filter(room__code=code).count(), 10)

    def test_long_target_survives_start_and_appears_whole_in_room_state(self) -> None:
        # The round trip, end to end: `admin_start` picks the target, writes it, and `room_state`
        # hands the current one to every phone. A long target has to come out of all three
        # unchanged, because `room_state`'s "target" is what the player is playing against.
        #
        # This was never caught by a test because it was never caught at all: `admin_start`
        # writes with `bulk_create`, which skips `full_clean()`, and `max_length` is a
        # validation rule rather than a database constraint. An over-long target used to be
        # stored silently. So the assertion here is on the exact string, not on the length
        # being "reasonable" — a truncation is what this is guarding.
        self._login()
        code = json.loads(self.c.post("/api/admin/rooms", content_type="application/json").content)["code"]
        long_target = "la recette " + "et encore un detail " * 40 + "de fin"
        # Patched where it is USED, not where it is defined: `views` does
        # `from game.targets import pick`, so the name in this module's namespace is the one
        # `admin_start` calls. Patching `game.targets.pick` would leave the real bank in play
        # and this test would silently assert against whichever target got drawn first.
        with patch("game.views.pick", return_value=[long_target] * 10):
            r = self.c.post(f"/api/admin/rooms/{code}/start", content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Round.objects.get(room__code=code, order=0).target, long_target)
        self.assertEqual(room_state(code)["target"], long_target)

    def test_start_is_idempotent_refuses_second_call(self) -> None:
        self._login()
        code = json.loads(self.c.post("/api/admin/rooms", content_type="application/json").content)["code"]
        self.c.post(f"/api/admin/rooms/{code}/start", content_type="application/json")
        self.assertEqual(self.c.post(f"/api/admin/rooms/{code}/start", content_type="application/json").status_code, 409)

    def test_next_advances_then_finishes(self) -> None:
        self._login()
        code = json.loads(self.c.post("/api/admin/rooms", content_type="application/json").content)["code"]
        self.c.post(f"/api/admin/rooms/{code}/start", content_type="application/json")
        url = f"/api/admin/rooms/{code}/next"
        seen = [json.loads(self.c.post(url, content_type="application/json").content)["status"] for _ in range(10)]
        self.assertEqual(seen, ["playing"] * 9 + ["done"])
        self.assertEqual(self.c.post(url, content_type="application/json").status_code, 409)

    def test_restart_requires_login(self) -> None:
        Room.objects.create(code="WXYZ", status=Room.Status.PLAYING)
        self.assertEqual(self.c.post("/api/admin/rooms/WXYZ/restart", content_type="application/json").status_code, 403)
        room = Room.objects.get(code="WXYZ")
        self.assertEqual(room.status, "playing")  # nothing was touched

    def test_restart_from_playing_returns_to_lobby_and_keeps_players(self) -> None:
        self._login()
        code = json.loads(self.c.post("/api/admin/rooms", content_type="application/json").content)["code"]
        self._join(code, "ana")
        self.c.post(f"/api/admin/rooms/{code}/start", content_type="application/json")
        r = self.c.post(f"/api/admin/rooms/{code}/restart", content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.content), {"ok": True, "status": "waiting"})
        room = Room.objects.get(code=code)
        self.assertEqual((room.status, room.current), ("waiting", 0))
        self.assertEqual(Round.objects.filter(room=room).count(), 0)
        self.assertEqual([p.name for p in room.players.all()], ["ana"])

    def test_restart_from_done_returns_to_lobby(self) -> None:
        self._login()
        code = json.loads(self.c.post("/api/admin/rooms", content_type="application/json").content)["code"]
        self._join(code, "ana")
        self.c.post(f"/api/admin/rooms/{code}/start", content_type="application/json")
        url = f"/api/admin/rooms/{code}/next"
        for _ in range(10):
            self.c.post(url, content_type="application/json")
        self.assertEqual(Room.objects.get(code=code).status, "done")
        r = self.c.post(f"/api/admin/rooms/{code}/restart", content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.content), {"ok": True, "status": "waiting"})
        room = Room.objects.get(code=code)
        self.assertEqual((room.status, room.current), ("waiting", 0))
        self.assertEqual(Round.objects.filter(room=room).count(), 0)
        self.assertEqual(room.players.count(), 1)

    def test_restart_erases_answers_and_keeps_tokens_usable(self) -> None:
        # The whole point of the feature: nobody rejoins. `ana`'s token from the first game is
        # what she submits with in the second, so this fails if the players were recreated.
        self._login()
        code = json.loads(self.c.post("/api/admin/rooms", content_type="application/json").content)["code"]
        token = self._join(code, "ana")
        self.c.post(f"/api/admin/rooms/{code}/start", content_type="application/json")
        with patch("game.llm.answer", return_value="42"), patch(
            "game.llm.score", return_value=Verdict(7, "pas mal")
        ):
            self.c.post(
                f"/api/rooms/{code}/answer",
                data=json.dumps({"token": token, "prompt": "repete 42"}),
                content_type="application/json",
            )
        self.assertEqual(Answer.objects.count(), 1)

        self.c.post(f"/api/admin/rooms/{code}/restart", content_type="application/json")
        self.assertEqual(Answer.objects.count(), 0)  # cascade through the deleted rounds
        self.assertEqual(Player.objects.get(room_id=code, name="ana").token, token)

        # Second game, same player, same token, no rejoin.
        self.assertEqual(
            self.c.post(f"/api/admin/rooms/{code}/start", content_type="application/json").status_code, 200
        )
        with patch("game.llm.answer", return_value="42"), patch(
            "game.llm.score", return_value=Verdict(9, "nickel")
        ):
            r = self.c.post(
                f"/api/rooms/{code}/answer",
                data=json.dumps({"token": token, "prompt": "repete 42"}),
                content_type="application/json",
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.content)["score"], 9)
        # Score is per-round, so the previous game's 7 is not carried over.
        self.assertEqual(Answer.objects.get().score, 9)
        self.assertEqual(Answer.objects.count(), 1)

    def test_second_restart_is_a_noop(self) -> None:
        # Double-clicking the admin button must not lock the admin out of their own room, so
        # this is 200 and not the 409 `admin_start` returns for its own second call.
        self._login()
        code = json.loads(self.c.post("/api/admin/rooms", content_type="application/json").content)["code"]
        self._join(code, "ana")
        self.c.post(f"/api/admin/rooms/{code}/start", content_type="application/json")
        url = f"/api/admin/rooms/{code}/restart"
        self.assertEqual(json.loads(self.c.post(url, content_type="application/json").content)["status"], "waiting")
        again = self.c.post(url, content_type="application/json")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(json.loads(again.content), {"ok": True, "status": "waiting"})
        self.assertEqual(Room.objects.get(code=code).players.count(), 1)

    def test_restart_on_unknown_room_is_404(self) -> None:
        self._login()
        self.assertEqual(
            self.c.post("/api/admin/rooms/NOPE/restart", content_type="application/json").status_code, 404
        )

    def _join(self, code: str, name: str) -> str:
        r = self.c.post(
            f"/api/rooms/{code}/join",
            data=json.dumps({"name": name}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        return json.loads(r.content)["token"]
