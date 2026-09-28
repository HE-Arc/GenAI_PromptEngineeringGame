import json
import secrets
import string
import time

from django.conf import settings
from django.db.models import Sum
from django.http import Http404, HttpRequest, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from game import llm
from game.models import Player, Room, Round
from game.targets import ROUNDS_PER_GAME, pick

_ALPHABET = string.ascii_uppercase.replace("O", "").replace("I", "")


def body(request: HttpRequest) -> dict:
    return json.loads(request.body or b"{}")


def json_error(msg: str, code: int = 400) -> JsonResponse:
    return JsonResponse({"error": msg}, status=code)


def require_admin(request: HttpRequest) -> JsonResponse | None:
    return None if request.session.get("admin") else json_error("admin only", 403)


def _new_code() -> str:
    for _ in range(50):
        code = "".join(secrets.choice(_ALPHABET) for _ in range(4))
        if not Room.objects.filter(code=code).exists():
            return code
    raise RuntimeError("could not allocate a room code")


@require_POST
def admin_login(request: HttpRequest) -> JsonResponse:
    if body(request).get("password") != settings.ADMIN_PASSWORD:
        return json_error("bad password", 403)
    request.session["admin"] = True
    return JsonResponse({"ok": True})


@require_POST
def admin_create_room(request: HttpRequest) -> JsonResponse:
    if (err := require_admin(request)):
        return err
    code = _new_code()
    Room.objects.create(code=code)
    return JsonResponse({"code": code})


@require_POST
def admin_start(request: HttpRequest, code: str) -> JsonResponse:
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
def admin_next(request: HttpRequest, code: str) -> JsonResponse:
    if (err := require_admin(request)):
        return err
    room = get_object_or_404(Room, code=code)
    if room.status != Room.Status.PLAYING:
        return json_error("not playing", 409)
    room.next_round()
    return JsonResponse({"ok": True, "status": room.status})


def room_state(code: str) -> dict:
    room = get_object_or_404(Room, code=code)
    rnd = room.rounds.filter(order=room.current).first()
    by_player = {a.player_id: a for a in (rnd.answers.all() if rnd else [])}
    players = []
    for p in room.players.all():
        a = by_player.get(p.id)
        players.append(
            {
                "name": p.name,
                "score": p.answers.aggregate(total=Sum("score"))["total"] or 0,
                "answered": a is not None,
                "prompt": a.prompt if a else None,
                "output": a.output if a else None,
                "comment": a.comment if a else None,
                "round_score": a.score if a else None,
            }
        )
    players.sort(key=lambda p: (-p["score"], p["name"]))
    return {
        "code": room.code,
        "status": room.status,
        "current": room.current,
        "total": room.rounds.count(),
        "target": rnd.target if rnd else None,
        "players": players,
    }


def maybe_advance(room: Room) -> None:
    """Close the round once every player has answered, so nobody is left waiting on a tab."""
    if room.status != Room.Status.PLAYING:
        return
    rnd = room.rounds.filter(order=room.current).first()
    if rnd and rnd.answers.count() == room.players.count():
        room.next_round()


@require_POST
def join(request: HttpRequest, code: str):
    room = get_object_or_404(Room, code=code)
    name = str(body(request).get("name", "")).strip()[:32]
    if not name:
        return json_error("name required")
    if room.status != Room.Status.WAITING:
        return json_error("game already started", 409)
    if room.players.filter(name=name).exists():
        return json_error("name taken", 409)
    p = Player.objects.create(room=room, name=name)
    return JsonResponse({"token": p.token, "name": p.name})


@require_GET
def events(request: HttpRequest, code: str):
    """Push the whole room snapshot whenever it changes. Ends when the client disconnects."""
    get_object_or_404(Room, code=code)

    def stream():
        last = None
        while True:
            try:
                payload = json.dumps(room_state(code), ensure_ascii=False)
            except Http404:  # room deleted mid-stream
                return
            yield f"data: {payload}\n\n" if payload != last else ": ping\n\n"
            last = payload
            time.sleep(0.5)

    return StreamingHttpResponse(
        stream(),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@require_POST
def answer(request: HttpRequest, code: str):
    room = get_object_or_404(Room, code=code)
    if room.status != Room.Status.PLAYING:
        return json_error("not playing", 409)
    data = body(request)
    try:
        player = Player.objects.get(room=room, token=data.get("token", ""))
    except Player.DoesNotExist:
        return json_error("bad token", 403)
    rnd = room.rounds.filter(order=room.current).first()
    if rnd is None:
        return json_error("no round", 409)
    if rnd.answers.filter(player=player).exists():
        return json_error("already answered", 409)
    prompt = str(data.get("prompt", "")).strip()[:2000]
    if not prompt:
        return json_error("prompt required")
    output = llm.answer(prompt)
    verdict = llm.score(prompt, output, rnd.target)
    rnd.answers.create(player=player, prompt=prompt, output=output, score=verdict.score, comment=verdict.comment)
    maybe_advance(room)
    return JsonResponse({"score": verdict.score, "comment": verdict.comment, "output": output})
