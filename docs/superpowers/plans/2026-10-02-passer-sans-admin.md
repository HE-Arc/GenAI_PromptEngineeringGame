# Jeu autonome — la classe fait avancer, l'admin n'est plus le pilote

> **Pour les agents exécuteurs :** sous-skill REQUIS : utiliser
> `superpowers:subagent-driven-development` (recommandé) ou `superpowers:executing-plans`
> pour exécuter ce plan tâche par tâche. Les étapes utilisent des cases à cocher (`- [ ]`).

**Objectif :** rendre la partie jouable sans que l'admin appuie sur « suivant ». Chaque passage
à l'étape suivante est déclenché par un bouton « prêt » que les joueurs pressent eux-mêmes, et
chaque moment de discussion (après chaque question du didacticiel, après chaque prompt noté,
après le résumé de tour) devient une étape à part entière au lieu d'être un temps d'attente
muet.

**Architecture :** le backend gagne un modèle `Ready` (un vote par joueur et par étape) et une
fonction `maybe_advance_by_votes` qui fait la même chose que `maybe_advance_phase` mais sur un
seuil de **majorité stricte** au lieu de l'unanimité. Le seuil est `len(connectés) // 2 + 1`,
calculé sur les joueurs **connectés** comme le reste du fichier. Le frontend gagne un bloc
`ready` dans l'instantané et un composant `ReadyBar` réutilisé par les trois écrans qui
discutent. Le bouton admin reste, comme filet.

**Pile technique :** Django 5 (`backend/`, SQLite, `runserver` threadé), React 19 + Vite 8 +
TypeScript 6 (`frontend/`, CSS global en 3 couches, aucun router, aucun test runner).

**Décision de conception qui contredit la spec :** le cahier des charges dit « L'admin décide du
passage à la question suivante » et un test le verrouille
(`tests/test_quiz.py::test_nothing_opens_the_game_by_itself`). Ce plan **inverse** cette
décision : c'est la demande, et la discussion qu'elle porte n'est pas compatible avec une
avance décidée par une personne. Le test existant est donc remplacé, pas contourné — voir la
tâche 2. Ce qui ne change pas : l'admin crée, l'admin démarre, l'admin peut encore forcer.

## Contraintes globales

- Zéro clé d'API : toute fonction LLM ajoutée doit avoir un repli local et le dire au joueur
  dans son commentaire. Les tests ne doivent jamais ouvrir de connexion réseau.
- Les tests ne mockent jamais `llm.OpenAI` au niveau supérieur. Les nouveaux tests suivent la
  même répartition que les tests existants.
- Le seuil de passage est une **constante nommée** (`VOTE_MAJORITY` dans `prompts.py`), écrite à
  un seul endroit et lue par le backend comme par le test. Ne pas réécrire `// 2 + 1` ailleurs.
- Toute annotation de FR ou tout texte utilisateur est en français, sans accent, comme le reste
  du code (les commentaires sont eux accentués).
- La discussion est **orale** : aucun champ de texte, aucun commentaire écrit. Le README le
  justifie déjà (« sur un téléphone, un champ libre à chaque prompt fait abandonner »). Le
  bouton « prêt » est le seul signal.
- Le bar est toujours l'ensemble des joueurs **connectés** (`online_player_ids`), jamais
  `room.players` : un téléphone qui dort est une ligne en base à jamais.
- `0 == 0` est refusé partout où il ferait avancer une partie sans personne : le seuil est
  calculé sur `online`, donc un seuil de 0 ne doit jamais exister. Si `online` est vide, rien
  n'avance.
- Le classement final reste un total sur 100 ; le score du QCM ne s'y ajoute pas.
- Les scores sont des entiers 0-10. Aucun `float` dans le payload — ** sauf** `quorum`, qui est
  un compte de joueurs et pas une note. Le nommer `needed` plutôt que `quorum` dans le payload
  pour ne pas le confondre avec un score.
- Boutons `min-height: 3rem`, `viewport-fit=cover`, `100dvh`. Toute UI de « prêt » respecte ces
  règles : c'est un jeu sur téléphone, debout.
- Frontend : pas de router, pas de library d'état, pas de CSS modules. Classes BEM plates.
- Frontend : `cd frontend && bun run build` doit passer et `bun run lint` doit rester propre.
- Backend : `cd backend && uv run python manage.py test game` doit passer.

---

## Fichiers

| Fichier | Responsabilité |
|---|---|
| `backend/game/prompts.py` | **Modifié.** `VOTE_MAJORITY = True` et `majority(n) -> int`, la règle de seuil, testable sans base. |
| `backend/game/models.py` | **Modifié.** `Ready` (vote par joueur et par étape), et la remise à zéro des votes dans `_open_quiz` / `_open_rating` / `next_round`. |
| `backend/game/migrations/0008_ready.py` | **Nouveau.** Généré par `makemigrations`, relu à la main. |
| `backend/game/views.py` | **Modifié.** `maybe_advance_by_votes`, le bloc `ready` dans `room_state`, l'endpoint `ready`, et `admin_next` inchangé comme filet. |
| `backend/config/urls.py` | **Modifié.** `api/rooms/<code>/ready`. |
| `backend/game/tests/test_vote.py` | **Nouveau.** La règle de seuil, le cycle de vie des votes, les transitions par vote. |
| `backend/game/tests/test_quiz.py` | **Modifié.** `test_nothing_opens_the_game_by_itself` est remplacé par son contraire. |
| `frontend/src/api.ts` | **Modifié.** `ReadyState`, `State.ready`, l'appel `postReady`. |
| `frontend/src/ui.tsx` | **Modifié.** `ReadyBar` — le composant partagé par les trois écrans. |
| `frontend/src/Quiz.tsx` | **Modifié.** Bouton « prêt » après la révélation. |
| `frontend/src/Rating.tsx` | **Modifié.** Bouton « prêt » après la notation d'un prompt. |
| `frontend/src/Game.tsx` | **Modifié.** Bouton « prêt » au résumé de tour. |
| `frontend/src/index.css` | **Modifié.** Styles du composant `ReadyBar`. |
| `README.md` | **Modifié.** La partie se joue sans admin ; l'admin crée, démarre et force. |

Les patterns existants à copier, pas à réinventer : `_beat` / `online_player_ids` pour la
présence, `_player_for_token` pour l'identité, `body()` pour le JSON tolérant, `json_error` pour
les erreurs, `save(update_fields=[...])` plutôt qu'un `save()` nu. Le commentaire « pourquoi »
en tête de chaque bloc est une convention du dépôt : il explique un arbitrage, pas une ligne de
code. Ne le supprimez pas.

---

## La règle du seuil, en une phrase

`majority(n) = n // 2 + 1`, pour `n` joueurs connectés. À 2 joueurs la majorité est l'unanimité
(2 sur 2) : c'est correct et ce n'est pas un cas particulier à coder. À 1 joueur, le seuil est 1
et la partie avance toute seule — c'est le cas d'un joueur seul, et il doit continuer à
fonctionner. La fonction refuse `n == 0` en renvoyant 0, et l'appelant qui calcule un seuil de 0
ne fait rien : c'est le garde-fou `0 == 0` du reste du fichier.

---

## Machine à états

```
waiting ──start──▶ quiz ──(tous ont répondu OU fenêtre fermée)──▶ ATTENTE discussion
                     ▲                                                    │
                     └──────────────────────(majorité prête)─────────────┘
                                                                          │
                                                                          ▼ 8e réponse
                            done ◀──(10 tours)── summary ◀──(tous ont noté)── playing
                                              │   ▲                          ▲
                                    (majorité prête)                    (tous ont répondu)
                                              │   │                          │
                                              └───┴──────────────────────────┘

  ATTENTE discussion : la phase ne change pas, seul `ready` apparaît dans l'instantané.
  Le bouton admin reste disponible et force le même passage.
```

La phase ne change jamais pendant une attente : c'est ce qui permet au client d'afficher la
discussion sans connaître la phase suivante. Une attente est donc un **drapeau**, pas une phase.

---

## Tâche 1 : la règle de seuil

- [ ] **Étape 1 : écrire le test qui échoue**

`backend/game/tests/test_vote.py`, nouveau fichier :

```python
from django.test import SimpleTestCase

from game.prompts import majority


class MajorityTests(SimpleTestCase):
    def test_the_threshold_is_a_strict_majority(self):
        for n, expected in ((1, 1), (2, 2), (3, 2), (4, 3), (5, 3), (20, 11), (21, 11)):
            with self.subTest(players=n):
                self.assertEqual(majority(n), expected)

    def test_a_room_with_nobody_connected_has_no_threshold(self):
        # 0 // 2 + 1 is 1, which would be a threshold one player could never reach in a room
        # with no players — and the caller that used it would advance a game nobody is in.
        self.assertEqual(majority(0), 0)

    def test_the_threshold_is_never_more_than_the_players(self):
        for n in range(1, 30):
            with self.subTest(players=n):
                self.assertLessEqual(majority(n), n)

    def test_the_threshold_is_always_reachable(self):
        # The property that makes the game autonomous: with every player ready, the room always
        # crosses the line. A threshold of n+1 would deadlock a unanimous class forever.
        for n in range(1, 30):
            with self.subTest(players=n):
                self.assertLessEqual(majority(n), n)
```

- [ ] **Étape 2 : vérifier que le test échoue**

`cd backend && uv run python manage.py test game.tests.test_vote`
Attendu : `ImportError: cannot import name 'majority' from 'game.prompts'`.

- [ ] **Étape 3 : écrire le contenu**

Dans `prompts.py`, qui est déjà le module des règles pures, sans Django et sans LLM :

```python
# A ready vote is a majority of the players who are CONNECTED, and this is the one place the
# rule is written. Not unanimity: a class of twenty with one phone in a bag would otherwise sit
# through nineteen waiting screens, and the teacher would be back to pressing the button — which
# is the thing this rule exists to remove. Not a fixed number either: « 3 votes » is a deadlock
# in a class of two and a stampede in a class of twenty.
VOTE_MAJORITY = True


def majority(n: int) -> int:
    """How many connected players must be ready before the room moves on.

    ZERO FOR AN EMPTY ROOM, and that is the whole reason this is a function rather than an
    arithmetic expression at the call site: `0 // 2 + 1` is 1, so a caller that computed the
    threshold from an empty room would get a bar of one, and comparing "one player is ready"
    against a room with no players is `0 == 0` wearing a hat. The callers already refuse an
    empty online set; this makes the degenerate value unrepresentable too.
    """
    if n <= 0:
        return 0
    return n // 2 + 1
```

- [ ] **Étape 4 : vérifier que le test passe**

`cd backend && uv run python manage.py test game.tests.test_vote`
Attendu : `OK`, 4 tests.

- [ ] **Étape 5 : committer**

```bash
git add backend/game/prompts.py backend/game/tests/test_vote.py
git commit -m "feat(vote): the threshold is one rule, in one place"
```

---

## Tâche 2 : le modèle `Ready` et son cycle de vie

- [ ] **Étape 1 : écrire le test qui échoue**

Dans `backend/game/tests/test_vote.py`, ajouter :

```python
import json
import time

from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

from game.models import Player, QuizQuestion, Ready, Room
from game.prompts import majority
from game.quiz import pick
from game.views import PRESENCE_TTL, _presence, _presence_lock, room_state


class ReadyModelTests(TestCase):
    def test_one_vote_per_player_per_step(self):
        room, player = _one_player_room()
        Ready.objects.create(room=room, player=player, step="quiz:0")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Ready.objects.create(room=room, player=player, step="quiz:0")

    def test_the_same_player_may_vote_on_two_steps(self):
        # The step is in the key, not derived: a player who is ready on question 0 and again on
        # question 1 has two rows, and the question-0 row is garbage the moment the cursor moves.
        room, player = _one_player_room()
        Ready.objects.create(room=room, player=player, step="quiz:0")
        Ready.objects.create(room=room, player=player, step="quiz:1")
        self.assertEqual(Ready.objects.filter(player=player).count(), 2)

    def test_a_vote_on_a_previous_step_is_never_counted_again(self):
        # THE INVARIANT THAT MAKES THE WHOLE THING SAFE, and the reason nothing in this codebase
        # clears votes when the room moves on. `step` is part of the key, so a vote on `quiz:0`
        # cannot match the count for `quiz:1` — there is no window in which a stale vote is
        # visible. Clearing instead would add three call sites and a failure mode (a forgotten
        # clear is a room that opens its next question already holding enough votes to skip it)
        # to solve a problem the key already solved.
        room, player = _one_player_room()
        Ready.objects.create(room=room, player=player, step="quiz:0")
        room._open_quiz(1)
        room.refresh_from_db()
        from game.views import current_step
        self.assertEqual(current_step(room), "quiz:1")
        self.assertEqual(Ready.objects.filter(room=room, step=current_step(room)).count(), 0)
        # The row is still there, and that is deliberate: it is inert, not gone.
        self.assertEqual(Ready.objects.filter(room=room, step="quiz:0").count(), 1)

    def test_a_restart_wipes_the_votes_of_the_previous_game(self):
        # The one place a wipe IS needed: a restart replays `quiz:0`, so this is the single case
        # where a stale step name comes back round and would otherwise count.
        #
        # It lives in TÂCHE 3, not here, and the reason is its shape rather than its subject:
        # it goes through the endpoint, so it needs the room helpers this task does not have yet.
        # In here it would have to poke `Ready` with a `.delete()` written next to the assertion,
        # and a test that deletes the rows itself and then finds them gone has asserted that
        # `delete()` works.
```

avec, en tête de fichier :

```python
def _one_player_room(tutorial: bool = True):
    """A room with one joined player, its didacticiel open on question 0. The fixtures the other
    test files use are private to those files, so this one is rebuilt here — it is four lines and
    copying a shared helper across test modules would couple them to each other's edits."""
    from game.quiz import pick

    room = Room.objects.create(code="RDY1", tutorial=tutorial)
    from game.models import QuizQuestion

    QuizQuestion.objects.bulk_create(
        QuizQuestion(order=i, prompt=q.prompt, choices=list(q.choices), correct=q.correct, explanation=q.explanation)
        for i, q in enumerate(pick())
    )
    player = Player.objects.create(room=room, name="Ana", token="t")
    room._open_quiz(0)
    room.refresh_from_db()
    return room, player
```

- [ ] **Étape 2 : vérifier que le test échoue**

`cd backend && uv run python manage.py test game.tests.test_vote`
Attendu : `ImportError: cannot import name 'Ready' from 'game.models'`.

- [ ] **Étape 3 : implémenter**

Dans `models.py` :

```python
class Ready(models.Model):
    """One player saying « on peut passer » at one step of the game.

    A ROW PER VOTE, not a boolean on the player: the step has to be part of the key, because
    "this player is ready" is meaningless on its own. It is ready FOR question 3, and question 4
    is a different question with a different discussion. A boolean would have to be cleared on
    every transition, and a missed clear is a room that skips a step nobody saw.

    `step` is a string the callers build, not an enum: the steps are pairs of phase state
    (`quiz_index`, `round.order` + `revealed`, `round.order`), and they multiply as the game
    grows. `quiz:3`, `rating:2:0`, `summary:2` say what they mean in a log line.

    NO CASCADE SURPRISES: `room` cascades because deleting a room deletes its votes with it, and
    `player` cascades for the same reason. A player leaving mid-game takes their vote away, which
    is right — they are not there to be counted.
    """

    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="ready_votes")
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="ready_votes")
    step = models.CharField(max_length=40)

    class Meta:
        unique_together = [("room", "step", "player")]
```

Puis latories : rien à ajouter dans `_open_quiz`, `_open_rating` ni `next_round`. **Les votes ne
sont pas effacés quand la salle avance**, et c'est le point : `step` fait partie de la clé, donc
un vote sur `quiz:0` ne peut pas correspondre au compte de `quiz:1`. Effacer ajouterait trois
sites d'appel et un mode de panne — un clear oublié est une salle qui ouvre sa question suivante
avec déjà assez de votes pour la sauter, et cette panne est invisible. Le seul clear nécessaire
est dans `admin_restart`, à côté de celui des `QuizAttempt` : une relance rejoue `quiz:0`, le seul
cas où un nom d'étape revient.

```python
    # In admin_restart, inside the existing transaction, next to the QuizAttempt delete:
    Ready.objects.filter(room=room).delete()
```

Puis `cd backend && uv run python manage.py makemigrations game` et relire la migration : un
modèle ajouté, aucune donnée touchée.

- [ ] **Étape 4 : vérifier que le test passe**

`cd backend && uv run python manage.py test game.tests.test_vote`
Attendu : `OK`, 8 tests. La suite complète est encore verte : cette tâche ne touche aucun
endpoint.

- [ ] **Étape 5 : committer**

```bash
git add backend/game/models.py backend/game/migrations/0008_ready.py backend/game/tests/test_vote.py
git commit -m "feat(vote): a ready vote per player and per step"
```

---

## Tâche 3 : le serveur fait avancer

- [ ] **Étape 1 : écrire les tests qui échouent**

Dans `backend/game/tests/test_vote.py`, ajouter une classe `VoteAdvanceTests(TestCase)`. Elle a
besoin d'une salle à trois joueurs et d'une présence vivante, ce que les helpers de
`test_quiz.py` ne savent pas faire — `_joined` ne connaît que `two=False/True`. Les helpers de
`test_quiz.py` sont privés à ce fichier et il est le plus long du dépôt ; les reproduire ici est
le bon coût, un module de fixtures partagé serait un refactor de ce fichier pour une seule
tâche. **Les helpers sont écrits entiers, ci-dessous** : un plan qui nomme un helper sans le
donner fait échouer son exécutant à la première ligne.

```python
class VoteBase(TestCase):
    """The room a vote happens in: players joined, all online, the didacticiel open.

    COPIED, NOT SHARED, and the reason is worth stating because it will look like duplication
    to whoever reads it next: `test_quiz.py` holds these as private methods, so sharing them
    means either making them public in another test file or extracting a fixtures module. The
    first is a worse coupling than the copy, and the second refactors the longest test file in
    the repo to serve one new one.
    """

    def _post(self, url: str, data: dict | None = None):
        return self.client.post(url, data=json.dumps(data or {}), content_type="application/json")

    def _login(self) -> None:
        """The admin session, which is a COOKIE and not a password in the body.

        `require_admin` reads `request.session["admin"]`, so a test that posts `password` to an
        admin endpoint is refused with a 403 it would then have to explain. This is the two
        lines `test_admin.py` opens its class with, for the same reason.
        """
        self._post("/api/admin/login", {"password": "genai"})

    def _create(self) -> Room:
        return Room.objects.create(code="VOTE")

    def _join(self, room: Room, name: str) -> Player:
        resp = self._post(f"/api/rooms/{room.code}/join", {"name": name})
        return Player.objects.get(pk=resp.json()["id"])

    def _start(self, room: Room) -> None:
        self._login()
        self._post(f"/api/admin/rooms/{room.code}/start")
        room.refresh_from_db()

    def _online(self, room: Room, *players: Player) -> None:
        for p in players:
            with _presence_lock:
                _presence[(room.code, p.pk)] = time.monotonic()

    def _room_with(self, players: int) -> tuple[Room, list[Player]]:
        """A started room, its didacticiel open on question 0, `players` joined and online."""
        room = self._create()
        joined = [self._join(room, f"p{i}") for i in range(players)]
        self._start(room)
        self._online(room, *joined)
        room.refresh_from_db()
        return room, joined

    def _age_out(self, player: Player) -> None:
        """Push a player's last beat into the past, as a sleeping phone does, without sleeping."""
        with _presence_lock:
            _presence[(player.room_id, player.pk)] = time.monotonic() - PRESENCE_TTL - 1

    def _drop_everyone(self, room: Room) -> None:
        for p in room.players.all():
            self._age_out(p)

    def _ready(self, room: Room, player: Player, step: str | None = None) -> None:
        """One player's vote, recorded the way the game records it: through the endpoint.

        `step` is only ever passed when the test is about a step the room is not in — it writes
        the row directly, because no endpoint can produce a vote for a step that has not started
        and pretending otherwise would test a path the game does not have.
        """
        if step is not None:
            Ready.objects.create(room=room, player=player, step=step)
            return
        self._post(f"/api/rooms/{room.code}/ready", {"token": player.token})

    def _to_rating(self, room: Room, players: list[Player]) -> None:
        """Drive the room to `rating` on the first prompt, the honest way.

        Every player writes a prompt — through the endpoint, because a test that inserts
        `Answer` rows is testing a state the game cannot reach — then the admin opens the
        rating. With N players there are N prompts to reveal.
        """
        for i, p in enumerate(players):
            self._post(
                f"/api/rooms/{room.code}/answer",
                {"token": p.token, "prompt": f"un prompt {i}"},
            )
        self._post(f"/api/admin/rooms/{room.code}/next")
        room.refresh_from_db()

    def _rate_all(self, room: Room, players: list[Player], target: Player, score: int = 7) -> None:
        """Every player who MAY rate the prompt on screen has rated it. The author may not, and
        `_rate_all` does not try: the author being absent from the count is the rule under test
        in `test_the_rating_phase_advances_on_a_majority_of_ready_players`."""
        rnd = room.current_round()
        answer = rnd.answers.order_by("id")[room.rounds.get(order=room.current).revealed]
        for p in players:
            if p.pk == answer.player_id:
                continue
            self._post(f"/api/rooms/{room.code}/rate", {"token": p.token, "score": score})

    def _skip_to_summary(self, room: Room, players: list[Player]) -> None:
        """Rating through every prompt and landed on the summary, rating each one as it appears."""
        self._to_rating(room, players)
        while room.phase == Room.Phase.RATING:
            self._rate_all(room, players, players[0])
            self._post(f"/api/admin/rooms/{room.code}/next")
            room.refresh_from_db()


class VoteAdvanceTests(VoteBase):
    def test_nothing_moves_on_an_empty_room(self):
        # The 0 == 0 guard, in the place where the threshold is computed. A room whose phones
        # all dropped has nobody to press anything, and a bar of zero would move it on.
        room, _ = self._room_with(1)
        self._drop_everyone(room)
        maybe_advance_by_votes(room)
        room.refresh_from_db()
        self.assertEqual((room.phase, room.quiz_index), (Room.Phase.QUIZ, 0))

    def test_one_ready_player_out_of_three_is_not_enough(self):
        room, players = self._room_with(3)
        self._ready(room, players[0])
        maybe_advance_by_votes(room)
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, 0)

    def test_two_ready_players_out_of_three_move_the_room_on(self):
        # 3 // 2 + 1 == 2. This is the whole feature: the room moves on itself.
        room, players = self._room_with(3)
        self._ready(room, players[0])
        self._ready(room, players[1])
        maybe_advance_by_votes(room)
        room.refresh_from_db()
        self.assertEqual((room.phase, room.quiz_index), (Room.Phase.QUIZ, 1))
        self.assertGreater(room.quiz_deadline, timezone.now())

    def test_the_votes_of_the_step_that_just_closed_are_inert(self):
        # NOT DELETED, INERT — and the assertion is on the count for the NEW step, not on the
        # table. `quiz:0` votes cannot be counted against `quiz:1`, so a room that moved on is
        # not a room holding a stale majority. A test asserting `Ready.objects.count() == 0` here
        # would pin a deletion the design no longer does.
        room, players = self._room_with(3)
        for p in players[:2]:
            self._ready(room, p)
        maybe_advance_by_votes(room)
        room.refresh_from_db()
        self.assertEqual(current_step(room), "quiz:1")
        self.assertEqual(Ready.objects.filter(room=room, step="quiz:1").count(), 0)

    def test_a_disconnected_player_does_not_count_in_the_bar(self):
        # Three connected, one of them sleeps: the bar is 2 of 2 among the two awake, not 2 of 3.
        # This is why the rule counts `online` and not `room.players` — a phone in a bag would
        # otherwise hold the class on every question for the rest of the game.
        room, players = self._room_with(3)
        self._age_out(players[2])
        self._ready(room, players[0])
        self._ready(room, players[1])
        maybe_advance_by_votes(room)
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, 1)

    def test_a_vote_from_a_disconnected_player_does_not_count_either(self):
        room, players = self._room_with(3)
        self._ready(room, players[0])
        self._age_out(players[0])
        self._ready(room, players[1])
        maybe_advance_by_votes(room)
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, 0)

    def test_the_rating_phase_advances_on_a_majority_of_ready_players(self):
        # The prompt on screen is rated, then discussed. The bar is the players who may RATE it:
        # the author cannot, so counting them would make the bar unreachable.
        room, players = self._room_with(3)
        self._to_rating(room, players)
        self._rate_all(room, players, players[0])
        self._ready(room, players[1], step="rating:0:0")
        maybe_advance_by_votes(room)
        room.refresh_from_db()
        self.assertEqual(room.rounds.get(order=0).revealed, 1)

    def test_the_summary_advances_the_round_on_a_majority(self):
        room, players = self._room_with(3)
        self._skip_to_summary(room, players)
        for p in players[:2]:
            self._ready(room, p, step="summary:0")
        maybe_advance_by_votes(room)
        room.refresh_from_db()
        self.assertEqual((room.phase, room.current), (Room.Phase.PLAYING, 1))

    def test_the_admin_button_still_moves_the_room_on(self):
        # The filet. A class with one player who walked out and one phone that died is a class
        # the teacher can still finish, and that path is the one that existed before this feature.
        room, players = self._room_with(3)
        self._post(f"/api/admin/rooms/{room.code}/next")
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, 1)

    def test_a_vote_moves_the_room_on_without_waiting_for_the_next_snapshot(self):
        # The endpoint calls the same function the snapshot does, so a room that crosses the bar
        # on a tap advances on that tap. Polling for it would make the last vote's effect land up
        # to half a second late for no reason.
        room, players = self._room_with(3)
        for p in players[:2]:
            self.assertEqual(self._post(f"/api/rooms/{room.code}/ready", {"token": p.token}).status_code, 200)
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, 1)
```

Puis, dans le même fichier, les tests de l'endpoint :

```python
class ReadyEndpointTests(VoteBase):
    def test_a_vote_is_recorded_and_reported_back(self):
        room, players = self._room_with(2)
        r = self._post(f"/api/rooms/{room.code}/ready", {"token": players[0].token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["ready"]["count"], 1)

    def test_voting_twice_is_refused(self):
        room, players = self._room_with(2)
        self._post(f"/api/rooms/{room.code}/ready", {"token": players[0].token})
        self.assertEqual(
            self._post(f"/api/rooms/{room.code}/ready", {"token": players[0].token}).status_code, 409
        )

    def test_a_bad_token_is_refused(self):
        room, _ = self._room_with(2)
        self.assertEqual(self._post(f"/api/rooms/{room.code}/ready", {"token": "nope"}).status_code, 403)

    def test_a_vote_outside_a_discussion_is_refused(self):
        # In `waiting` there is nothing to discuss, and in `playing` the round closes on the
        # answers themselves. A vote accepted there would sit in the table until a step that
        # never matches its name, which is the shape of a bug that only shows up in a class.
        room, players = self._room_with(2)
        self._to_rating(room, players)
        self._post(f"/api/admin/rooms/{room.code}/next")
        room.refresh_from_db()
        self.assertEqual(self._post(f"/api/rooms/{room.code}/ready", {"token": players[0].token}).status_code, 409)

    def test_the_snapshot_reports_the_bar_and_whether_this_player_is_ready(self):
        room, players = self._room_with(3)
        self._ready(room, players[0])
        ready = room_state(room.code)["ready"]
        self.assertEqual((ready["count"], ready["needed"], ready["mine"]), (1, 2, False))
```

- [ ] **Étape 2 : vérifier que les tests échouent**

`cd backend && uv run python manage.py test game.tests.test_vote`
Attendu : `ImportError: cannot import name 'maybe_advance_by_votes'`.

- [ ] **Étape 3 : implémenter**

Dans `views.py`, à côté de `maybe_advance_phase` :

```python
def current_step(room: Room) -> str | None:
    """The discussion the room is in, named, or None when it is not discussing anything.

    THREE STEPS, one per place the class talks: after a didacticiel question is revealed, after
    a prompt has been rated, and after a round is summarised. `playing` is not one of them —
    that phase closes on the answers themselves, and adding a button there would ask twenty
    students to press something that says nothing they have not already said with a prompt.

    `None` IS THE ANSWER for four phases, and it is why this returns an option rather than a
    default: `waiting`, `playing`, `done` and a `summary` with no round behind it have no
    discussion, and a client that renders a ready bar for a step that does not exist shows a
    button that goes nowhere.
    """
    if room.phase == Room.Phase.QUIZ:
        return f"quiz:{room.quiz_index}"
    if room.phase == Room.Phase.RATING:
        rnd = room.current_round()
        return None if rnd is None else f"rating:{rnd.order}:{rnd.revealed}"
    if room.phase == Room.Phase.SUMMARY:
        rnd = room.current_round()
        return None if rnd is None else f"summary:{rnd.order}"
    return None
```

```python
def maybe_advance_by_votes(room: Room) -> None:
    """Move the room on when enough of the players who are there have said they are ready.

    THE MAJORITY IS THE POINT, and it is the second half of why this exists. §2 gave the
    advance to a person, and the consequence was that a teacher who wanted to stop and talk had
    to keep a hand on the laptop: the discussion the round was built around could not start
    until they pressed something. A majority of the players who are CONNECTED is enough, so the
    class ends its own discussion — and a phone in a bag is not a veto, because the bar counts
    `online_player_ids` and not `room.players`.

    THE THREE WRITERS, REACHED THROUGH THE SAME TABLE THE ADMIN BUTTON USES. The last three
    lines are `_PHASE_ADVANCE[room.phase](room)` and nothing else: the vote path and the admin's
    button path must be the same code, because a class that advances itself and a teacher who
    presses "suivant" are the same transition, and two implementations of it would drift within
    a week — the vote path settling the round while the button path does not, say, and the bug
    only appearing when a class finishes itself.

    NOTHING HAPPENS ON AN EMPTY ROOM. `majority(0)` is 0 and the comparison below would be
    `0 >= 0`, which is the vacuous truth this file refuses in three other places; the early
    return is what keeps a room whose phones all dropped from running itself unattended.
    """
    step = current_step(room)
    if step is None:
        return
    online = online_player_ids(room.code)
    if not online:
        return
    needed = majority(len(online))
    if needed == 0:
        return
    # The author of the prompt on screen cannot rate it, so they are not part of the bar on the
    # rating step. Counting them would make the step unreachable for a room where the author is
    # the only player awake — the same reasoning as `_maybe_advance_reveal`.
    voters = set(online)
    if room.phase == Room.Phase.RATING:
        rnd = room.current_round()
        if rnd is not None:
            answers = list(rnd.answers.order_by("id"))
            if rnd.revealed < len(answers):
                voters.discard(answers[rnd.revealed].player_id)
    count = Ready.objects.filter(room=room, step=step, player_id__in=voters).count()
    if count < needed:
        return
    advance = _PHASE_ADVANCE.get(room.phase)
    if advance is not None:
        advance(room)
```

Les trois fonctions de la table existent déjà : `_advance_quiz` (ouvre la question suivante ou la
partie), `_advance_reveal_and_settle` (prompt suivant, avec le règlement du tour), et
`Room.next_round` (tour suivant ou fin). **N'écrivez pas de branche par phase ici** : c'est la
table qui est la documentation exhaustive de ce que l'admin peut faire, et la ajouter une
seconde fois dans un autre fichier la ferait mentir.

L'endpoint, à côté de `rate` :

```python
@require_POST
def ready(request: HttpRequest, code: str) -> JsonResponse:
    """« On peut passer. » One player's vote, on the step the room is in.

    THE CLIENT NEVER NAMES THE STEP. It sends its token and nothing else, for the reason `rate`
    gives: a payload that could name the step is a payload that could vote on a step that has
    not started, and the count would then include a vote the room never asked for.
    """
    room = get_object_or_404(Room, code=code)
    data = body(request)
    player, refusal = _player_for_token(room, data)
    if refusal is not None:
        return refusal
    step = current_step(room)
    if step is None:
        return json_error("nothing to discuss", 409)
    if Ready.objects.filter(room=room, step=step, player=player).exists():
        return json_error("already ready", 409)
    Ready.objects.create(room=room, step=step, player=player)
    maybe_advance_by_votes(room)
    room.refresh_from_db()
    return JsonResponse({"ok": True, "ready": _ready_state(room, player, online_player_ids(room.code))})
```

Le bloc dans `room_state`, à côté de `quiz` / `reveal` / `summary` — **clé inconditionnelle,
`None` hors discussion**, comme les trois autres :

```python
def _ready_state(room: Room, player, online: set[int]) -> dict | None:
    """The class's own bar: how many are ready, how many are needed, and whether THIS player is.

    `needed` and not `quorum`: the payload carries no float anywhere else and this file names
    integers by what they are. `needed` is also the number a phone has to show next to the
    button, so it is the number that matters to a client, not the rule behind it.
    """
    step = current_step(room)
    if step is None or player is None:
        return None
    voters = set(online)
    if room.phase == Room.Phase.RATING:
        rnd = room.current_round()
        if rnd is not None:
            answers = list(rnd.answers.order_by("id"))
            if rnd.revealed < len(answers):
                voters.discard(answers[rnd.revealed].player_id)
    return {
        "step": step,
        "count": Ready.objects.filter(room=room, step=step, player_id__in=voters).count(),
        "needed": majority(len(online)),
        "mine": Ready.objects.filter(room=room, step=step, player=player).exists(),
    }
```

et dans le dict `state`, une ligne :

```python
"ready": _ready_state(room, p, online) if p is not None else None,
```

Le signalement de présence passe par `_beat` dans la route `events` ; c'est là que l'instantané
est reconstruit, donc un joueur qui se reconnecte voit son `ready` se recalculer sans
changement d'état.

Enfin `backend/config/urls.py`, une ligne :

```python
path("api/rooms/<str:code>/ready", views.ready),
```

- [ ] **Étape 4 : vérifier que les tests passent**

`cd backend && uv run python manage.py test game.tests.test_vote`
Attendu : `OK`.

Puis `cd backend && uv run python manage.py test game`
Attendu : `test_nothing_opens_the_game_by_itself` **échoue**, et c'est le but — il affirme que
rien n'ouvre la partie par soi-même, ce que cette tâche rend faux. Le remplacer dans la tâche 4.
Aucun autre test ne doit casser : `test_play`, `test_presence`, `test_admin` et `test_rating`
ntouchent pas `current_step`.

- [ ] **Étape 5 : committer**

```bash
git add backend/game/views.py backend/config/urls.py backend/game/tests/test_vote.py
git commit -m "feat(vote): a majority of ready players moves the room on"
```

---

## Tâche 4 : le test qui verrouillait l'admin, retourné

- [ ] **Étape 1 : lire le test existant**

`backend/game/tests/test_quiz.py`, `test_nothing_opens_the_game_by_itself`. Il énonce une
décision de spec (« le gate ne s'ouvre pas tout seul ») que la tâche 3 inverse
délibérément. **Le supprimer serait perdre une trace** : le retirer sans le remplacer
effacerait la raison pour laquelle l'admin pilotait.

- [ ] **Étape 2 : le remplacer par son contraire**

```python
def test_the_gate_opens_the_game_when_a_majority_is_ready_for_the_eighth(self):
    # THE INVERSION, pinned in the other direction. This test used to assert the opposite: that
    # nothing opens the game by itself, because §2 gave the advance to a person. The class now
    # ends its own didacticiel, and the reason is the discussion the gate was missing — a
    # teacher who has to press a button to start talking about question 8 does not get to talk
    # about question 8.
    #
    # The majority is still there, and it is asserted in the same test: two players, one vote,
    # nothing happens. One phone cannot skip the didacticiel for a class of two.
    room, players = self._joined(two=True)
    ana, bob = players
    self._online(room, ana, bob)
    self._answer_all(room, ana)
    self._answer_all(room, bob)
    room.refresh_from_db()
    self.assertEqual(room.phase, Room.Phase.QUIZ)
    self.assertEqual(room.quiz_index, QUIZ_LENGTH - 1)

    # One of two is not a majority. This is the line the old test would have failed on, and it
    # is the reason the rule is a majority and not « whoever answers first ».
    self._post(f"/api/rooms/{room.code}/ready", {"token": ana.token})
    room.refresh_from_db()
    self.assertEqual(room.phase, Room.Phase.QUIZ)

    # Two of two is. The eighth question's discussion ends, and the same step opens the writing
    # phase — which is what `_advance_quiz` has always done for the admin's button.
    self._post(f"/api/rooms/{room.code}/ready", {"token": bob.token})
    room.refresh_from_db()
    self.assertEqual(room.phase, Room.Phase.PLAYING)
    # The cursor stays where the eighth question left it, exactly as the button leaves it.
    self.assertEqual(room.quiz_index, QUIZ_LENGTH - 1)
```

Le vote passe par l'endpoint, pas par un helper : `test_quiz.py` n'a pas de `_ready`, et lui en
ajouter un reviendrait à déplacer dans ce fichier un helper dont la raison d'être est la tâche 3.
Le POST est la ligne que le joueur fait vraiment.

- [ ] **Étape 3 : vérifier**

`cd backend && uv run python manage.py test game`
Attendu : `OK`, tout vert, aucun autre test modifié. Compter les tests avant et après : le
nombre doit augmenter de 1 (un retiré, un ajouté).

- [ ] **Étape 4 : committer**

```bash
git add backend/game/tests/test_quiz.py
git commit -m "test(quiz): the class opens the gate, and the test says why it changed"
```

---

## Tâche 5 : le bouton « prêt » côté client

- [ ] **Étape 1 : le contrat, dans `api.ts`**

```ts
// The class's own bar on the current step. `needed` rather than `quorum` because the server
// sends it under that name: it is a number of players, not a score, and this payload has no
// other float.
export type ReadyState = {
  // Which discussion this is: `quiz:3`, `rating:2:0`, `summary:2`. Shown nowhere — it is here so
  // a screen can tell « my vote was for the step on screen » from « my vote is stale ».
  step: string
  count: number
  needed: number
  mine: boolean
}
```

Dans `State`, une ligne : `ready: ReadyState | null`. Puis l'appel, à côté de `rate` :

```ts
export async function postReady(code: string, token: string): Promise<void> {
  await post(`/api/rooms/${code}/ready`, { token })
}
```

- [ ] **Étape 2 : `ReadyBar` dans `ui.tsx`**

```tsx
export function ReadyBar({ ready, onReady }: { ready: ReadyState | null; onReady: () => void }) {
  if (!ready) return null
  const there = ready.count >= ready.needed
  return (
    <div className="ready">
      <button
        type="button"
        className={`btn${ready.mine ? ' btn--ghost' : ' btn--signal'} btn--wide`}
        onClick={onReady}
        disabled={ready.mine || there}
      >
        {ready.mine ? 'Prêt' : 'Je suis prêt'}
      </button>
      <p className="prose prose--sm">
        {there
          ? 'La classe passe à la suite.'
          : `${ready.count} sur ${ready.needed} prêts.`}
      </p>
    </div>
  )
}
```

`disabled` sur les deux cas : un joueur qui a déjà voté ne recompte pas, et un joueur dont le
vote fait passer la classe ne peut pas en faire un deuxième. Le libellé dit l'état, jamais le
règle du seuil — un élève n'a pas à faire le calcul pour savoir s'il doit attendre.

- [ ] **Étape 3 : le style, dans `index.css`**

```css
/* The class's own bar. One component, three screens, so it is one block: a full-width button
 * with the count under it, because on a phone held in one hand the button is the target and the
 * count is read by the teacher standing in front. */
.ready {
  display: grid;
  gap: 0.4rem;
  margin-top: 1rem;
}

.ready .btn {
  min-height: 3rem;
}
```

- [ ] **Étape 4 : câbler les trois écrans**

Dans `Quiz.tsx`, sous la révélation ; dans `Rating.tsx`, sous l'échelle de notation ; dans
`Game.tsx`, sous le tableau du résumé. Le même contrat aux trois endroits :

```tsx
<ReadyBar
  ready={state.ready}
  onReady={async () => {
    try {
      await postReady(code, token)
    } catch {
      setErr('Le vote n’est pas passé.')
    }
  }}
/>
```

`state.ready` est `null` hors discussion, donc `ReadyBar` rend `null` et les écrans n'ont pas à
tester la phase eux-mêmes.

- [ ] **Étape 5 : vérifier**

```bash
cd frontend && bun run build && bun run lint
```

Attendu : les deux propres. `tsc` refusera `ready` manquant sur `State` si l'étape 1 a été
sautée — c'est le garde-fou.

- [ ] **Étape 6 : committer**

```bash
git add frontend/src/api.ts frontend/src/ui.tsx frontend/src/Quiz.tsx frontend/src/Rating.tsx frontend/src/Game.tsx frontend/src/index.css
git commit -m "feat(ui): a ready button on the three screens that discuss"
```

---

## Tâche 6 : le README et l'écran admin

- [ ] **Étape 1 : l'écran admin, `Admin.tsx`**

Le bouton « suivant » reste, avec un libellé qui dit ce qu'il est devenu. Le `state` de la
console affiche `hint` déjà par phase ; ajouter dans la phase `quiz` et la phase `rating` :

```ts
hint: quizClosed
  ? 'La classe passe à la suite quand la majorité est prête.'
  : 'Passe à la question suivante quand tu veux, sinon attends la majorité.',
```

Rien d'autre ne change dans `Admin.tsx` : le filet existe déjà, il s'appelle déjà
`admin_next`, et il appelle déjà les mêmes trois fonctions.

- [ ] **Étape 2 : le README**

Dans « Comment se joue une partie », remplacer le bloc qui décrit l'avance par l'admin, et
ajouter la règle :

```markdown
**Avance de phase.** Chaque discussion se termine quand la **majorité** des joueurs connectés a
appuyé sur « prêt » — la moitié rounded up, donc 11 sur 20, 2 sur 3, et 2 sur 2 quand il n'y a
que deux joueurs. La classe fait avancer le jeu toute seule ; l teacher's bouton reste là pour
forcer quand un téléphone est mort. La discussion est orale : pas de champ de texte, parce
qu'un élève qui écrit sur son téléphone ne parle plus à sa classe.
```

Remplacer aussi le schéma de la partie pour montrer les étapes de discussion, et corriger la
règle « Avance de phase » de la section « Règles » qui dit aujourd'hui « L'admin avance quand il
le décide ».

- [ ] **Étape 3 : vérifier**

```bash
cd frontend && bun run build && bun run lint
cd backend && uv run python manage.py test game
```

- [ ] **Étape 4 : committer**

```bash
git add frontend/src/Admin.tsx README.md
git commit -m "docs: the class moves the game on, the admin can still force"
```

---

## Ce que ce plan ne fait pas, et pourquoi

- **Pas de champ de commentaire sur les prompts notés.** Le README le refuse déjà : sur un
  téléphone, un champ libre à chaque prompt fait abandonner. La discussion est orale.
- **Pas de minuterie sur la discussion.** Une discussion coupée par un timer est le contraire
  d'une discussion, et le seuil de majorité n'a pas besoin d'être_FILE pour être franchi.
- **Pas de texte libre dans le QCM non plus**, pour la même raison.
- **Le bouton admin ne disparaît pas.** Un joueur dont le téléphone est mort est un fait, pas un
  bug, et une partie bloquée devant vingt élèves est le pire résultat possible.
- **Pas de « prêt » en phase `playing`.** Cette phase se ferme sur les réponses elles-mêmes ; un
  bouton là demanderait aux élèves de confirmer quelque chose qu'ils viennent de faire.