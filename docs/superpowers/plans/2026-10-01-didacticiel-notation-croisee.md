# Jeu de prompt engineering — didacticiel et notation croisée

> **Pour les agents exécuteurs :** sous-skill REQUIS : utiliser
> `superpowers:subagent-driven-development` (recommandé) ou `superpowers:executing-plans`
> pour exécuter ce plan tâche par tâche. Les étapes utilisent des cases à cocher (`- [ ]`).

**Objectif :** ajouter au jeu existant un didacticiel QCM bloquant et une phase de notation
croisée entre joueurs, avec un LLM qui borne les notes humaines, sans casser le socle
existant (10 manches, cibles communes, flux SSE, tests).

**Architecture :** le backend Django gagne trois modèles (`QuizQuestion`, `QuizAttempt`,
`Rating`) et deux champs (`Room.anonymous`, `Round.revealed`), plus une machine à états de
phase qui remplace l'avance implicite. Le flux SSE devient le véhicule de la notation : il
pousse `phase` et, en phase `rating`, un `reveal` par tour. Le frontend gagne deux écrans
(`Quiz`, `Rating`) et un sélecteur de phase dans `App.tsx`.

**Pile technique :** Django 5 (`backend/`, SQLite, `runserver` threadé), React 19 + Vite 8 +
TypeScript 6 (`frontend/`, CSS global en 3 couches, aucun router, aucun test runner).

**Spec :** le wiki du projet, page « Cahier des charges » (clone local :
`GenAI_PromptEngineeringGame.wiki/Cahier-des-charges.md`) — le plan argumente depuis la spec, la spec voyage
avec lui ; les exécuteurs lisent les deux.

## Contraintes globales

- Zéro clé d'API : toute fonction LLM ajoutée doit avoir un repli local et le dire au joueur
  dans son commentaire. Les tests ne doivent jamais ouvrir de connexion réseau.
- Les tests ne mockent jamais `llm.OpenAI` au niveau supérieur : `test_play.py` mocke
  `game.llm.answer` / `game.llm.score`, `test_llm.py` mocke `llm.OpenAI` pour que le vrai
  `_chat` s'exécute. Les nouveaux tests suivent la même répartition.
- Les questions du QCM et les cibles vivent dans des fichiers de données
  (`backend/game/quiz.py`, `backend/game/targets.py`), jamais dans `views.py`.
- Toute annotation deFR ou tout texte utilisateur est en français, sans accent, comme le
  reste du code (les commentaires sont eux accentués, voir `models.py:33-41`).
- `Room.code` est la clé primaire et reste généré par le serveur, jamais saisi.
- Une seule réponse par joueur et par tour : `unique_together` est la source de vérité, le
  `_inflight` process-local est une optimisation, et `IntegrityError` reste le filet.
- L'avancement de phase ne compte que les joueurs **connectés** (`online_player_ids`), et
  seulement si au moins un joueur est connecté — sinon `0 == 0` ferait tourner la partie
  toute seule.
- Le flux SSE pousse l'instantané complet toutes les 0,5 s. Tout nouveau champ de phase
  entre dans cet instantané, pas dans un second canal.
- Les scores sont des entiers 0-10. La qualité retenue est un entier. La note de tour est
  un entier. Aucun `float` ne se promène dans le payload.
- Le classement final est un total sur 100. Le score du QCM ne s'y ajoute pas.
- Plage de la note de prompt : 1 à 10, 10 = Excellent. Le même intervalle alimente
  l'échelle visuelle et la borne ±3.
- Boutons `min-height: 3rem`, `textarea` `min-height: 7.5rem`, `viewport-fit=cover`,
  `env(safe-area-inset-*)`, `100dvh`, `Cmd/Ctrl+Enter` pour valider. Toute UI de notation
  respecte ces règles : c'est un jeu sur téléphone, debout.
- Frontend : pas de library de router, pas de library d'état, pas de CSS modules. Les
  classes suivent le BEM plat du dépôt (`.block__part`, `.block__part--mod`).
- Frontend : `cd frontend && bun run build` doit passer (`tsc -b` puis `vite build`) et
  `bun run lint` doit rester propre.
- Backend : `cd backend && uv run python manage.py test game` doit passer.

---

## Fichiers

| Fichier | Responsabilité |
|---|---|
| `backend/game/quiz.py` | **Nouveau.** Banque de 8 questions QCM : énoncé, 4 choix, index de la bonne réponse, explication. Données pures, aucun import Django. |
| `backend/game/models.py` | **Modifié.** `QuizQuestion`, `QuizAttempt`, `Rating`, `Room.anonymous`, `Room.phase`, `Round.revealed`, `Room.tutorial`. |
| `backend/game/migrations/0003_*.py` | **Nouveau.** Généré par `makemigrations`, revu à la main. |
| `backend/game/llm.py` | **Modifié.** `grade_prompt(prompt) -> Verdict` avec `CRITIC_SYS`, plus `_stub_grade`. Même contrat de repli. |
| `backend/game/prompts.py` | **Nouveau.** Le libellé de la grille de notation croisée et la borne ±3, en un seul endroit. |
| `backend/game/views.py` | **Modifié.** `room_state` étendu (`phase`, `reveal`, `quiz`), nouveaux endpoints quiz/rating, `maybe_advance` devient `maybe_advance_phase`. |
| `backend/game/tests/test_quiz.py` | **Nouveau.** Banque de questions + endpoints QCM + blocage. |
| `backend/game/tests/test_rating.py` | **Nouveau.** Notation croisée, anonymat, borne LLM, reveal. |
| `backend/game/tests/test_play.py` | **Modifié.** Les 3 égalités de dict de `room_state` et les 409 de `not playing`. |
| `backend/game/tests/test_admin.py` | **Modifié.** `start`/`next`/`restart` avec phase et didacticiel. |
| `frontend/src/api.ts` | **Modifié.** `Phase`, `QuizState`, `RevealState`, `Player` nullable, `State` étendu. |
| `frontend/src/Quiz.tsx` | **Nouveau.** Écran du QCM : question, 4 choix, révélation, progression, classement QCM. |
| `frontend/src/Rating.tsx` | **Nouveau.** Écran de notation : prompt révélé, échelle 1-10, progression, « en attente de N ». |
| `frontend/src/App.tsx` | **Modifié.** Sélecteur de phase : `quiz` → `Quiz`, `rating` → `Rating`. |
| `frontend/src/Game.tsx` | **Modifié.** Masque le classement en phase `rating`, affiche le résumé de tour en phase `summary`. |
| `frontend/src/Results.tsx` | **Modifié.** Colonnes proximité / qualité / note de tour. |
| `frontend/src/Admin.tsx` | **Modifié.** Cases « avec didacticiel » et « anonyme », bouton de phase, relance avec choix. |
| `frontend/src/ui.tsx` | **Modifié.** `ScoreScale` accepte une plage, `Board` accepte des colonnes optionnelles. |
| `frontend/src/index.css` / `console.css` / `pages.css` | **Modifiés.** Styles de l'échelle, du résumé, des réponses de QCM. |

Les patterns existants à copier, pas à réinventer : `_beat` / `online_player_ids` pour la
présence, `_inflight` pour les appels facturés, `body()` pour le JSON tolérant,
`json_error` pour les erreurs, `Verdict` pour tout retour de LLM, `pick()` pour le tirage
partagé. Le commentaire « pourquoi » en tête de chaque bloc est une convention du dépôt :
il explique un arbitrage, pas une ligne de code. Ne le supprimez pas, ne le remplissez pas
de banalités.

---

## Machine à états

```
waiting ──start──▶ quiz ──(tous les QCM faits)──▶ playing
                    │                             │
                    └──(admin: passer)───────────▶ │
                                                  ▼
                          playing ──(tous ont répondu)──▶ rating
                                                  ▲         │
                                                  │         ▼
                                        (tous ont noté)  summary
                                                  ▲         │
                                                  └─────────┘
                                                          │
                                                          ▼ (10e tour)
                                                        done
```

`phase` est un champ `TextChoices` sur `Room` : `waiting`, `quiz`, `playing`, `rating`,
`summary`, `done`. Il remplace `status`, qui devient `@`deprecated` — un seul champ qui
décrit la phase, plutôt que `status` plus un booléen qui pourrait en contredire un autre.

---

## Tâche 1 : la banque de questions

- [ ] **Étape 1 : écrire le test qui échoue**

`backend/game/tests/test_quiz.py` :

```python
from django.test import SimpleTestCase

from game.quiz import QUESTIONS, QUIZ_LENGTH


class QuizBankTests(SimpleTestCase):
    def test_eight_questions(self):
        self.assertEqual(len(QUESTIONS), QUIZ_LENGTH)

    def test_every_question_has_four_distinct_choices(self):
        for i, q in enumerate(QUESTIONS):
            with self.subTest(question=i):
                self.assertEqual(len(q.choices), 4)
                self.assertEqual(len(set(q.choices)), 4)

    def test_every_correct_index_is_in_range(self):
        for i, q in enumerate(QUESTIONS):
            with self.subTest(question=i):
                self.assertIn(q.correct, range(4))

    def test_every_question_explains_its_answer(self):
        for i, q in enumerate(QUESTIONS):
            with self.subTest(question=i):
                self.assertGreater(len(q.explanation), 20)

    def test_a_question_is_never_trivially_guessable_by_position(self):
        # A Kahoot where the right answer is always first teaches position, not prompt
        # engineering. Require the correct index to be spread, not uniform-by-accident.
        positions = {q.correct for q in QUESTIONS}
        self.assertGreaterEqual(len(positions), 3)
```

- [ ] **Étape 2 : vérifier que le test échoue**

`cd backend && uv run python manage.py test game.tests.test_quiz`
Attendu : `ModuleNotFoundError: No module named 'game.quiz'`.

- [ ] **Étape 3 : écrire le contenu**

`backend/game/quiz.py` — un `NamedTuple` `Question(prompt, choices, correct, explanation)`,
une tuple `QUESTIONS` de 8, et `QUIZ_LENGTH = 8`. Les sujets, dans cet ordre : ce qu'est un
contexte, à quoi sert un rôle, l'effet d'une consigne de format de sortie, pourquoi un
prompt d'une ligne échoue, la différence entre objectif et contrainte, à quoi sert un
exemple dans le prompt, ce que le modèle ne devine pas, la bonne façon d'itérer. Les
mauvais choix doivent être plausibles : une erreur qu'un débutant repère sans savoir la
réponse n'est pas un distracteur, c'est une giveaway.

```python
"""The didacticiel's eight questions. Data, not logic — the admin edits this file, not views.py.

Each question is a NamedTuple so the four fields cannot be transposed at a call site: a
transposed `correct` would silently mark a wrong choice as right, and the test below would
still pass. `correct` is an INDEX, not the answer text, so a rewritten choice cannot
invalidate the key.
"""

from typing import NamedTuple

QUIZ_LENGTH = 8


class Question(NamedTuple):
    prompt: str
    choices: tuple[str, str, str, str]
    correct: int
    explanation: str


QUESTIONS: tuple[Question, ...] = ( ... )
```

- [ ] **Étape 4 : vérifier que le test passe**

`cd backend && uv run python manage.py test game.tests.test_quiz`
Attendu : `OK` pour `test_quiz`. Le fichier complet passe aussi — aucun import Django.

- [ ] **Étape 5 : committer**

```bash
git add backend/game/quiz.py backend/game/tests/test_quiz.py
git commit -m "feat(quiz): didacticiel bank of 8 QCM questions"
```

---

## Tâche 2 : les modèles

- [ ] **Étape 1 : écrire le test qui échoue**

Dans `backend/game/tests/test_models.py`, ajouter :

```python
class PhaseTests(SimpleTestCase):
    def test_status_is_replaced_by_phase(self):
        self.assertEqual(
            [p for p, _ in Room.Phase.choices],
            ["waiting", "quiz", "playing", "rating", "summary", "done"],
        )

    def test_room_defaults_to_waiting_without_tutorial(self):
        room = Room.objects.create(code="ABCD")
        self.assertEqual(room.phase, Room.Phase.WAITING)
        self.assertTrue(room.tutorial)
        self.assertTrue(room.anonymous)


class QuizAttemptTests(TestCase):
    def test_one_attempt_per_player_and_question(self):
        room, player, question = _quiz_fixture()
        QuizAttempt.objects.create(player=player, question=question, choice=0, correct=True)
        with self.assertRaises(IntegrityError):
            QuizAttempt.objects.create(player=player, question=question, choice=1, correct=False)

    def test_quiz_score_counts_correct_answers(self):
        room, player, _ = _quiz_fixture()
        for i, q in enumerate(QuizQuestion.objects.order_by("order")[:3]):
            QuizAttempt.objects.create(player=player, question=q, choice=0, correct=i % 2 == 0)
        self.assertEqual(player.quiz_score(), 2)


class RatingTests(TestCase):
    def test_one_rating_per_rater_per_answer(self):
        answer, raters = _rating_fixture()
        Rating.objects.create(answer=answer, rater=raters[0], score=7)
        with self.assertRaises(IntegrityError):
            Rating.objects.create(answer=answer, rater=raters[0], score=3)

    def test_a_player_cannot_rate_their_own_answer(self):
        # ValidationError, not IntegrityError: this is the `save()` guard, not a database
        # constraint — Django cannot express the rule in SQL, see the note in models.py.
        answer, raters = _rating_fixture()
        with self.assertRaises(ValidationError):
            Rating.objects.create(answer=answer, rater=answer.player, score=7)

    def test_rating_score_is_bounded(self):
        answer, raters = _rating_fixture()
        for bad in (0, 11, -1):
            with self.subTest(score=bad):
                with self.assertRaises(IntegrityError):
                    Rating.objects.create(answer=answer, rater=raters[0], score=bad)
```

avec `_quiz_fixture()` et `_rating_fixture()` définis en tête de fichier et renvoyant des
modèles sauvegardés. Les deux tests de borne s'appuient sur une contrainte
`CheckConstraint` : c'est une règle de domaine, pas une validation de formulaire, et une
note hors échelle produirait un classement faux en silence.

- [ ] **Étape 2 : vérifier que le test échoue**

`cd backend && uv run python manage.py test game.tests.test_models`
Attendu : `AttributeError: type object 'Room' has no attribute 'Phase'`.

- [ ] **Étape 3 : implémenter**

Dans `models.py` :

```python
class Room(models.Model):
    class Phase(models.TextChoices):
        WAITING = "waiting", "waiting"
        QUIZ = "quiz", "quiz"
        PLAYING = "playing", "playing"
        RATING = "rating", "rating"
        SUMMARY = "summary", "summary"
        DONE = "done", "done"

    code = models.CharField(max_length=8, primary_key=True)
    phase = models.CharField(max_length=8, choices=Phase.choices, default=Phase.WAITING)
    current = models.IntegerField(default=0)
    # Does this game open with the didacticiel? Fixed for the whole game, decided by the
    # admin at creation, so every player in the room faces the same gate. A restart carries
    # the flag: relaunching "without" means without, twice, not once.
    tutorial = models.BooleanField(default=True)
    # Anonymity during cross-rating is a per-game choice, not a per-round setting: flipping
    # it mid-game would tell players whose name appeared and whose did not, which leaks the
    # mapping the anonymity exists to hide.
    anonymous = models.BooleanField(default=True)

    def next_round(self) -> None: ...  # inchangé, mais passe le statut via Phase
```

Le modèle s'appelle `QuizQuestion`, pas `Question`. `game.quiz.Question` est déjà le
`NamedTuple` d'une entrée de banque : deux `Question` dans deux modules voisins, importés
dans le même `views.py`, sont un piège de lecture pour un nom qui ne distingue pas une
ligne en base d'un contenu en dur. `QuizQuestion` et `QuizAttempt` se lisent ensemble.

`QuizQuestion` porte `order` (unique), `prompt`, `choices` (JSONField liste de 4), `correct`
(entier), `explanation`. Elle est **matérialisée en base** par `admin_create_room`, tirée
depuis `quiz.QUESTIONS` : sans cela chaque joueur aurait sa banque et le QCM ne serait pas
le même pour tout le monde.

```python
class QuizQuestion(models.Model):
    order = models.IntegerField(unique=True)
    prompt = models.TextField()
    choices = models.JSONField()
    correct = models.IntegerField()
    explanation = models.TextField()
```

Le tirage vient de `quiz.py`, pas de `targets.pick` : les deux banques n'ont pas les mêmes
propriétés. `targets.pick` tire dans un pool de 24+ cibles pour en choisir 10 — un
échantillonnage. Ici la banque fait exactement `QUIZ_LENGTH` questions, donc un tirage
**est une permutation** : les huit mêmes questions, dans un ordre propre à la partie.
C'est ce qu'on veut — deux salles ne se passent pas le QCM dans le même ordre — mais
l'appeler « tirage » ferait croire à un sous-ensemble.

```python
def pick(n: int = QUIZ_LENGTH) -> tuple[Question, ...]:
    """The order this room asks its questions in.

    `random.sample(QUESTIONS, n)` with n == len(QUESTIONS) is a permutation, not a subset,
    and that is the whole point: the bank is exactly one quiz long, so the only variety
    available between two rooms is the ORDER. Calling this a draw would suggest a subset
    gets chosen, and a later edit that grows the bank would silently change what players
    get without anyone reading why.
    """
    return tuple(random.sample(QUESTIONS, n))
```

```python
class QuizAttempt(models.Model):
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="quiz_attempts")
    question = models.ForeignKey(QuizQuestion, on_delete=models.CASCADE, related_name="attempts")
    choice = models.IntegerField()
    correct = models.BooleanField()

    class Meta:
        unique_together = [("player", "question")]


class Rating(models.Model):
    answer = models.ForeignKey(Answer, on_delete=models.CASCADE, related_name="ratings")
    rater = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="ratings_given")
    score = models.IntegerField()

    class Meta:
        unique_together = [("answer", "rater")]
        constraints = [
            models.CheckConstraint(condition=models.Q(score__gte=1, score__lte=10), name="rating_score_1_10"),
        ]

    def save(self, *args, **kwargs):
        # NO-ONE-RATES-THEIR-OWN-PROMPT IS NOT A DATABASE CONSTRAINT. It looks like one and
        # the plan used to ask for it as one — `~models.Q(answer__player=models.F("rater"))`
        # — which Django rejects at import with `models.E041: 'constraints' refers to the
        # joined field`. A CheckConstraint is compiled to SQL against THIS table, and
        # `answer__player` is a JOIN through Answer. There is no way to write it.
        #
        # So the rule is enforced where it can be: the view refuses it, because the view
        # derives the target from the reveal and the client never names it, which makes that
        # check unforgeable; and this guard is the backstop for a path that does not go
        # through the view — a future management command, a shell, a data fix.
        if self.answer_id and self.rater_id and self.answer.player_id == self.rater_id:
            raise ValidationError("a player cannot rate their own prompt")
        super().save(*args, **kwargs)
```

Le test de modèle correspondant attend donc `ValidationError`, pas `IntegrityError` — et la
preuve que la règle tient vient du test de vue de la tâche 6
(`test_a_player_cannot_rate_their_own_prompt`, 409), pas d'une contrainte que la base ne
peut pas exprimer.

`Player.quiz_score()` renvoie `self.quiz_attempts.filter(correct=True).count()`.

Le modèle `Round` gagne le curseur de révélation. Il est **par tour** et pas par partie :
une partie a dix tours, et le prompt 7 du tour 3 n'a rien à voir avec le prompt 2 du
tour 4. Le remettre à zéro à chaque ouverture de la phase de notation est ce qui empêche
une partie relancée d'ouvrir sur le prompt 7 d'un tour précédent.

```python
class Round(models.Model):
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="rounds")
    order = models.IntegerField()
    target = models.CharField(max_length=1000)
    # Which of this round's prompts is on screen. An index into the round's answers ordered
    # by id, NOT a player id: the reveal order is the order the answers landed, which is
    # join order, and a rating is a rate against a specific revealed line.
    revealed = models.IntegerField(default=0)

    class Meta:
        unique_together = [("room", "order")]
```

`Room` gagne l'accès au tour courant, utilisé par `views.py` et par les transitions de
phase. Il centralise le `filter(order=…).first()` qui était écrit à la main dans
`room_state` et le serait dans chaque vue : une seule définition du « tour courant », donc
une seule chose à casser.

```python
def current_round(self):
    """The round being played, or None before the game starts. None is a real answer: in
    `waiting` there are no rounds yet, and every caller has to survive that."""
    return self.rounds.filter(order=self.current).first()
```

Puis `cd backend && uv run python manage.py makemigrations game` et relire la migration
générée : 6 modèles modifiés/ajoutés, aucune suppression de donnée.

- [ ] **Étape 4 : vérifier que le test passe**

`cd backend && uv run python manage.py test game.tests.test_models`
Attendu : `OK`. La suite complète échoue encore sur `test_admin` et `test_play`, qui
attendent l'ancienne machine à états — c'est la tâche 3 et la tâche 5, pas un défaut ici.

- [ ] **Étape 5 : committer**

```bash
git add backend/game/models.py backend/game/migrations/
git commit -m "feat(models): phases, quiz attempts, cross-ratings"
```

---

## Tâche 3 : la machine à phases côté admin

- [ ] **Étape 1 : écrire les tests qui échouent**

Dans `backend/game/tests/test_admin.py`, remplacer les assertions sur `status` :

```python
def test_start_with_tutorial_opens_the_quiz(self):
    room = self._room()
    self._post(f"/api/admin/rooms/{room.code}/start")
    self.assertEqual(room.phase, Room.Phase.QUIZ)
    self.assertEqual(room.rounds.count(), ROUNDS_PER_GAME)

def test_start_without_tutorial_goes_straight_to_playing(self):
    room = self._room(tutorial=False)
    self._post(f"/api/admin/rooms/{room.code}/start")
    self.assertEqual(room.phase, Room.Phase.PLAYING)

def test_rounds_are_created_at_start_even_with_the_quiz_open(self):
    # The ten targets are drawn once, at start, whoever is looking. Drawing them when the
    # quiz closes would make the quiz wait on a second event for no benefit.
    room = self._room()
    self._post(f"/api/admin/rooms/{room.code}/start")
    self.assertEqual(room.rounds.count(), ROUNDS_PER_GAME)

def test_next_from_quiz_starts_the_game(self):
    room = self._started(tutorial=True)
    self._post(f"/api/admin/rooms/{room.code}/next")
    self.assertEqual(room.phase, Room.Phase.PLAYING)

def test_next_from_playing_opens_the_rating(self):
    room = self._started(tutorial=False)
    self._post(f"/api/admin/rooms/{room.code}/next")
    self.assertEqual(room.phase, Room.Phase.RATING)

def test_next_from_rating_reveals_the_next_prompt(self):
    room = self._in_phase(Room.Phase.RATING)
    self._post(f"/api/admin/rooms/{room.code}/next")
    self.assertEqual(room.rounds.get(order=0).revealed, 1)

def test_next_from_rating_on_the_last_prompt_opens_the_summary(self):
    room = self._in_phase(Room.Phase.RATING, answers=3)
    self._post(f"/api/admin/rooms/{room.code}/next")
    self.assertEqual(room.phase, Room.Phase.SUMMARY)

def test_next_from_summary_starts_the_next_round(self):
    room = self._in_phase(Room.Phase.SUMMARY)
    self._post(f"/api/admin/rooms/{room.code}/next")
    self.assertEqual(room.phase, Room.Phase.PLAYING)
    self.assertEqual(room.current, 1)

def test_next_from_waiting_is_refused(self):
    room = self._room()
    resp = self._post(f"/api/admin/rooms/{room.code}/next")
    self.assertEqual(resp.status_code, 409)

def test_twelve_rounds_walk_to_done(self):
    # A round is now four admin actions, not one. Ten rounds must still end at `done`,
    # and the eleventh advance must be a 409 rather than an eleventh round.
    room = self._started(tutorial=False)
    for _ in range(40):
        if room.phase == Room.Phase.DONE:
            break
        self._post(f"/api/admin/rooms/{room.code}/next")
        room.refresh_from_db()
    self.assertEqual(room.phase, Room.Phase.DONE)
    self.assertEqual(self._post(f"/api/admin/rooms/{room.code}/next").status_code, 409)

def test_restart_keeps_the_tutorial_and_anonymity_flags(self):
    room = self._in_phase(Room.Phase.SUMMARY, tutorial=False, anonymous=False)
    self._post(f"/api/admin/rooms/{room.code}/restart")
    room.refresh_from_db()
    self.assertEqual(room.phase, Room.Phase.WAITING)
    self.assertFalse(room.tutorial)
    self.assertFalse(room.anonymous)

def test_restart_erases_quiz_attempts_and_ratings(self):
    room = self._in_phase(Room.Phase.SUMMARY)
    self._post(f"/api/admin/rooms/{room.code}/restart")
    self.assertEqual(QuizAttempt.objects.count(), 0)
    self.assertEqual(Rating.objects.count(), 0)
```

- [ ] **Étape 2 : vérifier que les tests échouent**

`cd backend && uv run python manage.py test game.tests.test_admin`
Attendu : `AttributeError: 'Room' object has no attribute 'status'`.

- [ ] **Étape 3 : implémenter**

`admin_start` crée les 10 tours puis choisit la phase :

```python
rounds = ROUNDS_PER_GAME
Room.objects.filter(code=code).update(phase=Room.Phase.QUIZ if room.tutorial else Room.Phase.PLAYING)
```

`admin_next` devient le seul « avancer » du jeu, dispatché sur la phase. Une table, pas une
chaîne de `if` : six phases, six Transitions, et c'est la table qui sert de documentation
exhaustive de ce que l'admin peut faire à un instant donné.

```python
# What the admin's single "advance" button means in each phase. A phase absent from this
# table has no advance: 409. Written as data so the six cases are readable at a glance and
# so adding a phase forces a decision here instead of defaulting to something arbitrary.
_PHASE_ADVANCE = {
    Room.Phase.QUIZ: lambda room: room._go(Room.Phase.PLAYING),
    Room.Phase.PLAYING: lambda room: room._open_rating(),
    Room.Phase.RATING: lambda room: room._advance_reveal(),
    Room.Phase.SUMMARY: lambda room: room.next_round(),
}
```

Sur le modèle :

```python
def _go(self, phase: str) -> None:
    self.phase = phase
    self.save(update_fields=["phase"])

def _open_rating(self) -> None:
    """Close the writing phase and reveal the first prompt. `revealed` is per round, and
    reset by `next_round`, so a restarted game never opens on prompt 7 of 3."""
    rnd = self.current_round()
    rnd.revealed = 0
    rnd.save(update_fields=["revealed"])
    self._go(self.Phase.RATING)

def _advance_reveal(self) -> None:
    """Next prompt, or the summary once the last one is behind us."""
    rnd = self.current_round()
    if rnd.revealed + 1 < rnd.answers.count():
        rnd.revealed += 1
        rnd.save(update_fields=["revealed"])
    else:
        self._go(self.Phase.SUMMARY)
```

`admin_restart` efface `rounds` (ce qui cascade sur `Answer`, puis sur `Rating` via
`Answer`, et sur `QuizAttempt` via `QuizQuestion`) et passe `phase` à `waiting`, en conservant
`tutorial`, `anonymous`, les joueurs et leurs jetons.

- [ ] **Étape 4 : vérifier que les tests passent**

`cd backend && uv run python manage.py test game.tests.test_admin`
Attendu : `OK`.

- [ ] **Étape 5 : committer**

```bash
git add backend/game/views.py backend/game/models.py backend/game/tests/test_admin.py
git commit -m "feat(admin): single advance dispatched per phase"
```

---

## Tâche 4 : le LLM critique de prompt

- [ ] **Étape 1 : écrire le test qui échoue**

Dans `backend/game/tests/test_llm.py`, ajouter :

```python
class PromptCriticTests(SimpleTestCase):
    def test_no_key_stub_rates_a_structured_prompt_above_a_bare_one(self):
        good = llm.grade_prompt("Tu es un critique. Public : debutants. Contexte : TP. Rends 3 puces.")
        bad = llm.grade_prompt("ecris un truc sur la recette")
        self.assertGreater(good.score, bad.score)

    def test_stub_comment_says_it_is_local(self):
        self.assertIn("Local", llm.grade_prompt("n'importe quoi").comment)

    def test_no_key_never_builds_a_client(self):
        with mock.patch.object(llm, "_chat") as chat:
            llm.grade_prompt("n'importe quoi")
            chat.assert_not_called()

    def test_keyed_path_parses_the_verdict(self):
        with mock.patch("game.llm._has_key", return_value=True), mock.patch.object(llm, "_chat", return_value='{"score": 6, "comment": "correct"}'):
            self.assertEqual(llm.grade_prompt("p").score, 6)

    def test_keyed_path_clamps_out_of_range(self):
        for raw, expected in (('{"score": 12}', 10), ('{"score": -3}', 0), ('{"score": null}', 0)):
            with self.subTest(raw=raw):
                with mock.patch("game.llm._has_key", return_value=True), mock.patch.object(llm, "_chat", return_value=raw):
                    self.assertEqual(llm.grade_prompt("p").score, expected)

    def test_critic_uses_its_own_token_cap(self):
        with mock.patch("game.llm._has_key", return_value=True), mock.patch.object(llm, "_chat", return_value='{"score": 1}') as chat:
            llm.grade_prompt("p")
            self.assertEqual(chat.call_args.kwargs["max_tokens"], llm.CRITIC_MAX_TOKENS)

    def test_critic_never_sees_a_peer_rating(self):
        # The whole guardrail rests on the critic judging the prompt alone. If peer scores
        # ever reached this call, a group rating low would pull the arbiter down with it.
        with mock.patch("game.llm._has_key", return_value=True), mock.patch.object(llm, "_chat", return_value='{"score": 1}') as chat:
            llm.grade_prompt("mon prompt", target="une recette")
        self.assertNotIn("pairs", json.dumps(chat.call_args.kwargs["messages"]))
```

- [ ] **Étape 2 : vérifier que les tests échouent**

`cd backend && uv run python manage.py test game.tests.test_llm`
Attendu : `AttributeError: module 'game.llm' has no attribute 'grade_prompt'`.

- [ ] **Étape 3 : implémenter**

Dans `llm.py`, ajouter `CRITIC_MODEL`, `CRITIC_MAX_TOKENS = 200` (identique à
`JUDGE_MAX_TOKENS` : les deux renvoient un objet JSON d'un entier et d'une phrase, donc la
borne est la même propriété, pas une coïncidence), `CRITIC_SYS`, `_stub_grade` et :

```python
def grade_prompt(prompt: str, target: str = "") -> Verdict:
    if not _has_key():
        return _stub_grade(prompt)
    payload = json.dumps({"prompt": prompt, "cible": target}, ensure_ascii=False)
    raw = _chat(CRITIC_MODEL, [{"role": "system", "content": CRITIC_SYS},
                               {"role": "user", "content": payload}],
                0.0, True, CRITIC_MAX_TOKENS)
    return _verdict(raw, STUB_COMMENT)
```

Extraire `_verdict(raw, default_comment)` et faire appeler `score()` par elle : la logique
de parsing (clamp 0-10, `"7"` lu, `null` → 0) est identique et ne doit pas exister en deux
copies. `CRITIC_SYS` doit noter la **méthode** et non le résultat : rôle et audience
énoncés, contexte fourni, format de sortie demandé, longueur ni sur-spécifiée ni
implicite, pas d'exemple contradicted. Le mot « proximity » et la cible ne doivent pas
apparaître dans la grille : le critique ne compare pas la réponse, il juge le prompt.

- [ ] **Étape 4 : vérifier que le test passe**

`cd backend && uv run python manage.py test game.tests.test_llm`
Attendu : `OK`.

- [ ] **Étape 5 : committer**

```bash
git add backend/game/llm.py backend/game/tests/test_llm.py
git commit -m "feat(llm): prompt critic as the peer-rating arbiter"
```

---

## Tâche 5 : le QCM jouable et bloquant

- [ ] **Étape 1 : écrire les tests qui échouent**

Dans `backend/game/tests/test_quiz.py` ajouter une classe `QuizPlayTests(TestCase)` :

```python
def test_join_is_refused_once_the_quiz_is_open(self):
    room = self._room(tutorial=True)
    self._start(room)
    resp = self._post(f"/api/rooms/{room.code}/join", {"name": "Ana"})
    self.assertEqual(resp.status_code, 409)

def test_quiz_returns_questions_in_order_without_the_answer(self):
    room, player = self._joined(tutorial=True)
    payload = self._get(f"/api/rooms/{room.code}/quiz", token=player.token).json()
    self.assertEqual(len(payload["questions"]), QUIZ_LENGTH)
    for q in payload["questions"]:
        self.assertNotIn("correct", q)
        self.assertEqual(len(q["choices"]), 4)

def test_answers_the_quiz_and_scores_it(self):
    room, player = self._joined(tutorial=True)
    resp = self._post(f"/api/rooms/{room.code}/quiz", {"token": player.token, "question": 0, "choice": 1})
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.json()["correct"], QuizQuestion.objects.get(order=0).correct)
    self.assertIn("explanation", resp.json())

def test_answers_a_question_only_once(self):
    room, player = self._joined(tutorial=True)
    self._post(f"/api/rooms/{room.code}/quiz", {"token": player.token, "question": 0, "choice": 0})
    resp = self._post(f"/api/rooms/{room.code}/quiz", {"token": player.token, "question": 0, "choice": 1})
    self.assertEqual(resp.status_code, 409)

def test_out_of_range_choice_is_refused(self):
    room, player = self._joined(tutorial=True)
    for choice in (-1, 4):
        with self.subTest(choice=choice):
            resp = self._post(f"/api/rooms/{room.code}/quiz", {"token": player.token, "question": 0, "choice": choice})
            self.assertEqual(resp.status_code, 400)

def test_bad_token_is_refused(self):
    room, _ = self._joined(tutorial=True)
    self.assertEqual(self._post(f"/api/rooms/{room.code}/quiz", {"token": "nope", "question": 0, "choice": 0}).status_code, 403)

def test_playing_is_refused_while_the_quiz_is_open(self):
    # The gate. This is the whole point of the didacticiel: no prompt is written before the
    # eight questions are done.
    room, player = self._joined(tutorial=True)
    for i in range(QUIZ_LENGTH):
        self._post(f"/api/rooms/{room.code}/quiz", {"token": player.token, "question": i, "choice": 0})
    self.assertEqual(room.phase, Room.Phase.PLAYING)  # auto-opened
    room.refresh_from_db()
    self.assertEqual(room.phase, Room.Phase.PLAYING)

def test_one_player_stuck_in_the_quiz_does_not_hold_the_room(self):
    # Presence, not rows: a phone that slept is still a row forever.
    room, fast, slow = self._joined(tutorial=True, two=True)
    self._heartbeat(fast)
    for i in range(QUIZ_LENGTH):
        self._post(f"/api/rooms/{room.code}/quiz", {"token": fast.token, "question": i, "choice": 0})
    room.refresh_from_db()
    self.assertEqual(room.phase, Room.Phase.PLAYING)

def test_quiz_score_does_not_reach_the_final_ranking(self):
    room, player = self._joined(tutorial=True)
    for i in range(QUIZ_LENGTH):
        self._post(f"/api/rooms/{room.code}/quiz", {"token": player.token, "question": i, "choice": 0})
    state = room_state(room.code)
    self.assertEqual(state["players"][0]["score"], 0)
```

- [ ] **Étape 2 : vérifier que les tests échouent**

`cd backend && uv run python manage.py test game.tests.test_quiz`
Attendu : `404` sur `/api/rooms/<code>/quiz`.

- [ ] **Étape 3 : implémenter**

`admin_create_room` matérialise la banque, dans l'ordre donné par `quiz.pick()` — et
`pick` vient de `quiz.py`, pas de `targets.pick` : voir la note de la tâche 2, les deux
banques n'ont pas les mêmes propriétés.

```python
QuizQuestion.objects.bulk_create([
    QuizQuestion(order=i, prompt=q.prompt, choices=list(q.choices), correct=q.correct, explanation=q.explanation)
    for i, q in enumerate(quiz.pick())
])
```

L'ordre est tiré **une fois par partie** : c'est ce qui rend le QCM comparable entre joueurs.

L'endpoint :

```python
@require_POST
def quiz(request: HttpRequest, code: str) -> JsonResponse:
    room = get_object_or_404(Room, code=code)
    if room.phase != Room.Phase.QUIZ:
        return json_error("quiz not open", 409)
    ...
    # `correct_index` is not the player's own correctness echoed back — it is the KEY's
    # position, and the client never holds the key. Without it the reveal can only say
    # "wrong" and cannot point at the good choice, which is the half of a Kahoot that teaches.
    # The two fields differ on exactly one case, a wrong answer, which is the case the test
    # below has to pin.
    return JsonResponse({
        "correct": correct,
        "correct_index": q.correct,
        "explanation": q.explanation,
        "score": player.quiz_score(),
    })
```

L'endpoint `GET` renvoie les questions **sans** `correct` ni `explanation` : le QCM doit
être un QCM, pas un formulaire que le lecteur d'écran pode. `room_state` ajoute :

```python
"quiz": {
    "total": QuizQuestion.objects.count(),
    "done": p.quiz_attempts.count(),
    "score": p.quiz_score(),
} if room.phase == Room.Phase.QUIZ else None,
```

- [ ] **Étape 4 : vérifier que le test passe**

`cd backend && uv run python manage.py test game.tests.test_quiz`
Attendu : `OK`.

- [ ] **Étape 5 : committer**

```bash
git add backend/game/views.py backend/game/tests/test_quiz.py
git commit -m "feat(quiz): playable QCM that gates the game"
```

---

## Tâche 6 : la notation croisée et sa borne

- [ ] **Étape 1 : écrire les tests qui échouent**

`backend/game/tests/test_rating.py`, nouveau :

```python
class PeerBoundTests(SimpleTestCase):
    def test_peers_cannot_push_more_than_three_points_above_the_llm(self):
        self.assertEqual(quality(llm=4, peers=[10, 10, 10]), 7)

    def test_peers_cannot_push_more_than_three_points_below(self):
        self.assertEqual(quality(llm=8, peers=[1, 1, 1]), 5)

    def test_a_sane_group_moves_the_score_within_the_band(self):
        self.assertEqual(quality(llm=5, peers=[7, 7, 8]), 7)

    def test_a_unanimous_group_inside_the_band_is_heard(self):
        # A group that judges correctly and disagrees with the LLM by two points is not
        # silenced: the bound is a rail, not a veto.
        self.assertEqual(quality(llm=5, peers=[7, 7, 7]), 7)

    def test_no_peers_falls_back_to_the_llm(self):
        self.assertEqual(quality(llm=6, peers=[]), 6)

    def test_no_llm_falls_back_to_the_peers(self):
        # Offline, the critic is a stub. The stub must not silently become the score.
        self.assertEqual(quality(llm=None, peers=[8, 8]), 8)
```

`from game.prompts import QUALITY_BAND, quality` — la fonction pure vit dans `prompts.py`,
hors de `views.py`, pour être testable sans base et sans HTTP.

Puis les tests de vue :

```python
def test_prompts_are_revealed_one_at_a_time(self):
    room, _ = self._rating_room(answers=3)
    state = room_state(room.code)
    self.assertEqual(state["phase"], "rating")
    self.assertEqual(state["reveal"]["index"], 0)
    self.assertEqual(state["reveal"]["count"], 3)

def test_rating_a_prompt_advances_the_reveal_for_everyone(self):
    room, players = self._rating_room(answers=3)
    for p in players[1:]:
        self._rate(room, players[0], p, 7)
    room.refresh_from_db()
    self.assertEqual(room.rounds.get(order=0).revealed, 1)

def test_a_player_cannot_rate_their_own_prompt(self):
    room, players = self._rating_room(answers=2)
    resp = self._rate(room, players[0], players[0], 7)
    self.assertEqual(resp.status_code, 409)

def test_rating_the_same_prompt_twice_is_refused(self):
    room, players = self._rating_room(answers=2)
    self._rate(room, players[0], players[1], 7)
    self.assertEqual(self._rate(room, players[0], players[1], 3).status_code, 409)

def test_rating_out_of_range_is_refused(self):
    room, players = self._rating_room(answers=2)
    for score in (0, 11):
        with self.subTest(score=score):
            self.assertEqual(self._rate(room, players[0], players[1], score).status_code, 400)

def test_anonymous_rating_hides_the_author(self):
    room, players = self._rating_room(answers=2, anonymous=True)
    state = room_state(room.code)
    self.assertIsNone(state["reveal"]["author"])
    self.assertEqual(state["reveal"]["author_label"], f"Joueur {2}")

def test_named_rating_shows_the_author(self):
    room, players = self._rating_room(answers=2, anonymous=False)
    self.assertEqual(room_state(room.code)["reveal"]["author"], players[0].name)

def test_anonymity_hides_every_prompt_and_score_during_rating(self):
    # The leak the spec names: if the payload still carries each player's prompt and score,
    # a player reads their own score, sees it missing from the column, and knows which
    # revealed line is theirs. Both fields go.
    room, players = self._rating_room(answers=3, anonymous=True)
    for p in room_state(room.code)["players"]:
        with self.subTest(player=p["name"]):
            self.assertIsNone(p["prompt"])
            self.assertIsNone(p["score"])

def test_names_survive_the_rating_phase(self):
    # The roster is not a leak: knowing who is in the room says nothing about who wrote
    # which prompt. Only the scores did that.
    room, _ = self._rating_room(answers=2, anonymous=True)
    self.assertEqual(len(room_state(room.code)["players"]), 2)

def test_the_summary_lifts_anonymity_and_shows_the_breakdown(self):
    room, players = self._rating_room(answers=2, anonymous=True)
    self._post(f"/api/admin/rooms/{room.code}/next")  # last prompt -> summary
    state = room_state(room.code)
    self.assertEqual(state["phase"], "summary")
    self.assertEqual(state["summary"]["rows"][0]["author"], players[0].name)
    for key in ("proximity", "quality_llm", "quality_peers", "quality", "round_score"):
        self.assertIn(key, state["summary"]["rows"][0])

def test_round_score_is_the_mean_of_its_two_dimensions(self):
    room, players = self._rating_room(answers=2, proximity=8, quality_llm=6, peer_score=8)
    self._post(f"/api/admin/rooms/{room.code}/next")
    row = room_state(room.code)["summary"]["rows"][0]
    self.assertEqual(row["round_score"], (row["proximity"] + row["quality"]) // 2)
```

- [ ] **Étape 2 : vérifier que les tests échouent**

`cd backend && uv run python manage.py test game.tests.test_rating`
Attendu : `ModuleNotFoundError: No module named 'game.prompts'`.

- [ ] **Étape 3 : implémenter**

`prompts.py` :

```python
"""The cross-rating scale, in one place. Pure functions, no Django, no LLM."""

QUALITY_BAND = 3


def quality(llm: int | None, peers: list[int]) -> int:
    """The peer average, held inside ±QUALITY_BAND of the critic's score.

    The LLM arbitrates, the peers adjust. A group that hates a prompt cannot keep it at 0,
    and a group that loves one cannot float it to 10 — the band is a rail, not a veto: a
    group within the band is heard exactly.
    """
    if not peers:
        return llm if llm is not None else 0
    mean = sum(peers) / len(peers)
    if llm is None:
        return round(mean)
    return round(max(llm - QUALITY_BAND, min(mean, llm + QUALITY_BAND)))
```

L'endpoint de notation. Le jeton désigne le noteur, la phase désigne l'answer révélé : le
client n'envoie **que sa note**, il ne peut pas nommer ce qu'il note.

```python
@require_POST
def rate(request: HttpRequest, code: str) -> JsonResponse:
    room = get_object_or_404(Room, code=code)
    if room.phase != Room.Phase.RATING:
        return json_error("not rating", 409)
    player = ...  # même résolution que `answer`
    rnd = room.current_round()
    target = rnd.answers.order_by("id")[rnd.revealed]
    if target.player_id == player.pk:
        return json_error("your own prompt", 409)
    score = body(request).get("score")
    if not isinstance(score, int) or isinstance(score, bool) or not 1 <= score <= 10:
        return json_error("score 1-10", 400)
    if Rating.objects.filter(answer=target, rater=player).exists():
        return json_error("already rated", 409)
```

Chaque refus porte un message distinct : `already rated` est le seul 409 propre à la
notation, et sans lui il tomberait sur la chaîne générique de `describe()` côté client.

### Le règlement du tour est ÉCRIT, JAMAIS RECALCULÉ

`Answer` gagne quatre colonnes nullables, remplies une fois pour toutes quand la notation du
tour se termine :

```python
class Answer(models.Model):
    # ... les champs existants inchangés ...
    quality_llm = models.IntegerField(null=True, blank=True)   # llm.grade_prompt sur le prompt seul
    quality_peers = models.IntegerField(null=True, blank=True)  # moyenne des pairs, avant bornage
    quality = models.IntegerField(null=True, blank=True)        # après bornage ±3
    round_score = models.IntegerField(null=True, blank=True)   # (proximité + qualité) // 2
```

C'est le point le plus lourd du plan et il n'est pas un choix de goût. Dans une salle de 20
joueurs sur 10 tours, l'instantané de la salle doit additionner 200 réponses et 3 800 notes
pour refaire le calcul à chaque tick — soit ~4 000 lectures par instantané, et chaque
instantané est recalculé deux fois par seconde pour chacun des 21 flux (20 téléphones plus
l'admin). C'est ~168 000 lectures par seconde sur un `runserver` threadé, dans un navigateur
sur un téléphone, sur le réseau local d'une classe. Ce n'est pas lent, c'est impossible.

Donc `settle_round(room)` est appelée UNE fois, au moment où la notation du tour se referme,
et elle écrit les quatre colonnes :

```python
def settle_round(room: Room) -> None:
    """Write the round's scores once, when its rating closes. Never recomputed.

    One write per answer, then every later snapshot is a SUM over a nullable integer column —
    the same shape the ranking already has. Recomputing instead would be correct and would
    melt the server, because `room_state` is evaluated twice a second per connected stream and
    the peers' mean needs every rating of the round.
    """
```

Trois conséquences, chacune testée :
- Le classement final est **stable**. Un joueur qui godote ne voit pas son score bouger, et une
  note de pair ajoutée à la dixième seconde ne réécrit pas les neuf tours précédents.
- Un joueur qui n'a pas répondu n'a pas de ligne, donc pas de tour compté : il garde les points
  des tours où il a joué, et rien de plus.
- `round_score` est `null` tant que le tour n'est pas réglé. Un `SUM` l'ignore, ce qui est le
  comportement voulu : pendant la rédaction du tour N, le total n'inclut pas encore le tour N.
```

`maybe_advance` devient `maybe_advance_phase(room)`, appelé après **chaque** answer, quiz
answer et rating. En phase `rating`, il avance le reveal quand tous les joueurs connectés
ont noté le prompt courant, et passe en `summary` au dernier. En `summary`, il ne bouge
rien : c'est l'admin qui décide, parce qu'un tour se commente.

`room_state` porte le reveal. Le numéro de joueur est stable par partie etwartz par
l'ordre d'arrivée, pas par un tirage : un même prompt ne doit pas être attribué à deux
personnes différentes selon la page consultée.

- [ ] **Étape 4 : vérifier que le test passe**

`cd backend && uv run python manage.py test game.tests.test_rating`
Attendu : `OK`.

- [ ] **Étape 5 : committer**

```bash
git add backend/game/prompts.py backend/game/views.py backend/game/tests/test_rating.py
git commit -m "feat(rating): cross-rating bounded by the critic"
```

---

## Tâche 7 : `room_state` et la suite existante

- [ ] **Étape 1 : mettre les tests existants à jour**

`test_play.py` : les trois égalités de dictionnaires doivent inclure les nouvelles clés. La
`phase` remplace `status` partout, et `score` devient `None` en phase `rating`. Écrire
d'abord l'assertion qui doit échouer, puis corriger.

- [ ] **Étape 2 : vérifier que les tests échouent**

`cd backend && uv run python manage.py test game`
Attendu : des échecs sur `test_play`, `test_presence`, `test_admin` — tous sur `status`
ou sur des clés manquantes.

- [ ] **Étape 3 : corriger `room_state` et les tests**

Le contrat final, que le frontend consomme tel quel :

```python
{
  "code": str, "phase": "waiting|quiz|playing|rating|summary|done",
  "current": int, "total": int, "target": str | None,
  "anonymous": bool,
  "players": [{"name", "score": int|None, "round_score": int|None, "answered",
               "online", "quiz_score": int|None, "quiz_done": bool,
               "proximity_total": int|None, "quality_total": int|None}],
  "online_count": int,
  "quiz": {"total", "done", "score"} | None,
  "reveal": {"index", "count", "prompt", "author", "author_label", "rated"} | None,
  "summary": {"round", "rows": [{"author", "prompt", "output", "proximity",
                                 "quality_llm", "quality_peers", "quality",
                                 "round_score"}]} | None,
}
```

`score` est `None` en phase `rating` et sinon le total des tours **résolus**. Le classement
final additionne les `round_score` de la phase `summary` de chaque tour : jamais le score
du QCM, jamais la proximité seule.

**Les deux totaux de dimension sont dans le payload, pas recalculés par le client.**
`proximity_total` et `quality_total` sont des `SUM` sur les colonnes écrites par
`settle_round`. Ils sont là pour une raison pédagogique, pas décorative : un élève qui
termine sur « proximité 42, qualité 58, total 50 » comprend qu'il écrit des prompts qui
marchent et qui sont mal construits — c'est exactement la leçon. Avec un total unique, cette
distinction disparaît derrière un chiffre, et le classement devient une note sur 100 comme
une note sur 10. L'écran final a ses quatre colonnes câblées et n'attend que ces deux
nombres ; le backend n'a plus qu'à les fournir.

**`quiz`, `reveal` et `summary` sont des clés TOUJOURS PRÉSENTES**, `None` hors de leur phase.
L'agent de la tâche 5 a livré un bloc `quiz` conditionnel, en documentant le choix : émettre
la clé partout change le JEU DE CLÉS de chaque instantané et casse les deux assertions de
dictionnaire entier de `test_play.py`. C'était le bon arbitrage à ce moment-là, avec une suite
verte comme critère. Cette tâche le paie : `quiz` devient inconditionnel, les deux assertions
sont mises à jour, et le test `test_the_quiz_block_is_absent_outside_the_quiz_phase` est
retourné en son contraire. Un client qui null-check une forme est plus robuste qu'un client
qui teste l'existence d'une clé.

**Le tri doit survivre à `score = None`.** Aujourd'hui `views.py:212` trie par
`key=lambda t: (-t[0], t[1])` où `t[0]` est le total. En phase `rating` tous les scores
valent `None`, et `-None` lève un `TypeError` : l'instantané ne se construit plus, le flux
SSE meurt, et la notation ne démarre jamais. La clé de tri doit donc retomber sur l'ordre
d'arrivée quand le score est absent :

```python
# The leading key must survive a None score, and it does not for free: `-None` is a
# TypeError, which would kill the whole snapshot the moment the rating phase blanks every
# score. Falling back to 0 keeps the sort total; the join-order tiebreak below is what
# actually orders the board, and it was already the tiebreak for equal scores.
players.sort(key=lambda t: (-(t[0] or 0), t[1]))
```

Un test le verrouille, sinon la régression ne se voit qu'en séance :

```python
def test_rating_phase_with_every_score_blanked_still_sorts(self):
    room, _ = self._rating_room(answers=3, anonymous=True)
    self.assertEqual(len(room_state(room.code)["players"]), 3)
```

**Sémantique de `reveal.rated` :** le nombre de joueurs ayant noté le prompt **courant**,
c'est-à-dire le numérateur du « 3 sur 9 » affiché au joueur. Le dénominateur est
`reveal.count`, le nombre de prompts du tour. Ce n'est donc pas le nombre de notes déjà
données sur le tour entier, et l'interface ne doit pas le compter deux fois.

**Portée des blocs :** `quiz` est non-`None` uniquement en phase `quiz`, `reveal`
uniquement en phase `rating`, `summary` uniquement en phase `summary`. Les trois sont
`None` ailleurs, y compris en `done` — le client null-check donc, ce qui est la lecture
sûre et cohérente avec le fait qu'il n'y a rien à afficher.

`quiz_score` vaut `0` tant que le joueur n'a rien répondu, et le nombre de bonnes réponses
ensuite : c'est un entier, jamais `None`. Le type `number | null` côté TypeScript est plus
large que nécessaire, ce qui est le bon sens pour un client.

- [ ] **Étape 4 : vérifier que toute la suite passe**

`cd backend && uv run python manage.py test game`
Attendu : `Ran N tests ... OK`.

- [ ] **Étape 5 : committer**

```bash
git add backend/game/views.py backend/game/tests/
git commit -m "feat(state): expose phase, reveal and summary in the snapshot"
```

---

## Tâche 8 : le contrat TypeScript

- [ ] **Étape 1 : réécrire `api.ts`**

Le fichier est le contrat entre le backend et le frontend : il doit correspondre à la
tâche 7, clé pour clé.

```typescript
export type Phase = 'waiting' | 'quiz' | 'playing' | 'rating' | 'summary' | 'done'

export interface QuizState { total: number; done: number; score: number }
export interface RevealState {
  index: number; count: number; prompt: string
  author: string | null; author_label: string; rated: number
}
export interface SummaryRow {
  author: string; prompt: string; output: string
  proximity: number; quality_llm: number; quality_peers: number
  quality: number; round_score: number
}
export interface Player {
  name: string
  score: number | null
  round_score: number | null
  answered: boolean
  online: boolean
  quiz_score: number | null
  quiz_done: boolean
}
export interface State {
  code: string; phase: Phase; current: number; total: number
  target: string | null; anonymous: boolean
  players: Player[]; online_count: number
  quiz: QuizState | null
  reveal: RevealState | null
  summary: { round: number; rows: SummaryRow[] } | null
}
```

Retirer `prompt`, `output` et `comment` de `Player` : le serveur ne les envoie plus en
phase `rating`, et les garder en type ferait croire à un client qu'ils sont là.

- [ ] **Étape 2 : vérifier que ça compile**

`cd frontend && bun run build`
Attendu : des erreurs `tsc` sur les screens qui lisent `state.status` et `player.prompt`.
Elles sont attendues : la tâche 9 les corrige. Vérifier seulement qu'aucune erreur ne
mentionne `api.ts` lui-même.

---

## Tâche 9 : les écrans joueur

- [ ] **Étape 1 : `Quiz.tsx`**

Un écran, trois états : la question courante, la révélation après réponse, la fin avec le
classement QCM. Un seul `useState` pour la question locale, un `fetch` par question
répondue, jamais de `useEffect` qui refait ce que le rendu peut dériver. Le bouton de
choi est un `min-height: 3rem` plein largeur, et la bonne réponse s'affiche en vert avec
l'explication dessous — la révélation fait partie de la valeur, pas de la mechanics.

- [ ] **Étape 2 : `Rating.tsx`**

Le prompt révélé, la cible en lecture seule au-dessus, l'échelle 1-10, « 3 sur 9 notés ».
`ScoreScale` doit accepter `min`/`max` pour réutiliser le composant existant. Bouton
« Noter » désactivé tant que le joueur n'a pas choisi. Un `aria-live` annonce « Prompt 3
sur 9 ». Aucun classement visible, aucune liste de prompts presented en vrac : l'anonymat tient
parce que l'interface ne montre rien d'autre.

- [ ] **Étape 3 : brancher dans `App.tsx`**

```typescript
if (state.phase === 'quiz') return <Quiz state={state} me={me} />
if (state.phase === 'rating') return <Rating state={state} me={me} />
```

`summary` reste sur `Game`, qui affiche le tableau de synthèse au-dessus de la cible
suivante : c'est le même écran, pas un nouvel onglet.

- [ ] **Étape 4 : `Game.tsx` et `Results.tsx`**

`Game` masque la colonne de classement quand `state.phase === 'rating'` et affiche
`summary.rows` quand `phase === 'summary'`. `Results` passe de deux colonnes à quatre :
joueur, proximité, qualité, total. `ScoreScale` et `Board` prennent des colonnes optionnelles
pour ne pas casser l'existant.

- [ ] **Étape 5 : vérifier**

`cd frontend && bun run build && bun run lint`
Attendu : build propre, lint propre. Tester à la main : `/room/<CODE>` en phase `rating`
sur un téléphone, vérifier qu'aucun nom ni score n'apparaît.

---

## Tâche 10 : l'écran admin

- [ ] **Étape 1 : les cases à la création**

Deux `checkbox` à côté du bouton « Créer une partie » : « avec didacticiel » (coché par
défaut) et « anonyme pendant la notation » (coché par défaut). Elles sont envoyées dans le
POST de création et n'existent plus après : la spec les fige pour la partie.

- [ ] **Étape 2 : le bouton d'avance, un seul**

Un bouton dont le libellé suit la phase : « Lancer la partie », « Ouvrir la notation »,
« Prompt suivant », « Tour suivant ». La phase est déjà dans l'instantané, donc le libellé
n'est pas un état du client à maintenir.

- [ ] **Étape 3 : la relance**

Le bouton de relance propose les deux cibles par une seule action : « Relancer avec
didactique » et « Relancer sans ». Elles passent le drapeau à `restart` sous le nom
`tutorial`, qui remplace la valeur conservée.

- [ ] **Étape 4 : vérifier**

`cd frontend && bun run build && bun run lint`, puis parcours complet à la main : créer
avec didacticiel, jouer le QCM, répondre, noter, résumer, dix tours, classement, relancer
sans didacticiel.

---

## Auto-revue

- [ ] Chaque exigence du cahier des charges a une tâche : didacticiel (5), blocage
  (5), notation croisée (6), anonymat (6), garde-fou ±3 (4, 6), note de tour (6), classement
  final (7), relance sans (3, 10), contenus en données (1, 2).
- [ ] Aucun marqueur de place : pas de `TBD`, pas de « gérer les cas limites », pas de
  « similar à la tâche N ».
- [ ] Cohérence des noms entre tâches : `phase` partout, `revealed` sur `Round`, `reveal`
  dans le snapshot, `quality(llm, peers)` dans `prompts.py`, `grade_prompt` dans `llm.py`.
- [ ] La suite backend passe : `cd backend && uv run python manage.py test game`.
- [ ] Le build frontend passe : `cd frontend && bun run build`.
- [ ] Le README décrit le flux réel : didacticiel, notation, anonymat, relance.
