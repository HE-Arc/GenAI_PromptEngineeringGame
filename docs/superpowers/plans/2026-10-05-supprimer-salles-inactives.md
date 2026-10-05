# Supprimer les parties inactives — un balayage qui tourne seul, 1 h

> **Pour les agents exécuteurs :** sous-skill REQUIS : utiliser
> `superpowers:subagent-driven-development` (recommandé) ou `superpowers:executing-plans`
> pour exécuter ce plan tâche par tâche. Les étapes utilisent des cases à cocher (`- [ ]`).

**Objectif :** une partie jouée et sans joueur connecté depuis `ROOM_KEEP_TTL = 3600` (1 h) est
**supprimée de la base**, et cette suppression n'attend plus qu'un admin ouvre la console.

**Architecture :** le balayage existe déjà (`_sweep_idle_rooms`, `views.py:145`) et fait deux
choses : masquer de la liste toute salle inactive depuis 10 min, supprimer un lobby jamais
lancé. Il ne se déclenche qu'à la lecture de `/api/admin/rooms` — le README le dit
lui-même (« sans console d'admin ouverte il ne se passe rien et la table grossit »). Ce plan
lui ajoute une troisième issue (supprimer une partie jouée) et un **déclencheur** : un thread
daemon dans le process du serveur. La décision est extraite dans une fonction pure, appelée
par les deux chemins, pour qu'ils ne puissent pas diverger.

## Décisions

| Décision | Alternative | Pourquoi |
|---|---|---|
| Thread daemon **dans le process du serveur**, gardé par une variable d'environnement | Une commande `manage.py` lancée en boucle par `run.sh` | L'instant d'inactivité est `_presence`, **en mémoire du process**. Un second process ne voit aucun beat : il verrait toutes les salles comme sans beat, donc après 10 min de marche il supprimerait des parties **en cours de jeu**. Le balayeur doit vivre là où sont les beats. |
| `GENAI_SWEEP=1` posée par `run.sh` | Démarrer le thread inconditionnellement dans `AppConfig.ready()` | `manage.py test` lance `ready()`. Un thread qui supprime des lignes pendant la suite est un test qui passe ou échoue selon l'horloge. La variable rend le destructeur opt-in : absent, rien ne tourne. |
| `Room.last_activity_at` (migration) | Se fier à `_BOOT_INSTANT` | Sans colonne, l'instant d'inactivité d'une partie rejouée est perdu au redémarrage : le seuil d'1 h redevient inatteignable et la table grossit malgré tout. C'est la différence entre une politique qui marche et une qui échoue silencieusement. |
| Écrire `last_activity_at` **une fois par tick**, pas à chaque beat | Persister à chaque heartbeat | Trente joueurs à deux beats par seconde feraient soixante écritures par seconde pour un nombre que seule une question lente lit. |

## Contraintes globales

- Les 343 tests existants doivent rester verts. `test_idle.py` (non suivi, dans l'arbre de
  travail) épingle le contrat du balayage : **le lire avant de le modifier**.
- Une salle avec un beat vivant ne doit jamais atteindre une ligne de suppression. La garantie
  est structurelle aujourd'hui (`PRESENCE_TTL` = 3 s, `ROOM_IDLE_TTL` = 600 s) : la conserver.
- `admin_list_rooms` doit continuer à exclure du payload les codes balayés, et le balayage doit
  continuer de tourner entre l'énumération et la réponse.
- **Les commentaires de code sont en anglais**, comme partout dans ce dépôt (`views.py`, `llm.py`,
  `models.py`, tout `frontend/src`). Les caractères accentués n'apparaissent que dans les chaînes
  de texte destinées au joueur. Commenter en français ici serait la seule prose française du
  dépôt.
- Tout texte joueur est en français.
- Pas de boucle d'événements ni de signal : un `time.sleep` entre deux ticks, et rien d'autre.

---

## Task 1 : `Room.last_activity_at`

**Files:**
- Modify: `backend/game/models.py` (champ `Room`, après `started_at`)
- Create: `backend/game/migrations/0010_room_last_activity_at.py`

- [ ] **Step 1 : le champ + la migration**

```python
    # WHEN THIS GAME WAS LAST USED, or null while the sweep has not written it yet. Written by
    # the sweep thread, once per tick, and only for a room somebody is connected to — never by a
    # heartbeat.
    #
    # THIS FIELD IS THE ONLY THING THAT SURVIVES A RESTART for the deletion policy. `_presence`
    # is in memory: after a restart a played room's idle instant fell back to `_BOOT_INSTANT`,
    # the one-hour threshold became unreachable, and the table grew anyway — a policy that
    # existed without ever applying.
    last_activity_at = models.DateTimeField(null=True, blank=True)
```

Générer la migration avec `uv run python manage.py makemigrations game` puis **lire le fichier
généré** pour vérifier qu'elle ne contient que ce champ.

- [ ] **Step 2 : la suite**

Run: `cd backend && uv run python manage.py test game 2>&1 | tail -5`
Expected: `OK` (343 tests).

- [ ] **Step 3 : commit**

```bash
git add backend/game/models.py backend/game/migrations/0010_room_last_activity_at.py
git commit -m "feat(room): remember when a game was last used, across restarts"
```

## Task 2 : trois issues, une seule décision

**Files:**
- Modify: `backend/game/views.py:64-73` (constantes), `145-197` (`_sweep_idle_rooms`)
- Test: `backend/game/tests/test_idle.py`

**Interfaces:**
- Produces: `_sweep_plan(rows, beats, boot, now) -> tuple[set[str], list[str], dict[str, datetime]]`
  — (codes à masquer, codes à supprimer, activité à écrire) — et
  `ROOM_KEEP_TTL: float = 3600.0`.

- [ ] **Step 1 : les constantes**

```python
# How long a played game survives with nobody in it. This is a DELETION, not a hide: past
# this the row goes and the players' tokens with it, so a teacher coming back the next day
# does not find their game. The delay is short because the database is local and nothing is
# backed up — an hour trades « the table stops growing » against « the lunch break does not eat
# the game ».
ROOM_KEEP_TTL = 3600.0
# The sweep thread's tick. Short enough that a room leaves the list shortly after its last
# player, long enough that a tick is free: the sweep reads one table and writes only for the
# rooms somebody is connected to.
SWEEP_EVERY = 60.0
```

- [ ] **Step 2 : les tests qui échouent**

Dans `test_idle.py`, trois tests :

```python
    def test_a_played_room_idle_over_an_hour_is_deleted(self) -> None:
        # Three outcomes, and the delete one is irreversible: an hour is the deal.
        ...
        self.assertEqual(self.room.__class__.objects.count(), 0)

    def test_a_played_room_idle_for_twenty_minutes_survives(self) -> None:
        # Hidden from the list, still in the base: this is the window a teacher uses to come
        # back to a game they paused.
        ...

    def test_the_last_activity_survives_a_restart(self) -> None:
        # `_presence` is process-local and a restart empties it. The durable column is what
        # makes the one-hour rule reachable on the second run rather than never.
        ...
```

Écrire aussi le test du **filet** : une salle dont `last_activity_at` vaut 2 h et qui a un beat
vivant ne est jamais supprimée.

- [ ] **Step 3 : vérifier que ça échoue**

Run: `cd backend && uv run python manage.py test game.tests.test_idle`
Expected: FAIL — `_sweep_plan` n'existe pas.

- [ ] **Step 4 : `_sweep_plan`, fonction pure**

La fonction prend les lignes déjà lues par `admin_list_rooms` (`code`, `started_at`), le
registre de beats, l'instant de boot et `now`, et renvoie les trois décisions. Aucun accès
base : c'est ce qui la rend testable sans horloge et appelable par les deux chemins.

```python
def _sweep_plan(
    rows: list[dict], beats: dict[str, float], boot: float, now: float
) -> tuple[set[str], list[str], set[str]]:
    """(codes à masquer, codes à supprimer, codes dont l'activité doit être écrite).

    TRIS ÉCHECS, ET LE DERNIER EST IRRÉVERSIBLE. Une salle inactive depuis `ROOM_IDLE_TTL`
    disparaît de la liste ; un lobby jamais lancé disparaît de la base ; une partie JOUÉE ne
    disparaît de la base qu'après `ROOM_KEEP_TTL`, parce qu'elle contient les jetons de joueurs
    et des rounds que quelqu'un peut vouloir retrouver au prix d'une pause.

    L'instant d'inactivité est le dernier beat si l'encre est en mémoire, sinon la colonne
    `last_activity_at`, sinon `started_at` — un `None` signifie « on n'a jamais rien su », et
    l'âge de la partie est alors le seul fait disponible.
    """
```

Écrire l'activité : `set[str]` des codes ayant un beat vivant, écrit en un seul
`Room.objects.filter(code__in=...).update(last_activity_at=timezone.now())` — **un seul UPDATE
par tick**, jamais un par joueur.

- [ ] **Step 5 : `_sweep_idle_rooms` devient un adaptateur**

Elle garde sa signature (`rooms: list[dict] -> set[str]`) et son contrat — c'est elle que
`admin_list_rooms` appelle et ce qu'elle renvoie est ce que la liste exclut — et délègue la
décision à `_sweep_plan`, puis exécute les deux écritures. Un seul endroit décide, deux
chemins exécutent : ils ne peuvent pas diverger.

- [ ] **Step 6 : la suite**

Run: `cd backend && uv run python manage.py test game 2>&1 | tail -5`
Expected: `OK`.

- [ ] **Step 7 : commit**

```bash
git add backend/game/views.py backend/game/tests/test_idle.py
git commit -m "feat(sweep): a played game is deleted after an hour, not just hidden"
```

## Task 3 : le balayage tourne sans admin

**Files:**
- Modify: `backend/game/views.py` (le thread)
- Modify: `backend/game/apps.py`
- Modify: `run.sh`
- Test: `backend/game/tests/test_idle.py`

- [ ] **Step 1 : le test du déclencheur qui échoue**

```python
    def test_no_thread_starts_without_the_env_var(self) -> None:
        # `AppConfig.ready()` also runs under `manage.py test`. A deleter thread under the
        # suite is a test that passes or fails depending on the wall clock, so the guard is
        # the thing under test, not a detail.
```

- [ ] **Step 2 : le thread**

```python
def _sweep_forever() -> None:
    """Balaye tant que le process vit. `close_old_connections` aux deux bouts : ce thread n'est
    attaché à aucune requête, donc rien ne fermera sa connexion pour lui — sans ça SQLite
    garde un descripteur ouvert par tick.

    Le cycle est `sweep` PUIS `sleep`, jamais l'inverse : balayer en premier fait partir la
    connexion d'un tick de repos au lieu de la rouvrir à chaque réveil.
    """
    while True:
        try:
            close_old_connections()
            _sweep_idle_rooms(list(Room.objects.values("code", "started_at")))
        except Exception:  # noqa: BLE001
            # Un balayeur qui meurt emporte la seule chose qui nettoie la base. Il se prend
            # une exception, la note, et recommence au tick suivant.
            logger.exception("room sweep failed")
        finally:
            close_old_connections()
        time.sleep(SWEEP_EVERY)
```

- [ ] **Step 3 : le démarrage**

Dans `apps.py`, `ready()` ne démarre le thread que si `os.environ.get("GENAI_SWEEP") == "1"`,
avec un `threading.Thread(target=_sweep_forever, daemon=True, name="room-sweep").start()`.
Un `ready()` qui importe `game.views` pour le cibler : le faire **dans** la fonction, pas au
niveau du module, pour ne pas créer un cycle d'import au chargement de l'app.

- [ ] **Step 4 : `run.sh`**

```bash
export GENAI_SWEEP=1
```

avant le lancement du backend, avec un commentaire d'une ligne disant ce que la variable
active. Le `trap` existant n'a pas à changer : le thread meurt avec le process.

- [ ] **Step 5 : la suite, puis le_commit**

Run: `cd backend && uv run python manage.py test game 2>&1 | tail -5`
Expected: `OK`, et **aucun** thread nommé `room-sweep` ne doit exister pendant la suite.

```bash
git add backend/game/views.py backend/game/apps.py run.sh backend/game/tests/test_idle.py
git commit -m "feat(sweep): the sweep runs on its own, in the process that knows who is here"
```

## Task 4 : le README

**Files:**
- Modify: `README.md` (le paragraphe « La liste des salles se nettoie toute seule »)

- [ ] **Step 1 : réécrire le paragraphe**

Il doit dire, dans cet ordre : ce qui est masqué (10 min), ce qui est supprimé (un lobby jamais
lancé ; une partie jouée après `ROOM_KEEP_TTL`, une heure, **et ce que ça veut dire** : plus de
reprise possible après une heure), et que le balayage tourne tout seul maintenant — donc
retirer la phrase « sans console d'admin ouverte il ne se passe rien », qui est devenue fausse.

- [ ] **Step 2 : commit**

```bash
git add README.md
git commit -m "docs: an hour is what a played game is kept for, and it is deleted, not hidden"
```

---

## Le chemin de preuve

| Claim | Preuve |
|---|---|
| Une partie jouée inactive depuis 1 h est supprimée | `test_a_played_room_idle_over_an_hour_is_deleted` |
| Une partie inactive depuis 20 min est masquée et **conservée** | `test_a_played_room_idle_for_twenty_minutes_survives` |
| La règle survit à un redémarrage | `test_the_last_activity_survives_a_restart` |
| Une salle avec des joueurs connectés n'est jamais supprimée | le test du filet |
| Aucun thread sous la suite | `test_no_thread_starts_without_the_env_var` |
| Rien d'autre n'a bougé | `uv run python manage.py test game` → 343+ `OK` |
| Le balayage tourne vraiment en conditions réelles | `./run.sh`, une partie, `kill -STOP` du serveur... **non couvert** : la preuve est lisible dans le code et le test du déclencheur |

**Non prouvé :** le comportement sur un redémarrage *réel* du serveur (le test simule la perte
de `_presence`, pas un redémarrage complet) ; le `logger.exception` qui survit à une base
cassée n'est pas testé.