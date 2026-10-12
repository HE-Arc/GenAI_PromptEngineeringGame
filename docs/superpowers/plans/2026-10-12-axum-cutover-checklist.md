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
| 5 | admin : **DERNIÈRE, NON FAITE TANT QUE L'AUTH SESSION N'EXISTE PAS CÔTÉ AXUM** (voir § Admin) | entrée proxy admin → `:3001` | contrat `403/404/409` + session admin |

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

## Admin : reste sur Django (écart Task 7 connu)

La console admin reste sur le SSE avec auth par cookie de session (écart
acté en Task 7 : pas de `admin_key` transporté comme le WS `?token=`).
Tant que l'auth session côté serveur n'existe pas dans Axum, les routes
admin restent câblées sur Django `:8000` et l'étape 5 du tableau ci-dessus
est **explicitement exclue** du cutover. La bascule admin n'a lieu qu'après
l'arrivée de l'auth session côté Axum, avec re-validation du contrat
`403/404/409` + session avant de toucher au proxy.
