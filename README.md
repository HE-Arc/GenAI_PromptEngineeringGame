# Jeu GenAi

Jeu de prompt engineering: chaque manche affiche une reponse cachee, les joueurs ecrivent
un prompt, le modele repond, un jury note la proximite sur 10.

## Lancer

```bash
cp backend/.env.example backend/.env   # remplir OPENAI_API_KEY, ADMIN_PASSWORD et SECRET_KEY
./run.sh
```

- Joueur: ouvrir http://<IP-LAN>:5173 depuis le telephone -> code de partie + pseudo
- Admin: ouvrir http://<IP-LAN>:5173/#/admin -> mot de passe, creer la partie, demarrer, manche suivante

`<IP-LAN>` est l'adresse IP de la machine qui lance `run.sh` sur le reseau local
(`ipconfig getifaddr en0` sous macOS, `hostname -I` sous Linux), pas `localhost`:
`localhost` ne marche que sur la machine qui heberge le serveur.

Tout le monde sur le reseau qui possede le code de la partie peut suivre les prompts
en direct: garde le code pour le groupe.

`backend/.env` n'est lu que par `run.sh`, qui fait `set -a; . backend/.env; set +a`.
`settings.py` lit `os.environ` directement et il n'y a pas de python-dotenv: lancer
`manage.py runserver` ou `manage.py test` directement depuis `backend/` ignore donc ce
fichier, et l'on retombe silencieusement sur `ADMIN_PASSWORD=genai` sans `OPENAI_API_KEY`.
Toujours passer par `run.sh`, ou exporter les variables soi-meme avant.

## Regles

- 10 manches tirees au sort parmi `backend/game/targets.py`.
- La manche avance des que tous les joueurs ont repondu, ou quand l admin force.
- Une seule reponse par joueur et par manche.
- Le classement est mis a jour en direct par SSE (`/api/rooms/<code>/events`).

## Tests

```bash
cd backend && uv run python manage.py test game
```

Les tests n'appellent jamais l'API, mais pas toujours au meme niveau: `test_play.py` mocke
`game.llm.answer` et `game.llm.score`, tandis que `test_llm.py` mocke `llm.OpenAI` lui-meme
pour que le vrai `_chat` s'execute. Aucun test n'ouvre de connexion reseau.
