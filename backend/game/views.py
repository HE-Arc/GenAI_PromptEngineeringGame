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
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from game import llm
from game.models import Player, QuizAttempt, QuizQuestion, Room, Round
# Named `pick_questions`, not `pick`, and imported as a name rather than as the `game.quiz`
# module: the didacticiel's draw is a PERMUTATION of a bank exactly one quiz long, and a reader
# who sees two things called `pick` in this header has to go and check which bank each is on.
# It is also the seam a test patches (`game.views.pick_questions`) for the same reason
# `game.views.pick` is: patched where it is USED.
from game.quiz import pick as pick_questions
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
    # `tutorial` and `anonymous` are read here, at creation, and never again: they are per-game
    # settings, and a game that could turn its own gate on halfway through would tell every
    # player in the room which screen they are supposed to be on. Absent from the body means the
    # model defaults, which is the safe direction — a gate on and names hidden.
    data = body(request)
    # The didacticiel is MATERIALISED here, once, and read back by `order` from then on. Handing
    # each player their own eight would make the gate untestable: a player who saw question 3
    # first has no answer key to compare with anyone else's, and "3 sur 8" would be a sum over
    # eight different quizzes.
    #
    # Drawn ONCE PER DATABASE rather than once per room, and that is forced by the schema rather
    # than chosen: `QuizQuestion.order` is UNIQUE and carries no room FK, so only one bank of
    # eight can exist at a time and a second `admin_create_room` has to reuse it. The honest
    # consequence, written down because it is the opposite of what `pick_questions`' docstring
    # implies: two rooms in one install face the eight questions in the SAME order. Per-room
    # order needs a per-room key on `QuizQuestion`, i.e. a migration this task does not own. The
    # bank also outlives `admin_restart`, which is the behaviour `test_restart_erases_quiz_
    # attempts_and_ratings` already pins.
    if not QuizQuestion.objects.exists():
        # `bulk_create` goes straight to INSERT and skips `full_clean()`, exactly like
        # `admin_start`'s rounds: nothing here is validated by Django, only by the database.
        # Safe for the same reason theirs is — `order` comes from `enumerate()`, cannot collide,
        # and the bank is one quiz long.
        QuizQuestion.objects.bulk_create(
            [
                QuizQuestion(
                    order=i,
                    prompt=q.prompt,
                    choices=list(q.choices),
                    correct=q.correct,
                    explanation=q.explanation,
                )
                for i, q in enumerate(pick_questions())
            ]
        )
    Room.objects.create(
        code=code,
        tutorial=bool(data.get("tutorial", True)),
        anonymous=bool(data.get("anonymous", True)),
    )
    return JsonResponse({"code": code})


@require_POST
def admin_start(request: HttpRequest, code: str) -> JsonResponse:
    if (err := require_admin(request)):
        return err
    room = get_object_or_404(Room, code=code)
    if room.rounds.exists():
        return json_error("already started", 409)
    Round.objects.bulk_create([Round(room=room, order=i, target=t) for i, t in enumerate(pick(ROUNDS_PER_GAME))])
    # The ten targets are drawn HERE and not when the didacticiel closes. Drawing them at the
    # moment the admin commits is the one instant nobody is waiting on anything, and it makes the
    # target set fixed for the whole game — which is what "same ten rounds for everybody" has to
    # mean. With a tutorial, the rounds sit behind the quiz until the admin advances out of it.
    room._go(Room.Phase.QUIZ if room.tutorial else Room.Phase.PLAYING)
    return JsonResponse({"ok": True, "total": ROUNDS_PER_GAME, "phase": room.phase})


# What the admin's single "advance" button means in each phase. A phase absent from this table has
# no advance: 409. Written as data so the six cases are readable at a glance, and so adding a phase
# forces a decision here instead of defaulting to something arbitrary — `waiting` has nothing to
# advance into and `done` has nothing left, and both would otherwise be whatever the last `else`
# happened to be.
#
# The room's own methods do the moving, not lambdas that write fields: a transition that has to
# touch the round's reveal cursor as well as the phase cannot be one line, and hiding that behind
# a table entry would make it look like it could.
_PHASE_ADVANCE = {
    Room.Phase.QUIZ: lambda room: room._go(Room.Phase.PLAYING),
    Room.Phase.PLAYING: lambda room: room._open_rating(),
    Room.Phase.RATING: lambda room: room._advance_reveal(),
    Room.Phase.SUMMARY: lambda room: room.next_round(),
}


@require_POST
def admin_next(request: HttpRequest, code: str) -> JsonResponse:
    """The one and only "move the game on" button. Its meaning depends on where the room is,
    which is why it is a table and not a chain of `if`s."""
    if (err := require_admin(request)):
        return err
    room = get_object_or_404(Room, code=code)
    advance = _PHASE_ADVANCE.get(room.phase)
    if advance is None:
        return json_error(f"no advance from {room.phase}", 409)
    advance(room)
    return JsonResponse({"ok": True, "phase": room.phase})


@require_POST
def admin_restart(request: HttpRequest, code: str) -> JsonResponse:
    """Wipe the game, keep the players: same room code, same tokens, back to the lobby.

    Idempotent — a restart of an already-empty room is a no-op, not a 409, so a double click
    on the admin button cannot lock the admin out of their own room.

    Atomic because `events` polls `room_state` every 0.5s and broadcasts the whole snapshot.
    In autocommit the delete of the rounds commits before the phase save, so a poller can
    catch the room mid-reset (rounds gone, phase still `playing`, `total` 0) and push that
    snapshot to every phone. One transaction means the poller only ever reads the before-state
    or the after-state.

    `tutorial` and `anonymous` deliberately survive, and so do the players and their tokens: they
    describe the game the admin is about to relaunch, not the one that just ended. A checkbox that
    silently unticks itself on restart is a flag nobody can trust, and a lobby everybody has to
    rejoin is the thing this endpoint exists to avoid.

    The quiz attempts are deleted explicitly, and this is not redundant with the cascade: they
    hang off `Player` and `QuizQuestion`, both of which survive this reset, so leaving them
    behind would tell a returning player they had already answered a question in a game that no
    longer exists.
    """
    if (err := require_admin(request)):
        return err
    room = get_object_or_404(Room, code=code)
    with transaction.atomic():
        room.rounds.all().delete()  # answers cascade via the round FK, ratings via Answer
        QuizAttempt.objects.filter(player__room=room).delete()
        room.current = 0
        room.phase = Room.Phase.WAITING
        room.save(update_fields=["current", "phase"])
    return JsonResponse({"ok": True, "phase": room.phase})


def _quiz_state(room: Room, online: set[int]) -> dict:
    """The room's quiz headline: how far the room is through the gate, and the best score in it.

    ROOM-level, not per-viewer, and it cannot be otherwise: `room_state(code)` deliberately takes
    no token — the snapshot is one payload for the admin screen, every phone and every spectator,
    and presence is global in this codebase — so it does not know who is reading. A player's OWN
    score is `players[].quiz_score`, which the same snapshot already carries. So this block is the
    number the lobby and the admin show, not the one a player is graded by.

    `done` counts the ONLINE players who have answered every question, matching the bar
    `maybe_advance` uses to open the game: if this and the gate disagreed, the screen would say
    "c'est parti" on a room that has not moved. A player whose phone dropped is not counted, for
    the reason every other advance in this file counts presence: the row is there forever and the
    person may never come back.
    """
    total = QuizQuestion.objects.count()  # the bank is per database, so this is the room's bank
    players = list(room.players.filter(pk__in=online))
    if not total:
        # An empty bank must not read as "everybody is done": `count() == 0` is true of every
        # player, so the naive comparison would report a full room at the top of the gate.
        return {"total": 0, "done": 0, "score": 0}
    return {
        "total": total,
        "done": sum(1 for p in players if p.quiz_attempts.count() == total),
        # 0, never None, with nobody online: same rule as `Player.quiz_score` — the client should
        # not carry a null branch only a room with nobody in it would ever take.
        "score": max((p.quiz_score() for p in players), default=0),
    }


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
    state = {
        "code": room.code,
        "phase": room.phase,
        "current": room.current,
        "total": room.rounds.count(),
        "target": rnd.target if rnd else None,
        "players": [d for _score, _joined, d in players],
        # Counted off the per-player flags rather than off `online` directly, so the total and the
        # flags can never disagree in the payload the client renders.
        "online_count": sum(1 for _s, _j, d in players if d["online"]),
    }
    # Only in the phase that has a quiz to show. The final contract puts the three phase blocks
    # (`quiz`, `reveal`, `summary`) in the snapshot unconditionally, `None` outside their phase, so
    # the client null-checks one shape instead of testing for the key's existence. That is not
    # done here: an always-present `quiz` key changes the key SET of every snapshot in every
    # phase, which is `test_play`'s two whole-dict assertions, and this task does not own those
    # files. The gate is the whole point of the block, so it ships where it is true.
    if room.phase == Room.Phase.QUIZ:
        state["quiz"] = _quiz_state(room, online)
    return state


def _maybe_open_playing(room: Room, online: set[int]) -> None:
    """Open the game once every player who is there has answered the whole didacticiel.

    Same bar as the round close, and for the same reason: `room.players` is a phone that slept,
    not a person who is going to answer. The count is the room's OWN online players rather than
    `len(online)`, so a presence entry left behind by a player deleted mid-quiz cannot make the
    comparison unreachable and pin the room on the gate forever.

    The question count is read from the bank rather than from `quiz.QUIZ_LENGTH`, so a bank the
    admin has grown past eight is not gated on a hardcoded eight.
    """
    total = QuizQuestion.objects.count()
    if not total:
        return  # nothing was materialised, so there is no gate to pass
    players = list(room.players.filter(pk__in=online))
    if not players:
        return  # every presence entry is a player this room does not have: `all([])` is True
    if all(p.quiz_attempts.count() == total for p in players):
        room._go(Room.Phase.PLAYING)


def maybe_advance(room: Room) -> None:
    """Move the game on if the players who are there have done what the phase is waiting for.

    Two phases advance on their own: the didacticiel, once everybody online has answered the
    eight questions, and the round, once everybody online has written a prompt. The others wait
    for the admin — a round gets commented on, and nothing should move the game while it is.

    The bar is the online players, not `room.players`: a phone that slept or lost wifi is still a
    row in that table forever, so the old comparison waited on someone who was never coming back and
    the whole party sat on a round it could not leave. A player who reconnects mid-round is back in
    the count on their next beat, and `answer` does not require presence, so a late arrival can
    still answer the round that is open when they get back.
    """
    if room.phase not in (Room.Phase.QUIZ, Room.Phase.PLAYING):
        return
    online = online_player_ids(room.code)
    if not online:
        # Nobody is streaming, so there is no evidence anyone is here. Without this guard a room
        # whose every phone dropped would satisfy "all online players have answered" vacuously —
        # `0 == 0` — and open itself unattended. A phase is only ever closed by a real player's
        # submit.
        return
    if room.phase == Room.Phase.QUIZ:
        _maybe_open_playing(room, online)
        return
    rnd = room.rounds.filter(order=room.current).first()
    if not rnd:
        return
    if rnd.answers.filter(player_id__in=online).count() == len(online):
        room.next_round()


@require_POST
def join(request: HttpRequest, code: str) -> JsonResponse:
    room = get_object_or_404(Room, code=code)
    name = str(body(request).get("name", "")).strip()[:32]
    if not name:
        return json_error("name required")
    if room.phase != Room.Phase.WAITING:
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
    if room.phase != Room.Phase.PLAYING:
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


@require_http_methods(["GET", "POST"])
def quiz(request: HttpRequest, code: str) -> JsonResponse:
    """The didacticiel on one route: GET reads the bank, POST answers ONE question.

    ONE route and not two `path()` entries, because `URLResolver.resolve` walks `urlpatterns` in
    order and returns the first pattern that MATCHES THE PATH — it does not know about methods. A
    `require_GET` pattern listed first would answer every POST with 405 and the pattern below it
    would never be reached. So the one decorator here is the method gate, and the two halves below
    are private: nothing calls them except this dispatch.
    """
    return _quiz_get(request, code) if request.method == "GET" else _quiz_post(request, code)


def _quiz_get(request: HttpRequest, code: str) -> JsonResponse:
    """The eight questions, in `order`, with no key and no explanation attached.

    Not cosmetic. This payload is a JSON body any player can read in devtools or in a proxy, and a
    QCM whose key ships to the client is not a QCM — it is a form with a lookup table. So `correct`
    and `explanation` are dropped HERE, at the only place they are read, rather than in the
    frontend that is supposed to forget them.

    Unauthenticated, and not phase-gated, on purpose: what it returns is the eight prompts and
    their four choices, which is what is on the screen anyway, so reading it leaks nothing the
    player could not read by looking. What it never returns is what a later phase cannot: the
    answer, and the reason. That stays in the POST, which is a write.
    """
    get_object_or_404(Room, code=code)
    return JsonResponse(
        {
            "questions": [
                {"order": q.order, "prompt": q.prompt, "choices": q.choices}
                for q in QuizQuestion.objects.order_by("order")
            ]
        }
    )


def _quiz_post(request: HttpRequest, code: str) -> JsonResponse:
    """Answer one question, and reveal it.

    The response is `{"correct", "explanation", "score"}` and the whole point of it is the
    revelation: the explanation travels in the SAME response that tells the player whether they
    were right, which is why `QuizQuestion.explanation` is stored rather than read from `quiz.py`
    at answer time — it belongs to the exact row the player was shown, so an edited bank cannot
    retroactively change the reason a past game gave.

    The client names a question by its `order` and a choice by its index, and sends nothing else
    that is checked: the player is the token, the question is the row, the key is the row's.
    """
    room = get_object_or_404(Room, code=code)
    if room.phase != Room.Phase.QUIZ:
        return json_error("quiz not open", 409)
    data = body(request)
    try:
        # Scoped to the room as well as the token, like `answer`: a token from another room must
        # not score a question in this one.
        player = Player.objects.get(room=room, token=data.get("token", ""))
    except Player.DoesNotExist:
        return json_error("bad token", 403)
    order = data.get("question")
    # `isinstance(True, int)` is True, so a JSON `true` is an int that would pass the type check
    # and then miss every row. Same guard as the 1-10 rating scale.
    if not isinstance(order, int) or isinstance(order, bool):
        return json_error("question index required", 400)
    question = QuizQuestion.objects.filter(order=order).first()
    if question is None:
        return json_error("no such question", 400)
    choice = data.get("choice")
    if not isinstance(choice, int) or isinstance(choice, bool) or not 0 <= choice < len(question.choices):
        return json_error("choice out of range", 400)
    if QuizAttempt.objects.filter(player=player, question=question).exists():
        return json_error("already answered", 409)
    correct = choice == question.correct
    try:
        # The durable backstop, for the same reason as in `answer`: a flaky phone retrying is
        # indistinguishable from a second answer, and `unique_together` is the source of truth.
        # Losing that race is the 409 the player already got, not a 500.
        with transaction.atomic():
            QuizAttempt.objects.create(player=player, question=question, choice=choice, correct=correct)
    except IntegrityError:
        return json_error("already answered", 409)
    maybe_advance(room)
    return JsonResponse({"correct": correct, "explanation": question.explanation, "score": player.quiz_score()})
