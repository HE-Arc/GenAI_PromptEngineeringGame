"""100-player WS load harness against Axum :3001 (Task 9).

Stdlib only (urllib + socket + threads): raw WS client (no deps).
Scenarios: REST join storm -> 100 concurrent WS subscribers (snapshot
first-byte + fan-out) -> 100 concurrent answer.start (chunk latency,
database-is-locked must be 0).

Refuses to run billed: aborts when OPENAI_API_KEY is set unless --allow-billed.
Room must exist in `waiting` phase (create via POST /api/admin/rooms).
"""
import argparse
import base64
import hashlib
import json
import os
import socket
import statistics
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def pct(data, q):
    if not data:
        return 0.0
    s = sorted(data)
    i = min(len(s) - 1, int(q / 100 * len(s)))
    return s[i]


def summarize(name, lat, ok, errors, wall):
    n = len(lat)
    thr = n / wall if wall > 0 else 0
    print(f"[{name}] n={n} ok={ok} err={len(errors)} wall={wall:.2f}s req/s={thr:.1f}")
    if lat:
        print(f"  latency s: p50={pct(lat, 50):.3f} p95={pct(lat, 95):.3f} max={max(lat):.3f}")
    if errors:
        counts: dict = {}
        for e in errors:
            counts[e] = counts.get(e, 0) + 1
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:8]:
            print(f"  err: {v}x {k[:160]}")


def post_json(url, payload, timeout=60, headers=None):
    t0 = time.monotonic()
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return time.monotonic() - t0, r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            eb = e.read()
        except Exception:
            eb = b""
        return time.monotonic() - t0, e.code, eb
    except Exception as e:
        return time.monotonic() - t0, -1, f"{type(e).__name__}: {e}".encode()


class WS:
    """Minimal WS client: handshake, masked text send, frame recv."""

    def __init__(self, base, code, token, timeout=30):
        u = urllib.parse.urlparse(base)
        self.host, self.port = u.hostname, u.port or 80
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = f"/api/rooms/{code}/ws?token={urllib.parse.quote(token)}"
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {self.host}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            b = self.sock.recv(4096)
            if not b:
                raise ConnectionError("ws handshake: closed")
            head += b
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"ws handshake failed: {head[:120]!r}")
        self.buf = b""

    def send(self, obj):
        raw = json.dumps(obj).encode()
        mask = os.urandom(4)
        hdr = bytes([0x81, 0x80 | len(raw) if len(raw) < 126 else 0x81FE])
        if len(raw) >= 126:
            hdr += struct.pack(">H", len(raw))
        self.sock.sendall(hdr + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(raw)))

    def _frame(self):
        while len(self.buf) < 2:
            b = self.sock.recv(65536)
            if not b:
                raise ConnectionError("ws closed")
            self.buf += b
        b1, b2 = self.buf[0], self.buf[1]
        ln, off = b2 & 0x7F, 2
        if ln == 126:
            while len(self.buf) < 4:
                self.buf += self.sock.recv(65536)
            ln, off = struct.unpack(">H", self.buf[2:4])[0], 4
        elif ln == 127:
            while len(self.buf) < 10:
                self.buf += self.sock.recv(65536)
            ln, off = struct.unpack(">Q", self.buf[2:10])[0], 10
        if b1 & 0x80 == 0:
            raise ConnectionError("fragmented frame unsupported")
        while len(self.buf) < off + (4 if b2 & 0x80 else 0) + ln:
            b = self.sock.recv(65536)
            if not b:
                raise ConnectionError("ws closed mid-frame")
            self.buf += b
        op = b1 & 0x0F
        if b2 & 0x80:
            mask, off2 = self.buf[off:off + 4], off + 4
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(self.buf[off2:off2 + ln]))
        else:
            payload = self.buf[off:off + ln]
        self.buf = self.buf[off + (4 if b2 & 0x80 else 0) + ln:]
        return op, payload

    def recv_json(self):
        while True:
            op, payload = self._frame()
            if op == 0x1:
                return json.loads(payload.decode())
            if op == 0x9:  # ping -> pong
                self.sock.sendall(b"\x8a\x00")
                continue
            if op == 0x8:
                raise ConnectionError(f"ws close: {payload[:80]!r}")

    def close(self):
        try:
            self.sock.sendall(b"\x88\x00")
            self.sock.close()
        except Exception:
            pass


def phase_join(base, code, players):
    url = f"{base}/api/rooms/{code}/join"
    lat, toks, errs = [], {}, []
    lock = threading.Lock()

    def one(i):
        dt, st, body = post_json(url, {"name": f"load{i:03d}"}, timeout=30)
        with lock:
            lat.append(dt)
            if st == 200:
                try:
                    toks[f"load{i:03d}"] = json.loads(body)["token"]
                except Exception as e:
                    errs.append(f"200-bad-body {e}")
            else:
                errs.append(f"HTTP {st} {body[:120]!r}")

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=players) as ex:
        list(ex.map(one, range(players)))
    wall = time.monotonic() - t0
    summarize("join", lat, len(toks), errs, wall)
    return toks, lat, errs


def phase_ws_subscribe(base, code, tokens):
    toks = list(tokens.values())
    first, errs = [], []
    ok = 0
    conns: list = [None] * len(toks)
    lock = threading.Lock()

    def one(idx_tok):
        idx, tok = idx_tok
        t0 = time.monotonic()
        try:
            ws = WS(base, code, tok, timeout=30)
            ws.send({"type": "hello", "token": tok})
            msg = ws.recv_json()
            dt = time.monotonic() - t0
            with lock:
                if isinstance(msg, dict) and msg.get("type") == "snapshot":
                    first.append(dt)
                    conns[idx] = (ws, tok)
                else:
                    errs.append(f"no-snapshot {str(msg)[:120]}")
                    ws.close()
        except Exception as e:
            with lock:
                first.append(-1)
                errs.append(f"{type(e).__name__}: {str(e)[:120]}")

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(toks)) as ex:
        list(ex.map(one, enumerate(toks)))
    wall = time.monotonic() - t0
    good = [x for x in first if x >= 0]
    ok = sum(1 for c in conns if c)
    summarize("ws-snapshot-first-byte", good, ok, errs, wall)
    return [c for c in conns if c], good, errs


def phase_answer(base, code, conns, prompt, timeout=120):
    first, total, chunks, errs, locked = [], [], [], [], [0]
    lock = threading.Lock()
    prompt_words = len(prompt.split())

    def one(wt):
        ws, tok = wt
        ws.sock.settimeout(timeout)
        t0 = time.monotonic()
        try:
            ws.send({"type": "answer.start", "prompt": prompt})
            t_first, nc, done, extra_snap = None, 0, False, 0
            while True:
                try:
                    msg = ws.recv_json()
                except Exception as e:
                    with lock:
                        errs.append(f"recv {type(e).__name__}: {str(e)[:100]}")
                    break
                typ = msg.get("type", "")
                if typ == "answer.chunk" and t_first is None:
                    t_first = time.monotonic() - t0
                if typ == "answer.chunk":
                    nc += 1
                elif typ == "snapshot":
                    extra_snap += 1  # lagged-resync or broadcast snapshot
                elif typ == "answer.done":
                    done = True
                    break
                elif typ == "error":
                    with lock:
                        errs.append(f"ws-error {msg.get('code')}: {msg.get('message', '')[:80]}")
                        if "locked" in json.dumps(msg).lower():
                            locked[0] += 1
                    break
                if "database is locked" in json.dumps(msg).lower():
                    with lock:
                        locked[0] += 1
            with lock:
                if t_first is not None:
                    first.append(t_first)
                total.append(time.monotonic() - t0)
                chunks.append(nc)
                if not done:
                    errs.append("no-done")
            return (done, extra_snap)
        except Exception as e:
            with lock:
                total.append(time.monotonic() - t0)
                errs.append(f"{type(e).__name__}: {str(e)[:120]}")
            return (False, 0)

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(conns)) as ex:
        results = list(ex.map(one, conns))
    wall = time.monotonic() - t0
    done = sum(1 for d, _ in results)
    resync = sum(s for _, s in results)
    summarize("answer-first-chunk", first, done, errs, wall)
    if total:
        print(f"  answer total s: p50={pct(total, 50):.3f} p95={pct(total, 95):.3f} max={max(total):.3f}")
    if chunks:
        print(f"  chunks/stream: p50={pct(chunks, 50):.0f} min={min(chunks)} max={max(chunks)}")
    if total:
        print(f"  mean total={statistics.mean(total):.3f}s n={len(total)}")
    print(f"  database-is-locked: {locked[0]}")
    print(f"  resync snapshots during storm: {resync} ({100.0 * resync / max(1, len(conns)):.1f}% of conns)")
    for ws, _ in conns:
        ws.close()
    return first, total, errs, locked[0], resync


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:3001")
    ap.add_argument("--code", required=True)
    ap.add_argument("--players", type=int, default=100)
    ap.add_argument("--phases", default="join,ws,answer")
    ap.add_argument("--prompt", default="Explique la photosynthese en une phrase simple.")
    ap.add_argument("--allow-billed", action="store_true")
    a = ap.parse_args()

    if os.environ.get("OPENAI_API_KEY") and not a.allow_billed:
        sys.exit("refusing: OPENAI_API_KEY is set (would bill 100 calls); unset it or pass --allow-billed")

    phases = [p.strip() for p in a.phases.split(",")]
    tokens: dict = {}
    join_lat, join_errs = [], []
    if "join" in phases:
        tokens, join_lat, join_errs = phase_join(a.base, a.code, a.players)
        if not tokens:
            sys.exit("no tokens from join phase")
    else:
        sys.exit("--code room needs join phase for tokens (no token file in WS harness)")
    conns, snap_lat, snap_errs = [], [], []
    if "ws" in phases or "answer" in phases:
        conns, snap_lat, snap_errs = phase_ws_subscribe(a.base, a.code, tokens)
        if not conns:
            sys.exit("no WS connections")
    ans_first, ans_total, ans_errs, locked, resync = [], [], [], 0, 0
    if "answer" in phases:
        ans_first, ans_total, ans_errs, locked, resync = phase_answer(a.base, a.code, conns, a.prompt)
    else:
        for ws, _ in conns:
            ws.close()

    # Verdict vs Django baselines (plan acceptance gates)
    print("--- verdict vs Django (join p50 5.7s, SSE first-byte 22s, answer 7.5s) ---")
    gates = [
        ("join p50<1s", pct(join_lat, 50) < 1.0, f"{pct(join_lat, 50):.3f}s"),
        ("snapshot first-byte p50<0.5s", pct(snap_lat, 50) < 0.5, f"{pct(snap_lat, 50):.3f}s"),
        ("answer first-byte p50<1s", (pct(ans_first, 50) < 1.0) if ans_first else None, f"{pct(ans_first, 50):.3f}s" if ans_first else "skipped"),
        ("zero database-is-locked", locked == 0, str(locked)),
        ("lagged-resync <5%", (resync / max(1, len(conns)) < 0.05) if conns and "answer" in phases else None,
         f"{100.0 * resync / max(1, len(conns)):.1f}%" if conns else "skipped"),
    ]
    allok = True
    for name, ok, val in gates:
        mark = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
        if ok is False:
            allok = False
        print(f"  [{mark}] {name}: {val}")
    print("VERDICT:", "PASS" if allok else "FAIL")


if __name__ == "__main__":
    main()
