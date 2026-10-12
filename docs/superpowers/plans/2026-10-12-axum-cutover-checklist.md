# Cutover strangler : Django → Axum, phase par phase (Task 8)

Objectif : basculer les routes `/api/*` de Django `:8000` vers Axum `:3001`
une par une, derrière le proxy Vite `:5173`. Django reste joignable en direct
(`:8000`) à chaque étape pour un rollback instantané.

## Mécanique du proxy

`frontend/vite.config.ts` route `/api` vers Django par défaut.
`VITE_API_WS=1` route `/api/rooms/*/ws` vers Axum `:3001` (cible Axum
surchargée par `AXUM_URL`, Django par `BACKEND_URL`).
Chaque étape suivante ajoute une entrée de même forme (regex spécifique
avant le préfixe générique `/api`) ; un rollback = remettre l'entrée sur
Django, Axum reste allumé mais inactif.

```bash
# Terminaux : Django :8000, Axum :3001, Vite :5173 avec les règles proxy
python backend/manage.py runserver 8000 --noreload &
cargo run -p server -- --port 3001 &
npm --prefix frontend run dev
curl -s localhost:5173/api/healthz  # via le proxy
# Retour arrière d'une route : remettre sa cible proxy sur :8000,
# vérifier que Django la sert de nouveau
```

## Ordre de bascule

| # | Étape | Proxy | Porte d'entrée |
|---|-------|-------|----------------|
| 0 | ws-read-mirror : Axum sert `GET /api/rooms/<code>/ws` en miroir lecture seule (polling interne sur Django, aucune écriture) | `VITE_API_WS=1` | `cargo test ws_flow::ws_hello` |
| 1 | snapshot ownership : Axum possède le snapshot + broadcast, Django garde les écritures, Axum invalide au poll | `VITE_API_WS=1` (inchangé) | `cargo test hub::` |
| 2 | join/ready : Axum possède join puis ready, chaque écriture incrémente `seq` | nouvelle entrée proxy join/ready → `:3001` | `cargo test rest_parity::join_wrong_phase_409_parity`, `write_invalidates_snapshot_seq` |
| 3 | rate/quiz : Axum possède rate puis quiz | entrée proxy rate/quiz → `:3001` | `cargo test rest_parity::` |
| 4 | answer : `answer.start` sur WS remplace `POST .../answer` SSE ; refus `already_answered` / `inflight` par `error` WS (sémantique 409 conservée) | entrée proxy answer → `:3001` | `cargo test llm:: answer::` |
| 5 | admin : session cookie signée (HMAC, `ADMIN_SECRET`) + clé `admin_key` en repli ; console abonnée en WS via le cookie | tout `/api/*` → `:3001` | `cargo test admin_` + `ws_admin_cookie` |

## Bascule finale (Task 10a, faite)

L'auth session existe côté Axum (`POST /api/admin/login` émet un cookie HttpOnly signé, `GET /api/admin/rooms` liste les parties, le WS lit le cookie), donc l'étape 5 est basculée et Django est supprimé : `backend/` n'existe plus, `docker-compose.yml` ne monte que l'Axum `:3001`, et `frontend/vite.config.ts` envoie tout `/api/*` vers Axum sans drapeau. Le harnais de charge vit en `server/load_test_ws.py`. Portes tenues avant suppression : suite Django 407 tests OK une dernière fois + smoke 10 joueurs PASS sur toutes les portes.

## Porte (gate) E2E par étape

Aucune étape n'est validée sans les deux feux verts :

```bash
python backend/manage.py test            # suite Python existante
python backend/load_test_ws.py --players 10   # smoke 10 joueurs
```

## Rollback

Bascule arrière d'une étape (remettre sa cible proxy sur `:8000`, Axum
tourne toujours) dès qu'**UN** critère tient 10 min en staging :

- taux HTTP 5xx > 0,5 % ;
- déconnexions WS > 5 %/min ;
- faux positifs `already_answered` > 0 (vérifié contre la base Django) ;
- smoke de charge en échec ;
- `seq` de snapshot qui recule.

Rollback total = tout `/api/*` vers Django, Axum inactif.
Sécurité des données : Axum et Django partagent le même fichier SQLite en
WAL pendant le strangler (lecteurs concurrents autorisés), un rollback ne
perd donc aucune ligne.

## Admin : basculée (écart Task 7 résorbé en Task 10a)

La console s'authentifiait par cookie de session, que le WS ne savait pas lire (écart acté en Task 7). L'auth session existe désormais côté Axum (voir § Bascule finale), la console est abonnée en WS via le cookie, et les routes admin sont câblées sur Axum `:3001`.
