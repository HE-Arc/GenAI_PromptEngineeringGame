import json
import secrets
import string

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from game import llm  # noqa: F401  (used from Task 4)
from game.models import Answer, Player, Room, Round
from game.targets import ROUNDS_PER_GAME, pick

_ALPHABET = string.ascii_uppercase.replace("O", "").replace("I", "")


def body(request) -> dict:
    return json.loads(request.body or b"{}")


def json_error(msg: str, code: int = 400) -> JsonResponse:
    return JsonResponse({"error": msg}, status=code)


def require_admin(request) -> JsonResponse | None:
    return None if request.session.get("admin") else json_error("admin only", 403)


def _new_code() -> str:
    for _ in range(50):
        code = "".join(secrets.choice(_ALPHABET) for _ in range(4))
        if not Room.objects.filter(code=code).exists():
            return code
    raise RuntimeError("could not allocate a room code")


@require_POST
def admin_login(request):
    if body(request).get("password") != settings.ADMIN_PASSWORD:
        return json_error("bad password", 403)
    request.session["admin"] = True
    return JsonResponse({"ok": True})


@require_POST
def admin_create_room(request):
    if (err := require_admin(request)):
        return err
    code = _new_code()
    Room.objects.create(code=code)
    return JsonResponse({"code": code})


@require_POST
def admin_start(request, code: str):
    if (err := require_admin(request)):
        return err
    room = get_object_or_404(Room, code=code)
    if room.rounds.exists():
        return json_error("already started", 409)
    Round.objects.bulk_create([Round(room=room, order=i, target=t) for i, t in enumerate(pick(ROUNDS_PER_GAME))])
    room.status = Room.Status.PLAYING
    room.save(update_fields=["status"])
    return JsonResponse({"ok": True, "total": ROUNDS_PER_GAME})


@require_POST
def admin_next(request, code: str):
    if (err := require_admin(request)):
        return err
    room = get_object_or_404(Room, code=code)
    if room.status != Room.Status.PLAYING:
        return json_error("not playing", 409)
    room.next_round()
    return JsonResponse({"ok": True, "status": room.status})
