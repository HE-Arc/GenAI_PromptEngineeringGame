import json
import logging
import queue
import secrets
import string
import threading
import time
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, close_old_connections, transaction
from django.db.models import Count, Sum
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from game import llm, prompts
from game.models import (
    RATE_SECONDS,
    RATE_SECONDS_RANGE,
    ROUND_SECONDS,
    ROUND_SECONDS_RANGE,
    Player,
    QuizAttempt,
    QuizQuestion,
    Rating,
    Ready,
    Room,
    Round,
)
# Named `pick_questions`, not `pick`, and imported as a name rather than as the `game.quiz`
# module: the didacticiel's draw is a PERMUTATION of a bank exactly one quiz long, and a reader
# who sees two things called `pick` in this header has to go and check which bank each is on.
# It is also the seam a test patches (`game.views.pick_questions`) for the same reason
# `game.views.pick` is: patched where it is USED.
from game.quiz import pick as pick_questions
from game.targets import ROUNDS_PER_GAME, image_for, pick

_ALPHABET = string.ascii_uppercase.replace("O", "").replace("I", "")

# In-flight (round, player) submits, claimed for the duration of the two billed model calls.
# PROCESS-LOCAL, and that is sufficient for this deployment: compose starts one threaded
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

# HOW LONG A ROOM MAY SIT WITH NOBODY IN IT before the admin's list stops showing it, and — for a
# room nobody ever played — before the sweep DELETES it. Ten minutes, and it is a named constant
# because the number appears in two different questions ("how stale is too stale" for the sweep,
# "how long is the floor after a restart" below) and a literal in both would be two policies
# pretending to be one.
#
# The floor idea is the reason this is not simply "ten minutes since the last beat": a teacher's
# admin screen is closed more often than it is open, so an install that is not in use all day
# accumulates rooms and the list would be pages long the next morning. Ten minutes of a classroom
# with nobody streaming is not a game in progress, it is a lobby somebody walked out of.
ROOM_IDLE_TTL = 600.0

# HOW LONG A ROOM THAT WAS PLAYED survives with nobody in it. This is a DELETE, not a hide: past
# that delay the row goes and the players' tokens go with it, so a teacher who comes back the next
# day does not find their game. Thirty minutes idle and the room is gone, played or not — one
# threshold for every room, measured from the last heartbeat the sweeper saw.
#
# THE FALLBACK, in practice: `Room.last_activity_at` is what the delete actually reads, and this
# number only judges a room the sweeper has never seen alive — a game started and abandoned with no
# player ever connected — where the age of the game is the only fact there is.
ROOM_KEEP_TTL = 1800.0
# The sweeper's tick. Short enough that a room leaves the list shortly after its last player leaves,
# long enough that a tick is free: the sweep reads one table and writes only for the rooms where
# somebody is connected.
SWEEP_EVERY = 60.0

# Named `logger` and used for exactly one thing: a tick of the sweeper that failed. A sweep that
# failed silently would be a sweep nobody notices has stopped cleaning anything.
logger = logging.getLogger(__name__)

# WHEN THIS PROCESS STARTED, on the same monotonic clock as the beats, captured ONCE at import.
#
# THE FLOOR FOR EVERY ROOM WITH NO BEATS, and the reason it exists is the clock itself:
# `time.monotonic()` is an arbitrary instant chosen by the OS, it is only meaningful inside one
# process, and it has no relation to yesterday. So a room whose last beat was recorded by the
# previous run has, in this run, a beat that is either impossibly far in the past or — after a
# reboot — not in the registry at all. Sweeping on the raw difference would therefore declare EVERY
# room the server has ever hosted idle on its first list poll after a restart, and delete ten
# minutes of yesterday's classrooms because someone rebooted a laptop.
#
# Treating the boot instant as the oldest possible beat means nothing is sweepable until this
# process has itself been up for `ROOM_IDLE_TTL`: a restart cannot lose a room, it only postpones
# the question by ten minutes. The cost is the obvious one and is the right way round — a room that
# was abandoned before the reboot survives one boot's grace period instead of being cleaned up on
# the spot.
_BOOT_INSTANT = time.monotonic()

# A lock is genuinely required here, unlike `_inflight`. Every stream thread writes this dict twice
# a second, there is one thread per connected phone, and `room_state` reads it from all of them at
# once; a dict mutated during another thread's iteration raises. It is held only for the few
# statements below, never across a query or a sleep, so it cannot serialise the streams themselves.
_presence: dict[tuple[str, int], float] = {}
_presence_lock = threading.Lock()

# When each room was created or relaunched, on the monotonic clock, under `_presence_lock`. It is
# the idle clock's starting point for a lobby no phone has streamed yet: without it the sweep fell
# back to `_BOOT_INSTANT`, so on a server up for more than `ROOM_IDLE_TTL` a room created a second
# ago already looked ten minutes idle and was deleted before anyone could join it.
_opened: dict[str, float] = {}


def _mark_opened(code: str) -> None:
    with _presence_lock:
        _opened[code] = time.monotonic()


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


def _last_beats() -> dict[str, float]:
    """The most recent beat in each room that has one, on the monotonic clock. One locked read of
    the whole registry, so the sweep below takes the lock once for every room rather than once per
    room.

    AGES ARE NOT JUDGED HERE, deliberately: this returns the last beat whether it is half a second
    or half a day old, and the caller decides what "idle" means. A stale entry is still the last
    thing that happened in that room — pruning ages out of the dict on its own schedule, and a room
    whose only beat is old is exactly the room this is looking for.

    `_BOOT_INSTANT` IS NOT THE DEFAULT HERE: a room with no beats at all is simply absent from the
    mapping, so the caller applies the floor and the two decisions stay apart.
    """
    with _presence_lock:
        # The opening instant counts as the room's first beat, so a fresh lobby has its whole
        # grace period; any later player beat supersedes it.
        last: dict[str, float] = dict(_opened)
        for (room_code, _pid), seen in _presence.items():
            if room_code not in last or seen > last[room_code]:
                last[room_code] = seen
    return last


def _sweep_plan(
    rooms: list[dict], beats: dict[str, float], boot: float, now: float, wall: datetime
) -> tuple[set[str], list[str], set[str]]:
    """(codes to hide, codes to delete, codes whose activity must be written).

    THREE OUTCOMES, AND THE LAST ONE IS IRREVERSIBLE. A room idle for `ROOM_IDLE_TTL` disappears
    from the list; a lobby never started disappears from the database; a PLAYED game only
    disappears after `ROOM_KEEP_TTL`, because it holds the players' tokens and the rounds somebody
    may want back at the price of a break.

    PURE — no query, no clock read, no write. Every fact it needs arrives as an argument, which is
    what lets the admin-list path and the thread path share ONE decision instead of two rules that
    drift apart the first time one of them is edited. `beats` is the last heartbeat per room on
    the monotonic clock and `boot` is the floor for a room with no beats at all; `now` is the same
    monotonic instant, and `wall` is the SAME MOMENT ON THE WALL CLOCK.

    THE TWO CLOCKS ARE BOTH ARGUMENTS on purpose. Heartbeats are monotonic because a phone cannot
    make the system time step backwards, and a persisted instant cannot be monotonic because a
    monotonic value read in a previous process means nothing in this one. Comparing the one with
    the other is not a conversion, it is a bug — so the caller reads both and passes both.

    THE DELETE DECISION OF A PLAYED ROOM READS THE DURABLE COLUMN, not `beats`. `last_activity_at`
    is what the sweeper itself wrote on an earlier tick, so it is the only instant that carries a
    thirty-minute threshold across a restart; without it the threshold would be unreachable on the
    second run and the table would grow anyway. `started_at` is the fallback for a room the sweeper
    has never seen alive, and a `None` there means "we never knew anything about this room", in
    which case the age of the game is the only fact left.
    """
    hide: set[str] = set()
    dead: list[str] = []
    active: set[str] = set()
    for room in rooms:
        code = room["code"]
        # The floor, read as a module global so it stays the boot instant rather than a copy taken
        # at import: `beats.get(code, _BOOT_INSTANT)` is the whole of "this process has been up for
        # less than ROOM_IDLE_TTL, so nothing is sweepable yet".
        seen = beats.get(code)
        # A LIVE BEAT IS NOT IDLE AND THAT IS THE STRUCTURAL GUARANTEE, not a careful check: a
        # heartbeat inside `PRESENCE_TTL` — three seconds, two hundred times shorter than the idle
        # TTL — puts the room in neither `hide` nor `dead`, so it never reaches a delete line even
        # if its stamps are years old. The sweep's own activity write is what keeps
        # `last_activity_at` young for exactly these rooms.
        #
        # ONLY A ROOM THAT HAS A BEAT AT ALL, and the membership test is the point: the boot floor
        # below is `time.monotonic()` minus a few seconds on a process that just started, so folding
        # it in here would call EVERY room live on the first tick after a restart, stamp
        # `last_activity_at` on the whole table, and push the thirty-minute deadline out for
        # every game this server has ever hosted. The column would be written by the restart it was
        # invented to survive.
        if seen is not None and now - seen <= PRESENCE_TTL:
            active.add(code)
            continue
        if now - (seen if seen is not None else boot) <= ROOM_IDLE_TTL:
            continue
        hide.add(code)
        if room["started_at"] is None:
            dead.append(code)
            continue
        # `or`, not a null check: the column is null until a tick has seen somebody connected, and
        # a game started and abandoned in the same minute has only its own age to be measured by.
        last = room["last_activity_at"] or room["started_at"]
        if wall - last > timedelta(seconds=ROOM_KEEP_TTL):
            dead.append(code)
    return hide, dead, active


def _delete_rooms(codes: list[str]) -> None:
    """Delete these rooms, or nothing at all when the list is empty.

    DELETING CASCADES ON PURPOSE, and the cascade is what makes one row enough: `QuizQuestion` and
    `Player` hang off `Room`, so the bank and the tokens go with the room rather than accumulating as
    orphans that a later room could never collide with but a database would carry forever. The empty
    guard keeps a tick that found nothing dead — the overwhelming majority, and every one of them
    before this feature existed — at zero queries.
    """
    if codes:
        Room.objects.filter(code__in=codes).delete()
        with _presence_lock:
            for code in codes:
                _opened.pop(code, None)


def _sweep_idle_rooms(rooms: list[dict]) -> set[str]:
    """Hide every room nobody has been in for `ROOM_IDLE_TTL`, and DELETE the ones nobody ever
    played or that nobody has touched for thirty minutes. Returns the codes to leave out of the answer.

    IT TAKES THE ROOMS RATHER THAN QUERYING FOR THEM, and that is a budget, not a convenience:
    `admin_list_rooms` already enumerates every room with one query, and a second enumeration of
    the same table on the same request would be paying twice for one fact — and it is a fact the
    list's own cost test pins. Each row is `admin_list_rooms`'s `.values(...)` dict and must carry
    `code`, `started_at` and `last_activity_at`; nothing else is read.

    READ-ONLY APART FROM THE DELETE, and the third element of the plan is discarded on purpose.
    `admin_list_rooms` is a GET: a console polls it every few seconds, and having it stamp
    `last_activity_at` too would mean recording the same instant an order of magnitude more often
    than the sweeper does, for a value only ever compared against `ROOM_KEEP_TTL` — a minute of
    staleness cannot change a thirty-minute verdict. It would also make "one query per GET" hold only
    while nobody is connected. `_sweep_tick` is the single writer instead.

    THE THIRD ELEMENT IS STILL COMPUTED because the decision is one function and not two: a list
    path with its own copy of the rule is a second rule, and the two drift apart the first time one
    of them is edited.
    """
    hide, dead, _active = _sweep_plan(
        rooms, _last_beats(), _BOOT_INSTANT, time.monotonic(), timezone.now()
    )
    _delete_rooms(dead)
    return hide


def _sweep_tick() -> None:
    """One tick of the sweeper, and the ONLY writer of `Room.last_activity_at`.

    The console polls `/api/admin/rooms` every few seconds while a game is live and this runs once
    a minute, so a write on both paths is a write an order of magnitude too often for a number
    whose only reader compares it to thirty minutes. Which is why the recording is HERE and not in
    `_sweep_idle_rooms`: one writer, and it is the writer that runs when nobody is looking.

    THE INSTANT JUDGED IS THE INSTANT WRITTEN, `wall` being read once and passed to both, so the
    column can never be a tick older than the decision that read it — an off-by-one-tick staleness
    would be harmless, but a rule that cannot be read off the value it wrote is a rule nobody can
    reason about.

    `hide` is computed and dropped: hiding is a property of the ANSWER, and this thread has no
    answer to filter. A room nobody is in leaves the list on the console's next poll, whether or not
    this tick is the one that noticed.
    """
    wall = timezone.now()
    _hide, dead, active = _sweep_plan(
        list(Room.objects.values("code", "started_at", "last_activity_at")),
        _last_beats(),
        _BOOT_INSTANT,
        time.monotonic(),
        wall,
    )
    # ONE BULK UPDATE FOR THE TICK, never one per room and never one per heartbeat: thirty players
    # beating twice a second would be sixty writes a second for a number only this loop reads.
    if active:
        Room.objects.filter(code__in=active).update(last_activity_at=wall)
    _delete_rooms(dead)


def _sweep_forever() -> None:
    """Sweeps for as long as the process lives.

    `close_old_connections` at both ends: this thread is attached to no request, so nothing closes
    its connection for it, and without that SQLite keeps a descriptor open per tick.

    THE CYCLE IS SWEEP THEN SLEEP, never the other way round: sweeping first lets the connection of
    a tick's rest go stale rather than reopening it every time the thread wakes up.

    AND IT LIVES IN THE SERVER PROCESS, which is the only place it could live. `_last_beats` reads
    `_presence`, a dict in this process's memory: a `manage.py` command or a cron would see no
    heartbeat at all, call every room idle, and delete games that are being played right now.

    IT SWALLOWS EVERY EXCEPTION. A sweeper that dies takes with it the only thing cleaning the
    table, and the symptom — a database that slowly fills up — would look like the feature never
    having worked rather than like a thread that crashed.
    """
    while True:
        try:
            close_old_connections()
            _sweep_tick()
        except Exception:  # noqa: BLE001
            logger.exception("room sweep failed")
        finally:
            close_old_connections()
        time.sleep(SWEEP_EVERY)


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


def _player_for_token(room: Room, data: dict) -> tuple[Player | None, JsonResponse | None]:
    """The player `data`'s token names IN THIS ROOM, or `(None, refusal)` when it names nobody.

    A token is a credential only within the room it was minted in, so the lookup is scoped on the
    room as well as the token: one from another room must not answer, rate or score in this one.
    Every endpoint that acts for a player asks the question the same way, and the answer is the
    same refusal each time, so it is settled here once rather than in each of them.

    The refusal is RETURNED, not raised, because every caller needs the player on the way through:
    returning it keeps each call site a lookup and a two-line guard, where raising would wrap the
    whole rest of the view in a `try` to catch something it did not throw.
    """
    try:
        return Player.objects.get(room=room, token=data.get("token", "")), None
    except Player.DoesNotExist:
        return None, json_error("bad token", 403)


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


def _seconds(value, default: int, bounds: tuple[int, int]) -> int:
    """A time limit from the admin's form: the default when absent or unreadable, and held
    inside `bounds` otherwise, so a typo cannot make a round last a second or a day."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    lo, hi = bounds
    return max(lo, min(hi, n))


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
        round_seconds=_seconds(data.get("round_seconds"), ROUND_SECONDS, ROUND_SECONDS_RANGE),
        rate_seconds=_seconds(data.get("rate_seconds"), RATE_SECONDS, RATE_SECONDS_RANGE),
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
    _mark_opened(code)
    return JsonResponse({"code": code})


# What the room list is ordered by, and WHY IT IS NOT NEWEST-FIRST. `Room` has no `created_at` —
# only `Player` does — and a migration to sort a table of room codes by age is the wrong price to
# pay for that, so the order is by NEED instead: a room whose game is running is the one an admin
# is about to press a button on, a lobby is waiting on a host, and a finished game is over. Ties
# break on `code`, which is the primary key — unique, and stable, which is the part that matters:
# without it two rooms in the same phase come back in whatever order the database happened to hand
# them over and the list reshuffles under the admin's thumb on every refresh. A phase absent from
# the table sorts last, so adding one to `Room.Phase` is a decision here rather than a silent
# ordering.
_PHASE_RANK = {
    Room.Phase.QUIZ: 0,
    Room.Phase.PLAYING: 0,
    Room.Phase.RATING: 0,
    Room.Phase.SUMMARY: 0,
    Room.Phase.WAITING: 1,
    Room.Phase.DONE: 2,
}


@require_GET
def admin_list_rooms(request: HttpRequest) -> JsonResponse:
    """Every room that exists, one flat entry each: enough for a console to choose one and to come
    back to it, and nothing else.

    THE LIST IS GLOBAL, and that is a consequence rather than a decision. `require_admin` tests ONE
    boolean in the session: there is no admin identity in this codebase, no owner column and no
    `created_by`, so there is nothing to scope the list by. Every admin sees every room and may
    start or advance any of them — the same authority the per-room admin routes already grant,
    since they check the same boolean. Inventing an admin model to narrow this would change what
    the other routes authorise and would be a much larger change than the feature.

    COUNTED, NOT SNAPSHOTTED, and that is the other arbitration. A `room_state` per room would be
    the tempting reuse — it is right there and it answers the same questions — but each call builds
    a roster, two aggregates PER PLAYER, the quiz bank, the reveal and the whole summary, so ten
    rooms means ten full snapshots (scores, rating state, every prompt a class has written) to
    render a table of codes, on a list the console re-reads as often as it likes. Nothing here
    asks a question `room_state` is the only place to answer, so the two room columns are counted
    in ONE query each and the entry is eight scalars.

    `distinct=True` on both counts is not optional: `rounds` and `players` are two multi-valued
    relations and one join would multiply them together, so a room with 10 rounds and 6 players
    would report 60 of each. Two queries for the whole list — the rooms, and the online players of
    every room at once — rather than two per room.

    `online_count` is `online_player_ids`, the same helper and the same TTL as every other view, and
    it is intersected with the roster rather than counted off the presence dict directly: a beat
    left behind by a player deleted mid-quiz must not claim a phone in a room that has none. Same
    reason `room_state` counts `online_count` off its per-player flags.

    The phase is the RAW string, not a label. The words are the client's to choose.

    IT ALSO SWEEPS, on the way past, so that the answer a console reads is already the answer AFTER
    the cleanup: one request does both jobs, and a caller never sees a room the same GET is
    deleting. Sweeping HERE IS NOT THE ONLY SWEEP — `runserver` starts a daemon thread that runs
    `_sweep_tick` every `SWEEP_EVERY` seconds, which is what cleans a table nobody is looking at and
    what records `last_activity_at`. This GET keeps the call because a console must not show a room
    it is about to delete even if the thread has not run yet; both go through `_sweep_plan`, so they
    cannot disagree, and neither of them records the activity instant.

    THE THREAD IS NOT A `manage.py` LOOP, and that is the whole reason it is not: it reads
    `_presence`, which is this process's memory, so a second process would see no heartbeat
    anywhere and delete games in progress. That is also why it is behind `GENAI_SWEEP` — `ready()`
    runs under `manage.py test` too, and a deleter thread under the suite is a suite whose result
    depends on the wall clock.
    """
    if (err := require_admin(request)):
        return err
    rooms = list(
        Room.objects.annotate(
            players_count=Count("players", distinct=True), total=Count("rounds", distinct=True)
        ).values(
            "code", "phase", "current", "tutorial", "anonymous", "players_count", "total",
            "started_at", "last_activity_at",
        )
    )
    # The sweep runs between the enumeration and the answer, on the rows just read, so this GET both
    # sweeps and answers: the codes it deletes or hides are filtered out of the list it is about to
    # build, and a caller never sees a room the same request is deleting. `started_at` and
    # `last_activity_at` are selected here for that reason and are not in the payload — they are the
    # sweep's inputs, not the console's.
    idle = _sweep_idle_rooms(rooms)
    # One presence read per room, not one query: the registry is a process-local dict of heartbeats,
    # so this is an in-memory filter rather than the N+1 the room rows above would otherwise be.
    online = {room["code"]: online_player_ids(room["code"]) for room in rooms}
    connected = set().union(*online.values())
    # One query for the whole list, so a room nobody is in costs nothing.
    present = Counter(Player.objects.filter(pk__in=connected).values_list("room__code", flat=True))
    rooms.sort(key=lambda r: (_PHASE_RANK.get(r["phase"], len(_PHASE_RANK)), r["code"]))
    # The idle ones go out of the ANSWER, not out of existence: for a played room this is the only
    # thing that happened, and a player who reconnects is back in the next poll with the game
    # exactly as they left it.
    rooms = [room for room in rooms if room["code"] not in idle]
    return JsonResponse(
        {
            "rooms": [
                {
                    "code": room["code"],
                    "phase": room["phase"],
                    "current": room["current"],
                    "total": room["total"],
                    "online_count": present[room["code"]],
                    "players": room["players_count"],
                    "tutorial": room["tutorial"],
                    "anonymous": room["anonymous"],
                }
                for room in rooms
            ]
        }
    )


@require_http_methods(["GET", "POST"])
def admin_rooms(request: HttpRequest) -> JsonResponse:
    """The one pattern behind `api/admin/rooms`: GET lists the rooms, POST creates one.

    TWO METHODS ON ONE PATTERN, and for the reason `quiz` is two methods on one pattern: the
    resolver returns the FIRST pattern that matches the path and never looks at the method, so a
    GET route registered under the same URL as the POST create would either swallow the creation
    or be unreachable behind it. Both halves delegate to the view that owns their own method, so
    `admin_create_room` keeps its `@require_POST` and this stays a dispatcher with no logic of its
    own.
    """
    if request.method == "GET":
        return admin_list_rooms(request)
    return admin_create_room(request)


def _start_game(room: Room) -> bool:
    """Draw the ten rounds, open the didacticiel and leave the lobby. True when it started, False
    when the room had already begun.

    EXTRACTED FROM `admin_start` AND NOT FROM ANYTHING ELSE, because the class can now open its own
    game and the two writers have to be the same code: a start that draws ten targets in one path
    and goes to `playing` in the other is a game whose opening phase depends on who pressed which
    button. `admin_start` keeps its session check and its 409 — it maps this False back to
    « already started », so the admin's double press still refuses and so a vote that wins the race
    is reported to a teacher as a room that has already begun, which is what it is.

    FALSE MEANS `rounds.exists()`, and that table is the definition of a started room rather than a
    field on `Room`: the phase alone says `waiting` after a restart whose rounds were deleted, and a
    second ten-round draw on top of a half-played game is the failure this check exists to stop.

    ATOMIC, WITH THE RE-CHECK INSIDE IT, because `runserver` is threaded and the admin's press on
    « lancer » and the last player's vote are now the same transition landing at once. Both writers
    re-read `rounds` inside the transaction, and `unique_together` on `(room, order)` is the last
    word: whichever loses raises `IntegrityError` and is caught as the admin having won the race,
    which is a no-op rather than a second set of rounds. The inner `atomic` is a SAVEPOINT and not
    tidiness — without it the caught error would poison the enclosing transaction and the `return`
    would fail on the way out. The transaction pattern is `admin_restart`'s, for the same reason.

    THE TEN TARGETS ARE DRAWN HERE AND NOW. Drawing them at the moment the game commits is the one
    instant nobody is waiting on anything, and it makes the target set fixed for the whole game —
    which is what « the same ten rounds for everybody » has to mean. With a tutorial, the rounds
    sit behind the quiz until the room advances out of it.

    THE FIRST QUIZ QUESTION IS PUT ON SCREEN HERE, in the same write that opens the phase, and its
    thirty-second window opens with it. The last player to press « prêt » in the lobby is answered
    with thirty seconds on question 0 rather than an empty screen and a button to find — the same
    promise the admin's button has always made, now kept by the same line. Every later question is
    opened by `_advance_quiz`, and both paths go through `Room._open_quiz`, so the two cannot
    disagree about how long a window lasts.
    """
    with transaction.atomic():
        if room.rounds.exists():
            return False
        try:
            with transaction.atomic():
                Round.objects.bulk_create(
                    [
                        Round(room=room, order=i, target=t)
                        for i, t in enumerate(pick(ROUNDS_PER_GAME))
                    ]
                )
        except IntegrityError:
            # THE OTHER WRITER GOT THERE FIRST, and that is a success for the room: one game, ten
            # rounds, opened by whoever pressed first. The vote path treats this as a no-op and the
            # admin's button reports « already started », which is exactly the truth.
            return False
        if room.tutorial:
            room._open_quiz()
        room._go(Room.Phase.QUIZ if room.tutorial else Room.Phase.PLAYING)
        # THE STAMP IS WRITTEN HERE, AFTER THE ROUNDS EXIST AND ONCE PER GAME, because this is the
        # only place a game is ever opened and the idle sweep may only delete what was never opened.
        # `rounds.exists()` cannot say that on its own — `admin_restart` deletes the rounds of a
        # game that WAS played and puts the room back in the lobby — so the fact lives in a column
        # nothing ever takes back.
        #
        # AFTER `_go` AND NOT INSIDE IT, so the phase transition stays the single write `_go` owns;
        # and `update_fields` rather than a bare `save()`, for the reason every other write in this
        # file says it. `_go` has just saved, so the instance is current and this touches one field.
        room.started_at = timezone.now()
        room.save(update_fields=["started_at"])
    return True


@require_POST
def admin_start(request: HttpRequest, code: str) -> JsonResponse:
    """« Lancer la partie. » The admin's own button, and one of two ways into `_start_game`.

    THE SESSION IS STILL REQUIRED HERE, and it is worth being explicit that the vote path did not
    weaken it: a player is allowed to press « prêt » in the lobby, which opens the game, and is NOT
    allowed to press the admin's button, which is a room-management verb with no player-facing
    equivalent. Creation, restart and start stay behind the password because those are the acts
    that configure a room rather than take part in one.
    """
    if (err := require_admin(request)):
        return err
    room = get_object_or_404(Room, code=code)
    if not _start_game(room):
        return json_error("already started", 409)
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

    TWO ROUNDINGS, AND THEY ARE THE SAME ONE. `quality_peers` and the `quality` it feeds both go
    through `prompts.half_up`, because the summary prints them side by side and `Game.tsx` tells
    the class to check that arithmetic: a peer mean of exactly 6.5 used to settle to 6 here while
    `prompts.quality` — reading the same rows — returned 7, so the screen said « moyenne des pairs
    6 » beside « qualité retenue 7 » under a formula printed beneath both. One rule, one
    function, no way for the two columns to disagree.
    """
    rnd = room.current_round()
    if rnd is None:
        return
    for a in rnd.answers.order_by("id"):
        scores = list(a.ratings.values_list("score", flat=True))
        a.quality_peers = prompts.half_up(sum(scores) / len(scores)) if scores else None
        a.quality = prompts.quality(a.quality_llm, scores)
        # THE SPEC'S MEAN, EXACTLY: §7 says `note_tour = (proximité + qualité) / 2`, and the `/`
        # is a real division, not an integer one. The `// 2` that used to stand here was the
        # implementation quietly deciding a mean of two out-of-ten integers is a whole number, and
        # it charged every game for it: ten rounds of proximity 7 and quality 6 settled to 60 on a
        # scale the final screen still calls « sur 100 », where the spec's own arithmetic gives 65.
        # The column is a FloatField for this one line.
        a.round_score = (a.score + a.quality) / 2
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


def _advance_quiz(room: Room) -> None:
    """The admin's "question suivante": open the next question, or the game once the eighth is
    behind them.

    ONE BUTTON FOR BOTH CASES, which is the spec's own sentence (§2) and the reason this is a
    function rather than a lambda on the table below: « Le même bouton referme la question en cours
    quand il est pressé avant l'échéance » and « Après la huitième révélation, le même bouton
    ouvre la partie ». An admin interface that had to know which of the two it was would be an
    admin interface a teacher has to read while a class waits.

    THE PRESSED-BEFORE-THE-DEADLINE PATH IS THE SAME CODE, and that is deliberate rather than
    incidental. `_next_quiz` opens question n+1 with a fresh window; question n is never
    revealed on the way out, because the reveal is derived from a deadline that nothing writes
    when the admin skips ahead. A class that has understood the question moves on; a class that
    has not keeps the thirty seconds they had.

    Nothing here reads a player's answer. The clock opens and closes the window, and the game is
    moved by a person: « le chronomètre ne fait qu'ouvrir et fermer la fenêtre de réponse ; il ne
    fait pas avancer la partie ».
    """
    if not room._next_quiz():
        room._go(Room.Phase.PLAYING)


# What the admin's single "advance" button means in each phase. A phase absent from this table has
# no advance: 409. Written as data so the six cases are readable at a glance, and so adding a phase
# forces a decision here instead of defaulting to something arbitrary — `done` has nothing left,
# and it would otherwise be whatever the last `else` happened to be.
#
# `waiting` IS DELIBERATELY ABSENT, and it now HAS something to advance into: the class opens its
# own game from the lobby. It stays out because this table is « suivant », and opening the game is
# not « suivant » — it is the verb `admin_start` owns, with its own « already started » on a room
# that has begun. An entry here would hand a teacher a second button that starts a game twenty
# phones can also start, and this table is the one place that says what the admin's button means:
# two ways to start, one of them a coincidence, is how a room ends up started twice or not at all.
# The lobby is dispatched in `maybe_advance_by_votes` instead, through the same `_start_game` the
# admin's button calls.
#
# The room's own methods do the moving, not lambdas that write fields: a transition that has to
# touch the round's reveal cursor as well as the phase cannot be one line, and hiding that behind
# a table entry would make it look like it could. Two entries are not one-liners, because for
# each of them the phase change is not the whole transition: the quiz has to move the room's
# question cursor and open its window, and the rating row has to be settled on the way out.
_PHASE_ADVANCE = {
    Room.Phase.QUIZ: _advance_quiz,
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
    # Rating an empty round would strand the room on a reveal with no prompt to show.
    if room.phase == Room.Phase.PLAYING:
        rnd = room.current_round()
        if rnd is None or not rnd.answers.exists():
            return json_error("Notation impossible : aucun prompt envoyé pour cette manche.", 409)
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

    `tutorial` is read here and only here, which is what §8 asks for: « L'admin peut relancer une
    partie avec les mêmes joueurs, avec ou sans didacticiel ». The two relaunch buttons post
    `{"tutorial": true|false}`, and this view used to never call `body(request)` at all — so the
    flag was dropped on the floor and "without the didacticiel" launched a room that started in
    the quiz, exactly like "with it". The difference is the whole point of the second button, and a
    teacher who clicks it gets a class stuck on a QCM they have already sat.

    THE FLAG IS HONOURED WHEN IT IS THERE AND KEPT WHEN IT IS NOT, which is why this is an
    `isinstance` test and not `data.get("tutorial", True)`: an absent key is not a request to turn
    the gate off, it is a client that never had the button (a curl, an older build, the second tap
    of a double click), and defaulting it would silently reset the room's setting to the one nobody
    chose. Because a restart of an already-empty room is a no-op, that second tap cannot do any
    harm either way.

    A real `bool` or nothing: `isinstance(True, int)` is True, so a JSON `1` or a string would
    pass a truthiness test and a `bool()` of `"false"` is True, which is a flag inverted by a
    client's own typo. Same guard as `rate`'s score and `_quiz_post`'s question index. Anything
    else leaves the room as it was.

    `anonymous` is NOT settable here, and that asymmetry is deliberate: §1 fixes the didacticiel
    « pour toute la partie » and the Règles transverses fix anonymity « par partie », and a
    relaunch is a NEW game — which is exactly why §8 lets the admin re-ask the gate and not the
    mask. The admin screen offers one of the two on a relaunch, and a second per-game flag the
    endpoint would quietly accept is a flag with no UI behind it: a way to change a game's rules
    with a hand-written POST and nothing to tell the class it happened. So `anonymous` survives
    every restart, whatever the body says.

    The players and their tokens survive too: they are the thing this endpoint exists to spare
    the class. A lobby everybody has to rejoin is not a relaunch, and a token that changed would
    log twenty phones out of a game they were about to start again.

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

    `started_at` SURVIVES THE RESTART, and it is the one field the transaction below does not
    rewrite, because it is the one fact about this room that the restart did not undo. This IS a
    game that has been played — ten rounds of prompts, ratings and a score — even though its rounds
    are about to be deleted and its phase is going back to the lobby. Clearing the stamp would put
    the room back into the only state the idle sweep is allowed to delete, and a finished class that
    went quiet after its relaunch would lose everything the moment a teacher closed the console.
    """
    if (err := require_admin(request)):
        return err
    room = get_object_or_404(Room, code=code)
    tutorial = body(request).get("tutorial")
    with transaction.atomic():
        room.rounds.all().delete()  # answers cascade via the round FK, ratings via Answer
        QuizAttempt.objects.filter(player__room=room).delete()
        # THE READY VOTES GO WITH THEM, and this is the ONLY place a wipe is needed. Everywhere
        # else the votes survive the room moving on, because `step` is part of their key: a vote
        # on `quiz:0` cannot match the count for `quiz:1`. A restart is the one transition that
        # brings a step name BACK — it replays `quiz:0` — so it is the one place where a stale
        # vote would come round and count. Same argument as the quiz attempts above, same
        # transaction: a poller must not see a wiped room still holding a majority.
        Ready.objects.filter(room=room).delete()
        # In the same block as the deletes it describes: a poller that caught the room between the
        # wipe and this write would see a room whose ten targets are gone and whose gate is still
        # the old one, and `start` reads that flag to decide between the quiz and the writing.
        if isinstance(tutorial, bool):
            room.tutorial = tutorial
        room.current = 0
        # THE QUIZ CURSOR AND ITS WINDOW GO BACK TO NOTHING, in the same transaction as the deletes
        # and in the same `update_fields`. A relaunched game that resumed on question 5, with a
        # deadline that had already passed, would show a room an unrevealed key for a question
        # nobody was asked and then open the writing phase underneath it. §8's « Sans
        # didacticiel, la relance va directement dans la rédaction » is the same reset read from
        # the phase: no question, no window, nothing to resume.
        room.quiz_index = 0
        room.quiz_deadline = None
        room.phase_deadline = None
        room.phase = Room.Phase.WAITING
        room.save(
            update_fields=["current", "phase", "tutorial", "quiz_index", "quiz_deadline", "phase_deadline"]
        )
    # Back in the lobby: its idle clock starts again from now, as for a new room.
    _mark_opened(room.code)
    return JsonResponse({"ok": True, "phase": room.phase})


def _epoch_ms(when) -> int:
    """`when` as epoch milliseconds — the unit `QuizState.deadline_ms` and `server_ms` are in.

    MILLISECONDS and not seconds because that is what `Date.now()` returns on the other side of
    the wire, and a client that had to multiply by a thousand to compare two clocks it was handed
    would eventually forget.
    """
    return int(when.timestamp() * 1000)


def _quiz_state(room: Room, online: set[int]) -> dict:
    """The room's question on screen: which one, how long is left, and whether it has been
    revealed.

    ROOM-level, not per-viewer, and it cannot be otherwise: `room_state(code)` deliberately takes
    no token — the snapshot is one payload for the admin screen, every phone and every spectator,
    and presence is global in this codebase — so it does not know who is reading. A player's OWN
    progress is `players[].quiz_score` here and `answered_orders` on the quiz GET.

    THE THREE CLOCKS ARE THE FEATURE, and each is there for a different phone:

    - `deadline_ms` is the instant the window closes, stored on the room and read here. Thirty
      seconds are SHARED: one question for the room, so everyone counts the same seconds and the
      reveal lands on every screen at once.
    - `server_ms` is this server's own clock at the moment the snapshot was built, sent so a
      client can measure its skew against it. A phone whose clock is two minutes out would
      otherwise count from the wrong instant and lock on the wrong side of the question — and
      "wrong side" here is a locked screen on a question it could still have answered, for the
      whole class. One subtraction, `(local_now - server_ms)`, and the phone has the server's
      clock to work from.
    - `revealed` is the comparison itself, `now >= deadline`, evaluated HERE rather than by a
      timer thread. That is why the server owns no timer: the window is thirty seconds of shared
      waiting, the SSE stream already rebuilds this whole snapshot every 0.5s, and so the reveal
      reaches every phone within half a second of the deadline with nothing running in the
      background to be started, leaked, or lost on a restart.

    `correct` and `explanation` ARE NULL UNTIL `revealed`, and this is the security property of
    the whole phase. The snapshot is shared by every phone in the room and lands in devtools on
    each of them, so a key present early is a QCM the class reads instead of plays. One gate,
    here, at the only place these two fields are read — the same discipline `_quiz_get` applies
    to the bank.

    `done` counts the ONLINE players who have answered THE CURRENT question, so it is the
    numerator of the "7 ont répondu" the room is waiting on and it goes back to zero on the next
    question. Presence, not rows: a phone that slept is a row forever, and counting it would show
    a bar that can never fill.

    Every read here is scoped to `room.questions`, not to `QuizQuestion.objects`: the bank is the
    room's, so a global count is this room's eight PLUS every other room's eight, and `total`
    would be a number no player could ever reach.
    """
    now = timezone.now()
    total = room.questions.count()
    question = room.questions.filter(order=room.quiz_index).first()
    # NULL deadline, NO window rather than an expired one: a room parked in the quiz phase with
    # nothing opened on it (a fixture, a lobby that was never started) reports `revealed: False`
    # and no key, which is the safe direction for both.
    revealed = room.quiz_deadline is not None and now >= room.quiz_deadline
    players = list(room.players.filter(pk__in=online))
    return {
        "index": room.quiz_index,
        "total": total,
        "deadline_ms": _epoch_ms(room.quiz_deadline) if room.quiz_deadline else 0,
        "server_ms": _epoch_ms(now),
        "revealed": revealed,
        "correct": question.correct if (revealed and question) else None,
        "explanation": question.explanation if (revealed and question) else None,
        # Distinct on `player_id`: the same player answered question 3 twice is impossible
        # (`unique_together`), but counting rows rather than players would make the shape of the
        # answer table a progress indicator.
        "done": QuizAttempt.objects.filter(
            player_id__in=online, question__room=room, question__order=room.quiz_index
        )
        .values_list("player_id", flat=True)
        .distinct()
        .count(),
        # The room's own online players, counted from the roster rather than from `online` — a
        # presence entry left behind by a player deleted mid-quiz must not claim a phone in a
        # room that has none. Same reason `done` and `online_count` cannot disagree on screen.
        "online_count": len(players),
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

    It carries whatever is stored rather than a default: `settle_round` always writes the three
    settlement numbers together, so a null in here means the round was never settled at all —
    which the admin's next button fixes, and which a silent 0 would have hidden as a real score.

    `quality_comment` is the critic's own reason for `quality_llm`, and it is the only honest way
    to label that number: offline the critic is a keyword-marker heuristic in `llm._stub_grade`,
    so « note du modèle » is a claim about a model nobody called. `api.ts` types the field
    `string` and the summary screen shows it, and the `frozen SummaryRow` note that used to sit
    in `answer` is the story of why the frozen type was wrong.
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
                "quality_comment": a.quality_comment,
                "quality_peers": a.quality_peers,
                "quality": a.quality,
                "round_score": a.round_score,
            }
            for a in rnd.answers.order_by("id")
        ],
    }


def _ready_state(room: Room, player, online: set[int]) -> dict | None:
    """The class's own bar: how many are ready, how many are needed, and whether THIS player is.

    `needed` and not `quorum`: the payload carries no float anywhere else and this file names
    integers by what they are. `needed` is also the number a phone has to show next to the
    button, so it is the number that matters to a client, not the rule behind it.

    `None` OUTSIDE A DISCUSSION, INCLUDING DURING THE ANSWERING WINDOW. `current_step` is what
    decides it, and `current_step` withholds the quiz step until the reveal: a bar during the
    thirty seconds of answering would let a majority skip a question nobody had been shown the
    answer to.

    COUNT AND NEEDED ARE MEASURED OVER THE SAME POPULATION, both through `_ready_voters`. The
    author of the prompt on screen is excluded from both halves, because the endpoint refuses
    their vote outright — a `mine` of True beside a count that ignores the vote would tell a
    player their press landed when it was thrown away.

    `needed` COMES OUT OF `prompts.threshold` WITH THIS `step` AND THIS `voters`, which is the
    whole reason the number on a phone is the number the server acts on: a caller that read
    `majority` here would put a majority bar in the lobby and let the class start the game without
    the one phone that has not been picked up yet. In the lobby the bar therefore reads « X sur Y
    prêts » with Y the CONNECTED players, because starting a game asks for everybody.

    NO VIEWER IS NOT NO BAR. `room_state` is called with no player by the admin screen and by any
    spectator, and `None` there means "nobody in particular is asking" rather than "there is
    nothing to report": the count and the bar are properties of the ROOM and are correct without
    a viewer, so only `mine` — the one field that cannot exist without one — is False. This is
    also why `room_state` takes the player as an argument rather than reading a loop variable:
    `p` inside its roster loop is whichever player happened to be built last, and reporting
    `mine` for that one would tell every phone in the room whether a stranger had voted.
    """
    step = current_step(room)
    if step is None:
        return None
    voters = _ready_voters(room, online)
    return {
        "step": step,
        "count": Ready.objects.filter(room=room, step=step, player_id__in=voters).count(),
        "needed": prompts.threshold(step, len(voters)),
        "mine": player is not None
        and Ready.objects.filter(room=room, step=step, player=player).exists(),
    }


def room_state(code: str, player=None) -> dict:
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
        # THE RANKING TOTAL. Three cases, and getting this wrong deletes the game.
        #
        # `Answer.score` is the judge's proximity on the output alone. `Answer.round_score` is
        # `(proximity + quality) / 2`, written once `settle_round` closes the round's rating.
        # The spec says the final ranking "additionne les dix tours" where a tour is the second
        # one — so summing `score` alone, which is what this used to do, drops the quality
        # dimension entirely and lets a player whose prompts are badly built but whose output
        # happened to land tie with a player who writes carefully.
        #
        # But `round_score` is NULL until the class has rated the round, and the spec also says
        # the board is "mis à jour en direct pendant la rédaction". Summing settled rounds alone
        # would freeze the board for the whole writing phase. So the open round contributes its
        # PROXIMITY, provisionally: the board moves the moment a player answers, and the number
        # is corrected — not merely recomputed — when the round settles. That correction is the
        # teaching moment: a player who sees their total drop by two at the summary is seeing the
        # price of a weak prompt, which is the thing the whole didacticiel is for. On top of the
        # rounds sits the quiz bonus: one correct answer is one point (see below).
        settled = p.answers.aggregate(total=Sum("round_score"))["total"] or 0
        open_round = p.answers.filter(round_score__isnull=True).aggregate(s=Sum("score"))["s"] or 0
        # Quiz carryover: one correct answer is one point, added raw on top of the rounds.
        # Quiz is N/8 while rounds are /10-based, so the bonus tops out at the bank size rather
        # than at a round mark; kept raw so a player can read the division they were taught.
        total = p.quiz_score() + settled + open_round
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
                    #
                    # AND THEY ARE MASKED WITH THE OTHER TWO, which is the whole point of the
                    # phase. They were emitted unconditionally, and that defeats the feature from
                    # underneath: a player reads their own round score, adds the two halves they
                    # can already see, and subtracts to get the other three rows' totals off the
                    # board — the exact cross-reference §4 closes by hiding the ranking, and the
                    # spec is explicit that what must go is the SCORES (« Ce sont les scores qui
                    # font la fuite »), not the names. `api.ts` has always typed these two
                    # `number | null` "during the rating"; the contract was right and the server
                    # is what drifted when the columns were added.
                    "proximity_total": None if hiding else settled["proximity"],
                    "quality_total": None if hiding else settled["quality"],
                    # An int, never None: the client formats "3 sur 8" and should not carry a
                    # null branch that only a player who never answered would ever take.
                    "quiz_score": p.quiz_score(),
                    # Has THIS player sat the whole didacticiel — a per-player fact that used to
                    # be the gate's own condition and is now only a line on the roster, since
                    # nothing opens the game on it. `quiz.done` in the snapshot is a different
                    # number: the players who have answered the question ON SCREEN.
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
        "target_image": image_for(rnd.target) if rnd else None,
        # The clock of the phase on screen (writing a prompt, rating one), or None when nothing is
        # timed. `server_ms` lets a phone measure its own clock's skew, as `quiz` does.
        "timer": (
            {"deadline_ms": _epoch_ms(room.phase_deadline), "server_ms": _epoch_ms(timezone.now())}
            if room.phase_deadline is not None
            else None
        ),
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
        "ready": _ready_state(room, player, online),
    }
    return state


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
    _advance_reveal_and_settle(room)


def _maybe_close_quiz_window(room: Room) -> None:
    """Close the current question's window early if every ONLINE player has answered it.

    A shared thirty-second window the class has finished with is thirty seconds of a frozen
    countdown on every phone in the room, and the answer to that is not a new state but the
    comparison `_quiz_state` already makes: move the deadline to an instant that has passed and
    the reveal opens on the next frame, with no timer thread and nothing new in the payload.

    WHY IT IS SAFE TO CLOSE THE WINDOW BUT NOT TO ADVANCE. §2 gives the advance to a person, and
    the two look identical from a phone's point of view — both are "question over". Closing is
    safe because it cannot give one player an advantage: the attempt it writes is already
    recorded and the key it unlocks is the key the clock was going to unlock anyway, only
    earlier and with nobody scoring on it. Advancing is not, because it would hand the first
    player to answer the next question a head start over the rest of the class — « §2 donne
    l'avance à une personne » — and open the game on the last answer of the didacticiel, which
    is the removal `test_nothing_opens_the_game_by_itself` pins. So this writes a deadline and
    nothing else: `maybe_advance_phase` is not called from here, and the cursor and the phase
    are the admin's button alone.

    THE BAR IS THE ONLINE PLAYERS, and it is `maybe_advance_phase`'s bar, its reasoning and its
    guard, in the same order. Not `room.players`, because a phone that slept is a row forever and
    the window would then never close. And `if not online` first, because that same row count
    taken to the limit is `0 == 0`: a room whose every phone dropped has nobody to be the last
    answerer, and closing its own window unattended is a party teaching itself with nobody in it.

    Only the question on screen is ever considered, and it is the room's own bank — the same
    `(room, order)` address the POST used, so a late tap for a question the cursor has passed
    cannot close a window that no longer exists. `_close_quiz` is idempotent, so the call is
    harmless on every answer after the first that satisfied the condition.
    """
    online = online_player_ids(room.code)
    if not online:
        return
    answered = set(
        QuizAttempt.objects.filter(
            player_id__in=online, question__room=room, question__order=room.quiz_index
        ).values_list("player_id", flat=True)
    )
    if answered != online:
        return
    room._close_quiz()


def current_step(room: Room) -> str | None:
    """The step the room is in, named, or None when it is not discussing anything.

    TWO STEPS, the lobby before the game opens and a prompt being rated. `playing` is not one —
    that phase closes on the answers themselves — and neither are a revealed didacticiel question
    nor a round summary: going on to the next question is the admin's decision alone, so the class
    has no vote there.

    THE LOBBY IS A STEP AND NOT THE ABSENCE OF ONE. The lobby is the one moment where the bar
    costs nothing to be strict on — everybody has just arrived — and `prompts.threshold` is what
    makes it ask for all of them rather than half.

    `None` IS THE ANSWER for every other phase — `playing`, `quiz`, `summary` and `done` — and
    it is why this returns an option rather than a default: a client that renders a ready bar for a step that does not exist shows a
    button that goes nowhere.
    """
    if room.phase == Room.Phase.WAITING:
        return prompts.LOBBY_STEP
    if room.phase == Room.Phase.RATING:
        rnd = room.current_round()
        return None if rnd is None else f"rating:{rnd.order}:{rnd.revealed}"
    # QUIZ and SUMMARY have no step: moving on to the next question is the admin's button alone.
    return None


def _ready_voters(room: Room, online: set[int]) -> set[int]:
    """The players whose ready vote counts on the step the room is in.

    THE ONLINE PLAYERS, MINUS THE AUTHOR OF THE PROMPT ON SCREEN during the rating, because the
    author is forbidden to rate their own prompt and a bar they can never reach is a step the
    room cannot end. Same reasoning, same set, as `_maybe_advance_reveal`.

    ONE FUNCTION, because the count and the threshold have to be measured over the SAME
    population. `_ready_state` once counted over the voters but took `needed` from `len(online)`,
    which in a two-player room put `count` on one player and `needed` on two: the bar read « 1 sur
    2 prêts » on every frame and the room never moved. The bar is the population, both halves of
    it, or it is not a bar.
    """
    voters = set(online)
    if room.phase == Room.Phase.RATING:
        rnd = room.current_round()
        if rnd is not None:
            answers = list(rnd.answers.order_by("id"))
            if rnd.revealed < len(answers):
                voters.discard(answers[rnd.revealed].player_id)
    return voters


def maybe_advance_by_votes(room: Room) -> None:
    """Move the room on when enough of the players who are there have said they are ready.

    THE MAJORITY IS THE POINT, and it is the second half of why this exists. §2 gave the
    advance to a person, and the consequence was that a teacher who wanted to stop and talk had
    to keep a hand on the laptop: the discussion the round was built around could not start
    until they pressed something. A majority of the players who are CONNECTED is enough, so the
    class ends its own discussion — and a phone in a bag is not a veto, because the bar counts
    `online_player_ids` and not `room.players`. The lobby is the one step that asks for all of
    them instead, and `prompts.threshold` carries the reason rather than this function: everybody
    is in the lobby because they just arrived, so waiting there is free, and waiting for a phone
    that has gone to sleep twenty minutes into a discussion is not.

    THE LOBBY IS DISPATCHED BEFORE THE TABLE, NOT THROUGH IT. Starting the game is `admin_start`'s
    verb and `WAITING` is absent from `_PHASE_ADVANCE` on purpose, so the branch that opens the
    game from a vote is written here rather than reached through the table the admin's « suivant »
    uses. Every other step still ends in `_PHASE_ADVANCE[room.phase](room)` and nothing else: a
    class that advances itself and a teacher who presses « suivant » are the same transition, and
    two implementations of it would drift within a week — the vote path settling the round while the
    button path does not, say, and the bug only appearing when a class finishes itself.

    NOTHING HAPPENS ON AN EMPTY ROOM. `threshold(step, 0)` is 0 and the comparison below would be
    `0 >= 0`, which is the vacuous truth this file refuses in three other places; the early return
    is what keeps a room whose phones all dropped from starting itself unattended.
    """
    step = current_step(room)
    if step is None:
        return
    online = online_player_ids(room.code)
    if not online:
        return
    voters = _ready_voters(room, online)
    # The bar is the voters, not the room: during the rating the author of the prompt on screen
    # is not one of them, and a threshold counted over players whose votes are excluded is a
    # step that cannot end in a two-player room. `_ready_state` reads the same pair, so the
    # number on the phone and the number acted on here are the same number by construction.
    # `threshold`, not `majority`: the lobby asks for everybody and a discussion for half, and
    # picking the rule here instead of asking it is how the two would drift apart.
    needed = prompts.threshold(step, len(voters))
    if needed == 0:
        return
    count = Ready.objects.filter(room=room, step=step, player_id__in=voters).count()
    if count < needed:
        return
    if step == prompts.LOBBY_STEP:
        # THE RACE IS ALREADY HANDLED IN THERE, and the return value is deliberately dropped:
        # `_start_game` re-checks the rounds inside a transaction and treats the unique constraint
        # on `(room, order)` as the admin having pressed « lancer » first, so the loser of a
        # simultaneous start is a no-op. There is nobody here to hand a refusal to — the caller is
        # a tap on a button whose whole job was to make the game happen.
        _start_game(room)
        return
    advance = _PHASE_ADVANCE.get(room.phase)
    if advance is not None:
        advance(room)


def expire_phase(code: str) -> None:
    """Move the room on when the clock of its phase has run out. Called on every SSE tick.

    `playing` out of time opens the rating, or goes straight to the summary when nobody wrote a
    prompt — but only once the prompts already sent have finished generating, so an answer that
    beat the clock is never lost. `rating` out of time puts the next prompt on screen, exactly as
    if every rater had voted.

    THE DEADLINE IS CLAIMED BEFORE ANYTHING MOVES. Every phone's stream calls this, so several can
    see the same expired deadline on the same tick; the conditional UPDATE lets exactly one of
    them through, and the transition it then runs arms the next deadline.
    """
    room = Room.objects.filter(code=code).first()
    if room is None or room.phase_deadline is None or timezone.now() < room.phase_deadline:
        return
    if room.phase not in (Room.Phase.PLAYING, Room.Phase.RATING):
        return
    rnd = room.current_round()
    if rnd is None:
        return
    if room.phase == Room.Phase.PLAYING and any(rid == rnd.pk for rid, _ in list(_inflight)):
        return
    claimed = Room.objects.filter(
        code=code, phase=room.phase, phase_deadline=room.phase_deadline
    ).update(phase_deadline=None)
    if claimed != 1:
        return
    room.phase_deadline = None
    if room.phase == Room.Phase.RATING:
        _advance_reveal_and_settle(room)
    elif rnd.answers.exists():
        room._open_rating()
    else:
        room._go(Room.Phase.SUMMARY)
        settle_round(room)


def maybe_advance_phase(room: Room) -> None:
    """Move the game on if the players who are there have done what the phase is waiting for.

    TWO phases advance on their own, not three: the round, once everybody online has written a
    prompt, and the rating, one prompt at a time. The others wait for the admin — and that
    includes the DIDACTICIEL, which is the change this function exists to record. The gate used
    to open itself when the last online player answered the eighth question, and it does not any
    more: §2 gives the advance to a person (« L'admin décide du passage à la question suivante »)
    for the same reason it gives the commentary of a round to a person — a tour se commente, and
    so does a QCM. Nothing here reads a quiz answer, and `_PHASE_ADVANCE[QUIZ]` is the only thing
    that moves the room out of the gate now.

    The bar is the online players, not `room.players`: a phone that slept or lost wifi is still a
    row in that table forever, so the old comparison waited on someone who was never coming back and
    the whole party sat on a round it could not leave. A player who reconnects mid-round is back in
    the count on their next beat, and `answer` does not require presence, so a late arrival can
    still answer the round that is open when they get back.

    Renamed from `maybe_advance` because it is no longer "maybe the round": it dispatches on the
    phase now, and there are two things it can advance.
    """
    if room.phase not in (Room.Phase.PLAYING, Room.Phase.RATING):
        return
    online = online_player_ids(room.code)
    if not online:
        # Nobody is streaming, so there is no evidence anyone is here. Without this guard a room
        # whose every phone dropped would satisfy "all online players have answered" vacuously —
        # `0 == 0` — and open itself unattended. A phase is only ever closed by a real player's
        # submit.
        return
    rnd = room.rounds.filter(order=room.current).first()
    if not rnd:
        return
    if room.phase == Room.Phase.RATING:
        _maybe_advance_reveal(room, rnd, online)
        return
    if rnd.answers.filter(player_id__in=online).count() == len(online):
        # `_open_rating`, NOT `next_round`. This line used to be `next_round()`, which skipped
        # `rating` and `summary` outright and went straight to the following round: the last
        # player to answer ended the round before the class ever saw a prompt to rate, so the
        # cross-rating never ran in a normal game and a teacher had to advance by hand every
        # round to get the feature at all.
        #
        # It read correctly when there was no rating phase, and stayed correct-looking after one
        # was added, because `next_round` is what this function had always done. The whole test
        # module parked its rooms in `rating` on the ORM, so nothing in it could catch the
        # transition INTO the phase it was testing. `test_playing_a_round_actually_opens_the_rating`
        # walks the real path for that reason.
        room._open_rating()


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
            # Before the snapshot too, so the frame that follows already shows the next phase.
            expire_phase(code)
            try:
                # The player goes IN, because `ready.mine` is the one field of the snapshot that
                # is about the reader rather than the room: without it every phone would be told
                # the same thing about its own button, and the admin's stream (no token) gets
                # `mine` False rather than somebody else's.
                payload = json.dumps(room_state(code, player), ensure_ascii=False)
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
def answer(request: HttpRequest, code: str) -> HttpResponse:
    """The play call, streamed: `chunk` per delta, then `scoring`, then `done` or `error`.

    EVERY REFUSAL IS DECIDED BEFORE THE FIRST BYTE, and that ordering is the contract rather
    than an implementation detail: `already answered`, `not playing`, `bad token` and `prompt
    required` are JSON with their status code, and the client reads them off `r.status`. Once
    the 200 and its `text/event-stream` are out, the status is spent, so a duplicate arriving a
    second later can only be reported in-band — which is why the claim below is the thing that
    has to be right rather than the status.

    The three billed calls are split around one frame: the text streams, `scoring` says the jury
    is working, and `done` carries the verdict. The pause in between is two more model calls
    and nothing else, and the client shows a label rather than a spinner that means "hung".
    """
    room = get_object_or_404(Room, code=code)
    if room.phase != Room.Phase.PLAYING:
        return json_error("not playing", 409)
    data = body(request)
    player, refusal = _player_for_token(room, data)
    if refusal is not None:
        return refusal
    rnd = room.rounds.filter(order=room.current).first()
    if rnd is None:
        return json_error("no round", 409)
    if rnd.answers.filter(player=player).exists():
        return json_error("already answered", 409)
    if room.phase_deadline is not None and timezone.now() >= room.phase_deadline:
        return json_error("Temps écoulé : la manche est fermée.", 409)
    prompt = str(data.get("prompt", "")).strip()[:2000]
    if not prompt:
        return json_error("prompt required")
    # The duplicate check above cannot cover the ~5s the two billed calls take. Claimed here,
    # before the calls, so a second copy arriving in that window is refused without paying for
    # the model a second time. Released in the worker's `finally` however that block leaves.
    claim = (rnd.pk, player.pk)
    if claim in _inflight:
        return json_error("already answered", 409)
    _inflight.add(claim)

    def frame(name: str, **payload: object) -> str:
        # The blank line is the frame terminator and the client splits on it: a frame without it
        # is never dispatched, so it is part of the value and not formatting.
        return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    out: queue.Queue[str | None] = queue.Queue()

    def work() -> None:
        """The billed work, off the request thread, reporting its steps through `out`.

        A GENERATOR CANNOT DO THIS JOB, and both of its failures are the reason. It cannot
        `yield` inside an `except`, so an LLM call blowing up would close the response with no
        frame at all and the client could only see a truncated stream. And the server calls a
        generator's `close()` when the client goes away — a phone that locks its screen mid
        generation would cancel work it has already been billed for, storing nothing, and the
        round would then wait forever for a player who is asleep. So the work is already paid
        for and already claimed: it finishes, and nobody left to read it is not a reason to
        throw away a player's answer.
        """
        try:
            parts: list[str] = []
            stream = llm.answer_stream(prompt)
            for delta in stream:
                parts.append(delta)
                out.put(frame("chunk", delta=delta))
            # The judge scores the SAME text the client just watched, joined from the same deltas
            # rather than re-asked for. `.strip()` is what the one-shot call did, so a stored
            # answer is byte-identical to what the old path stored — and a score computed on a
            # text nobody saw is a score about nothing.
            output = "".join(parts).strip()
            out.put(frame("scoring"))
            verdict = llm.score(prompt, output, rnd.target)
            # THE CRITIC'S CALL BELONGS HERE, NOT IN `settle_round`. It is the third billed call of the
            # submit the player is already waiting on: each player's own request pays for their own
            # critic, and `runserver` is threaded, so twenty phones are graded concurrently. In
            # `settle_round` it would be twenty back-to-back calls inside ONE request, fired by the
            # last player's rating tap — a single HTTP call blocking for the better part of a minute,
            # on the phase where every phone is waiting for a snapshot that will not come. Same total
            # spend, a fraction of the latency, and settlement stays arithmetic: free and instant.
            #
            # The critic's COMMENT IS STORED, on `Answer.quality_comment`, and reaches the player
            # through the summary row. It used to be discarded here, on the reasoning that the frozen
            # `SummaryRow` had no room for it and the summary's four numbers told the story anyway.
            # Both halves of that were wrong, and the audit that found the bug is the evidence: with no
            # API key `llm.grade_prompt` is `_stub_grade`, a count of keyword markers, and the plan's
            # four numbers then put a heuristic on screen under the label « note du modèle » with
            # nothing anywhere saying no model was called. The spec asks for the opposite — « le modèle
            # devient un écho, le jury devient un chevauchement de mots, et l'écran le dit
            # explicitement » — and this string is the only thing on that screen that can say it. It is
            # the same sentence the judge already returns as `verdict.comment` and `answer` hands
            # straight back, so the reason a prompt is bad and the reason its quality is low are both
            # now stored rather than one of them.
            #
            # The target travels with the prompt so the critic knows what the prompt is FOR, and the
            # rubric is what forbids it from comparing anything: it never sees the answer it produced,
            # nor a single peer rating, so it cannot be pulled by either.
            critique = llm.grade_prompt(prompt, rnd.target)
            # ONE WRITE, AT THE END. SQLite has a single writer, so a write per chunk would serialise
            # the whole class on one lock for the length of a generation, and a row that exists
            # before the jury has ruled is a row the summary would show as an answer nobody judged.
            # `transaction.atomic()` keeps the durable backstop — another process winning
            # unique_together — from poisoning the rest of this thread's work. That race can no
            # longer answer 409, because the status was spent with the first chunk; it arrives as
            # the error frame below, with nothing written, which is the same outcome the player had.
            with transaction.atomic():
                rnd.answers.create(
                    player=player,
                    prompt=prompt,
                    output=output,
                    score=verdict.score,
                    comment=verdict.comment,
                    quality_llm=critique.score,
                    quality_comment=critique.comment,
                )
            maybe_advance_phase(room)
            out.put(
                frame(
                    "done",
                    score=verdict.score,
                    comment=verdict.comment,
                    # The round this answer belongs to, not `room.current`: the phase may have
                    # moved on by the time the last player answers, and a verdict labelled with
                    # the NEXT round is one the client would file under the wrong prompt.
                    round=rnd.order,
                    truncated=stream.truncated,
                )
            )
        except Exception:  # noqa: BLE001 — one player's model call, reported to that player
            # Nothing is written, so the player may send again: the composer stays usable on the
            # client precisely because the row does not exist. Logged as well as streamed, because
            # the player is only told that it broke, not why, and the animateur is the one who can
            # do something about it.
            logger.exception("answer failed in room %s", room.code)
            out.put(frame("error", error="La partie a planté côté serveur. Préviens l'animateur."))
        finally:
            _inflight.discard(claim)
            out.put(None)  # the sentinel: the stream ends
            # A thread attached to no request never gets `request_finished`, so nothing else
            # closes the connection it opened. Same reason, and the same call, as `_sweep_forever`.
            close_old_connections()

    threading.Thread(target=work, daemon=True, name=f"answer-{room.code}").start()

    def stream() -> Iterator[str]:
        # A SYNC generator, deliberately: an async one is consumed whole by Django before the
        # first byte is sent, which is the buffering this endpoint exists to avoid.
        while True:
            item = out.get()
            if item is None:
                return
            yield item

    return StreamingHttpResponse(
        stream(),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
    # Scoped to the room as well as the token, like `answer`: a token from another room must
    # not vote in this one.
    player, refusal = _player_for_token(room, data)
    if refusal is not None:
        return refusal
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


@require_POST
def ready(request: HttpRequest, code: str) -> JsonResponse:
    """« On peut passer. » One player's vote, on the step the room is in.

    THE CLIENT NEVER NAMES THE STEP. It sends its token and nothing else, for the reason `rate`
    gives: a payload that could name the step is a payload that could vote on a step that has
    not started, and the count would then include a vote the room never asked for. The step is
    read from the room, and `current_step` returns None outside a discussion — including during
    the thirty seconds a question is still being answered — so there is nothing here that can
    put a row on a step nobody is discussing.

    THE AUTHOR OF THE PROMPT ON SCREEN IS REFUSED, with a message of their own. They cannot
    rate their own prompt, so their vote is not in the bar either, and the alternative — a
    successful vote that the count silently ignores — is the worst of the three outcomes: the
    button says « Prêt », the number does not move, and nothing on the phone says why. The
    frontend already surfaces refusals, so the player reads the reason instead of guessing.

    THE BAR IS CROSSED HERE, NOT AT THE NEXT POLL. The room moves on this tap, so a player who
    pressed last does not spend half a second watching a bar that already says « 2 sur 2 »
    before the screen changes underneath them.
    """
    room = get_object_or_404(Room, code=code)
    data = body(request)
    player, refusal = _player_for_token(room, data)
    if refusal is not None:
        return refusal
    step = current_step(room)
    if step is None:
        return json_error("nothing to discuss", 409)
    if room.phase == Room.Phase.RATING:
        rnd = room.current_round()
        answers = list(rnd.answers.order_by("id")) if rnd is not None else []
        if rnd is not None and rnd.revealed < len(answers):
            if answers[rnd.revealed].player_id == player.pk:
                return json_error("you wrote this one", 409)
    if Ready.objects.filter(room=room, step=step, player=player).exists():
        return json_error("already ready", 409)
    Ready.objects.create(room=room, step=step, player=player)
    maybe_advance_by_votes(room)
    room.refresh_from_db()
    return JsonResponse({"ok": True, "ready": _ready_state(room, player, online_player_ids(room.code))})


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
    their choices, which is what is on the screen anyway, so reading it leaks nothing the player
    could not read by looking. What it never returns is what a later phase cannot: the
    answer, and the reason. Those travel in the POST response, and to the room's phones through
    the snapshot's `revealed` gate, which is closed until the deadline passes.

    ONE THING IS DERIVED FROM `?token=`, and it is `answered_orders` — the question ORDERS THIS
    player has already answered. A LIST and not the count it used to be, and the reason is the
    change above: the question on screen belongs to the room, so "how many have I done" cannot
    answer "have I done THIS one". A player who answered question 0 and then looked away has one
    answered and is looking at question 4, which they have not done; a client that resumed on a
    count would either re-ask the four it skipped past the one, or lock a player out of a
    question they never saw. Membership in this list is the whole of "my screen is locked".

    The client sends its own token, the same spelling `events` uses. It is a list of the CALLER'S
    OWN rows, so reading a token here discloses nothing to anyone else: the same token from
    another room, or a token nobody has, simply resolves to no player and the list is empty — the
    pre-token behaviour, never a 403. Refusing the request instead would break a screen that is
    already entitled to the eight questions.

    `question__room=room` on the filter is belt and braces rather than a fix for a live bug — a
    `Player` belongs to exactly one room, so its attempts are already this room's — but it makes
    "answered an order this room never asked" unrepresentable rather than merely improbable.
    Ordered, so the client reads a stable list rather than whatever the database returned.
    """
    room = get_object_or_404(Room, code=code)
    player = room.players.filter(token=request.GET.get("token", "")).first()
    answered = (
        list(
            player.quiz_attempts.filter(question__room=room)
            .order_by("question__order")
            .values_list("question__order", flat=True)
        )
        if player
        else []
    )
    return JsonResponse(
        {
            "questions": [
                {"order": q.order, "prompt": q.prompt, "choices": q.choices}
                for q in room.questions.order_by("order")
            ],
            "answered_orders": answered,
        }
    )


def _quiz_post(request: HttpRequest, code: str) -> JsonResponse:
    """Answer the question on screen, without being told whether it was right.

    THE RESPONSE STILL CARRIES `correct`, `correct_index`, `explanation` and `score` — they are
    the answer to THIS player's tap, and the answer arrives here. What changed is that they are
    NOT the thing the player is waiting for: the thirty seconds are a shared clock, the screen
    locks on the tap, and the class-wide reveal comes when the deadline passes. A client that
    wants the suspense reads `revealed` from the snapshot and ignores these four until then.

    Two refusals stand between the tap and the write, and both are the deadline rather than the
    player:

    - past `quiz_deadline`, the server refuses. A player who did not tap has nothing, and blocks
      nobody: this is the rule that makes a slow phone a private loss instead of a frozen class.
    - an order that is not the room's current question is refused too, because a shared clock
      means there is exactly one question worth an attempt. Under the old per-player flow a
      client walked its own cursor through the eight and any order in the bank was legal; now the
      question on screen is the only one that exists.

    The client names a question by its `order` and a choice by its index, and sends nothing else
    that is checked: the player is the token, the question is this room's row with that order,
    the key is the row's.
    """
    room = get_object_or_404(Room, code=code)
    if room.phase != Room.Phase.QUIZ:
        return json_error("quiz not open", 409)
    data = body(request)
    # Scoped to the room as well as the token, like `answer`: a token from another room must
    # not score a question in this one.
    player, refusal = _player_for_token(room, data)
    if refusal is not None:
        return refusal
    order = data.get("question")
    # `isinstance(True, int)` is True, so a JSON `true` is an int that would pass the type check
    # and then miss every row. Same guard as the 1-10 rating scale.
    if not isinstance(order, int) or isinstance(order, bool):
        return json_error("question index required", 400)
    # THE SHARED QUESTION. One question for the room, so an answer to any other one is an answer
    # to a question nobody was shown — which is what the admin pressing "question suivante" early
    # produces, and what a stale client produces after a reconnect onto a question the room has
    # already left. Checked before the deadline so the message names what the player actually got
    # wrong: they are late, on a question that has moved on.
    if order != room.quiz_index:
        return json_error("not the current question", 409)
    # THE DEADLINE, AND THE ONLY THING IT IS. A NULL deadline is no window at all rather than an
    # open one, so a room nobody has started asking in refuses rather than accepting forever.
    # Compared on the SERVER's clock: the phone's own is two minutes out on half the class and
    # the window is thirty seconds long.
    if room.quiz_deadline is None or timezone.now() >= room.quiz_deadline:
        return json_error("time's up", 409)
    # `(room, order)` and not `order` alone: `order` is unique WITHIN a room, so an unscoped
    # lookup would grade this player against whichever room's row happened to come first — an
    # order that exists in this room and not in that one, a question they were never shown, and a
    # score for it. Unreachable in normal play, which is exactly why the fixture that walks the
    # whole bank needs it.
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
    # NO `maybe_advance_phase` HERE, and its absence is the point of the whole phase: the last
    # question answered does not open the game. The admin's button does, from
    # `_PHASE_ADVANCE[QUIZ]`, exactly as it opens each of the next seven questions.
    #
    # What DOES happen here is the window closing early, and it is deliberately a different
    # function: the answer just recorded is the last one the room was waiting for, so the thirty
    # seconds are over as far as this question goes and the class should be reading the
    # explanation rather than a countdown. A deadline, and nothing else — see
    # `_maybe_close_quiz_window` for the arbitration against advancing.
    _maybe_close_quiz_window(room)
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
