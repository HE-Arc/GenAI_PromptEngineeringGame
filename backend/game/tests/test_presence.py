import json
import time
from unittest.mock import patch

from django.test import Client, TestCase

from game.llm import Verdict
from game.models import Answer, Player, Room, Round
from game.views import (
    PRESENCE_TTL,
    _beat,
    _inflight,
    _presence,
    _presence_lock,
    maybe_advance,
    online_player_ids,
    room_state,
)


class PresenceTest(TestCase):
    def setUp(self) -> None:
        self.c = Client()
        self.room = Room.objects.create(code="ABCD", status=Room.Status.PLAYING)
        Round.objects.create(room=self.room, order=0, target="42")
        Round.objects.create(room=self.room, order=1, target="Nantes")
        # Process-local, so a DB rollback does not clear it. Without this, a heartbeat from an
        # earlier test marks a same-named player online in a later one and every assertion below
        # is measuring the previous test's leftovers.
        _presence.clear()

    def _player(self, name: str) -> Player:
        return Player.objects.create(room=self.room, name=name)

    def _stream(self, token: str | None):
        """Open the SSE stream and pull exactly one frame, which is when the first tick beats.

        A second `next()` would block for 0.5s, so the frame boundary is also the test's clock:
        everything the stream did before its first `yield` has already happened.
        """
        url = "/api/rooms/ABCD/events" + (f"?token={token}" if token is not None else "")
        r = self.c.get(url)
        self.assertEqual(r.status_code, 200)
        frame = next(r.streaming_content)
        r.close()
        return frame

    def _age(self, name: str, seconds: float) -> None:
        """Push a player's last beat into the past without sleeping, by writing the registry
        directly. The entry is still there — the point is that age alone must decide presence.
        Takes the registry's own lock, so a test writing entries is not doing what the SSE
        threads would be protected from doing."""
        pid = Player.objects.get(room=self.room, name=name).pk
        with _presence_lock:
            _presence[("ABCD", pid)] = time.monotonic() - seconds

    # --- flags and count -------------------------------------------------------------------

    def test_player_with_a_live_stream_is_online(self) -> None:
        ana = self._player("ana")
        bob = self._player("bob")
        self._stream(ana.token)
        st = room_state("ABCD")
        flags = {p["name"]: p["online"] for p in st["players"]}
        self.assertEqual(flags, {"ana": True, "bob": False})  # bob never opened a stream
        self.assertEqual(online_player_ids("ABCD"), {ana.pk})

    def test_stream_with_a_missing_token_registers_nobody_and_still_streams(self) -> None:
        # The admin screen and any spectator watch the same endpoint with no token. A 403 here
        # would take the lobby down, so the frame arriving is what matters, not the auth.
        self._player("ana")
        frame = self._stream(None)
        self.assertTrue(frame.startswith(b"data: "), frame)
        self.assertEqual(online_player_ids("ABCD"), set())
        self.assertEqual(room_state("ABCD")["online_count"], 0)

    def test_stream_with_a_bogus_token_registers_nobody(self) -> None:
        self._player("ana")
        frame = self._stream("not-a-real-token")
        self.assertTrue(frame.startswith(b"data: "), frame)
        self.assertEqual(online_player_ids("ABCD"), set())
        self.assertEqual(room_state("ABCD")["online_count"], 0)

    def test_token_from_another_room_cannot_claim_a_heartbeat_here(self) -> None:
        # The lookup filters on `room` as well as `token`. Dropping `room=room` lets a player
        # who is not in this room hold the round open, and show in the roster as online.
        self._player("ana")
        other = Room.objects.create(code="WXYZ", status=Room.Status.PLAYING)
        stranger = Player.objects.create(room=other, name="zoe")
        self._stream(stranger.token)
        self.assertEqual(online_player_ids("ABCD"), set())
        # Not registered in their own room either: this stream is ABCD's, and a token is only ever
        # a heartbeat for the room the stream was opened on.
        self.assertEqual(online_player_ids("WXYZ"), set())

    def test_two_streams_on_one_player_are_one_player(self) -> None:
        # A phone that reconnects, or a second tab, is still one presence entry. The registry is
        # keyed on the player, not on the stream, so the roster cannot show them twice.
        ana = self._player("ana")
        self._stream(ana.token)
        self._stream(ana.token)
        st = room_state("ABCD")
        self.assertEqual(len(st["players"]), 1)
        self.assertEqual(st["online_count"], 1)

    def test_beat_expires_after_the_ttl(self) -> None:
        self._player("ana")
        self._age("ana", PRESENCE_TTL + 0.1)
        self.assertEqual(online_player_ids("ABCD"), set())
        self.assertEqual(room_state("ABCD")["online_count"], 0)
        # One beat inside the window is still online: the TTL is deliberately several ticks, so a
        # single late beat does not flap the indicator.
        self._age("ana", PRESENCE_TTL - 0.5)
        self.assertEqual(len(online_player_ids("ABCD")), 1)

    def test_a_stale_beat_is_evicted_by_the_next_tick(self) -> None:
        # The prune is what stops a room abandoned mid-game from pinning its dead players in the
        # registry forever. A *different* player ticking is what evicts them: a player's own beat
        # revives their own entry, which is the recovery path, not the eviction path.
        ana = self._player("ana")
        bob = self._player("bob")
        self._age("ana", PRESENCE_TTL + 1)
        self.assertIn(("ABCD", ana.pk), _presence)
        _beat("ABCD", bob.pk)
        self.assertNotIn(("ABCD", ana.pk), _presence)
        self.assertIn(("ABCD", bob.pk), _presence)

    def test_online_count_matches_the_per_player_flags(self) -> None:
        for name in ("ana", "bob", "cleo"):
            self._player(name)
        self._stream(Player.objects.get(name="ana").token)
        self._stream(Player.objects.get(name="cleo").token)
        st = room_state("ABCD")
        self.assertEqual(st["online_count"], sum(1 for p in st["players"] if p["online"]))
        self.assertEqual(st["online_count"], 2)
        self.assertIsInstance(st["online_count"], int)

    def test_presence_in_one_room_does_not_leak_into_another(self) -> None:
        self._player("ana")
        self._stream(Player.objects.get(name="ana").token)
        Room.objects.create(code="WXYZ", status=Room.Status.WAITING)
        self.assertEqual(room_state("WXYZ")["online_count"], 0)
        self.assertEqual(online_player_ids("WXYZ"), set())

    # --- game logic ------------------------------------------------------------------------

    @patch("game.llm.score", return_value=Verdict(8, "tres proche"))
    @patch("game.llm.answer", return_value="quarante-deux")
    def test_round_closes_when_every_online_player_answered(self, _m_answer, _m_score) -> None:
        # The whole point of the feature. `bob` is in `room.players` and will never answer: under
        # the old `answers.count() == players.count()` this round never closed and the party hung
        # on it. Ana online and answering is enough.
        ana = self._player("ana")
        self._player("bob")
        _beat("ABCD", ana.pk)
        r = self.c.post(
            "/api/rooms/ABCD/answer",
            data=json.dumps({"token": ana.token, "prompt": "repete 42"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.room.refresh_from_db()
        self.assertEqual((self.room.current, self.room.status), (1, "playing"))

    def test_round_stays_open_while_an_online_player_has_not_answered(self) -> None:
        # The flip side: presence must not make the round close early on the players who ARE
        # there. Ana beating is not an answer.
        self._player("ana")
        self._player("bob")
        for name in ("ana", "bob"):
            _beat("ABCD", Player.objects.get(room=self.room, name=name).pk)
        maybe_advance(self.room)
        self.room.refresh_from_db()
        self.assertEqual((self.room.current, self.room.status), (0, "playing"))

    def test_round_does_not_advance_when_nobody_is_online(self) -> None:
        # `0 == 0` is true: with every phone dropped, "all online players have answered" holds
        # vacuously and the room would run itself to `done`. A round is only ever closed by a real
        # player's submit, never by the absence of one.
        ana = self._player("ana")
        rnd = Round.objects.get(room=self.room, order=0)
        rnd.answers.create(player=ana, prompt="a", output="42", score=8)
        maybe_advance(self.room)
        self.room.refresh_from_db()
        self.assertEqual((self.room.current, self.room.status), (0, "playing"))

    def test_a_player_going_offline_stops_holding_the_round(self) -> None:
        ana, bob = self._player("ana"), self._player("bob")
        _beat("ABCD", ana.pk)
        _beat("ABCD", bob.pk)
        # Bob's phone sleeps: no more beats, so he ages out on the TTL alone.
        self._age("bob", PRESENCE_TTL + 0.1)
        Round.objects.get(room=self.room, order=0).answers.create(player=ana, prompt="a", output="42", score=8)
        maybe_advance(self.room)
        self.room.refresh_from_db()
        self.assertEqual((self.room.current, self.room.status), (1, "playing"))

    # --- the round trip: offline, then back, in a ten-round game ----------------------------

    @patch("game.llm.score", return_value=Verdict(8, "tres proche"))
    @patch("game.llm.answer", return_value="quarante-deux")
    def test_an_offline_player_who_comes_back_can_still_answer(self, m_answer, _m_score) -> None:
        """The stall fix must not cost a player the rest of the game.

        This is the whole round trip, in order, because each step is a place the feature could
        quietly break the next one: bob goes offline, the round closes over his head, his beats
        resume, and he answers the round that is open *now*. Ten rounds is long enough for a
        phone to drop once; nothing here may turn that into a permanent lockout.
        """
        ana, bob = self._player("ana"), self._player("bob")
        Round.objects.create(room=self.room, order=2, target="Bordeaux")  # long enough to drop a phone in
        _beat("ABCD", ana.pk)
        _beat("ABCD", bob.pk)

        # 1. Bob's phone sleeps. No more beats, so the TTL retires him on its own — no close
        #    event, which is the whole reason presence is a heartbeat and not a disconnect.
        self._age("bob", PRESENCE_TTL + 0.1)
        self.assertEqual(online_player_ids("ABCD"), {ana.pk})

        # 2. Ana answers. The round closes without waiting for bob: the stall fix.
        r = self._answer(ana, "repete 42")
        self.assertEqual(r.status_code, 200)
        self.room.refresh_from_db()
        self.assertEqual((self.room.current, self.room.status), (1, "playing"))

        # 3. No retroactive "already answered" for a round bob never submitted to. The only
        #    durable record of an answer is the Answer row, and the `answered` flag in the payload
        #    is derived from it — bob has none, so he is not shown as having answered round 0.
        self.assertEqual(Answer.objects.filter(player=bob).count(), 0)
        bob_view = next(p for p in room_state("ABCD")["players"] if p["name"] == "bob")
        self.assertFalse(bob_view["answered"])
        self.assertFalse(bob_view["online"])
        self.assertIsNone(bob_view["prompt"])
        self.assertIsNone(bob_view["output"])
        self.assertIsNone(bob_view["round_score"])

        # 4. Bob's beats resume. Presence is derived from the registry, not stored anywhere
        #    durable, so there is no flag to flip back and nothing to repair: the next beat is
        #    the whole recovery. That is why there is no `Player.online` column — a stored flag
        #    would need exactly this code to clear it, and would be wrong whenever the process
        #    restarted instead.
        _beat("ABCD", bob.pk)
        self.assertEqual(online_player_ids("ABCD"), {ana.pk, bob.pk})
        self.assertEqual(room_state("ABCD")["online_count"], 2)

        # 5. He can still answer the round that is open now, and is still not shown as having
        #    answered round 0 — his comeback does not rewrite history in either direction.
        r = self._answer(bob, "Nantes")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(json.loads(r.content)["score"], 8)
        self.assertEqual(Answer.objects.filter(player=bob).count(), 1)
        self.assertEqual(Answer.objects.get(player=bob).prompt, "Nantes")
        self.assertFalse(Answer.objects.filter(player=bob, round__order=0).exists())
        self.assertEqual(m_answer.call_count, 2)  # both submits reached the model

        # 6. And the round now waits for him again, like anyone else who is there: presence is
        #    live in both directions. Ana is the only other player online and she has not answered
        #    round 1, so it stays open rather than closing over bob's head a second time.
        self.room.refresh_from_db()
        self.assertEqual((self.room.current, self.room.status), (1, "playing"))
        maybe_advance(self.room)
        self.room.refresh_from_db()
        self.assertEqual((self.room.current, self.room.status), (1, "playing"))
        self._answer(ana, "ville du monde")
        self.room.refresh_from_db()
        self.assertEqual((self.room.current, self.room.status), (2, "playing"))  # back in the game
        self.assertEqual(room_state("ABCD")["online_count"], 2)

    @patch("game.llm.score", return_value=Verdict(5, "moyen"))
    @patch("game.llm.answer", return_value="42")
    def test_a_dropped_submit_leaves_no_answer_row(self, _m_answer, _m_score) -> None:
        """A prompt typed but never sent is client state, and it stays there.

        Bob's browser dies between typing and the POST landing. Nothing server-side has run: the
        row is created only after both billed calls return, inside the `try` whose `finally`
        releases the `_inflight` claim. So the client sees no answer, and must not be shown one —
        which is what keeps him eligible to answer this round when he reconnects.
        """
        bob = self._player("bob")
        _beat("ABCD", bob.pk)
        self.assertEqual(Answer.objects.filter(player=bob).count(), 0)
        st = room_state("ABCD")
        self.assertEqual(st["players"][0]["answered"], False)
        self.assertEqual(st["online_count"], 1)
        # And the round he never answered is still open, with him in it.
        maybe_advance(self.room)
        self.room.refresh_from_db()
        self.assertEqual((self.room.current, self.room.status), (0, "playing"))

    @patch("game.llm.score", side_effect=RuntimeError("model call failed"))
    @patch("game.llm.answer", return_value="42")
    def test_a_failed_submit_rolls_back_completely(self, _m_answer, _m_score) -> None:
        """The all-or-nothing claim, from the client's point of view.

        `llm.score` raising is the model call blowing up after `llm.answer` already returned. The
        `create` is never reached, so no row exists; `finally` still discards the claim, so the
        player is not locked out of the round by a request that failed. A row written before the
        second call returned would be the bug this guards: bob would reconnect to a round showing
        `answered: true` for a submission that never completed.
        """
        bob = self._player("bob")
        _beat("ABCD", bob.pk)
        with self.assertRaises(RuntimeError):
            self._answer(bob, "repete 42")
        self.assertEqual(Answer.objects.filter(player=bob).count(), 0)
        self.assertEqual(len(_inflight), 0)  # the claim was released despite the exception
        self.assertFalse(room_state("ABCD")["players"][0]["answered"])

    def _answer(self, player: Player, prompt: str):
        return self.c.post(
            "/api/rooms/ABCD/answer",
            data=json.dumps({"token": player.token, "prompt": prompt}),
            content_type="application/json",
        )

    # --- serialisation ---------------------------------------------------------------------

    def test_state_still_serialises_to_json(self) -> None:
        # A past bug: the sort key carried `created_at`, a datetime, and this payload goes
        # straight into `json.dumps` on the SSE thread. `online` must not reintroduce anything
        # unserialisable — a monotonic float or a set would raise there and kill the stream.
        for name in ("ana", "bob"):
            self._player(name)
        self._stream(Player.objects.get(name="ana").token)
        payload = room_state("ABCD")
        dumped = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(json.loads(dumped), payload)
        self.assertIsInstance(payload["online_count"], int)
        for p in payload["players"]:
            self.assertIsInstance(p["online"], bool)
