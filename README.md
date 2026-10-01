# Jeu GenAi

Jeu de classe pour apprendre le prompt engineering en jouant. Chaque tour montre une cible
cachée, chacun écrit un prompt pour l'obtenir, le modèle répond, et le groupe juge ensuite
les prompts les uns des autres.

La note d'un tour ne mesure pas le résultat seul. Elle moyenne deux choses : la **proximité**
entre la réponse obtenue et la cible, et la **qualité du prompt**, c'est-à-dire sa conformité
aux règles que le didacticiel vient d'enseigner. Un élève dont les prompts sont mal écrits
mais dont la sortie chance bien ne sort pas premier.

La spécification complète est dans le [wiki du projet](https://github.com/HE-Arc/GenAI_PromptEngineeringGame/wiki/Cahier-des-charges).
Ce README couvre l'installation, le déroulé et les commandes. Le plan d'implémentation qui a
produit ce jeu est suivi dans [`docs/superpowers/plans/`](docs/superpowers/plans/).

## Lancer

```bash
./run.sh
```

**Aucune clé n'est nécessaire.** Sans `OPENAI_API_KEY`, les trois appels au modèle tombent sur
une doublure locale : le modèle est remplacé par un écho du prompt, le jury de proximité note
par chevauchement de mots, et le critique de prompt compte des marqueurs de structure. La
partie se joue donc de bout en bout hors ligne, et le commentaire affiché sous chaque note dit
explicitement qu'il s'agit d'une règle locale et non d'un jury. Jouable, mais pas classable : le
chevauchement de mots plafonne dès que la cible est longue, donc les notes y sont basses par
construction. Ce n'est pas la faute du prompt, et le commentaire le dit.

Pour le vrai modèle, et seulement lui :

```bash
cp backend/.env.example backend/.env   # remplir OPENAI_API_KEY, ADMIN_PASSWORD et SECRET_KEY
```

- Joueur : ouvrir `http://<IP-LAN>:5173` depuis le téléphone, puis code de partie et pseudo
- Admin : ouvrir `http://<IP-LAN>:5173/admin`, puis mot de passe

`<IP-LAN>` est l'adresse IP de la machine qui lance `run.sh` sur le réseau local
(`ipconfig getifaddr en0` sous macOS, `hostname -I` sous Linux), pas `localhost` : `localhost`
ne marche que sur la machine qui héberge le serveur. Tout le monde sur le réseau qui possède
le code peut suivre la partie en direct ; garde le code pour le groupe.

Les pages sont de vrais chemins (`/`, `/room/<CODE>`, `/admin`), pas des ancres `#/`, et le code
de la partie est toujours généré par le serveur : l'admin ne le saisit nulle part, donc deux
parties ne peuvent pas entrer en collision. Une partie n'est accessible que depuis le
navigateur qui l'a créée, le code étant conservé dans le `localStorage` de cet onglet.

`backend/.env` n'est lu que par `run.sh`, qui fait `set -a; . backend/.env; set +a`.
`settings.py` lit `os.environ` directement et il n'y a pas de python-dotenv : lancer
`manage.py runserver` ou `manage.py test` directement depuis `backend/` ignore donc ce
fichier. Toujours passer par `run.sh`, ou exporter les variables soi-même avant.
`ADMIN_PASSWORD` sans valeur vaut `genai` et `SECRET_KEY` vaut une valeur de dev : changez les
deux avant d'exposer la partie, sinon n'importe qui ayant lu le dépôt peut forger une session
d'admin, et une session forgée ne peut pas être révoquée.

## Comment se joue une partie

```
lobby ──▶ didacticiel ──▶ ┌─ tour 1 ─┐
         (8 questions)   │          │
                          ▼          │
                   rédaction ──▶ notation ──▶ résumé ──▶ tour suivant
                                                         └──▶ classement final
```

**Didacticiel.** Huit questions à quatre choix, tirées dans un ordre propre à la partie et
identiques pour tous les joueurs. Chaque réponse est révélée avec son explication. Le
didacticiel bloque la partie : personne n'écrit un prompt avant d'avoir vu les huit questions.
Il s'ouvre seul dès que tous les joueurs connectés ont répondu, et l'admin peut le passer. Son
score est affiché et classé à la fin, mais **n'entre pas** dans le classement de la partie : une
porte à passer ne doit pas être un score à farmer.

**Rédaction.** Les dix cibles sont tirées au sort au lancement, identiques et dans le même
ordre pour tout le monde, donc le classement mesure les joueurs et pas la chance du tirage.
Une réponse par joueur et par tour.

**Notation croisée.** Les prompts sont révélés un à un. Chaque joueur, sauf l'auteur, donne une
note de 1 à 10 sur la conformité du prompt aux règles du prompt engineering. Pas de commentaire :
sur un téléphone, un champ libre à chaque prompt fait abandonner.

**Résumé de tour.** Le résumé lève l'anonymat et montre, par joueur, le prompt, la proximité,
la note du critique, la moyenne des pairs, la qualité retenue et la note du tour.

## Règles

- **Anonymat**, choisi par l'admin à la création de la partie. Les noms restent visibles, les
  scores sont masqués pendant toute la notation. Ce sont les scores qui font la fuite : un
  joueur qui lit son propre score, voit qu'il manque dans la colonne, et en déduit quelle
  ligne révélée est la sienne a défait l'anonymat. Le classement réapparaît au résumé.
- **Garde-fou.** La note de qualité est la moyenne des pairs **bornée à ±3 points** autour de la
  note du modèle, qui joue le rôle d'arbitre. Une classe ne peut ni maintenir un prompt nul à
  la perfection, ni lyncher un prompt solide. En revanche une classe qui juge sainement peut
  corriger le modèle de trois points dans un sens ou dans l'autre. La note du modèle n'est
  montrée qu'au résumé : l'afficher pendant la notation ouvrirait l'ancrage.
- **Note de tour** : `(proximité + qualité) / 2`. Pendant la rédaction, le tour en cours compte
  pour sa seule proximité, de façon provisoire ; au résumé il est corrigé. Cette correction
  peut être une baisse, et c'est le moment pédagogique : on y voit le prix d'un prompt faible.
- **Classement final** : la somme des dix tours, sur 100.
- **Avance de phase** : elle ne compte que les joueurs **connectés**, et seulement s'il y en a
  au moins un, sinon `0 == 0` ferait avancer la partie toute seule. Un téléphone qui dort ne
  bloque donc pas la classe. L'admin avance quand il le décide, à n'importe quelle phase ; le
  résumé ne bouge jamais tout seul, parce qu'un tour se commente.
- **Un joueur hors ligne** qui revient est retrouvé par son jeton, sans rien ressaisir. Il peut
  noter, et il participe au tour suivant. En revanche il ne peut plus écrire pour le tour
  qu'il a manqué.
- **Une seule prise par joueur et par phase.**
- Le classement est mis à jour en direct par SSE (`/api/rooms/<code>/events?token=<token>`). Le
  jeton identifie le joueur derrière le flux, il est donc requis pour être compté comme présent.

## Contenus

Les questions du didacticiel (`backend/game/quiz.py`) et les cibles
(`backend/game/targets.py`) sont des données, pas de la logique : on les édite sans toucher au
code. La banque du QCM est tirée par partie, donc deux salles ne se passent pas les questions
dans le même ordre.

## Tests

```bash
cd backend && uv run python manage.py test game
```

238 tests. Aucun n'ouvre de connexion réseau, et tous passent sans `OPENAI_API_KEY`, donc avec
les doublures locales. Ils ne mockent pas tous au même niveau : `test_play.py` et
`test_rating.py` mockent `game.llm.answer`, `game.llm.score` et `game.llm.grade_prompt`, tandis
que `test_llm.py` mocke `llm.OpenAI` lui-même pour que le vrai `_chat` s'exécute.
