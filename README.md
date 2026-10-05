# Jeu GenAi

Jeu de classe pour apprendre le prompt engineering en jouant. Chaque tour montre une cible
cachée, chacun écrit un prompt pour l'obtenir, le modèle répond, et le groupe juge ensuite
les prompts les uns des autres.

La note d'un tour ne mesure pas le résultat seul. Elle moyenne deux choses : la **proximité**
entre la réponse obtenue et la cible, et la **qualité du prompt**, c'est-à-dire sa conformité
aux règles que le didacticiel vient d'enseigner. Un élève dont les prompts sont mal écrits
mais dont la sortie chance bien ne sort pas premier.

La spécification complète est dans le [wiki du projet](https://github.com/HE-Arc/GenAI_PromptEngineeringGame/wiki/Cahier-des-charges).
Ce README couvre l'installation, le déroulé et les commandes.

## Lancer

```bash
./run.sh
```

**La clé d'API décide ce que le jeu mesure, pas s'il démarre.** Sans `OPENAI_API_KEY`, les
trois appels au modèle tombent sur une doublure locale : le modèle est remplacé par un écho du
prompt, le jury de proximité note par chevauchement de mots, et le critique de prompt compte
des marqueurs de structure. La partie se joue de bout en bout hors ligne, et le commentaire
affiché sous chaque note dit explicitement qu'il s'agit d'une règle locale et non d'un jury.

**Ce mode est réservé au développement.** Il fait tourner la mécanique — phases, didacticiel,
notation croisée, SSE — sans clé et sans réseau, mais **les notes qu'il produit ne mesurent
rien**. L'écho ne répond pas à la cible, et le chevauchement de mots plafonne dès que la cible
est longue, donc les proximités y sont basses par construction, ce qui n'est pas la faute du
prompt. Utilisé en séance, ce mode apprend aux élèves à recopier les mots de la cible, pas à
écrire des prompts.

Pour une partie réelle, la clé doit etre presente dans `.env` :

```bash
cp backend/.env.example backend/.env   # remplir OPENAI_API_KEY, ADMIN_PASSWORD et SECRET_KEY
```

`OPENAI_API_KEY` est ce qui fait qu'un classement classe des prompts : sans elle, aucun des
trois jugements n'est rendu par un modèle. `ADMIN_PASSWORD` et `SECRET_KEY` se changent au même
moment, pour la raison donnée plus bas.

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

**La liste des salles se nettoie toute seule, mais pas à la même profondeur.** Une salle sans
joueur connecté depuis dix minutes disparaît de la liste de l'admin. Un **lobby** jamais lancé,
lui, est **supprimé** : il ne contient qu'une banque de questions et des jetons, et personne n'a de
chose à y retrouver. Une partie **jouée** disparaît d'abord de la liste, reste en base tel quel le
temps d'une pause — un prof qui s'absente retrouve sa partie et ses pseudos dès qu'un joueur se
reconnecte — mais est **supprimée au bout d'une heure** sans personne : la ligne part, et avec elle
les jetons et les rounds. Après une heure, cette partie n'est plus récupérable. Le nettoyage
tourne **tout seul** désormais, un fil de fond dans le processus du serveur : plus besoin de laisser
une console d'admin ouverte. Cette « dernière utilisation » n'est pourtant écrite que par ce fil de
fond, donc seulement si le serveur est lancé via `run.sh` (qui exporte `GENAI_SWEEP=1`) : lancé
avec `manage.py runserver` tout seul, le fil de fond n'existe pas, la colonne reste vide et une
partie jouée est mesurée depuis son **démarrage** plutôt que depuis son dernier usage — elle peut
donc être supprimée alors qu'elle a encore servi il y a deux heures.

`backend/.env` n'est lu que par `run.sh`, qui fait `set -a; . backend/.env; set +a`.
`settings.py` lit `os.environ` directement et il n'y a pas de python-dotenv : lancer
`manage.py runserver` ou `manage.py test` directement depuis `backend/` ignore donc ce
fichier. Toujours passer par `run.sh`, ou exporter les variables soi-même avant.
`ADMIN_PASSWORD` sans valeur vaut `genai` et `SECRET_KEY` vaut une valeur de dev : changez les
deux avant d'exposer la partie, sinon n'importe qui ayant lu le dépôt peut forger une session
d'admin, et une session forgée ne peut pas être révoquée.

## Comment se joue une partie

```
lobby ──(tous prêts)──▶ didacticiel ──(majorité prête)──▶ ┌─ tour 1 ─┐
                        (8 questions)                     │          │
                                                          ▼          │
                                                   rédaction ──▶ notation ──▶ résumé ──▶ tour suivant
                                                                                          └──▶ classement final
```

**La classe lance la partie.** Dans le lobby, tout le monde appuie sur « prêt », et le jeu s'ouvre
quand tout le monde l'a fait. Entre chaque étape, la classe **discute à voix haute**, puis appuie
sur « prêt » : quand la majorité l'a fait, le jeu passe à la suite tout seul. L'admin crée la
partie, et n'a plus de bouton à presser pendant la séance.

**Didacticiel.** Huit questions à quatre choix, tirées dans un ordre propre à la partie et
identiques pour tous les joueurs. Chaque réponse est révélée avec son explication. Le
didacticiel bloque la partie : personne n'écrit un prompt avant d'avoir vu les huit questions.
Il s'ouvre dès que la majorité des joueurs connectés a appuyé sur « prêt » après la révélation,
et l'admin peut le passer. Son score est affiché et classé à la fin, mais **n'entre pas** dans le
classement de la partie : une porte à passer ne doit pas être un score à farmer.

**Rédaction.** Les dix cibles sont tirées au sort au lancement, identiques et dans le même
ordre pour tout le monde, donc le classement mesure les joueurs et pas la chance du tirage.
Une réponse par joueur et par tour.

**La réponse s'écrit à l'écran.** Le texte arrive à mesure que le modèle le produit, puis se lit
une fois terminé comme du markdown — listes, gras, titres — au lieu d'un bloc brut. La note
arrive **après** : le jury est un appel distinct, donc un silence sépare les derniers mots du
score, et ce silence est le jury qui travaille, pas un plantage. Le mode sans clé en fait
autant, lui aussi morceau par morceau : la mécanique se montre donc sans clé et sans facture,
mais elle ne juge toujours pas. Si la limite de longueur du modèle coupe la réponse, l'écran
le dit.

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
- **Deux seuils, et ils ne se ressemblent pas.** Le lobby est une **unanimité** : la partie démarre
  quand tous les joueurs connectés ont appuyé sur « prêt ». Chaque discussion — après une question
  du didacticiel révélée, après un prompt noté, après le résumé d'un tour — se termine sur une
  **majorité**, la moitié arrondie au supérieur : 11 sur 20, 2 sur 3, et 2 sur 2 quand il n'y a que
  deux joueurs. L'écart est voulu. Dans le lobby tout le monde vient d'arriver et regarde le même
  écran, donc attendre le dernier ne coûte rien : un téléphone pas encore sorti de la poche est un
  téléphone qui va sortir. Au milieu d'une discussion c'est l'inverse — un téléphone qui dort
  bloquerait toute la classe sur une barre qui n'avancera plus, et personne n'oserait le rallumer.
  La classe fait avancer le jeu toute seule ; le bouton de l'admin reste, pour le cas où un
  téléphone est mort.
- **Le décompte ne compte que les joueurs connectés**, et seulement s'il y en a au moins un,
  sinon `0 == 0` ferait avancer une partie vide. Un téléphone qui dort ne bloque donc personne,
  sauf dans le lobby, où il faut attendre tout le monde — c'est le seul endroit où le jeu s'ouvre.
  L'auteur du prompt à l'écran ne vote pas : personne ne note le sien, donc il ne fait pas
  partie du décompte.
- **La discussion est orale.** Aucun champ de texte, aucun commentaire écrit : sur un
  téléphone, un élève qui écrit ne parle plus à sa classe. Le didacticiel garde sa fenêtre de
  trente secondes, et elle se ferme dès que tous les joueurs connectés ont répondu.
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

362 tests. Aucun n'ouvre de connexion réseau, et tous passent sans `OPENAI_API_KEY`, donc avec
les doublures locales. Ils ne mockent pas tous au même niveau : `test_play.py` et
`test_rating.py` mockent `game.llm.answer_stream`, `game.llm.score` et `game.llm.grade_prompt`,
tandis que `test_llm.py` mocke `llm.OpenAI` lui-même pour que le vrai `_chat` s'exécute.
