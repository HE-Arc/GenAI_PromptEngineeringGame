# Jeu GenAi

Jeu de prompt engineering: chaque manche affiche une reponse cachee, les joueurs ecrivent
un prompt, le modele repond, un jury note la proximite sur 10.

## Lancer

```bash
cp backend/.env.example backend/.env   # remplir OPENAI_API_KEY et ADMIN_PASSWORD
./run.sh
```

- Joueur: http://localhost:5173 -> code de partie + pseudo
- Admin: http://localhost:5173/#/admin -> mot de passe, creer la partie, demarrer, manche suivante

## Regles

- 10 manches tirees au sort parmi `backend/game/targets.py`.
- La manche avance des que tous les joueurs ont repondu, ou quand l admin force.
- Une seule reponse par joueur et par manche.
- Le classement est mis a jour en direct par SSE (`/api/rooms/<code>/events`).

## Tests

```bash
cd backend && uv run python manage.py test game
```

Les tests n'appellent pas l'API: `game.llm.answer` et `game.llm.score` sont mockes.
