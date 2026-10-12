"""100-player load harness: join storm, SSE fan-out, simultaneous answers.

Stdlib only (urllib + threads) so staging needs no new deps. Run against a
throwaway room on local runserver --noreload, single process: the server keeps
presence/inflight state in process memory, so extra workers would split it.

Refuses to run billed: aborts when OPENAI_API_KEY is set unless --allow-billed.
"""
import argparse
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

TOKEN_FILE = "/tmp/genai-load-tokens.json"


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


def post_json(url, payload, timeout=60):
    t0 = time.monotonic()
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return time.monotonic() - t0, r.status, body
    except urllib.error.HTTPError as e:
        try:
            eb = e.read()
        except Exception:
            eb = b""
        return time.monotonic() - t0, e.code, eb
    except Exception as e:
        return time.monotonic() - t0, -1, f"{type(e).__name__}: {e}".encode()


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
    with open(TOKEN_FILE, "w") as f:
        json.dump({"code": code, "tokens": toks}, f)
    print(f"  tokens saved: {len(toks)} -> {TOKEN_FILE}")
    return toks


def load_tokens(code):
    with open(TOKEN_FILE) as f:
        d = json.load(f)
    if d.get("code") != code:
        print(f"token file room {d.get('code')} != {code}, ignoring file", file=sys.stderr)
        return {}
    return d.get("tokens", {})


def phase_events(base, code, tokens, hold):
    toks = list(tokens.values())
    n = len(toks)
    first, frames, errs = [], [], []
    lock = threading.Lock()

    def one(tok):
        url = f"{base}/api/rooms/{code}/events?token={tok}"
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=hold + 20) as r:
                t_first = None
                nf = 0
                deadline = time.monotonic() + hold
                while time.monotonic() < deadline:
                    line = r.readline()
                    if not line:
                        break
                    if t_first is None:
                        t_first = time.monotonic() - t0
                    if line.startswith(b"data:"):
                        nf += 1
                with lock:
                    first.append(t_first or -1)
                    frames.append(nf)
        except Exception as e:
            with lock:
                first.append(-1)
                errs.append(f"{type(e).__name__}: {str(e)[:120]}")

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=n) as ex:
        list(ex.map(one, toks))
    wall = time.monotonic() - t0
    good_first = [x for x in first if x >= 0]
    summarize("events-first-byte", good_first, len(good_first), errs, wall)
    if frames:
        print(f"  frames/stream over {hold}s: p50={pct(frames, 50):.0f} min={min(frames)} max={max(frames)}")
        print(f"  expected ~{hold * 2}/stream at 0.5s tick; 'min=0' means a stream never got a frame")


def phase_answer(base, code, tokens, prompt):
    toks = list(tokens.values())
    lat, chunks, errs, done = [], [], [], 0
    lock = threading.Lock()

    def one(tok):
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(
                f"{base}/api/rooms/{code}/answer",
                data=json.dumps({"token": tok, "prompt": prompt}).encode(),
                headers={"Content-Type": "application/json"},
            )
            t0 = time.monotonic()
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read().decode(errors="replace")
            dt = time.monotonic() - t0
            nc = raw.count("event: chunk")
            is_done = "event: done" in raw
            with lock:
                lat.append(dt)
                chunks.append(nc)
                if is_done:
                    pass
                else:
                    errs.append(f"HTTP 200 no-done tail={raw[-80:]!r}")
            return is_done
        except urllib.error.HTTPError as e:
            try:
                eb = e.read()
            except Exception:
                eb = b""
            with lock:
                lat.append(time.monotonic() - t0)
                errs.append(f"HTTP {e.code} {eb[:120]!r}")
            return False
        except Exception as e:
            with lock:
                lat.append(time.monotonic() - t0)
                errs.append(f"{type(e).__name__}: {str(e)[:120]}")
            return False

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(toks)) as ex:
        results = list(ex.map(one, toks))
    wall = time.monotonic() - t0
    done = sum(1 for r in results)
    summarize("answer", lat, done, errs, wall)
    if chunks:
        print(f"  chunks/stream: p50={pct(chunks, 50):.0f} min={min(chunks)} max={max(chunks)}")
    # Mean latency via statistics for a second opinion on skew
    if lat:
        print(f"  mean={statistics.mean(lat):.3f}s n={len(lat)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--code", required=True)
    ap.add_argument("--players", type=int, default=100)
    ap.add_argument("--phases", default="join,events,answer")
    ap.add_argument("--events-hold", type=int, default=30)
    ap.add_argument("--prompt", default="Explique la photosynthese en une phrase simple.")
    ap.add_argument("--allow-billed", action="store_true")
    a = ap.parse_args()

    if os.environ.get("OPENAI_API_KEY") and not a.allow_billed:
        sys.exit("refusing: OPENAI_API_KEY is set (would bill 100 calls); unset it or pass --allow-billed")

    phases = [p.strip() for p in a.phases.split(",")]
    tokens: dict = {}
    if "join" in phases:
        tokens = phase_join(a.base, a.code, a.players)
    if "events" in phases or "answer" in phases:
        if not tokens:
            tokens = load_tokens(a.code)
            print(f"loaded {len(tokens)} tokens from {TOKEN_FILE}")
    if "events" in phases:
        if not tokens:
            sys.exit("no tokens for events phase (run join first)")
        phase_events(a.base, a.code, tokens, a.events_hold)
    if "answer" in phases:
        if not tokens:
            sys.exit("no tokens for answer phase (run join first)")
        phase_answer(a.base, a.code, tokens, a.prompt)


if __name__ == "__main__":
    main()
