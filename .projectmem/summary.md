# projectmem - GenAI_PromptEngineeringGame

_Last updated: 2026-11-06_

## Project purpose
Spécification complète : [wiki Cahier-des-charges](https://github.com/HE-Arc/GenAI_PromptEngineeringGame/wiki/Cahier-des-charges).

## Recent issues
- [DONE] #legacy_f7c7 Legacy issue: Port the Django game engine to Axum, fix presence and WS delivery, add load harnesses -> Port the Django game engine to Axum, fix presence and WS delivery, add load harnesses (fixed)
- [DONE] #legacy_b305 Legacy issue: Fix empty and truncated model answers -> Fix empty and truncated model answers (fixed)
- [DONE] #legacy_8af3 Legacy issue: Markdown + math targets, tap-to-copy room code, logo home, fix WS unknown-room retry loop -> Markdown + math targets, tap-to-copy room code, logo home, fix WS unknown-room retry loop (fixed)
- [DONE] #legacy_142a Legacy issue: fix(deploy): make the SQLite PVC writable by the non-root user -> fix(deploy): make the SQLite PVC writable by the non-root user (fixed)

## Decisions
- No decisions logged yet.

## Notes
- Optimize server for heavy load: tower limits, pool tuning, indexes, batched snapshots, WS backpressure, LLM deadlines, hardened container
- Merge pull request #2 from HE-Arc/server_rewrite
- Rustls, limites 5000 tokens / 1000 chars, intitule prompt en accent
- Fusion server_rewrite : rustls, limites 5000 tokens / 1000 chars, intitule prompt en accent
- Improve frontend UI with layered visual polish
- feat(deploy): single Docker image serving API and SPA, with k8s manifests and Docker Hub CI
- New feature: feat: configurable quiz window with reveal delay and ticker fix [frontend/src/Admin.tsx]
- Merge: feat: wait for answers in flight, keep the best attempt, 4096-char prompts
- New feature: feat: token usage stats page and in-panel exercise copy button [frontend/src/App.tsx]
- Merge: fix: keep the page put while typing low in an autosizing field

## Key files
- `server/Cargo.lock`
- `server/.env.example`
- `server/src/llm/mod.rs`
- `server/src/llm/openai.rs`
- `.env.example`
- `README.md`
- `muse-spark-1.3`
- `.gitignore`
- `server/src/db/rooms.rs`
- `server/src/hub.rs`
- `server/src/rest/mod.rs`
- `server/src/ws/mod.rs`
- `server/tests/rest_parity.rs`
- `server/tests/ws_flow.rs`
- `server/Cargo.toml`
- `server/src/admin_session.rs`
- `server/src/main.rs`
- `test.sh`
- `frontend/src/useRoom.ts`
- `frontend/src/Game.tsx`

## Open questions
- None logged yet.
