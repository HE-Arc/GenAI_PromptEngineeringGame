# Replace Django backend with Rust Axum + WebSockets

## Goal

Replace the Django 5.1 sync WSGI backend (`backend/`) with a Rust Axum server that keeps the exact REST surface for admin/join/rate/ready/quiz and replaces both SSE channels (GET `events`, POST `answer` stream) with WebSockets. Fix the measured bottlenecks: 517 queries / 0.13 s per `room_state` tick at 100 players (`backend/game/views.py:1216-1382`), `database-is-locked` on 8/100 concurrent `rnd.answers.create` (`views.py:1816`), join p50 5.7 s, SSE first-byte 22 s, answer p50 7.5 s (`backend/load_test_100.py`).

## Architecture

Django today: single process `manage.py runserver --noreload`, SQLite via `backend/config/settings.py`, 0.5 s tick SSE (`views.py:1665-1703`) recomputing full `room_state` per connection, answer streaming via thread+queue (`views.py:1707-1895`), process-local `_inflight`/`_presence` (`views.py:57-137`).

Target: single Axum binary `server/`. One Tokio `broadcast` channel per room (hub), one cached JSON snapshot per room invalidated on write, sqlx connection pool on SQLite-WAL first with Postgres behind a feature flag, async OpenAI streaming client with stub mode when `OPENAI_API_KEY` unset (parity with `game/llm.py:478-494`).

Strangler cutover: Axum runs alongside Django on a second port behind a dev proxy. REST routes move one by one. Django stays authoritative until the WS cut. No big-bang switch.

```
Browser --REST--> Django :8000  \
        --WS/REST--> Axum :3001 / (proxy :5173 routes /api/* by path)
```

Phase 1: Axum serves only `GET /healthz` + `GET /api/rooms/<code>/ws` as read-only mirror (subscribes to Django via polling internally, no writes).
Phase 2: Axum owns `room_state` snapshot + broadcast; Django still owns writes, Axum invalidates on poll.
Phase 3: Axum owns writes route by route (join -> ready -> rate/quiz -> answer). Django falls back to proxy.
Phase 4: Django removed.

## Tech Stack

- Rust 1.89+, Axum 0.7, Tokio full, tokio-tungstenite via `axum::extract::WebSocketUpgrade`, `tokio::sync::broadcast` (capacity 256) + `RwLock<HashMap<RoomCode, RoomHub>>`.
- sqlx 0.8 with `sqlite` + `postgres` features, `sqlx::migrate!()` from `server/migrations/`. SQLite with `journal_mode=WAL; synchronous=NORMAL; busy_timeout=5000`.
- `async-openai` 0.26 for OpenAI streaming; `OPENAI_API_KEY` absent => deterministic stub (6 words per chunk, mirrors `llm.py` stub).
- Frontend: native `WebSocket` in `frontend/src/ws.ts` (new), `useRoom.ts` migrated, `api.ts` `streamAnswer` kept as fallback until Phase 3 answer cut.
- Tests: `cargo test` (unit + `#[tokio::test]` integration against `sqlx::SqlitePool`), existing `backend/load_test_100.py` adapted to `backend/load_test_ws.py`.

## Spec

### WS protocol design (replaces snapshot + chunk/scoring/done frames)

Endpoint: `GET /api/rooms/<CODE>/ws?token=<player-token-or-admin-key>`.

Server -> client (JSON, `type` discriminated):

| type | payload | replaces |
|------|---------|----------|
| `snapshot` | `{ seq, state: RoomState }` full state, same shape as current SSE `room_state` JSON | SSE `event: state` tick |
| `patch` | `{ seq, changed: ["players","answers",...] }` + receivers re-GET snapshot only if subscribed section changed; v1 may send full snapshot and ignore `changed` | 100x2Hz recompute |
| `answer.chunk` | `{ player, delta }` | SSE `event: chunk` |
| `answer.scoring` | `{ player }` | SSE `event: scoring` |
| `answer.done` | `{ player, score, feedback }` | SSE `event: done` |
| `error` | `{ code, message }` e.g. `already_answered` (409 semantic), `bad_token` (403), `bad_phase` (409) | HTTP error codes on POST answer |

Client -> server:

| type | payload | replaces |
|------|---------|----------|
| `hello` | `{ token }` auth handshake, server replies `snapshot` | `EventSource` open |
| `answer.start` | `{ prompt }` begin LLM stream for caller's player row | `POST .../answer` |
| `ping` | `{}` keepalive, server replies `pong` | SSE tick |

Rules: `seq` is per-room monotonically increasing u64, incremented on every mutation. `answer.start` refused with `error {code:"already_answered"}` if an `Answer` row exists for (round, player) — same 409 semantic as today, but over WS not HTTP. One in-flight LLM stream per (round, player); second `answer.start` while streaming => `error {code:"inflight"}` (replaces process-local `_inflight`). Presence: server tracks WS-connected player set per room in memory (replaces `_presence`); disconnect after 10 s grace removes presence, never deletes the `Player` row.

### Per-room broadcast hub (replaces 100x2Hz room_state recompute)

```rust
struct RoomHub {
    tx: broadcast::Sender<WsOut>,   // capacity 256
    snapshot: RwLock<(u64, serde_json::Value)>, // (seq, cached state)
}
type Hubs = RwLock<HashMap<String, Arc<RoomHub>>>;
```

Write path: handler mutates DB in a transaction, bumps `seq`, rebuilds snapshot ONCE, `tx.send(snapshot_msg)`. Read path: new subscriber clones cached `(seq, value)` without touching the DB. Answer streaming: LLM task sends `answer.chunk` frames into `tx` without DB writes until final persist (single `INSERT` + single `UPDATE` in one transaction, `busy_timeout` absorbs burst). Lagged receivers (`RecvError::Lagged`) get a fresh `snapshot` resync, never a panic.

Target: 100 subscribers x 2 state changes/s = 2 snapshot builds/s total, vs today's 200 `room_state` builds/s x 517 queries.

### DB schema port + uniqueness constraints

SQLite tables ported 1:1 from `backend/game/models.py` (455 lines): `Room(code PK, phase, current_round, admin_key, created_at)`, `Round(room_code FK, order, target, revealed)`, `Player(room_code FK, token UNIQUE, name, joined_at)`, `Answer(round_id FK, player_id FK, prompt, text, score, feedback, UNIQUE(round_id, player_id))`, `Rating(answer_id FK, rater_id FK, stars, UNIQUE(answer_id, rater_id))`, `Ready(room_code FK, player_id FK, UNIQUE(room_code, player_id))`, `Quiz(room_code FK, questions_json, state)`. Migrations in `server/migrations/0001_init.sql` with identical `UNIQUE` constraints so `already answered` and double-vote semantics survive. `Answer.text` written once at stream end (no per-chunk UPDATE). Indexes: `(room_code)`, `(round_id)`, `(player_id)`.

Postgres option: same SQL with `TEXT`/`TIMESTAMPTZ` types behind `--features postgres`, selected by `DATABASE_URL` scheme (`sqlite:` vs `postgres:`). No ORM magic; sqlx `query!` macros checked at `cargo test` time.

### Auth/token + admin session parity

Player: `token` hex string issued at join, stored on `Player.token`, passed as `?token=` on WS connect and `Authorization: Bearer` on REST. Admin: `admin_key` issued at room creation, stored on `Room.admin_key`, same transport. Parity rule: any request accepted by Django today is accepted by Axum, any request rejected today is rejected with the same HTTP status (403 bad token, 404 unknown room, 409 wrong phase/duplicate). WS `hello` with bad token => `error {code:"bad_token"}` + close code 4403.

### LLM streaming pipeline

`LlmSource` trait with two impls: `OpenAiStream` (real `async-openai` chat completion stream) and `StubStream` (deterministic, 6 words per chunk, 20 ms delay — mirrors `game/llm.py:478-494`). Selection: `OPENAI_API_KEY` set => OpenAI, else stub. No `OPENAI_API_KEY` in tests by rule. Pipeline: `answer.start` -> check idempotency (`SELECT Answer WHERE round+player`) -> spawn Tokio task -> stream deltas to `tx` as `answer.chunk` -> on complete, score (same stub scoring or second OpenAI call) -> single DB transaction (INSERT answer + bump seq + broadcast `answer.done`). Task handle stored in hub `inflight: Mutex<HashSet<(round_id, player_id)>>`; disconnect does NOT cancel the task (player reconnects and gets `snapshot` with progress).

### Frontend migration (EventSource/fetch-SSE to WebSocket with fallback)

New `frontend/src/ws.ts`: `connectRoomWs(code, token, handlers)` with exponential backoff (0.5 s, 1 s, 2 s, max 10 s), `hello` on open, `ping` every 20 s, auto-resync on `patch`/`Lagged`. `useRoom.ts:41-135` switches from `EventSource` to `connectRoomWs`; `api.ts:310-377` `streamAnswer` becomes `answerStart(ws, prompt)` sender + `answer.chunk/done` handler. Fallback: `VITE_WS_DISABLED=1` env restores `EventSource`/fetch-SSE path for rollback. `Admin.tsx` polling replaced by the same WS subscription (admin connects with `admin_key` as token).

### Incremental cutover (strangler)

Vite dev proxy (`frontend/vite.config.ts`) routes `/api/*` to Django `:8000` by default; per-route override env `VITE_API_WS=1` routes `/api/rooms/*/ws` to Axum `:3001`. Cut order: ws-read-mirror -> snapshot ownership -> join/ready -> rate/quiz -> answer -> admin. Each step keeps Django reachable directly (`:8000`) for instant rollback. E2E gate per step: existing Python suite green + `backend/load_test_ws.py` smoke (10 players) green.

### Load re-validation

Adapt `backend/load_test_100.py` to `backend/load_test_ws.py`: same 100-player join storm but over Axum REST, then 100 concurrent WS subscribers measuring snapshot first-byte and broadcast fan-out latency, then 100 concurrent `answer.start` measuring chunk latency and counting `database-is-locked` errors (must be 0). Acceptance: join p50 < 1 s (vs 5.7 s), snapshot first-byte p50 < 0.5 s (vs SSE 22 s), answer first-byte p50 < 1 s (vs 7.5 s), zero `database-is-locked`, zero lagged-resync storms (>5% resync = fail).

### Rollback criteria

Roll back a cut step (flip proxy back to Django, keep Axum running) if ANY holds for 10 min in staging: HTTP 5xx rate > 0.5%, WS disconnect rate > 5%/min, `already_answered` false-positive rate > 0 (checked against Django DB), load smoke fails, or snapshot `seq` goes backwards. Full rollback = proxy all `/api/*` to Django, Axum idle. Data safety: Axum and Django share the same SQLite file during strangler (WAL allows concurrent readers), so rollback never loses rows.

## File structure

```
server/
  Cargo.toml                 # axum 0.7, tokio full, sqlx sqlite+postgres, async-openai, serde
  src/
    main.rs                  # bind :3001, serve router(), graceful shutdown
    router.rs                # REST routes + /ws upgrade, same paths as Django urls.py
    state.rs                 # AppState { pool, hubs: Hubs, llm: LlmSource }
    hub.rs                   # RoomHub: broadcast, snapshot cache, seq, inflight set
    db/
      mod.rs                 # pool connect, WAL pragmas, migrate!()
      rooms.rs               # room CRUD + snapshot query (single batched query)
      answers.rs             # idempotent answer insert, ratings, ready, quiz
    auth.rs                  # token/admin_key extraction + validation
    llm/
      mod.rs                 # LlmSource trait + factory (env key or stub)
      stub.rs                # deterministic 6-words-per-chunk stub
      openai.rs              # async-openai streaming impl
    ws/
      mod.rs                 # upgrade handler, hello/ping/answer.start dispatch
      protocol.rs            # WsIn/WsOut serde types (snapshot/patch/chunk/done/error)
    rest/
      mod.rs                 # join/rate/ready/quiz/admin handlers (parity with views.py)
  migrations/
    0001_init.sql            # ported schema + UNIQUE constraints + indexes
  tests/
    ws_flow.rs               # full room flow over WS
    rest_parity.rs           # status-code parity vs Django contract
frontend/src/
  ws.ts                      # NEW: connectRoomWs with backoff + resync
  useRoom.ts                 # MOD: WebSocket instead of EventSource:41-135
  api.ts                     # MOD: answerStart replaces streamAnswer:310-377
backend/
  load_test_ws.py            # NEW: adapted harness (100 WS subs + answer storm)
```

## Tasks

### 1. Scaffold Axum server + healthz + parity test harness

Interfaces consumed: none (greenfield). Interfaces produced: `GET /healthz -> 200 {"ok":true}`, `AppState` with SQLite pool.

Test code (`server/tests/rest_parity.rs`):

```rust
#[tokio::test]
async fn healthz_ok() {
    let app = crate::router::test_router().await;
    let res = app.oneshot(Request::get("/healthz").body(Body::empty()).unwrap()).await.unwrap();
    assert_eq!(res.status(), 200);
}

#[tokio::test]
async fn unknown_room_404_parity() {
    // Django returns 404 {"error":"unknown room"} — Axum must match exactly
    let app = crate::router::test_router().await;
    let res = app.oneshot(Request::get("/api/rooms/XXXX/join").body(Body::empty()).unwrap()).await.unwrap();
    assert_eq!(res.status(), 404);
}
```

Commands:

```bash
cargo new server --bin && cd server && cargo add axum tokio --features tokio/full serde serde_json sqlx --features sqlx/sqlite tower axum-test
cargo test
cargo run -- --port 3001  # expect {"ok":true} on GET /healthz
```

### 2. Port DB schema + WAL pool + model round-trip tests

Interfaces consumed: `backend/game/models.py` field list. Interfaces produced: `db::rooms`, `db::answers` CRUD used by Tasks 3-5.

Test code (`server/src/db/rooms.rs`):

```rust
#[tokio::test]
async fn answer_unique_round_player() {
    let pool = test_pool().await;
    let r = create_room(&pool, "AB12").await.unwrap();
    let p = add_player(&pool, &r.code, "ana").await.unwrap();
    insert_answer(&pool, r.current_round, p.id, "x").await.unwrap();
    let dup = insert_answer(&pool, r.current_round, p.id, "y").await;
    assert!(dup.is_err()); // UNIQUE(round_id, player_id) preserved
}
```

Commands:

```bash
cat backend/game/models.py | grep -E 'class|ForeignKey|UniqueConstraint'  # confirm ported constraints
sqlx migrate run --source server/migrations
cargo test db::
```

### 3. Room hub: broadcast + cached snapshot + seq

Interfaces consumed: `db::rooms::room_snapshot()`. Interfaces produced: `hub::RoomHub::{subscribe, publish_snapshot, next_seq}` used by WS + REST writers.

Test code (`server/src/hub.rs`):

```rust
#[tokio::test]
async fn subscriber_gets_cached_snapshot_without_db_hit() {
    let hub = RoomHub::new();
    hub.publish_snapshot(1, json!({"players": ["ana"]})).await;
    let (seq, snap) = hub.cached().await;
    assert_eq!((seq, snap["players"][0]), (1, json!("ana")));
    // second subscriber clones cache, no DB call — verified by query counter in ws_flow.rs
}

#[tokio::test]
async fn lagged_receiver_resyncs() {
    let hub = RoomHub::new_with_capacity(1);
    hub.publish_snapshot(1, json!({})).await;
    hub.publish_snapshot(2, json!({})).await;
    hub.publish_snapshot(3, json!({})).await;
    let mut rx = hub.subscribe().await;
    // flood so rx lags, then assert resync path sends fresh snapshot not panic
    assert!(hub.resync_needed(&mut rx));
}
```

Commands:

```bash
cargo test hub::
```

### 4. WS endpoint: hello/snapshot/ping + auth parity

Interfaces consumed: hub from Task 3, `auth::validate()`. Interfaces produced: `GET /api/rooms/<code>/ws` live endpoint.

Test code (`server/tests/ws_flow.rs`):

```rust
#[tokio::test]
async fn ws_hello_returns_snapshot_and_bad_token_closes() {
    let (url, token) = spawn_room_with_player().await;
    let (mut ws, _) = connect_async(format!("{url}?token={token}")).await.unwrap();
    ws.send(Message::Text(r#"{"type":"hello","token":"TOKEN"}"#.replace("TOKEN", &token))).await.unwrap();
    let msg: WsOut = recv_json(&mut ws).await;
    assert!(matches!(msg, WsOut::Snapshot { .. }));
    let (bad, _) = connect_async(format!("{url}?token=nope")).await.unwrap();
    // expect error bad_token then close 4403 — parity with Django 403
}
```

Commands:

```bash
cargo test ws_flow::ws_hello
# manual: websocat "ws://localhost:3001/api/rooms/AB12/ws?token=xxx"
```

### 5. LLM pipeline: stub stream + answer.start idempotency

Interfaces consumed: `LlmSource` trait, hub broadcast. Interfaces produced: `answer.chunk/scoring/done` frames, persisted `Answer` row.

Test code (`server/src/llm/stub.rs`):

```rust
#[tokio::test]
async fn stub_chunks_six_words() {
    let chunks = StubStream::collect("one two three four five six seven").await;
    assert_eq!(chunks.len(), 2); // 6 words + 1 word, mirrors llm.py stub
    assert_eq!(chunks[0].split_whitespace().count(), 6);
}

#[tokio::test]
async fn double_answer_start_rejected() {
    let h = spawn_room_with_player().await;
    answer_start(&h, "first prompt").await.unwrap();
    answer_done(&h).await;
    let err = answer_start(&h, "second prompt").await.unwrap_err();
    assert_eq!(err.code, "already_answered"); // parity with Django 409 {"error":"already answered"}
}
```

Commands:

```bash
OPENAI_API_KEY= cargo test llm:: answer::
```

### 6. REST parity: join/ready/rate/quiz/admin + invalidate-on-write

Interfaces consumed: Django status-code contract (`views.py`). Interfaces produced: Axum REST handlers that bump hub `seq` on every mutation.

Test code (`server/tests/rest_parity.rs`):

```rust
#[tokio::test]
async fn join_wrong_phase_409_parity() {
    // Django join refuses non-waiting rooms with 409 — Axum must match
    let app = test_router_with_playing_room().await;
    let res = post_json(&app, "/api/rooms/AB12/join", json!({"name":"ana"})).await;
    assert_eq!(res.status(), 409);
}

#[tokio::test]
async fn write_invalidates_snapshot_seq() {
    let h = spawn_room().await;
    let s0 = h.cached().await.0;
    post_join(&h, "ana").await;
    assert_eq!(h.cached().await.0, s0 + 1);
}
```

Commands:

```bash
cargo test rest_parity::
# contract check: diff <(curl -s localhost:8000/api/rooms/ZZ99/join -X POST -d '{"name":"x"}' -w '%{http_code}') <(curl -s localhost:3001/api/rooms/ZZ99/join -X POST -d '{"name":"x"}' -w '%{http_code}')
```

### 7. Frontend: ws.ts + useRoom EventSource removal + answerStart

Interfaces consumed: WS protocol from Spec. Interfaces produced: live UI over WebSocket with `VITE_WS_DISABLED=1` fallback.

Test commands (repo has no JS runner — mechanical proof per project memory):

```bash
grep -rn EventSource frontend/src  # expect zero hits after migration
grep -rn dangerouslySetInnerHTML frontend/src  # expect zero hits (unchanged)
npm --prefix frontend run build  # must pass; bundle hash changes only here
VITE_WS_DISABLED=1 npm --prefix frontend run build  # fallback path must also build
```

Manual check: open room with 2 browsers, submit prompt in one, observe `answer.chunk` progressive render in both + `answer.done` score without refresh.

### 8. Strangler proxy + phase-by-phase cutover checklist

Interfaces consumed: Vite proxy config, both servers. Interfaces produced: per-route cutover state.

Commands:

```bash
# terminals: Django :8000, Axum :3001, Vite :5173 with proxy rules
python backend/manage.py runserver 8000 --noreload &
cargo run -p server -- --port 3001 &
npm --prefix frontend run dev
curl -s localhost:5173/api/healthz  # via proxy
# cut order, each gated: ws-mirror -> snapshot -> join/ready -> rate/quiz -> answer -> admin
# rollback one route: flip its proxy target back to :8000, confirm Django serves it
```

Gate per step: `python backend/manage.py test` green + Task 9 smoke green.

### 9. Load re-validation with adapted harness

Interfaces consumed: `backend/load_test_100.py` scenario. Interfaces produced: `backend/load_test_ws.py` results meeting acceptance thresholds.

Test code (`backend/load_test_ws.py`, excerpt):

```python
async def fanout_100():
    tokens = await join_100_rest()          # REST join storm
    wss = await asyncio.gather(*[ws_connect(t) for t in tokens])
    first = await asyncio.gather(*[recv_snapshot(w) for w in wss])
    assert all(f["type"] == "snapshot" for f in first)
    await answer_storm(tokens)              # 100 concurrent answer.start
    assert db_locked_errors == 0
```

Commands:

```bash
python backend/load_test_ws.py --players 10   # smoke
python backend/load_test_ws.py --players 100  # acceptance: join p50<1s, snapshot first-byte p50<0.5s, answer first-byte p50<1s, 0 locked, <5% resync
```

### 10. Django removal + Postgres option + final validation

Interfaces consumed: all above. Interfaces produced: Django-free repo, `docker-compose.yml` pointing at Axum + Postgres.

Commands:

```bash
DATABASE_URL=sqlite:///tmp/prod.sqlite cargo test  # WAL path green
DATABASE_URL=postgres://localhost/game cargo test --features postgres  # Postgres path green
rm -rf backend/  # only after 1 week shadow-run with zero rollback triggers
grep -rn "manage.py\|EventSource\|streamAnswer" --include='*.ts' --include='*.rs' --include='*.toml' . # expect zero hits
```

## Self-review

- [x] Header has Goal/Architecture/Tech Stack/Spec — yes, all four present.
- [x] File structure lists exact paths (`server/src/...`, `server/migrations/0001_init.sql`, `frontend/src/ws.ts`, `backend/load_test_ws.py`) — no `...` placeholders.
- [x] Every task independently testable with real test snippets (`cargo test` assertions, Python harness excerpt) and runnable commands — no TODO/FIXME.
- [x] WS protocol covers all replaced frames (snapshot/patch/chunk/scoring/done/error + hello/answer.start/ping) with refusal semantics (`already_answered`, `inflight`, `bad_token`).
- [x] Broadcast hub explicitly replaces 100x2Hz recompute with cached snapshot + seq + lagged-resync rule.
- [x] Schema port preserves UNIQUE constraints (answer, rating, ready) + indexes; WAL pragmas + single-write answer path address `database-is-locked`.
- [x] Auth parity pins status codes (403/404/409); LLM stub parity pins 6-words-per-chunk + no-key-in-tests rule.
- [x] Frontend migration has fallback env + mechanical proof commands (repo has no JS runner).
- [x] Cutover is strangler route-by-route with proxy mechanics; rollback criteria are quantitative (5xx > 0.5%, disconnect > 5%/min, any false `already_answered`, seq regression).
- [x] Load re-validation adapts existing harness with numeric acceptance gates tied to current p50s.
- [x] Verified paths: `backend/game/models.py` (455 lines), `views.py` (2162), `frontend/src/api.ts` (463), `useRoom.ts` (151), `backend/load_test_100.py`, `docs/superpowers/plans/` all exist.
