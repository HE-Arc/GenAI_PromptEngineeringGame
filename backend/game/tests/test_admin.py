import json

from django.http import HttpResponse
from django.test import Client, TestCase, override_settings

from game.models import Room, Round


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
