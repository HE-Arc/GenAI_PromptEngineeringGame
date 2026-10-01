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

from game import llm, prompts
from game.models import Player, QuizAttempt, QuizQuestion, Rating, Room, Round
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
# promise the network cannot keep — and `maybe_advance_phase` waiting on a player who is never
# coming back is a frozen party. A player counts as online only while their own stream keeps
# beating, so a dead stream ages out on its own.
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
    # The didacticiel is MATERIALISED here, once PER ROOM, and read back by `(room, order)` from
    # then on. Handing each player their own eight would make the gate untestable: a player who
    # saw question 3 first has no answer key to compare with anyone else's, and "3 sur 8" would
    # be a sum over eight different quizzes.
    #
    # ONCE PER ROOM, not once per database: `QuizQuestion` hangs off `Room` and `order` is unique
    # within a room, so this runs for EVERY room and two rooms draw their own order — which is
    # what `pick_questions`' docstring promises and what a class of thirty in one install needs.
    # The bank belongs to the room, which also makes it refreshable: this endpoint runs again for
    # the next room and draws again from `quiz.py`, so an admin who edited the file sees the
    # change on the next room they create. It did not used to, because the bank was materialised
    # once behind `if not QuizQuestion.objects.exists()` and never refreshed — an edit to
    # `quiz.py` was invisible until the database was wiped.
    #
    # A restart does NOT come through here: it keeps the room, so it keeps this room's bank, and
    # the players who already answered are re-asked the same eight in the same order rather than
    # handed a new set. `admin_restart` deliberately does not touch `room.questions`.
    #
    # The Room row is created FIRST, because the bank needs its `room_id`.
    room = Room.objects.create(
        code=code,
        tutorial=bool(data.get("tutorial", True)),
        anonymous=bool(data.get("anonymous", True)),
    )
    # `bulk_create` goes straight to INSERT and skips `full_clean()`, exactly like
    # `admin_start`'s rounds: nothing here is validated by Django, only by the database. Safe for
    # the same reason theirs is — `order` comes from `enumerate()`, cannot collide inside one
    # room, and the bank is one quiz long.
    # `room=room` on every row rather than relying on the related manager to fill it in:
    # `bulk_create` writes the columns it is given, and a row with a null `room_id` is exactly
    # what the NOT NULL constraint is there to refuse.
    QuizQuestion.objects.bulk_create(
        [
            QuizQuestion(
                room=room,
                order=i,
                prompt=q.prompt,
                choices=list(q.choices),
                correct=q.correct,
                explanation=q.explanation,
            )
            for i, q in enumerate(pick_questions())
        ]
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


def settle_round(room: Room) -> None:
    """Write the round's scores once, when its rating closes. Never recomputed.

    One write per answer, then every later snapshot is a SUM over a nullable integer column —
    the same shape the ranking already has. Recomputing instead would be correct and would melt
    the server, because `room_state` is evaluated twice a second per connected stream and the
    peers' mean needs every rating of the round. See the four columns on `Answer` for the
    arithmetic.

    Called from two places, and both of them are "the rating of this round is over":
    `maybe_advance_phase` when the last online rater rated the last prompt, and
    `_advance_reveal_and_settle` when the ADMIN skips the remaining prompts. A round the players
    abandoned has to be settled by the admin's button too, or its summary screen would show a
    column of nulls.

    Idempotent by construction — it writes the same numbers from the same rows — so the two paths
    converging on one round cannot disagree, and an admin who advances twice over a stuck round
    rewrites the settlement with its own value rather than doubling it.

    A player who wrote no prompt has no row, so no round is counted for them and there is nothing
    to settle: they keep the rounds they played and nothing else. A player who wrote one and
    drew no ratings at all (a one-player room, or everyone else was asleep) gets `quality_peers`
    left NULL rather than a mean of nothing — which is why the division is behind `if scores`.
    """
    rnd = room.current_round()
    if rnd is None:
        return
    for a in rnd.answers.order_by("id"):
        scores = list(a.ratings.values_list("score", flat=True))
        a.quality_peers = round(sum(scores) / len(scores)) if scores else None
        a.quality = prompts.quality(a.quality_llm, scores)
        a.round_score = (a.score + a.quality) // 2
        a.save(update_fields=["quality_peers", "quality", "round_score"])


def _advance_reveal_and_settle(room: Room) -> None:
    """The admin's "next prompt" during the rating: `_advance_reveal`, plus a settle when that
    closed the round.

    The admin can push through prompts nobody got round to, and the summary they land on has to
    carry the settlement like any other — an un-settled summary is a screen of nulls and the
    admin has no way to tell it apart from a bug.
    """
    room._advance_reveal()
    if room.phase == Room.Phase.SUMMARY:
        settle_round(room)


# What the admin's single "advance" button means in each phase. A phase absent from this table has
# no advance: 409. Written as data so the six cases are readable at a glance, and so adding a phase
# forces a decision here instead of defaulting to something arbitrary — `waiting` has nothing to
# advance into and `done` has nothing left, and both would otherwise be whatever the last `else`
# happened to be.
#
# The room's own methods do the moving, not lambdas that write fields: a transition that has to
# touch the round's reveal cursor as well as the phase cannot be one line, and hiding that behind
# a table entry would make it look like it could. The rating row is the one entry that is not a
# one-liner, because the phase change is not the whole transition there: the round is settled on
# the way out.
_PHASE_ADVANCE = {
    Room.Phase.QUIZ: lambda room: room._go(Room.Phase.PLAYING),
    Room.Phase.PLAYING: lambda room: room._open_rating(),
    Room.Phase.RATING: _advance_reveal_and_settle,
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

    The room's BANK survives too, and that is now a cascade boundary rather than an accident:
    `QuizQuestion` hangs off `Room`, so a deleted room takes its eight questions with it — which
    is right, they were only ever that room's — but a restart deletes rounds, never the room, so
    the questions stay. The players who already answered them are re-asked the same eight in the
    same order on the relaunch, instead of being handed a freshly drawn set they cannot compare
    with the attempt they already made.
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
    `maybe_advance_phase` uses to open the game: if this and the gate disagreed, the screen would
    say "c'est parti" on a room that has not moved. A player whose phone dropped is not counted,
    for the reason every other advance in this file counts presence: the row is there forever and
    the person may never come back.

    Every read here is scoped to `room.questions`, not to `QuizQuestion.objects`: the bank is the
    room's, so a global count is this room's eight PLUS every other room's eight, and the total
    would be a number no player could ever reach.
    """
    total = room.questions.count()
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


def _roster_labels(room: Room) -> dict[int, str]:
    """Each player's pseudonym: their real name, or `Joueur N` where N is their place in the room.

    Under anonymity the number is their PLACE IN THE ROOM, counted by `(created_at, id)` — arrival,
    not the alphabet — and therefore FIXED FOR THE WHOLE GAME. That stability is the feature: a
    pseudonym that changed between two prompts of the same game would let a player recognise their
    own writing in a second costume, which defeats the anonymity twice over, once for the revealed
    prompt and once for the ranking underneath it. It is derived from nothing but who arrived
    first, so it does not move when a phone sleeps, when a prompt is revealed, or when a late
    joiner appears at the end of the roster.

    An `id` is the tiebreak behind `created_at` because two players can join inside the same
    timestamp's resolution, and a sort that left them in an arbitrary order would number them
    differently from one snapshot to the next.

    Built for the whole room and not per snapshot caller, because a label computed from whatever
    the current snapshot happened to be holding is exactly how a handle starts moving.
    """
    return {
        p.id: (f"Joueur {i}" if room.anonymous else p.name)
        for i, p in enumerate(room.players.order_by("created_at", "id"), start=1)
    }


def _reveal_state(room: Room, rnd, labels: dict[int, str]) -> dict | None:
    """The one prompt on screen: which one, who wrote it, and how many have rated it so far.

    None when the cursor has nothing behind it, which is a rating phase on a round nobody answered
    — a room that reached the reveal with no prompt in it. There is no slide to describe, and an
    empty `prompt` string would be a screen that renders a blank card rather than a phase with
    nothing in it.

    `rated` counts the raters of the CURRENT prompt, not the notes given so far this round: it is
    the numerator of the "3 notés" the player is waiting on, and summing every rating of the round
    would show a number that jumps from 2 to 7 as the reveal walks the prompts.
    """
    answers = list(rnd.answers.order_by("id"))
    if rnd.revealed >= len(answers):
        return None
    target = answers[rnd.revealed]
    return {
        "index": rnd.revealed,
        "count": len(answers),
        "prompt": target.prompt,
        # `author` is the real name, or null under anonymity. `author_label` is ALWAYS a string to
        # draw: a screen must never branch on anonymity to decide whether it has a label, so the
        # pseudonym travels beside the name rather than replacing it.
        "author": None if room.anonymous else target.player.name,
        "author_label": labels[target.player_id],
        "rated": target.ratings.count(),
    }


def _summary_state(room: Room, rnd) -> dict:
    """The round's synthesis, once anonymity is lifted: one row per prompt, with all four numbers
    that produced its score.

    The order is the round's answers by id — the same order the reveal walked — so the summary is
    the transcript of what the players just saw, in sequence. The four numbers are NOT the final
    contract's `SummaryRow` being a summary; they are the pedagogy: proximity and the two halves
    of the quality bound, so a player can see their prompt was CLOSE but badly BUILT, which is the
    lesson the two dimensions exist to teach.

    It carries whatever is stored rather than a default: `settle_round` always writes all three
    numbers together, so a null in here means the round was never settled at all — which the
    admin's next button fixes, and which a silent 0 would have hidden as a real score.
    """
    return {
        "round": rnd.order,
        "rows": [
            {
                "author": a.player.name,
                "prompt": a.prompt,
                "output": a.output,
                "proximity": a.score,
                "quality_llm": a.quality_llm,
                "quality_peers": a.quality_peers,
                "quality": a.quality,
                "round_score": a.round_score,
            }
            for a in rnd.answers.order_by("id")
        ],
    }


def room_state(code: str) -> dict:
    """The whole room, one payload, for everybody: the admin screen, every phone, every spectator.

    THE CONTRACT IS FIXED KEY FOR KEY, and it is the `Player` / `State` types in
    `frontend/src/api.ts`. Two decisions in it are worth stating here rather than leaving a reader
    to infer them from the lines below.

    THE THREE PHASE BLOCKS ARE ALWAYS PRESENT, `None` outside their own phase. A client that
    null-checks one shape is more robust than a client testing for a key's existence, and the
    types already say `| null`. The cost is that the key SET of every snapshot is now identical in
    all six phases, which is exactly what `test_play`'s two whole-dict assertions pin.

    THE PLAYER ROW CARRIES NO PROMPT, NO OUTPUT AND NO COMMENT, IN ANY PHASE. They are not in the
    frozen `Player` type and no screen reads them: the judge's verdict reaches a player through the
    `answer` POST response, which is where they were always delivered, and the prompt and output
    they need to read the round arrive in the `summary` rows. The rating phase used to blank them
    here and send them everywhere else — a key that is a string in one phase and null in another is
    a key every consumer has to null-check for a reason that no longer exists.
    """
    room = get_object_or_404(Room, code=code)
    rnd = room.rounds.filter(order=room.current).first()
    by_player = {a.player_id: a for a in (rnd.answers.all() if rnd else [])}
    online = online_player_ids(room.code)
    # The rating phase blanks the score of every player, and the sort below reads the same value —
    # so the two can never disagree about which players are hidden.
    hiding = room.phase == Room.Phase.RATING
    # The bank's size, read ONCE and off this room's questions: it is the bar `quiz_done` is
    # measured against, and counting it per player would be one query per player for a number that
    # is a property of the room.
    bank = room.questions.count()
    players = []
    for p in room.players.all():
        a = by_player.get(p.id)
        total = p.answers.aggregate(total=Sum("score"))["total"] or 0
        shown = None if hiding else total
        # The two dimensions behind `total`, over the rounds that have been SETTLED — the same
        # rows `settle_round` writes `round_score` on, so the three numbers a player reads always
        # describe the same set of rounds. `Sum` skips the NULLs of an unsettled round, and a
        # player with none at all gets NULL rather than a 0 that would read as a real, terrible
        # score.
        settled = p.answers.filter(round_score__isnull=False).aggregate(
            proximity=Sum("score"), quality=Sum("quality")
        )
        players.append(
            (
                shown,
                p.created_at,
                {
                    "name": p.name,
                    "score": shown,
                    "answered": a is not None,
                    "round_score": None if hiding else (a.score if a else None),
                    # These two are in the payload for a teaching reason, not a decorative one. A
                    # player who finishes on "proximité 42, qualité 58, total 50" learns that
                    # their prompts WORK and are badly BUILT, which is the entire lesson. Collapse
                    # the two into one number out of a hundred and that distinction disappears
                    # behind something that reads like a grade.
                    "proximity_total": settled["proximity"],
                    "quality_total": settled["quality"],
                    # An int, never None: the client formats "3 sur 8" and should not carry a
                    # null branch that only a player who never answered would ever take.
                    "quiz_score": p.quiz_score(),
                    # This player's OWN progress through the gate, where `quiz.done` is the room's.
                    # `bool(bank)` first, for the reason `_quiz_state` checks its own empty case:
                    # `count() == 0` is true of every player, so an unmaterialised bank would read
                    # as a lobby that had all passed.
                    "quiz_done": bool(bank) and p.quiz_attempts.count() == bank,
                    # The NAME stays. The roster is not a leak: knowing who is in the room says
                    # nothing about who wrote which prompt. It is the scores that cross-reference.
                    "online": p.id in online,
                },
            )
        )
    # Join order breaks ties: it is stable across rounds, unlike a name sort where the Python
    # ordering puts "Zoe" before "alice". Sorting on a tuple rather than on the dict keeps
    # `created_at`, a datetime, out of the JSON payload this snapshot is serialised into.
    #
    # The leading key must survive a None score, and it does not for free: `-None` is a
    # TypeError, which would kill the whole snapshot the moment the rating phase blanks every
    # score — and with it the SSE stream, so the rating phase would never even start. Falling
    # back to 0 keeps the sort total; the join-order tiebreak below is what actually orders the
    # board while every score is hidden, and it was already the tiebreak for equal scores.
    players.sort(key=lambda t: (-(t[0] or 0), t[1]))
    state = {
        "code": room.code,
        "phase": room.phase,
        "current": room.current,
        "total": room.rounds.count(),
        "target": rnd.target if rnd else None,
        # Fixed for the whole game, so a screen reads it once instead of inferring anonymity from
        # whether a name happens to be present in `reveal.author`.
        "anonymous": room.anonymous,
        "players": [d for _score, _joined, d in players],
        # Counted off the per-player flags rather than off `online` directly, so the total and the
        # flags can never disagree in the payload the client renders.
        "online_count": sum(1 for _s, _j, d in players if d["online"]),
        # THE THREE PHASE BLOCKS ARE UNCONDITIONAL KEYS. `None` outside the phase that fills them,
        # never absent, so the client null-checks one shape rather than testing for a key's
        # existence — and the frozen `State` types them `| null` rather than optional. `reveal` and
        # `summary` are ALSO null in their own phase when there is nothing behind them: a rating
        # phase on a round nobody answered has no prompt to describe, and a summary of nothing is
        # still a summary.
        "quiz": _quiz_state(room, online) if room.phase == Room.Phase.QUIZ else None,
        "reveal": (
            _reveal_state(room, rnd, _roster_labels(room))
            if room.phase == Room.Phase.RATING and rnd is not None
            else None
        ),
        "summary": (
            _summary_state(room, rnd) if room.phase == Room.Phase.SUMMARY and rnd is not None else None
        ),
    }
    return state


def _maybe_open_playing(room: Room, online: set[int]) -> None:
    """Open the game once every player who is there has answered the whole didacticiel.

    Same bar as the round close, and for the same reason: `room.players` is a phone that slept,
    not a person who is going to answer. The count is the room's OWN online players rather than
    `len(online)`, so a presence entry left behind by a player deleted mid-quiz cannot make the
    comparison unreachable and pin the room on the gate forever.

    The question count is read from this room's bank rather than from `quiz.QUIZ_LENGTH`, so a
    bank the admin has grown past eight is not gated on a hardcoded eight — and it is read off
    `room.questions`, so a second room's eight questions cannot hold this room's gate shut.
    """
    total = room.questions.count()
    if not total:
        return  # nothing was materialised, so there is no gate to pass
    players = list(room.players.filter(pk__in=online))
    if not players:
        return  # every presence entry is a player this room does not have: `all([])` is True
    if all(p.quiz_attempts.count() == total for p in players):
        room._go(Room.Phase.PLAYING)


def _maybe_advance_reveal(room: Room, rnd, online: set[int]) -> None:
    """Walk the reveal when everyone who is here and may rate this prompt has rated it.

    THE AUTHOR IS NOT A RATER. They are forbidden to rate their own prompt, so counting them
    would make the condition unreachable — the round would wait forever for a note the game
    refuses to accept. So the bar is the online players MINUS the author of the prompt on screen.

    And when that set is EMPTY — the author is the only person connected, so there is nobody the
    game is waiting for — the phase must NOT advance. `all([])` is `True`, and that is the same
    vacuous truth this file already refuses twice: a room whose every phone dropped must not run
    itself, and neither must a room whose only awake player is the one who cannot vote.

    The last prompt settles the round on the way to the summary, so the summary a player lands on
    already carries the four numbers. `SUMMARY` then stays put: the round gets commented on, and
    that is the admin's button, not a race between twenty phones.
    """
    answers = list(rnd.answers.order_by("id"))
    if rnd.revealed >= len(answers):
        return  # nothing to reveal: the admin's button decides, not a rating nobody can give
    target = answers[rnd.revealed]
    raters = online - {target.player_id}
    if not raters:
        return
    voted = set(target.ratings.filter(rater_id__in=raters).values_list("rater_id", flat=True))
    if voted != raters:
        return
    if rnd.revealed + 1 < len(answers):
        rnd.revealed += 1
        rnd.save(update_fields=["revealed"])
    else:
        room._go(Room.Phase.SUMMARY)
        settle_round(room)


def maybe_advance_phase(room: Room) -> None:
    """Move the game on if the players who are there have done what the phase is waiting for.

    Three phases advance on their own: the didacticiel, once everybody online has answered the
    eight questions; the round, once everybody online has written a prompt; and the rating, one
    prompt at a time. The others wait for the admin — a round gets commented on, and nothing
    should move the game while it is.

    The bar is the online players, not `room.players`: a phone that slept or lost wifi is still a
    row in that table forever, so the old comparison waited on someone who was never coming back and
    the whole party sat on a round it could not leave. A player who reconnects mid-round is back in
    the count on their next beat, and `answer` does not require presence, so a late arrival can
    still answer the round that is open when they get back.

    Renamed from `maybe_advance` because it is no longer "maybe the round": it dispatches on the
    phase now, and there are three things it can advance.
    """
    if room.phase not in (Room.Phase.QUIZ, Room.Phase.PLAYING, Room.Phase.RATING):
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
    if room.phase == Room.Phase.RATING:
        _maybe_advance_reveal(room, rnd, online)
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
        # THE CRITIC'S CALL BELONGS HERE, NOT IN `settle_round`. It is the third billed call of the
        # submit the player is already waiting on: each player's own request pays for their own
        # critic, and `runserver` is threaded, so twenty phones are graded concurrently. In
        # `settle_round` it would be twenty back-to-back calls inside ONE request, fired by the
        # last player's rating tap — a single HTTP call blocking for the better part of a minute,
        # on the phase where every phone is waiting for a snapshot that will not come. Same total
        # spend, a fraction of the latency, and settlement stays arithmetic: free and instant.
        #
        # The critic's COMMENT IS DISCARDED, and that is a decision rather than an oversight. The
        # frozen `SummaryRow` has no room for it, and a finished UI is already coded against that
        # type: adding a field now would change a contract the frontend reads. The summary tells
        # the story with the four numbers it already carries — the critic's score, the peers'
        # mean, the bounded quality and the round's note — which is what a player can act on. The
        # comment was the only piece of prose about method that is not on screen, and the didacticiel
        # is where method is taught.
        #
        # The target travels with the prompt so the critic knows what the prompt is FOR, and the
        # rubric is what forbids it from comparing anything: it never sees the answer it produced,
        # nor a single peer rating, so it cannot be pulled by either.
        critique = llm.grade_prompt(prompt, rnd.target)
        # The durable backstop, for the case the process-local claim cannot see: another
        # process won unique_together. `transaction.atomic()` is defensive here — with
        # ATOMIC_REQUESTS unset, autocommit already rolls back the single failed statement, so
        # it changes nothing today. It is what makes the `except` reachable at all under
        # ATOMIC_REQUESTS, where an IntegrityError would otherwise poison the rest of the
        # request. Losing the race is the same 409 the player already got, not a 500.
        try:
            with transaction.atomic():
                rnd.answers.create(
                    player=player,
                    prompt=prompt,
                    output=output,
                    score=verdict.score,
                    comment=verdict.comment,
                    quality_llm=critique.score,
                )
        except IntegrityError:
            return json_error("already answered", 409)
    finally:
        _inflight.discard(claim)
    maybe_advance_phase(room)
    return JsonResponse({"score": verdict.score, "comment": verdict.comment, "output": output})


@require_POST
def rate(request: HttpRequest, code: str) -> JsonResponse:
    """Give the prompt currently on screen a score out of ten.

    THE BODY CARRIES A TOKEN AND A SCORE, AND NOTHING ELSE. The target is derived here, from
    `room.current_round()` at the round's `revealed` cursor — so the client never names what it is
    rating, and that is the security property the whole feature rests on. A body carrying an answer
    id would have to be checked against three forgeries: voting on your own prompt, skipping ahead
    to a prompt you like better, and rating the same prompt twice by lying about the index.
    Deriving the target makes all three unrepresentable rather than merely rejected, which is why
    `Rating.save()`'s self-rating guard is a backstop here and not the enforcement.

    The score is NOT returned with the progress it produced. The rating screen reads its "3 notés"
    from the `reveal` block of the next SSE frame, which is the same source every other progress
    indicator in the game comes from; answering with the count as well would give the client two
    truths about one number and a way to disagree with the snapshot. `{"ok": true}` is the whole
    contract, and it is enough: the next frame is at most half a second away.
    """
    room = get_object_or_404(Room, code=code)
    if room.phase != Room.Phase.RATING:
        return json_error("not rating", 409)
    data = body(request)
    try:
        # Scoped to the room as well as the token, like `answer`: a token from another room must
        # not vote in this one.
        player = Player.objects.get(room=room, token=data.get("token", ""))
    except Player.DoesNotExist:
        return json_error("bad token", 403)
    rnd = room.current_round()
    answers = list(rnd.answers.order_by("id")) if rnd is not None else []
    # Nothing behind the cursor — no round at all, or a round nobody answered — is the same answer:
    # there is no prompt on screen, so there is nothing to send a score about. 409 rather than an
    # IndexError, which would be a 500 on the one phone that is still awake.
    if not answers or rnd.revealed >= len(answers):
        return json_error("not rating", 409)
    target = answers[rnd.revealed]
    if target.player_id == player.pk:
        return json_error("your own prompt", 409)
    score = data.get("score")
    # `isinstance(True, int)` is True, so a JSON `true` is an int that would pass the type check
    # and then fail the `Rating` check constraint as a 500 instead of a 400. Same guard as the
    # quiz's `choice`, and the same reason: a boolean is not a score of 1.
    if (
        not isinstance(score, int)
        or isinstance(score, bool)
        or not prompts.SCALE_MIN <= score <= prompts.SCALE_MAX
    ):
        return json_error("score 1-10", 400)
    # Checked here and in the database. The view's check is the one a player waits on; the
    # constraint is the one that survives a race between two taps on a phone that answered twice
    # before either request came back.
    if Rating.objects.filter(answer=target, rater=player).exists():
        return json_error("already rated", 409)
    try:
        with transaction.atomic():
            Rating.objects.create(answer=target, rater=player, score=score)
    except IntegrityError:
        return json_error("already rated", 409)
    maybe_advance_phase(room)
    return JsonResponse({"ok": True})


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

    Scoped to this room's bank: `order` is only meaningful inside a room, so returning every
    room's rows would hand this player questions numbered 0..7 that are not the ones `_quiz_post`
    will grade them against.
    """
    room = get_object_or_404(Room, code=code)
    return JsonResponse(
        {
            "questions": [
                {"order": q.order, "prompt": q.prompt, "choices": q.choices}
                for q in room.questions.order_by("order")
            ]
        }
    )


def _quiz_post(request: HttpRequest, code: str) -> JsonResponse:
    """Answer one question, and reveal it.

    The response is `{"correct", "correct_index", "explanation", "score"}` and the whole point of it
    is the revelation: the explanation travels in the SAME response that tells the player whether
    they were right, which is why `QuizQuestion.explanation` is stored rather than read from `quiz.py`
    at answer time — it belongs to the exact row the player was shown, so an edited bank cannot
    retroactively change the reason a past game gave.

    The client names a question by its `order` and a choice by its index, and sends nothing else
    that is checked: the player is the token, the question is this room's row with that order, the
    key is the row's.
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
    # `(room, order)` and not `order` alone: `order` is unique WITHIN a room, so an unscoped
    # lookup would grade this player against whichever room's row happened to come first — an
    # order that exists in this room and not in that one, a question they were never shown, and a
    # score for it.
    question = room.questions.filter(order=order).first()
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
    maybe_advance_phase(room)
    return JsonResponse(
        {
            "correct": correct,
            # The screen marks the right choice after the reveal, and the client never holds the
            # key, so the index has to travel with the answer. Without it the reveal can only say
            # "wrong" and cannot point at the good one, which is the half of the QCM that teaches.
            "correct_index": question.correct,
            "explanation": question.explanation,
            "score": player.quiz_score(),
        }
    )
