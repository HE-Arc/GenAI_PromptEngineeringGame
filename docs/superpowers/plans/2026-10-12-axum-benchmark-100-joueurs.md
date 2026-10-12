# Benchmark 100 joueurs — Axum vs Django

Conditions : LLM stubbé (`OPENAI_API_KEY` non définie, zéro appel facturé), base jetable, 100 joueurs simultanés dans une room. Harnais : `backend/load_test_100.py` (Django) et `backend/load_test_ws.py` (Axum). Mesures du 2026-10-12.

| Scénario | Axum | Django avant | Gain |
|---|---|---|---|
| Join 100 REST | p50 0,104 s, 636 req/s, 0 err | 5,7 s | ~55x |
| Snapshot 100 WS | first-byte 0,015 s | 22 s | ~1400x |
| Answer 100 WS | first-chunk 0,013 s, 100/100 done | 7,5 s + 8 locked | ~580x, 0 locked |

Verdict : 5/5 gates PASS — 0 `database-is-locked`, 0,0 % de resync lagged (< 5 %), 100/100 lignes `Answer` persistées.
