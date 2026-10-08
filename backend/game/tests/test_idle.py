import json
import os
import threading
import time
from datetime import timedelta
from unittest import mock

from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from game.models import Player, QuizQuestion, Room
from game.targets import ROUNDS_PER_GAME
from game.views import ROOM_IDLE_TTL, ROOM_KEEP_TTL, _beat, _opened, _presence, _presence_lock, _sweep_tick

# HOW LONG AGO a room has to look to be idle, wherever a test means "well past ten minutes".
# `ROOM_IDLE_TTL + 60` rather than a round number, so the boundary tests read as "one minute past"
# and nobody has to remember which side of the line a literal is on.
IDLE = ROOM_IDLE_TTL + 60


class IdleBase(TestCase):
    """The room an idle sweep is measured on: players joined, a beat written or not, game started or
    not.

    COPIED, NOT SHARED, and the reason is the one `test_vote.VoteBase` gives for the same helpers:
    `test_admin.py` holds its room builders as private methods of a class that also pins every other
    admin behaviour, so sharing them means making them public in a test file whose subject is a
    different feature. Four short helpers, duplicated, beat a refactor of the admin suite to serve a
    sweep test.

    THE PRESENCE REGISTRY IS CLEARED IN `setUp` for the reason `test_admin` clears it in
    `tearDown`: it is PROCESS-LOCAL and outlives the transaction each test rolls back, so a beat
    written against one test's player would still be in the dict for the next one under an id the
    database has handed out again. Beats are keyed by `(room code, player id)`, and these tests
    reuse ids across tests, so an uncleared registry is not a theoretical mess — it is a room that
    looks alive because a previous test's phone beat in it.
    """

    def setUp(self) -> None:
        with _presence_lock:
            _presence.clear()
            _opened.clear()

    def _post(self, url: str, data: dict | None = None):
        return self.client.post(url, data=json.dumps(data or {}), content_type="application/json")

    def _login(self) -> None:
        """The admin session, which is a COOKIE and not a password in the body — the two lines
        `test_admin.py` opens its class with, for the same reason."""
        self._post("/api/admin/login", {"password": "genai"})

    def _room(self, tutorial: bool = True) -> Room:
        """A room through the ADMIN ENDPOINT, so it has its quiz bank. The sweep deletes rooms by
        cascade and the bank is what a cascade assertion needs to bite on: a room built on the ORM
        would let that assertion pass on rows that were never there.

        THE CODE IS THE ONE THE SERVER MINTED, and the tests read `room.code` rather than a literal
        of their own. Renaming the room after the fact looks tempting and breaks the bank: the code
        is the primary key `QuizQuestion.room_id` and `Player.room_id` point at, and SQLite has no
        ON UPDATE CASCADE to follow the rename.
        """
        self._login()
        made = self._post("/api/admin/rooms", {"tutorial": tutorial}).json()["code"]
        return Room.objects.get(code=made)

    def _join(self, room: Room, name: str) -> Player:
        return Player.objects.get(
            token=self._post(f"/api/rooms/{room.code}/join", {"name": name}).json()["token"]
        )

    def _start(self, room: Room) -> None:
        self._login()
        self.assertEqual(self._post(f"/api/admin/rooms/{room.code}/start").status_code, 200)
        room.refresh_from_db()

    def _restart(self, room: Room) -> None:
        self._login()
        self.assertEqual(
            self._post(f"/api/admin/rooms/{room.code}/restart", {"tutorial": True}).status_code, 200
        )
        room.refresh_from_db()

    def _beat(self, room: Room, *players: Player) -> None:
        """One live heartbeat per player, written the way a stream writes it."""
        for p in players:
            _beat(room.code, p.pk)

    def _idle_for(self, room: Room, seconds: float) -> None:
        """Every beat of this room lands `seconds` in the past, without sleeping.

        THE WHOLE ROOM AND NOT ONE PLAYER, because the sweep reads the LAST beat in the room: moving
        a single player's back would leave the others' recent beats to speak for the room, and the
        test would then be measuring the wrong fact.
        """
        with _presence_lock:
            for key in [k for k in _presence if k[0] == room.code]:
                _presence[key] = time.monotonic() - seconds
            # The opening instant counts as a beat too, so it ages with the rest of the room.
            if room.code in _opened:
                _opened[room.code] = time.monotonic() - seconds

    def _entries(self) -> dict[str, dict]:
        """The admin list, as `{code: entry}`, so a test about a room is not a test about an order.

        ONE GET DOES THE SWEEP AND THE ANSWERING, so every assertion about "is it in the list" is
        also an assertion about what that same request swept.
        """
        r = self.client.get("/api/admin/rooms")
        self.assertEqual(r.status_code, 200, r.content)
        return {entry["code"]: entry for entry in json.loads(r.content)["rooms"]}


class IdleSweepTests(IdleBase):
    def test_a_lobby_nobody_played_is_deleted_and_leaves_the_list(self):
        # THE DELETE HALF, and the reason it is allowed at all: a lobby that was never started holds
        # a bank, some tokens and an admin's typo. Nothing in it is a class's work, so after ten
        # minutes of nobody streaming there is nothing to come back to — and the row is the only
        # thing in the install that grows on its own.
        room = self._room()
        player = self._join(room, "ana")
        self._beat(room, player)
        self._idle_for(room, IDLE)
        self.assertEqual(QuizQuestion.objects.filter(room=room).count(), 8)

        self._entries()
        self.assertFalse(Room.objects.filter(code=room.code).exists())
        # The bank and the tokens go with it. Players left behind would be rows that answer no room,
        # and the player who came back with his token would get a 404 he cannot explain.
        self.assertFalse(Player.objects.filter(pk=player.pk).exists())
        self.assertEqual(QuizQuestion.objects.filter(room__code=room.code).count(), 0)

    def test_a_played_room_leaves_the_list_and_comes_back_with_one_beat(self):
        # THE OTHER HALF, AND THE USER'S DECISION: a game that has been played only DISAPPEARS. A
        # teacher who steps away for ten minutes — a class change, a phone call, a walk to the staff
        # room — comes back to the game they left, players and tokens intact, and the list shows it
        # again the moment somebody reconnects.
        room = self._room()
        player = self._join(room, "ana")
        self._beat(room, player)
        self._start(room)
        self.assertIsNotNone(room.started_at)
        self._idle_for(room, IDLE)

        self.assertNotIn(room.code, self._entries())
        # STILL IN THE DATABASE, and intact: ten rounds, and the player whose token it was.
        self.assertTrue(Room.objects.filter(code=room.code).exists())
        self.assertEqual(room.rounds.count(), ROUNDS_PER_GAME)
        self.assertTrue(Player.objects.filter(pk=player.pk).exists())
        # And the room is still the one that token belongs to — no rejoin, no new code.
        self.assertEqual(self._post(f"/api/rooms/{room.code}/join", {"name": "ana"}).status_code, 409)

        # ONE HEARTBEAT puts it back in the list — no write, no flag, no admin action: the sweep's
        # hide is a function of presence, not a column somebody had to unset.
        self._beat(room, player)
        self.assertEqual(self._entries()[room.code]["phase"], Room.Phase.QUIZ)

    def test_a_room_with_a_live_beat_is_never_idle_however_old_it_is(self):
        # THE STRUCTURAL CASE. `started_at` here is a DAY old, so any rule reading the stamp as an
        # age would have hidden this room; the sweep reads the last BEAT instead, and a phone beating
        # now is a room in use whatever its history says. `Room` has no `created_at` — the room list
        # says so itself — so the stamp is the only thing that can say how old a room is, and this
        # is the test that says it must not be asked.
        room = self._room()
        player = self._join(room, "ana")
        self._beat(room, player)
        self._start(room)
        Room.objects.filter(code=room.code).update(started_at=timezone.now() - timedelta(days=1))
        entry = self._entries()[room.code]
        self.assertEqual((entry["online_count"], entry["phase"]), (1, Room.Phase.QUIZ))
        # Nothing was swept, and structurally so: a room with a beat inside `PRESENCE_TTL` never
        # reaches the delete line at all, three seconds being two hundred times shorter than the
        # idle TTL.
        self.assertTrue(Room.objects.filter(code=room.code).exists())

    def test_a_restarted_room_is_hidden_and_never_deleted(self):
        # THE REGRESSION THAT PINS THE WHOLE DESIGN. After a restart this room has NO ROUNDS, is
        # back in the lobby, and HAS BEEN PLAYED: ten rounds of prompts, ratings and a score
        # happened in it, and its players keep their tokens. `rounds.exists()` — the definition of
        # "started" that `_start_game` itself uses — is FALSE for it. A sweep written on that would
        # delete a finished game the first time it went quiet, which is the exact data loss this
        # repository has refused everywhere else, `Admin.tsx`'s abandon button included.
        room = self._room()
        players = [self._join(room, n) for n in ("ana", "bob")]
        self._beat(room, *players)
        self._start(room)
        self._restart(room)
        self.assertEqual((room.phase, room.rounds.count()), (Room.Phase.WAITING, 0))
        self.assertIsNotNone(room.started_at)
        self._idle_for(room, IDLE)

        self.assertNotIn(room.code, self._entries())
        self.assertTrue(Room.objects.filter(code=room.code).exists())
        # TWICE, because a sweep that only survives its first pass is a sweep that got lucky: the
        # second poll is the one an admin leaves open on a console all afternoon.
        self.assertNotIn(room.code, self._entries())
        self.assertTrue(Room.objects.filter(code=room.code).exists())
        # And the class that relaunched it can still come back into it, tokens and all.
        self.assertEqual(self._post(f"/api/rooms/{room.code}/join", {"name": "zoe"}).status_code, 200)
        self.assertEqual(Player.objects.filter(room=room).count(), 3)

    def test_a_room_with_no_beats_is_not_sweepable_until_the_process_is_ten_minutes_old(self):
        # THE BOOT FLOOR. `time.monotonic()` is meaningless outside the process that read it, so
        # after a restart every room looks idle since before the epoch and the first list poll would
        # delete everything this server ever hosted. The floor makes the question unaskable until
        # this process has itself been up for `ROOM_IDLE_TTL`.
        #
        # THE CLOCK IS PATCHED RATHER THAN WAITED FOR, and that is the only way to test ten minutes:
        # `_BOOT_INSTANT` is a module global read at call time precisely so a test can move it. The
        # second half patches it BACK past the TTL and gets the deletion, which is what proves the
        # first half was the floor doing the work rather than a room that happened to be young.
        room = self._room()
        self._join(room, "ana")
        with _presence_lock:
            self.assertEqual(len(_presence), 0, "this room must have no beats at all")
            # A room from before a restart: this process never saw it opened.
            _opened.pop(room.code)
        self.assertIn(room.code, self._entries())
        self.assertTrue(Room.objects.filter(code=room.code).exists())

        with mock.patch("game.views._BOOT_INSTANT", time.monotonic() - ROOM_IDLE_TTL - 1):
            self.assertNotIn(room.code, self._entries())
        self.assertFalse(Room.objects.filter(code=room.code).exists())

    def test_a_new_room_on_a_long_running_server_survives(self):
        # THE BUG THIS PINS: a server up for more than ten minutes deleted a lobby created a second
        # ago, before anyone could join, because a room with no beats fell back to the boot floor.
        # Its opening instant is its first beat now, so it gets its whole grace period.
        with mock.patch("game.views._BOOT_INSTANT", time.monotonic() - ROOM_IDLE_TTL - 600):
            room = self._room()
            self.assertIn(room.code, self._entries())
        self.assertTrue(Room.objects.filter(code=room.code).exists())
        self.assertEqual(self._post(f"/api/rooms/{room.code}/join", {"name": "ana"}).status_code, 200)

    def test_the_boundary_is_ten_minutes(self):
        # 9 MINUTES IS LISTED, 11 IS NOT, and the two rooms differ in nothing else: same phase,
        # same one player, both never started, both answered in the same response. A rule that used
        # `>=` where it means `>` would put the nine-minute room in the wrong column, and a rule
        # with the wrong constant would put the eleven-minute room in this one.
        near, far = self._room(), self._room()
        for room in (near, far):
            self._beat(room, self._join(room, "ana"))
        self._idle_for(near, ROOM_IDLE_TTL - 60)
        self._idle_for(far, ROOM_IDLE_TTL + 60)

        entries = self._entries()
        self.assertIn(near.code, entries)
        self.assertNotIn(far.code, entries)

    def test_one_get_sweeps_and_answers(self):
        # ONE REQUEST DOES BOTH JOBS, and the response is the answer AFTER the sweep: a console
        # polling this endpoint cannot be shown a room the same request is deleting. Both rooms are
        # idle the same way, so the payload is the only thing that tells them apart — one is hidden,
        # the other is gone.
        played = self._room()
        self._beat(played, self._join(played, "ana"))
        self._start(played)
        lobby = self._room()
        self._beat(lobby, self._join(lobby, "ana"))
        self._idle_for(played, IDLE)
        self._idle_for(lobby, IDLE)

        entries = self._entries()
        self.assertNotIn(played.code, entries)
        self.assertNotIn(lobby.code, entries)
        # The one that went is really gone, in the same request that answered without it.
        self.assertFalse(Room.objects.filter(code=lobby.code).exists())
        self.assertTrue(Room.objects.filter(code=played.code).exists())

    def test_the_entry_shape_is_unchanged_for_a_room_that_stays(self):
        # `started_at` IS THE SWEEP'S INPUT, NOT THE CONSOLE'S. It is selected because the sweep
        # reuses the rows this query already reads, and it must not travel: a ninth key in
        # `RoomSummary` is a field the client has no contract for and no use for.
        room = self._room()
        player = self._join(room, "ana")
        self._beat(room, player)
        self.assertEqual(
            self._entries()[room.code],
            {
                "code": room.code,
                "phase": "waiting",
                "current": 0,
                "total": 0,
                "online_count": 1,
                "players": 1,
                "tutorial": True,
                "anonymous": True,
            },
        )
        # And a room that WAS swept does not change the shape of the ones that stay.
        swept = self._room()
        self._beat(swept, self._join(swept, "ana"))
        self._idle_for(swept, IDLE)
        entries = self._entries()
        self.assertNotIn(swept.code, entries)
        self.assertEqual(set(entries[room.code]), set(self._entries()[room.code]))
        # Eight keys, still: the assertion above compares the entry to itself across two requests,
        # which would hold even if a ninth had appeared.
        self.assertEqual(
            len(entries[room.code]),
            8,
        )


class PlayedRoomDeletionTests(IdleBase):
    """A played game, and the hour after which it is gone for good.

    SEPARATE FROM `IdleSweepTests` because the subject is different: that class pins a rule about
    visibility, this one pins a rule about DELETION, and the only thing they share is a room and
    the four helpers that build one.
    """

    def _played(self) -> Room:
        """A room with a player and a started game — the half the sweep is now allowed to delete."""
        room = self._room()
        self._beat(room, self._join(room, "ana"))
        self._start(room)
        return room

    def _last_seen(self, room: Room, seconds: float) -> None:
        """`last_activity_at` as the sweeper would have written it `seconds` ago.

        WRITTEN THROUGH `.update()` and not by assigning the attribute, because that is how the
        sweep writes it and a test that used `save()` would be exercising a code path the server
        never takes.
        """
        Room.objects.filter(code=room.code).update(
            last_activity_at=timezone.now() - timedelta(seconds=seconds)
        )

    def test_a_played_room_idle_over_an_hour_is_deleted(self) -> None:
        # THREE OUTCOMES, AND THE DELETE ONE IS IRREVERSIBLE: an hour is the deal. Hidden at ten
        # minutes, deleted at sixty, and after that a teacher coming back tomorrow finds nothing.
        room = self._played()
        self._idle_for(room, IDLE)
        self._last_seen(room, ROOM_KEEP_TTL + 60)

        self.assertNotIn(room.code, self._entries())
        self.assertFalse(Room.objects.filter(code=room.code).exists())
        # THE TOKENS GO WITH IT, which is what makes the hour irreversible rather than merely
        # inconvenient: the player who comes back with his token gets a 404 instead of a game.
        self.assertFalse(Player.objects.filter(room__code=room.code).exists())
        self.assertEqual(QuizQuestion.objects.filter(room__code=room.code).count(), 0)
        self.assertEqual(self._post(f"/api/rooms/{room.code}/join", {"name": "ana"}).status_code, 404)

    def test_a_played_room_idle_for_twenty_minutes_survives(self) -> None:
        # HIDDEN FROM THE LIST, STILL IN THE DATABASE: this is the window a teacher uses to come
        # back to a game they paused, and it is the reason the two thresholds are two numbers.
        room = self._played()
        self._idle_for(room, IDLE)
        self._last_seen(room, 1200.0)
        # The window this test is about: past the hide threshold, well short of the delete one.
        self.assertGreater(1200.0, ROOM_IDLE_TTL)
        self.assertLess(1200.0, ROOM_KEEP_TTL)

        self.assertNotIn(room.code, self._entries())
        self.assertTrue(Room.objects.filter(code=room.code).exists())
        self.assertEqual(room.rounds.count(), ROUNDS_PER_GAME)
        # One beat brings it back, tokens and all, and nothing was lost in the meantime.
        self._beat(room, Player.objects.get(room=room))
        self.assertEqual(self._entries()[room.code]["phase"], Room.Phase.QUIZ)

    def test_the_last_activity_survives_a_restart(self) -> None:
        # `_presence` and `_opened` ARE PROCESS-LOCAL and a restart empties them, so this test empties them by hand and
        # moves the boot floor past the idle TTL — which is the state the server is in one minute
        # after being stopped and started. The durable column is the only reason the one-hour rule
        # is reachable on the SECOND run rather than never.
        room = self._played()
        # The thread is the writer, so a tick is what has to happen here: a GET would leave the
        # column null and the restart below would have nothing to remember.
        _sweep_tick()
        self.assertIsNotNone(Room.objects.get(code=room.code).last_activity_at)
        # Twenty minutes of nobody, then a restart, then two hours of nobody. The beats are gone
        # with the old process; only the column remembers.
        self._last_seen(room, ROOM_KEEP_TTL + 60)
        with _presence_lock:
            _presence.clear()
            _opened.clear()
        with mock.patch("game.views._BOOT_INSTANT", time.monotonic() - ROOM_IDLE_TTL - 60):
            self.assertNotIn(room.code, self._entries())
        self.assertFalse(Room.objects.filter(code=room.code).exists())

    def test_a_live_beat_beats_a_two_hour_old_last_activity(self) -> None:
        # THE SAFETY NET. `last_activity_at` is written once a minute, so between two ticks it is
        # up to `SWEEP_EVERY` stale — but a STALE COLUMN MUST NEVER OUTRANK A LIVE BEAT, or a tick
        # that failed to write would delete the game being played right now. The beats come first
        # and a room with one never reaches a delete line.
        room = self._played()
        stale = timezone.now() - timedelta(hours=2)
        Room.objects.filter(code=room.code).update(last_activity_at=stale)
        self._beat(room, Player.objects.get(room=room))

        self.assertIn(room.code, self._entries())
        self.assertTrue(Room.objects.filter(code=room.code).exists())
        # The GET left the stale stamp exactly as it found it, which is the whole point of the
        # split: the read path cannot make the column younger or older than the last tick left it.
        self.assertEqual(Room.objects.get(code=room.code).last_activity_at, stale)
        # And the tick that spared the room moved the instant forward, so an hour from now the
        # delete is measured from now rather than from two hours ago.
        _sweep_tick()
        self.assertGreater(
            Room.objects.get(code=room.code).last_activity_at, timezone.now() - timedelta(minutes=1)
        )

    def test_the_thread_tick_is_the_only_writer_of_the_activity(self) -> None:
        # THE COVERAGE THIS SPLIT HAD TO KEEP. With the write off the GET, `_sweep_tick` is the
        # single place `last_activity_at` is ever written; if that were lost, nothing would fail
        # loudly — the column would simply stay null, every played room would fall back to
        # `started_at`, and the one-hour rule would quietly start measuring from the wrong instant.
        room = self._played()
        self.assertIsNone(Room.objects.get(code=room.code).last_activity_at)

        _sweep_tick()
        self.assertIsNotNone(Room.objects.get(code=room.code).last_activity_at)
        # AND A TICK WITH NOBODY CONNECTED WRITES NOTHING, so an idle install pays no write per
        # minute: no beat means no room in `active`, and no beat means nothing worth dating.
        self._idle_for(room, IDLE)
        Room.objects.filter(code=room.code).update(last_activity_at=None)
        _sweep_tick()
        self.assertIsNone(Room.objects.get(code=room.code).last_activity_at)

    def test_the_admin_list_writes_nothing_even_with_a_live_beat(self) -> None:
        # THE INVARIANT, UNCONDITIONAL. `admin_list_rooms` is a GET polled every few seconds while a
        # game is live; stamping `last_activity_at` from there would mean an order of magnitude
        # more writes than the sweeper makes, for a value only ever compared against an hour.
        # Measured the way `test_admin.py` measures it, so a second write here would fail in the
        # idiom that already pins the enumeration.
        room = self._played()
        self._beat(room, Player.objects.get(room=room))
        # A live beat, not a fixture with none: the UPDATE used to be issued exactly when somebody
        # was connected, the only moment the property was not free.
        with CaptureQueriesContext(connection) as ctx:
            self.assertIn(room.code, self._entries())
        # TWO SELECTS, NOT ONE, and the second is not the sweep's: it is the online-count lookup
        # over `Player.objects.filter(pk__in=connected)`, which Django short-circuits to no query at
        # all when nobody is connected — which is the only reason `test_admin` gets to say one.
        # That lookup is how `online_count` is filled in and is nobody's business here.
        self.assertEqual(len(ctx.captured_queries), 2)
        # What this test is actually about: NOTHING IS WRITTEN. A sweep that records the activity
        # from the read path would show up as a third query, an UPDATE, and no other assertion in
        # the suite would notice.
        self.assertTrue(all(q["sql"].startswith("SELECT") for q in ctx.captured_queries))
        # And the read path leaves the column exactly as it found it, so the value in the row is
        # always one a `_sweep_tick` wrote and never one a console GET aged.
        self.assertIsNone(Room.objects.get(code=room.code).last_activity_at)

    def test_no_thread_starts_without_the_env_var(self) -> None:
        # THE GUARD IS THE THING UNDER TEST, not a detail: `AppConfig.ready()` also runs under
        # `manage.py test`, and a thread deleting rows under the suite is a suite that passes or
        # fails depending on the wall clock. Compose is the only thing that sets the variable.
        self.assertIsNone(os.environ.get("GENAI_SWEEP"))
        self.assertEqual(
            [t.name for t in threading.enumerate() if t.name == "room-sweep"],
            [],
            "no sweeper may exist while the suite runs",
        )
        # Calling `ready()` here must stay a no-op too, not merely be a no-op by accident of the
        # thread already being absent.
        with mock.patch.dict(os.environ, {}, clear=True):
            apps.get_app_config("game").ready()
        self.assertEqual([t.name for t in threading.enumerate() if t.name == "room-sweep"], [])

class ThirtyMinuteExpiryTests(IdleBase):
    """Thirty minutes idle deletes, a live beat spares."""

    def test_idle_thirty_minutes_is_purged_while_active_survives(self) -> None:
        from game.views import ROOM_KEEP_TTL

        # Both rooms are built first: `_beat` prunes entries older than `PRESENCE_TTL`,
        # so ageing idle before beating live would wipe the aged beats, not test them.
        idle = self._room()
        idle_player = self._join(idle, "ana")
        self._beat(idle, idle_player)
        self._start(idle)
        live = self._room()
        live_player = self._join(live, "bob")
        self._beat(live, live_player)
        self._start(live)

        self._idle_for(idle, IDLE)
        Room.objects.filter(code=idle.code).update(
            last_activity_at=timezone.now() - timedelta(seconds=ROOM_KEEP_TTL + 60)
        )

        entries = self._entries()
        self.assertFalse(Room.objects.filter(code=idle.code).exists())
        self.assertNotIn(idle.code, entries)
        self.assertTrue(Room.objects.filter(code=live.code).exists())
        self.assertIn(live.code, entries)
