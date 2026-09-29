import json
import secrets
import string
import threading
import time

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.http import Http404, HttpRequest, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from game import llm
from game.models import Player, Room, Round
from game.targets import ROUNDS_PER_GAME, pick

_ALPHABET = string.ascii_uppercase.replace("O", "").replace("I", "")

# In-flight (round, player) submits, claimed for the duration of the two billed model calls.
# PROCESS-LOCAL, and that is sufficient for this deployment: `run.sh` starts one threaded
# `runserver` on a LAN, so both copies of a double submit are served by this process and this
# set. An entry lives only while a model call is in flight, so nothing leaks across a restart
# and the process clearing it is the cleanup. Under a multi-process or multi-host deployment
# this set sees only its own process's requests and the `IntegrityError` backstop in `answer`
# is the thing that remains correct.
#
# Keyed on the row's natural key (round, player), not on `Answer(...).pk`: `pk` is None until
# the row is saved, so an unsaved instance would collapse every claim onto one key and hand a
# second, unrelated player a spurious "already answered".
_inflight: set[tuple[int, int]] = set()

# Player presence: (room_code, player_id) -> last heartbeat, on the monotonic clock.
#
# PROCESS-LOCAL for the same deployment reason as `_inflight` above: one threaded `runserver` on a
# LAN, so a phone's only stream is served by this process and this dict. It is deliberately not a
# column: presence is worthless if stale. A row that says "last seen 4 minutes ago" from a phone
# that has since reconnected would take a write on every 0.5s tick, and a heartbeat nobody has
# refreshed yet is not information worth persisting.
#
# HEARTBEAT WITH A TTL, NOT DISCONNECT DETECTION. A phone that sleeps, loses wifi or is backgrounded
# does not reliably close its socket, so waiting for a close event to mark a player gone is a
# promise the network cannot keep — and `maybe_advance` waiting on a player who is never coming back
# is a frozen party. A player counts as online only while their own stream keeps beating, so a dead
# stream ages out on its own.
#
# TTL = 3.0s against a 0.5s tick, six beats of slack. Comfortably more than one tick so a single
# late beat — a GC pause, a slow query in `room_state`, a phone that hitched — does not flap the
# indicator to offline and back; and short enough that a dropped phone is not waited on for longer
# than a round of reading-and-typing already takes. Monotonic, not wall clock: NTP stepping the
# system time backwards would otherwise pin every player online (or drop them all) at once.
PRESENCE_TTL = 3.0
_TICK = 0.5

# A lock is genuinely required here, unlike `_inflight`. Every stream thread writes this dict twice
# a second, there is one thread per connected phone, and `room_state` reads it from all of them at
# once; a dict mutated during another thread's iteration raises. It is held only for the few
# statements below, never across a query or a sleep, so it cannot serialise the streams themselves.
_presence: dict[tuple[str, int], float] = {}
_presence_lock = threading.Lock()


def _beat(code: str, player_id: int) -> None:
    """Record a heartbeat and drop entries that have aged out.

    The prune iterates a snapshot, not the live dict: another thread's `_beat` may be re-inserting
    a key while this one walks, which would raise "dictionary changed size during iteration".
    Entries are only ever deleted, never resurrected, so dropping a key a concurrent beat just
    re-added costs at most one tick of staleness — the next beat brings it straight back.
    """
    now = time.monotonic()
    with _presence_lock:
        _presence[(code, player_id)] = now
        for key in [k for k, seen in _presence.items() if now - seen > PRESENCE_TTL]:
            del _presence[key]


def online_player_ids(code: str) -> set[int]:
    """The ids in `code` whose last beat is inside the TTL. A read-only filter, no pruning:
    age is judged here, not by presence in the dict, so a stale entry is offline even if no
    stream is ticking to evict it."""
    now = time.monotonic()
    with _presence_lock:
        return {pid for (room_code, pid), seen in _presence.items() if room_code == code and now - seen <= PRESENCE_TTL}


def body(request: HttpRequest) -> dict:
    """The parsed JSON object, or `{}` for anything that is not one.

    A malformed body is a client error, not a server one, and this runs on unauthenticated
    endpoints: a raw `json.loads` would answer any garbage POST with a 500 and a debug
    traceback. An empty parse then falls to each view's own validation, which returns the
    400 the client should have got. A JSON array or bare string parses fine but has no
    `.get`, so it is rejected here too.
    """
    try:
        data = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


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


@require_POST
def admin_restart(request: HttpRequest, code: str) -> JsonResponse:
    """Wipe the game, keep the players: same room code, same tokens, back to the lobby.

    Idempotent — a restart of an already-empty room is a no-op, not a 409, so a double click
    on the admin button cannot lock the admin out of their own room.

    Atomic because `events` polls `room_state` every 0.5s and broadcasts the whole snapshot.
    In autocommit the delete of the rounds commits before the status save, so a poller can
    catch the room mid-reset (rounds gone, status still `playing`, `total` 0) and push that
    snapshot to every phone. One transaction means the poller only ever reads the before-state
    or the after-state.
    """
    if (err := require_admin(request)):
        return err
    room = get_object_or_404(Room, code=code)
    with transaction.atomic():
        room.rounds.all().delete()  # answers cascade via the round FK
        room.current = 0
        room.status = Room.Status.WAITING
        room.save(update_fields=["current", "status"])
    return JsonResponse({"ok": True, "status": room.status})


def room_state(code: str) -> dict:
    room = get_object_or_404(Room, code=code)
    rnd = room.rounds.filter(order=room.current).first()
    by_player = {a.player_id: a for a in (rnd.answers.all() if rnd else [])}
    online = online_player_ids(room.code)
    players = []
    for p in room.players.all():
        a = by_player.get(p.id)
        total = p.answers.aggregate(total=Sum("score"))["total"] or 0
        players.append(
            (
                total,
                p.created_at,
                {
                    "name": p.name,
                    "score": total,
                    "answered": a is not None,
                    "prompt": a.prompt if a else None,
                    "output": a.output if a else None,
                    "comment": a.comment if a else None,
                    "round_score": a.score if a else None,
                    "online": p.id in online,
                },
            )
        )
    # Join order breaks ties: it is stable across rounds, unlike a name sort where the Python
    # ordering puts "Zoe" before "alice". Sorting on a tuple rather than on the dict keeps
    # `created_at`, a datetime, out of the JSON payload this snapshot is serialised into.
    players.sort(key=lambda t: (-t[0], t[1]))
    return {
        "code": room.code,
        "status": room.status,
        "current": room.current,
        "total": room.rounds.count(),
        "target": rnd.target if rnd else None,
        "players": [d for _score, _joined, d in players],
        # Counted off the per-player flags rather than off `online` directly, so the total and the
        # flags can never disagree in the payload the client renders.
        "online_count": sum(1 for _s, _j, d in players if d["online"]),
    }


def maybe_advance(room: Room) -> None:
    """Close the round once every player who is actually there has answered.

    The bar is the online players, not `room.players`: a phone that slept or lost wifi is still a
    row in that table forever, so the old comparison waited on someone who was never coming back and
    the whole party sat on a round it could not leave. A player who reconnects mid-round is back in
    the count on their next beat, and `answer` does not require presence, so a late arrival can
    still answer the round that is open when they get back.
    """
    if room.status != Room.Status.PLAYING:
        return
    rnd = room.rounds.filter(order=room.current).first()
    if not rnd:
        return
    online = online_player_ids(room.code)
    if not online:
        # Nobody is streaming, so there is no evidence anyone is here. Without this guard a room
        # whose every phone dropped would satisfy "all online players have answered" vacuously —
        # `0 == 0` — and run itself to `done` unattended. A round is only ever closed by a real
        # player's submit.
        return
    if rnd.answers.filter(player_id__in=online).count() == len(online):
        room.next_round()


@require_POST
def join(request: HttpRequest, code: str) -> JsonResponse:
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
def events(request: HttpRequest, code: str) -> StreamingHttpResponse:
    """Push the whole room snapshot whenever it changes. Ends when the client disconnects.

    The optional `?token=` identifies which player this stream belongs to, so its ticks can be
    counted as that player's heartbeat. It is optional by design: the admin screen and any
    spectator watch the same endpoint with no token, and a missing or unrecognised one is simply
    not a heartbeat from anyone — never an error, and never a 403 on a stream the lobby depends on.
    The lookup filters on `room` as well as `token`, so a token from another room cannot claim a
    heartbeat here and make a player who is not in this room look present.
    """
    room = get_object_or_404(Room, code=code)
    token = request.GET.get("token", "")
    player = room.players.filter(token=token).first() if token else None

    def stream():
        last = None
        while True:
            # Before the snapshot, so the first frame a player ever sees already shows them online.
            if player is not None:
                _beat(code, player.pk)
            try:
                payload = json.dumps(room_state(code), ensure_ascii=False)
            except Http404:  # room deleted mid-stream
                return
            yield f"data: {payload}\n\n" if payload != last else ": ping\n\n"
            last = payload
            time.sleep(_TICK)

    return StreamingHttpResponse(
        stream(),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@require_POST
def answer(request: HttpRequest, code: str) -> JsonResponse:
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
    # The duplicate check above cannot cover the ~5s the two billed calls take. Claimed here,
    # before the calls, so a second copy arriving in that window is refused without paying for
    # the model a second time. Released in `finally` however this block leaves.
    claim = (rnd.pk, player.pk)
    if claim in _inflight:
        return json_error("already answered", 409)
    _inflight.add(claim)
    try:
        output = llm.answer(prompt)
        verdict = llm.score(prompt, output, rnd.target)
        # The durable backstop, for the case the process-local claim cannot see: another
        # process won unique_together. `transaction.atomic()` is defensive here — with
        # ATOMIC_REQUESTS unset, autocommit already rolls back the single failed statement, so
        # it changes nothing today. It is what makes the `except` reachable at all under
        # ATOMIC_REQUESTS, where an IntegrityError would otherwise poison the rest of the
        # request. Losing the race is the same 409 the player already got, not a 500.
        try:
            with transaction.atomic():
                rnd.answers.create(player=player, prompt=prompt, output=output, score=verdict.score, comment=verdict.comment)
        except IntegrityError:
            return json_error("already answered", 409)
    finally:
        _inflight.discard(claim)
    maybe_advance(room)
    return JsonResponse({"score": verdict.score, "comment": verdict.comment, "output": output})
