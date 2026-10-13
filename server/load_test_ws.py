"""100-player load harness against the Axum server: one full game, end to end.

Stdlib only (urllib + socket + threads), raw WS client, no deps.

Drives ONE room through every phase with N concurrent players and checks two
things at each step: latency (how long the burst takes) and delivery (every
socket actually received the frames the step must fan out — a fast server
that drops events on a lagged broadcast channel fails here, not silently).

    join storm -> N sockets + hello -> lobby vote storm -> didacticiel answer
    storm (early close + reveal) -> 8 admin advances -> N concurrent prompts
    (every socket must see every player's chunks and done) -> rating storm

Creates its own room through the admin login. Refuses to run billed: aborts
when an LLM key is set unless --allow-billed.
"""
import argparse
import base64
import http.cookiejar
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def pct(data, q):
    if not data:
        return 0.0
    s = sorted(data)
    return s[min(len(s) - 1, int(q / 100 * len(s)))]


def fmt_lat(lat):
    if not lat:
        return "n/a"
    return f"p50={pct(lat, 50) * 1000:.0f}ms p95={pct(lat, 95) * 1000:.0f}ms max={max(lat) * 1000:.0f}ms"


class Http:
    """JSON over urllib with an optional shared cookie jar (admin session)."""

    def __init__(self, base, jar=None):
        self.base = base
        handlers = [urllib.request.HTTPCookieProcessor(jar)] if jar is not None else []
        self.opener = urllib.request.build_opener(*handlers)

    def call(self, method, path, payload=None, timeout=30):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        t0 = time.monotonic()
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return time.monotonic() - t0, r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                body = json.loads(body)
            except Exception:
                pass
            return time.monotonic() - t0, e.code, body
        except Exception as e:
            return time.monotonic() - t0, -1, f"{type(e).__name__}: {e}"


class Client:
    """One player's socket. A reader thread records every frame so a phase
    can later assert what this phone actually received."""

    def __init__(self, base, code, name, token, timeout=30, cookie=None):
        host, port = base.split("//", 1)[1].split(":")
        self.name, self.token = name, token
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET /api/rooms/{code}/ws?token={token} HTTP/1.1\r\nHost: {host}\r\n"
                f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                + (f"Cookie: {cookie}\r\n" if cookie else "")
                + "\r\n"
            ).encode()
        )
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake: closed")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"handshake failed: {head[:80]!r}")
        self.buf = head.split(b"\r\n\r\n", 1)[1]
        self.sock.settimeout(None)
        self.lock = threading.Condition()
        self.frames = []  # (monotonic time, decoded message)
        self.snapshot = None
        self.closed = None
        self.wlock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _need(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk

    def _raw_send(self, opcode, raw):
        mask = os.urandom(4)
        n = len(raw)
        if n < 126:
            hdr = bytes([0x80 | opcode, 0x80 | n])
        else:
            hdr = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", n)
        body = bytes(c ^ mask[i % 4] for i, c in enumerate(raw))
        with self.wlock:
            self.sock.sendall(hdr + mask + body)

    def send(self, obj):
        self._raw_send(0x1, json.dumps(obj).encode())

    def _read(self):
        try:
            while True:
                self._need(2)
                b1, b2 = self.buf[0], self.buf[1]
                ln, off = b2 & 0x7F, 2
                if ln == 126:
                    self._need(4)
                    ln, off = struct.unpack(">H", self.buf[2:4])[0], 4
                elif ln == 127:
                    self._need(10)
                    ln, off = struct.unpack(">Q", self.buf[2:10])[0], 10
                self._need(off + ln)
                payload, self.buf = self.buf[off:off + ln], self.buf[off + ln:]
                op = b1 & 0x0F
                if op == 0x9:  # server keepalive: answer like a browser would
                    self._raw_send(0xA, payload)
                elif op == 0x8:
                    raise ConnectionError(f"server close {payload[:40]!r}")
                elif op == 0x1:
                    msg = json.loads(payload)
                    with self.lock:
                        self.frames.append((time.monotonic(), msg))
                        if msg.get("type") == "snapshot":
                            self.snapshot = msg["state"]
                        self.lock.notify_all()
        except Exception as e:
            with self.lock:
                self.closed = f"{type(e).__name__}: {e}"
                self.lock.notify_all()

    def wait(self, pred, timeout):
        """Block until pred(self) holds; returns the instant it first held."""
        end = time.monotonic() + timeout
        with self.lock:
            while not pred(self):
                left = end - time.monotonic()
                if left <= 0 or self.closed:
                    return None
                self.lock.wait(left)
            return time.monotonic()

    def close(self):
        try:
            self.sock.sendall(b"\x88\x80" + os.urandom(4))
            self.sock.close()
        except Exception:
            pass


class Report:
    def __init__(self):
        self.gates = []

    def gate(self, name, ok, detail):
        self.gates.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def storm(fn, items):
    """Run fn over items all at once; return wall time and results."""
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(items)) as ex:
        out = list(ex.map(fn, items))
    return time.monotonic() - t0, out


def converge(clients, pred, t0, timeout):
    """Time from t0 until every client satisfies pred; also how many never did."""
    # One shared deadline: a missed event costs `timeout` once, not per socket.
    end = time.monotonic() + timeout
    times = [c.wait(pred, max(0.0, end - time.monotonic())) for c in clients]
    missing = [c.name for c, t in zip(clients, times) if t is None]
    done = [t - t0 for t in times if t is not None]
    return done, missing


def server_usage(pid):
    if not pid:
        return None
    try:
        out = subprocess.run(["ps", "-o", "rss=,%cpu=", "-p", str(pid)], capture_output=True, text=True).stdout.split()
        return int(out[0]) // 1024, float(out[1])
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:3001")
    ap.add_argument("--players", type=int, default=100)
    ap.add_argument("--password", default=os.environ.get("ADMIN_PASSWORD", "genai"))
    ap.add_argument("--prompt", default="Tu es un professeur. Explique la photosynthese en une phrase simple.")
    ap.add_argument("--server-pid", type=int, default=0, help="report the server's RSS/CPU")
    ap.add_argument("--rest-p95", type=float, default=1.0, help="gate: REST burst p95 (s)")
    ap.add_argument("--fanout", type=float, default=2.0, help="gate: every socket sees the change (s)")
    ap.add_argument("--allow-billed", action="store_true")
    a = ap.parse_args()

    if (os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")) and not a.allow_billed:
        sys.exit("refusing: an LLM key is set (would bill the answer storm); unset it or pass --allow-billed")

    n, rep = a.players, Report()
    admin = Http(a.base, http.cookiejar.CookieJar())
    anon = Http(a.base)
    _, st, _ = admin.call("POST", "/api/admin/login", {"password": a.password})
    if st != 200:
        sys.exit(f"admin login failed: HTTP {st}")
    # Long clocks so the phase timers never move the room during a burst.
    _, st, body = admin.call("POST", "/api/admin/rooms", {"tutorial": True, "round_seconds": 600, "rate_seconds": 300})
    code = body["code"]
    print(f"room {code}, {n} players")

    # 1. Join storm.
    print("\n[1] join storm")
    wall, res = storm(lambda i: anon.call("POST", f"/api/rooms/{code}/join", {"name": f"load{i:03d}"}), range(n))
    lat = [r[0] for r in res]
    tokens = {f"load{i:03d}": r[2]["token"] for i, r in enumerate(res) if r[1] == 200}
    print(f"  {len(tokens)}/{n} joined in {wall:.2f}s  {fmt_lat(lat)}")
    rep.gate("join: all accepted", len(tokens) == n, f"{len(tokens)}/{n}")
    rep.gate("join: p95", pct(lat, 95) < a.rest_p95, fmt_lat(lat))

    # 2. Sockets + hello: first snapshot, then everybody sees everybody online.
    print("\n[2] websocket connect + hello")
    errors = []

    def connect(item):
        name, tok = item
        t0 = time.monotonic()
        try:
            c = Client(a.base, code, name, tok)
            c.send({"type": "hello", "token": tok})
            t = c.wait(lambda c: c.snapshot is not None, 30)
            return c, (t - t0) if t else None
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
            return None, None

    t0 = time.monotonic()
    wall, res = storm(connect, list(tokens.items()))
    clients = [c for c, _ in res if c]
    first = [t for _, t in res if t is not None]
    print(f"  {len(clients)}/{n} connected in {wall:.2f}s  first snapshot {fmt_lat(first)}")
    rep.gate("ws: all connected with a snapshot", len(first) == n and not errors, f"{len(first)}/{n} {errors[:2]}")
    done, missing = converge(clients, lambda c: c.snapshot["online_count"] == n, t0, 15)
    rep.gate("ws: presence fan-out (all see online_count=N)", not missing, f"{fmt_lat(done)} missing={len(missing)}")

    # 3. Lobby vote storm: the last vote starts the game.
    print("\n[3] lobby vote storm")
    t0 = time.monotonic()
    wall, res = storm(lambda c: anon.call("POST", f"/api/rooms/{code}/ready", {"token": c.token}), clients)
    lat = [r[0] for r in res]
    bad = [r for r in res if r[1] != 200]
    print(f"  {n - len(bad)}/{n} votes in {wall:.2f}s  {fmt_lat(lat)}")
    rep.gate("ready: all accepted", not bad, f"{len(bad)} refused {[b[1:] for b in bad[:2]]}")
    rep.gate("ready: p95", pct(lat, 95) < a.rest_p95, fmt_lat(lat))
    done, missing = converge(clients, lambda c: c.snapshot["phase"] == "quiz" and c.snapshot["quiz"], t0, 15)
    rep.gate("ready: game start reaches every socket", not missing, f"{fmt_lat(done)} missing={len(missing)}")

    # 4. Didacticiel: bank fetch storm, answer storm, early close + reveal.
    print("\n[4] didacticiel")
    wall, res = storm(lambda c: anon.call("GET", f"/api/rooms/{code}/quiz?token={c.token}"), clients)
    lat = [r[0] for r in res]
    full = sum(1 for r in res if r[1] == 200 and len(r[2]["questions"]) == 8)
    rep.gate("quiz: bank served to all (8 questions)", full == n, f"{full}/{n} {fmt_lat(lat)}")
    t0 = time.monotonic()
    wall, res = storm(
        lambda c: anon.call("POST", f"/api/rooms/{code}/quiz", {"token": c.token, "question": 0, "choice": 1}), clients
    )
    lat = [r[0] for r in res]
    bad = [r for r in res if r[1] != 200]
    print(f"  {n - len(bad)}/{n} answers in {wall:.2f}s  {fmt_lat(lat)}")
    rep.gate("quiz: all answers accepted", not bad, f"{len(bad)} refused {[b[1:] for b in bad[:2]]}")
    rep.gate("quiz: answer p95", pct(lat, 95) < a.rest_p95, fmt_lat(lat))
    done, missing = converge(
        clients, lambda c: (c.snapshot["quiz"] or {}).get("revealed") and c.snapshot["quiz"]["correct"] is not None, t0, 15
    )
    rep.gate("quiz: early close reveal reaches every socket", not missing, f"{fmt_lat(done)} missing={len(missing)}")

    # 5. Eight admin advances: each phase change must reach all sockets.
    print("\n[5] admin advances (7 questions, then the game)")
    worst, lost = [], 0
    for q in range(1, 9):
        t0 = time.monotonic()
        admin.call("POST", f"/api/admin/rooms/{code}/next")
        target = (lambda c, q=q: (c.snapshot["quiz"] or {}).get("index") == q) if q < 8 else (
            lambda c: c.snapshot["phase"] == "playing"
        )
        done, missing = converge(clients, target, t0, 15)
        worst.append(max(done) if done else 99)
        lost += len(missing)
    rep.gate("advance: every change reaches every socket", lost == 0, f"worst fan-out per advance {fmt_lat(worst)}, missed={lost}")

    # 6. Answer storm: N concurrent model streams over the sockets.
    print("\n[6] answer storm (stub LLM)")
    for c in clients:
        with c.lock:
            c.mark = len(c.frames)
    t0 = time.monotonic()
    storm(lambda c: c.send({"type": "answer.start", "prompt": a.prompt}), clients)
    own_first, own_done = [], []
    end = time.monotonic() + 60
    for c in clients:
        t = c.wait(lambda c: any(m.get("type") == "answer.chunk" and m["player"] == c.name for _, m in c.frames[c.mark:]), max(0.0, end - time.monotonic()))
        if t:
            own_first.append(t - t0)
        t = c.wait(lambda c: any(m.get("type") == "answer.done" and m["player"] == c.name for _, m in c.frames[c.mark:]), max(0.0, end - time.monotonic()))
        if t:
            own_done.append(t - t0)
    print(f"  own first chunk {fmt_lat(own_first)}")
    print(f"  own verdict     {fmt_lat(own_done)}")
    rep.gate("answer: every player got their verdict", len(own_done) == n, f"{len(own_done)}/{n}")
    errs = [m for c in clients for _, m in c.frames[c.mark:] if m.get("type") == "error"]
    rep.gate("answer: no error frames", not errs, f"{len(errs)} {errs[:2]}")
    done, missing = converge(clients, lambda c: c.snapshot["phase"] == "rating", t0, 30)
    rep.gate("answer: rating opens on every socket", not missing, f"{fmt_lat(done)} missing={len(missing)}")
    # Fan-out completeness: every phone watches every stream. The author's
    # own chunk count is the truth for that player.
    expected = {}
    for c in clients:
        expected[c.name] = sum(1 for _, m in c.frames[c.mark:] if m.get("type") == "answer.chunk" and m["player"] == c.name)
    short = 0
    for c in clients:
        got_done = {m["player"] for _, m in c.frames[c.mark:] if m.get("type") == "answer.done"}
        chunks = {}
        for _, m in c.frames[c.mark:]:
            if m.get("type") == "answer.chunk":
                chunks[m["player"]] = chunks.get(m["player"], 0) + 1
        if len(got_done) != n or any(chunks.get(p, 0) != k for p, k in expected.items()):
            short += 1
    frames = sum(len(c.frames) - c.mark for c in clients)
    rep.gate(
        "answer: every socket saw all N streams (chunks + done)",
        short == 0,
        f"{short} sockets incomplete, {frames} frames delivered",
    )
    rows = clients[0].snapshot["reveal"]["count"] if clients[0].snapshot.get("reveal") else 0
    rep.gate("answer: N answer rows persisted", rows == n, f"{rows}/{n}")

    # 7. Rating storm on the first revealed prompt (author excluded).
    print("\n[7] rating storm")
    raters = [c for c in clients if not c.snapshot["reveal"]["mine"]]
    authors = [c.name for c in clients if c.snapshot["reveal"]["mine"]]
    rep.gate("rate: exactly one socket is told it wrote the prompt", len(authors) == 1, f"{authors}")
    t0 = time.monotonic()
    wall, res = storm(lambda c: anon.call("POST", f"/api/rooms/{code}/rate", {"token": c.token, "score": 7, "index": 0}), raters)
    lat = [r[0] for r in res]
    bad = [r for r in res if r[1] != 200]
    print(f"  {len(raters) - len(bad)}/{len(raters)} ratings in {wall:.2f}s  {fmt_lat(lat)}")
    rep.gate("rate: all accepted", not bad, f"{len(bad)} refused {[b[1:] for b in bad[:2]]}")
    rep.gate("rate: p95", pct(lat, 95) < a.rest_p95, fmt_lat(lat))
    done, missing = converge(clients, lambda c: (c.snapshot.get("reveal") or {}).get("index") == 1, t0, 15)
    rep.gate("rate: next prompt reaches every socket", not missing, f"{fmt_lat(done)} missing={len(missing)}")

    # 8. Health: sockets alive, server footprint.
    dead = [c.closed for c in clients if c.closed]
    rep.gate("sockets: none dropped during the game", not dead, f"{len(dead)} {dead[:2]}")
    usage = server_usage(a.server_pid)
    if usage:
        print(f"\nserver RSS {usage[0]} MB, CPU {usage[1]:.0f}% (ps snapshot)")

    worst_fanout = max(
        (float(d.split("max=")[1].rstrip("ms").split("ms")[0]) for name, ok, d in rep.gates if "max=" in d and "reach" in name),
        default=0,
    )
    rep.gate("fan-out: worst delivery under budget", worst_fanout / 1000 < a.fanout, f"{worst_fanout:.0f}ms < {a.fanout * 1000:.0f}ms")
    for c in clients:
        c.close()
    failed = [g for g in rep.gates if not g[1]]
    print(f"\nVERDICT: {'PASS' if not failed else 'FAIL'} ({len(rep.gates) - len(failed)}/{len(rep.gates)} gates)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
