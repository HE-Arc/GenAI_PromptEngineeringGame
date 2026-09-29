# Jeu GenAi

Jeu de prompt engineering: chaque manche affiche une reponse cachee, les joueurs ecrivent
un prompt, le modele repond, un jury note la proximite sur 10.

## Lancer

```bash
./run.sh
```

**Aucune cle n'est necessaire.** Sans `OPENAI_API_KEY`, le jeu tourne sur une doublure
locale: le modele est remplace par un echo du prompt, et le jury note par chevauchement de
mots. La partie est donc jouable de bout en bout hors ligne, et le commentaire du jury,
affiche sous la note, dit explicitement qu'il ne s'agit pas d'un vrai modele.

Pour le vrai modele, et seulement lui:

```bash
cp backend/.env.example backend/.env   # remplir OPENAI_API_KEY, ADMIN_PASSWORD et SECRET_KEY
```

- Joueur: ouvrir http://<IP-LAN>:5173 depuis le telephone -> code de partie + pseudo
- Admin: ouvrir http://<IP-LAN>:5173/admin -> mot de passe, creer la partie, demarrer, manche suivante

Les pages sont des vrais chemins (`/`, `/room/<CODE>`, `/admin`), pas des ancres `#/`. Le
serveur Vite repond `index.html` sur ces trois chemins, donc un rechargement fonctionne et un
signet vers `/admin` reste valide. Le code de la partie est toujours genere par le serveur:
l'admin ne le saisit nulle part, donc deux parties ne peuvent pas entrer en collision.

Une partie n'est accessible que depuis le navigateur qui l'a creee (le code est conserve
dans le `localStorage` de cet onglet). Vider les donnees du site, ou changer d'appareil,
la perd.

`<IP-LAN>` est l'adresse IP de la machine qui lance `run.sh` sur le reseau local
(`ipconfig getifaddr en0` sous macOS, `hostname -I` sous Linux), pas `localhost`:
`localhost` ne marche que sur la machine qui heberge le serveur.

Tout le monde sur le reseau qui possede le code de la partie peut suivre les prompts
en direct: garde le code pour le groupe.

`backend/.env` n'est lu que par `run.sh`, qui fait `set -a; . backend/.env; set +a`.
`settings.py` lit `os.environ` directement et il n'y a pas de python-dotenv: lancer
`manage.py runserver` ou `manage.py test` directement depuis `backend/` ignore donc ce
fichier. Toujours passer par `run.sh`, ou exporter les variables soi-meme avant.
`ADMIN_PASSWORD` sans valeur vaut `genai` et `SECRET_KEY` vaut une valeur de dev: changez
les deux avant d'exposer la partie, sinon n'importe qui ayant lu le depot peut forger une
session d'admin.

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
