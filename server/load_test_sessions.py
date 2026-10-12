"""Realistic classroom load: R rooms of P players, each player on their own phone.

load_test_ws.py is the worst case (N requests in the same millisecond). This is
the expected case: every player is an independent agent that reads the room
state off its own socket and acts on it after a human think time, the way the
frontend does — so actions spread out instead of arriving together. Phones
also misbehave: a screen locks mid-game (socket gone, frames missed), comes
back, says hello and must resync to the live state.

Each room has an admin console watching over the admin cookie and pressing
« suivant » after a short discussion, exactly as a teacher would.

What it checks, per room and overall:
- every room finishes the requested number of rounds (no stuck phase);
- no server failure (HTTP 5xx, `internal` error frames);
- every phone that is connected sees each change of the room within the
  fan-out budget, and a reconnecting phone gets the live state on hello;
- a short phone lock (< the server's 60 s grace) never shows the player as
  offline to the others.

Think times are divided by --speed so a 25-minute class replays in a few
minutes; phone locks are NOT scaled, because the server's presence grace is a
real 60 s and the point is to test it. Stdlib only. Refuses to run billed.
"""
import argparse
import json
import os
import random
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from load_test_ws import Client, Http, fmt_lat, pct  # noqa: E402

LOCK = threading.Lock()
METRICS = {
    "action": {},  # kind -> [latency s] of the player's own request
    "verdict": [],  # answer.start -> own answer.done
    "resync": [],  # reconnect -> live snapshot
    "benign": {},  # expected refusals of a phone racing the room
    "failures": [],  # anything that is a server bug
}


def metric(kind, value):
    with LOCK:
        METRICS["action"].setdefault(kind, []).append(value)


def note(bucket, key):
    with LOCK:
        METRICS[bucket][key] = METRICS[bucket].get(key, 0) + 1


def fail(msg):
    with LOCK:
        METRICS["failures"].append(msg)


# Refusals a real phone gets when the room moved between its read and its tap.
# They are the protocol working, not errors.
BENIGN = {"time's up", "not the current question", "not the current prompt", "already rated",
          "already ready", "nothing to discuss", "not rating", "quiz not open", "already answered"}


def key_of(state):
    """The room position a screen shows; a change of key is what must fan out."""
    quiz = state.get("quiz") or {}
    reveal = state.get("reveal") or {}
    return (state["phase"], state["current"], quiz.get("index"), quiz.get("revealed"), reveal.get("index"))


class Phone:
    """One player: a socket that comes and goes, and a person who acts on it."""

    def __init__(self, room, name, token, args, rng):
        self.room, self.name, self.token, self.a, self.rng = room, name, token, args, rng
        self.client = None
        self.locked_until = 0.0
        self.locks = []  # (start, end) monotonic
        self.done_keys = set()
        self.clients = []  # every socket this phone ever had, for fan-out analysis

    def think(self, lo, hi):
        time.sleep(self.rng.uniform(lo, hi) / self.a.speed)

    def connect(self):
        t0 = time.monotonic()
        c = Client(self.a.base, self.room.code, self.name, self.token)
        c.send({"type": "hello", "token": self.token})
        if c.wait(lambda c: c.snapshot is not None, 10) is None:
            fail(f"{self.name}: no snapshot after hello")
        elif self.client is not None:
            METRICS["resync"].append(time.monotonic() - t0)
        self.client = c
        self.clients.append(c)

    def maybe_lock(self):
        """Some phones lock for a while: socket closed, frames missed."""
        if self.rng.random() >= self.a.lock_chance:
            return
        duration = self.rng.uniform(5, 40)  # real seconds: under the 60 s grace
        start = time.monotonic()
        self.client.close()
        time.sleep(duration)
        self.locks.append((start, time.monotonic()))
        self.connect()

    def post(self, kind, path, body):
        dt, status, resp = self.room.http.call("POST", path, body)
        metric(kind, dt)
        if status == 200:
            return resp
        msg = resp.get("error") if isinstance(resp, dict) else str(resp)
        if status in (409,) and msg in BENIGN:
            note("benign", f"{kind}: {msg}")
        else:
            fail(f"{self.name} {kind}: HTTP {status} {msg}")
        return None

    def answer(self, state):
        c = self.client
        mark = len(c.frames)
        t0 = time.monotonic()
        c.send({"type": "answer.start", "prompt": f"Tu es un professeur. Contexte: classe de {self.name}. "
                                                  "Format: liste de 3 puces, maximum 40 mots."})

        def settled(c):
            for _, m in c.frames[mark:]:
                if m.get("type") == "answer.done" and m["player"] == self.name:
                    return True
                if m.get("type") == "error" and m.get("player", self.name) == self.name:
                    return True
            return False

        if c.wait(settled, 60) is None:
            fail(f"{self.name}: no verdict within 60 s")
            return
        for _, m in c.frames[mark:]:
            if m.get("type") == "error" and m.get("player", self.name) == self.name:
                if m.get("message") in BENIGN:
                    note("benign", f"answer: {m['message']}")
                else:
                    fail(f"{self.name} answer: {m.get('code')} {m.get('message')}")
                return
        METRICS["verdict"].append(time.monotonic() - t0)

    def run(self):
        self.think(0, 20)  # arrivals spread over the first seconds of class
        joined = self.room.http.call("POST", f"/api/rooms/{self.room.code}/join", {"name": self.name})
        metric("join", joined[0])
        if joined[1] != 200:
            fail(f"{self.name} join: HTTP {joined[1]} {joined[2]}")
            return
        self.token = joined[2]["token"]
        self.room.arrived()
        self.connect()
        seen = None
        while not self.room.over.is_set():
            c = self.client
            # Wait for the room to move (or a short beat, to notice the end).
            c.wait(lambda c: c.snapshot is not None and key_of(c.snapshot) != seen, 1.0)
            if c.closed:
                self.connect()
                continue
            state = c.snapshot
            k = key_of(state)
            if k == seen:
                continue
            seen = k
            if k in self.done_keys:
                continue
            self.maybe_lock()
            if self.client is not c:
                seen = None  # locked through the step: act on the live state instead
                continue
            self.act(state)
            self.done_keys.add(k)

    def act(self, state):
        code, phase = self.room.code, state["phase"]
        if phase == "waiting":
            # The lobby vote starts the game as soon as every CONNECTED player
            # is ready, which locks out anyone not yet in the room. A class
            # votes once everybody is there, so the phone waits for that.
            self.room.all_in.wait()
            self.think(2, 15)
            self.post("ready", f"/api/rooms/{code}/ready", {"token": self.token})
        elif phase == "quiz" and state["quiz"] and not state["quiz"]["revealed"]:
            self.think(4, 25)  # read the question and four long choices
            self.post("quiz", f"/api/rooms/{code}/quiz",
                      {"token": self.token, "question": state["quiz"]["index"], "choice": self.rng.randrange(4)})
        elif phase == "playing":
            me = next((p for p in state["players"] if p["name"] == self.name), None)
            if me and not me["answered"]:
                self.think(15, 50)  # write a prompt on a phone keyboard
                self.answer(state)
        elif phase == "rating" and state["reveal"] and not state["reveal"]["mine"]:
            self.think(3, 12)
            self.post("rate", f"/api/rooms/{code}/rate",
                      {"token": self.token, "score": self.rng.randint(1, 10), "index": state["reveal"]["index"]})


class Room:
    """One class: an admin console plus its players."""

    def __init__(self, args, admin_http, cookie, idx):
        self.a, self.http, self.cookie, self.idx = args, Http(args.base), cookie, idx
        self.admin = admin_http
        _, st, body = admin_http.call("POST", "/api/admin/rooms", {
            "tutorial": True, "round_seconds": args.round_seconds, "rate_seconds": args.rate_seconds})
        if st != 200:
            sys.exit(f"room create failed: HTTP {st} {body}")
        self.code = body["code"]
        self.over = threading.Event()
        self.all_in = threading.Event()
        self._arrived = 0
        self.finished = False
        self.min_online_while_short_locks = None
        self.phones = [Phone(self, f"r{idx}p{i}", None, args, random.Random(idx * 1000 + i))
                       for i in range(args.players)]

    def arrived(self):
        with LOCK:
            self._arrived += 1
            if self._arrived == len(self.phones):
                self.all_in.set()

    def run_admin(self):
        console = Client(self.a.base, self.code, f"admin{self.idx}", "", cookie=self.cookie)
        console.send({"type": "hello", "token": ""})
        self.console = console
        deadline = time.monotonic() + self.a.timeout
        seen = None
        while time.monotonic() < deadline:
            console.wait(lambda c: c.snapshot is not None and key_of(c.snapshot) != seen, 1.0)
            s = console.snapshot
            if s is None:
                continue
            seen = key_of(s)
            phase = s["phase"]
            # The teacher comments, then presses « suivant ».
            if phase == "quiz" and s["quiz"]["revealed"]:
                time.sleep(4 / self.a.speed)
                self.admin.call("POST", f"/api/admin/rooms/{self.code}/next")
            elif phase == "summary":
                if s["current"] + 1 >= self.a.rounds:
                    self.finished = True
                    break
                time.sleep(8 / self.a.speed)
                self.admin.call("POST", f"/api/admin/rooms/{self.code}/next")
            elif phase == "done":
                self.finished = True
                break
        self.over.set()

    def run(self):
        admin = threading.Thread(target=self.run_admin, daemon=True)
        admin.start()
        with ThreadPoolExecutor(max_workers=len(self.phones)) as ex:
            futures = [ex.submit(p.run) for p in self.phones]
            for f in futures:
                try:
                    f.result()
                except Exception as e:  # a harness bug must not look like a pass
                    fail(f"room {self.code} player crashed: {type(e).__name__}: {e}")
        admin.join()

    def analyse(self):
        """Fan-out spread per room change, and presence during short locks."""
        locked = [(p.name, s, e) for p in self.phones for s, e in p.locks]
        first_seen = {}  # key -> {player: first time}
        for p in self.phones:
            for c in p.clients:
                snaps = [(t, m) for t, m in c.frames if m.get("type") == "snapshot"]
                # Only TRANSITIONS count: the first frame where this phone's
                # view moves on. The hello reply (join/resync, measured
                # separately) only sets the starting point.
                prev = key_of(snaps[0][1]["state"]) if snaps else None
                for t, m in snaps[1:]:
                    k = key_of(m["state"])
                    if k == prev:
                        continue
                    prev = k
                    first_seen.setdefault(k, {})
                    if p.name not in first_seen[k] or t < first_seen[k][p.name]:
                        first_seen[k][p.name] = t
        spreads, late = [], 0
        for k, by_player in first_seen.items():
            t0 = min(by_player.values())
            for name, t in by_player.items():
                # A phone whose screen was locked when the change happened sees
                # it on reconnect; that is the resync path, not fan-out.
                if any(n == name and s - 1 <= t0 <= e + 1 for n, s, e in locked):
                    continue
                spreads.append(t - t0)
                if os.environ.get("FANOUT_DEBUG") and t - t0 > 0.3:
                    first = min(by_player, key=by_player.get)
                    print(f"    late {t - t0:.3f}s {name} key={k} first={first}")
        # Presence: during any short lock, the console must still count everyone.
        dips = 0
        for t, m in self.console.frames:
            if m.get("type") == "snapshot" and m["state"]["phase"] != "waiting":
                if m["state"]["online_count"] < len(self.phones):
                    dips += 1
        return spreads, dips, locked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:3001")
    ap.add_argument("--password", default=os.environ.get("ADMIN_PASSWORD", "genai"))
    ap.add_argument("--rooms", type=int, default=10)
    ap.add_argument("--players", type=int, default=10, help="players per room")
    ap.add_argument("--rounds", type=int, default=2, help="prompt rounds to play after the didacticiel")
    ap.add_argument("--speed", type=float, default=5.0, help="divide human think times by this")
    ap.add_argument("--lock-chance", type=float, default=0.03, help="chance a phone locks at each room change")
    ap.add_argument("--round-seconds", type=int, default=120)
    ap.add_argument("--rate-seconds", type=int, default=30)
    ap.add_argument("--timeout", type=float, default=900, help="max seconds per room")
    ap.add_argument("--fanout", type=float, default=1.0, help="gate: p99 fan-out spread (s)")
    ap.add_argument("--allow-billed", action="store_true")
    a = ap.parse_args()
    if (os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")) and not a.allow_billed:
        sys.exit("refusing: an LLM key is set (would bill every prompt); unset it or pass --allow-billed")

    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    admin = Http(a.base, jar)
    if admin.call("POST", "/api/admin/login", {"password": a.password})[1] != 200:
        sys.exit("admin login failed")
    cookie = "; ".join(f"{c.name}={c.value}" for c in jar)
    rooms = [Room(a, admin, cookie, i) for i in range(a.rooms)]
    total = a.rooms * a.players
    print(f"{a.rooms} rooms x {a.players} players = {total} phones, {a.rounds} rounds, "
          f"think times /{a.speed:g}, lock chance {a.lock_chance:.0%} per change")
    t0 = time.monotonic()
    threads = [threading.Thread(target=r.run, daemon=True) for r in rooms]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.monotonic() - t0
    print(f"\nall rooms ended in {wall:.0f}s")

    gates = []
    spreads, dips, locks = [], 0, 0
    for r in rooms:
        s, d, lk = r.analyse()
        spreads += s
        dips += d
        locks += len(lk)
        print(f"  room {r.code}: {'finished' if r.finished else 'STUCK in ' + str(key_of(r.console.snapshot))}, "
              f"{len(lk)} phone locks")
    for kind, lat in sorted(METRICS["action"].items()):
        print(f"  {kind:6s} n={len(lat):4d}  {fmt_lat(lat)}")
    print(f"  verdict n={len(METRICS['verdict']):4d}  {fmt_lat(METRICS['verdict'])}  (answer.start -> own score)")
    print(f"  resync  n={len(METRICS['resync']):4d}  {fmt_lat(METRICS['resync'])}  (phone unlock -> live state)")
    print(f"  fan-out spread n={len(spreads)}  p50={pct(spreads, 50)*1000:.0f}ms "
          f"p99={pct(spreads, 99)*1000:.0f}ms max={max(spreads, default=0)*1000:.0f}ms")
    if METRICS["benign"]:
        print("  expected race refusals: " + ", ".join(f"{k} x{v}" for k, v in sorted(METRICS["benign"].items())))

    gates.append(("every room finished", all(r.finished for r in rooms),
                  f"{sum(r.finished for r in rooms)}/{len(rooms)}"))
    gates.append(("no server failure", not METRICS["failures"],
                  f"{len(METRICS['failures'])} {METRICS['failures'][:3]}"))
    gates.append(("fan-out p99 under budget", pct(spreads, 99) < a.fanout,
                  f"{pct(spreads, 99)*1000:.0f}ms < {a.fanout*1000:.0f}ms"))
    rs = METRICS["resync"]
    gates.append(("reconnect resync p95 < 1s", not rs or pct(rs, 95) < 1.0, fmt_lat(rs)))
    gates.append(("short locks never show a player offline", dips == 0,
                  f"{dips} snapshots with a missing player over {locks} locks"))
    rest = [x for k, v in METRICS["action"].items() for x in v]
    gates.append(("player requests p95 < 300ms", pct(rest, 95) < 0.3, fmt_lat(rest)))
    print()
    for name, ok, detail in gates:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    failed = [g for g in gates if not g[1]]
    print(f"\nVERDICT: {'PASS' if not failed else 'FAIL'} ({len(gates) - len(failed)}/{len(gates)} gates)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
