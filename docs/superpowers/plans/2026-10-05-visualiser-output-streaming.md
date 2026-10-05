# Voir la réponse du modèle s'écrire — markdown et streaming

> **Pour les agents exécuteurs :** sous-skill REQUIS : utiliser
> `superpowers:subagent-driven-development` (recommandé) ou `superpowers:executing-plans`
> pour exécuter ce plan tâche par tâche. Les étapes utilisent des cases à cocher (`- [ ]`).

**Objectif :** la réponse du modèle s'écrit à l'écran pendant qu'elle est générée, puis se lit
en markdown (listes, gras, titres) au lieu d'un `<pre>` brut, dans le verdict du joueur comme
dans le résumé de la manche.

**Architecture :** `POST /api/rooms/<code>/answer` passe de « un JSON à la fin » à « un flux SSE
qui rend la main toutes les ~30 ms ». Les refus (409) restent des JSON synchrones : toutes les
validations ont lieu avant que la réponse ne démarre. La génération, le jury et l'écriture en
base ont lieu dans un **thread de travail** qui pousse ses étapes dans une `queue.Queue`, et le
générateur SSE se contente de vider cette file — ce qui rend le travail insensible à une
déconnexion (un téléphone qui verrouille son écran continue d'être noté) et permet d'envoyer
une trame `error` structurée, ce qu'un générateur ne peut pas faire depuis un `except` (yield
interdit dans un `except`). Côté front, un composant `ModelOutput` rend le markdown et sert
aux **deux** endroits qui affichent une sortie de modèle ; pendant la génération il affiche le
texte brut, parce qu'un document markdown à moitié écrit n'est pas un document.

**Pile technique :** Django 5 (`backend/`, SQLite, `runserver` threadé, aucun test runner JS),
React 19 + Vite 8 + TypeScript 6 (`frontend/`, CSS global, aucun router).

**Spec :** ce plan est la spec. Les deux décisions de conception qu'il prend — markdown en flux
brut puis rendu, et le refus de protocole par repli — sont explicitées dans « Décisions ».

## Décisions (et ce qu'elles coûtent)

| Décision | Alternative écartée | Pourquoi |
|---|---|---|
| `POST /answer` répond en SSE | Gardner le JSON et pousser les tokens dans le flux de salle `/events` | Le flux de salle est **public**. Pousser une génération dedans obligerait chaque phone à filtrer ce qui le concerne : un bug de filtrage montre la réponse d'un joueur à toute la classe. Une réponse par requête est privée par construction, sans nouveau canal. |
| Thread de travail + `queue.Queue` | Le générateur SSE fait lui-même les appels LLM | On ne peut pas `yield` dans un `except` : impossible d'envoyer une trame `error` après un échec LLM. Et un générateur fermé (déconnexion) tuerait la génération **après** l'avoir payée, sans rien enregistrer — la manche ne se fermerait jamais pour ce joueur. |
| Pas de repli sur l'ancien protocole | Négocier par `Accept: text/event-stream` | Un second chemin dans la même vue est la moitié du travail pour un client qui n'existe pas (le front est le seul client) et qui ne serait jamais exercé par un test de bout en bout. Le coût retenu est un plancher de plateforme, écrit plus bas. |
| Texte **brut** pendant la génération, markdown à la fin | Rendre le markdown à chaque trame | Un chunk coupe au milieu d'un `**` : le re-parser 400 fois par réponse est du travail jeté et fait clignoter `**gra` en texte puis en gras. La bascule est une seule fois, à la fin. |
| Markdown rendu en **éléments React**, jamais en HTML | `dangerouslySetInnerHTML` | La sortie du modèle est le texte d'un prompt **écrit par un joueur** : c'est une surface d'injection, pas une donnée de confiance. `react-markdown` échappe le HTML brut par défaut, ce qui couvre le cas sans une seule ligne de sanitation maison. |

## Contraintes globales

- Zéro clé d'API : toute fonction LLM ajoutée a un repli local et **le dit au joueur**. Le
  repli doit **streamer lui aussi**, sinon le mode dev n'exerce jamais le chemin de production.
- Les tests n'ouvrent jamais de connexion réseau ; `llm` est mocké au même niveau qu'aujourd'hui.
- Pas de `dangerouslySetInnerHTML` nulle part dans le dépôt. Pas de HTML brut rendu.
- Frontend : pas de router, pas de library d'état, pas de CSS modules, pas de **test runner**.
  Classes BEM plates. `bun run build` et `bun run lint` doivent rester propres.
- Backend : `uv run python manage.py test game` doit passer (343 tests verts avant ce plan).
- **Les commentaires de code sont en anglais**, comme partout dans ce dépôt (`views.py`, `llm.py`,
  `models.py`, tout `frontend/src`). Les caractères accentués n'apparaissent que dans les chaînes
  de texte destinées au joueur. Les commentaires en français dans les extraits de code ci-dessous
  sont un défaut de ce document : **les traduire en anglais en les appliquant.**
- Tout texte joueur est en français.
- `Answer.output` reste la vérité stockée : c'est elle que le jury note et que le résumé montre.
  Le flux ne la remplace pas, il la fait apparaître.
- Le refus `already answered`, `not playing`, `bad token`, `prompt required` garde son JSON et
  son code HTTP. `describe()` côté front en dépend.
- Le ratings phase reste inchangé : `Rating.tsx:168` masque volontairement l'output pendant la
  notation croisée. Ce plan ne touche pas à ce choix.

---

## Fichiers

| Fichier | Responsabilité |
|---|---|
| `backend/game/llm.py` | `answer_stream()` : le seul chemin de génération, chunk par chunk. Remplace `answer()`. |
| `backend/game/views.py` | `answer()` : validation synchrone, puis flux SSE alimenté par un thread de travail. |
| `backend/game/tests/support.py` | **Nouveau.** Double de test partagé : `answer_chunks()`, `_read_frames()`. |
| `backend/game/tests/test_llm.py` | Tests du vrai `answer_stream` (chunk OpenAI + repli). |
| `backend/game/tests/test_play.py`, `test_rating.py`, `test_presence.py`, `test_admin.py`, `test_vote.py` | Migration des ~20 mocks `llm.answer` + assertions de trames. |
| `frontend/src/ModelOutput.tsx` | **Nouveau.** Le rendu markdown d'une sortie de modèle. Un seul composant, deux usages. |
| `frontend/src/api.ts` | `streamAnswer()` : client SSE par-dessus `fetch` + `ReadableStream`. |
| `frontend/src/Game.tsx` | Câblage : texte en direct, état « le jury délibère », verdict à la fin. |
| `frontend/src/index.css` | Styles des blocs markdown (`.md`). |
| `README.md` | Le streaming, le repli, le plancher de plateforme. |

---

## Phase A — Markdown (livrable autonome)

### Task A1 : `ModelOutput`, le rendu markdown

**Files:**
- Create: `frontend/src/ModelOutput.tsx`
- Modify: `frontend/src/Game.tsx:221-224` (verdict), `frontend/src/Game.tsx:297-300` (détail du résumé)
- Modify: `frontend/src/index.css`
- Modify: `frontend/package.json` (dépendance)

**Interfaces:**
- Consumes: rien.
- Produces: `ModelOutput({ text: string, streaming?: boolean })` — `streaming` vrai = texte brut
  dans un `<pre>`, absent/faux = markdown. Utilisé par `Game.tsx` aux deux endroits.

- [ ] **Step 1 : ajouter la dépendance**

```bash
cd frontend && bun add react-markdown remark-gfm
```

Noter les versions exactes résolues dans le fichier — `react-markdown` est ESM-only et
majeur-versionnable, le build le prouvera à l'étape 5.

- [ ] **Step 2 : écrire le composant**

```tsx
// The model's answer, read. One component for the player's verdict and for the round summary:
// the same text deserves the same rendering, and two callers assembling their own markdown
// diverge at the first forgotten tag.
//
// `streaming` is the only difference between the two modes, and it lives in the API rather
// than in the caller because the reason is a single one: a markdown document interrupted in
// the middle of a `**` is not a document. So it is drawn raw while it is being written, and
// rendered once it is finished — one switch, not a flicker per frame.
//
// No raw HTML is rendered: `react-markdown` escapes what comes from the source, so a player
// whose prompt asks for `<img onerror>` gets the text of the tag, not a tag. That is the whole
// reason for this library rather than a `dangerouslySetInnerHTML` over a plain `<div>`.
import Markdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

// Every component receives a `node` prop carrying the hast element. It MUST be stripped before
// spreading onto a DOM node, or React warns once per element per render. This is the only reason
// the map below exists rather than the four lines of CSS the task looks like it needs.
const BLOCKS: Components = {
  p: ({ node: _node, ...props }) => <p {...props} />,
  ul: ({ node: _node, ...props }) => <ul {...props} />,
  ol: ({ node: _node, ...props }) => <ol {...props} />,
  li: ({ node: _node, ...props }) => <li {...props} />,
  blockquote: ({ node: _node, ...props }) => <blockquote {...props} />,
  h1: ({ node: _node, ...props }) => <h1 {...props} />,
  h2: ({ node: _node, ...props }) => <h2 {...props} />,
  h3: ({ node: _node, ...props }) => <h3 {...props} />,
  h4: ({ node: _node, ...props }) => <h4 {...props} />,
  pre: ({ node: _node, ...props }) => <pre {...props} />,
  table: ({ node: _node, ...props }) => <table {...props} />,
}

export default function ModelOutput({ text, streaming = false }: { text: string; streaming?: boolean }) {
  if (streaming) return <pre className="slot md md--raw">{text}</pre>
  return (
    <div className="md">
      {/* No `skipHtml`: the default escapes a raw tag into visible text, and a player who asked
          the model for `<img onerror>` should SEE the tag it emitted. Dropping it would hide the
          very thing worth reading. `urlTransform` is left at its default, which strips
          `javascript:` URLs — do not override it. */}
      <Markdown remarkPlugins={[remarkGfm]} components={BLOCKS}>{text}</Markdown>
    </div>
  )
}
```

- [ ] **Step 3 : le brancher aux deux endroits de `Game.tsx`**

Remplacer `Game.tsx:221-224` :

```tsx
            <div>
              <p className="label">Le modèle a répondu</p>
              <ModelOutput text={result.output} />
            </div>
```

et `Game.tsx:297-300` :

```tsx
                <details className="tally__more">
                  <summary className="tally__morebtn">Voir la réponse du modèle</summary>
                  <ModelOutput text={row.output} />
                </details>
```

Ajouter l'import en tête de `Game.tsx`, à côté de `import { ... } from './ui'` :
`import ModelOutput from './ModelOutput'`

- [ ] **Step 4 : styler les blocs**

Ajouter à `index.css`, après le bloc `.prose--cool`. Un markdown rendu est une suite de blocs :
il faut des marges **entre** eux, sinon tout est un mur. Pas de couleur propre au markdown —
`--ink` et `--ink-dim` viennent du design system.

```css
/* La sortie d'un modèle, rendue. Les mêmes tokens que `.prose`, et le rôle se joue sur les
 * espacements : un markdown sans marge entre blocs est un paragraphe unique illisible. */
.md {
  color: var(--ink);
  overflow-wrap: anywhere;
}
.md > *:first-child { margin-top: 0; }
.md > *:last-child { margin-bottom: 0; }
.md p { margin: 0.6rem 0; }
.md h1, .md h2, .md h3, .md h4 { margin: 0.9rem 0 0.4rem; line-height: 1.2; }
.md ul, .md ol { margin: 0.6rem 0; padding-left: 1.3rem; }
.md li { margin: 0.2rem 0; }
.md blockquote {
  margin: 0.6rem 0;
  padding-left: 0.7rem;
  border-left: 2px solid var(--cool);
  color: var(--ink-dim);
}
.md code {
  font-family: ui-monospace, monospace;
  font-size: 0.9em;
}
.md pre {
  margin: 0.6rem 0;
  padding: 0.6rem 0.7rem;
  overflow-x: auto;
  background: var(--panel);
}
.md pre code { font-size: 0.8125rem; }
.md table { border-collapse: collapse; margin: 0.6rem 0; }
.md th, .md td { border: 1px solid var(--line); padding: 0.3rem 0.5rem; text-align: left; }
.md a { color: var(--cool-lit); }
/* Pendant la génération : le texte arrive brut, sans balises. Même police que le markdown
 * rendu pour que la bascule à la fin ne soit pas un saut de casse. */
.md--raw { margin: 0; font-family: inherit; white-space: pre-wrap; }
```

**Avant de coller ce bloc :** vérifier que `--panel`, `--line` et `--cool-lit` existent bien
dans `:root` de `index.css`. Si `--line` n'existe pas, utiliser la couleur de bordure déjà
 employée par `.tally__head` ou `table` existant — ne pas inventer un token.

- [ ] **Step 5 : compiler et lint**

Run: `cd frontend && bun run build && bun run lint`
Expected: build OK, `✓ built in …`, oxlint sans avertissement.

- [ ] **Step 6 : vérifier à l'œil, deux cas**

Run: `./run.sh` puis ouvrir une partie, envoyer un prompt **containing markdown**, ex.
`liste 3 recettes en markdown, avec **gras**`, et lire le verdict.

Expected: listes et gras rendus. Vérifier ensuite le `<details>` du résumé.

- [ ] **Step 7 : commit**

```bash
git add frontend/package.json frontend/bun.lock frontend/src/ModelOutput.tsx frontend/src/Game.tsx frontend/src/index.css
git commit -m "feat(ui): the model's answer reads as markdown, not as a pre block"
```

---

## Phase B — Streaming

### Task B1 : `llm.answer_stream`, la génération en chunks

**Files:**
- Modify: `backend/game/llm.py:135-138` (`_stub_answer`), `backend/game/llm.py:194-205` (`_chat`), `backend/game/llm.py:228-231` (`answer`)
- Test: `backend/game/tests/test_llm.py`

**Interfaces:**
- Consumes: rien.
- Produces: `llm.answer_stream(prompt: str) -> Iterator[str]`, un générateur de deltas
  (fragments de texte, jamais vides). `llm.answer` **disparaît**. Les 20 mocks qui le patchent
  doivent migrer — tâche B2.

- [ ] **Step 1 : écrire le test qui échoue**

Dans `test_llm.py`, à côté des tests existants de `llm.answer` (lignes 87, 101, 197, 201) :

```python
    def test_answer_stream_yields_each_delta_then_nothing_empty(self) -> None:
        with patch.object(
            llm, "_chat", return_value=iter(["Bonjour", "", " le monde"])
        ) as m_chat:
            self.assertEqual(list(llm.answer_stream("salut")), ["Bonjour", " le monde"])
        self.assertTrue(m_chat.call_args.kwargs["stream"])

    def test_answer_stream_stub_chunks_the_echo(self) -> None:
        # No key: the dev loop still has to show progressive text, or the streaming path is
        # only ever exercised with a bill attached. Chunked on words so the chunks are not
        # one per character.
        with patch.object(llm, "_has_key", return_value=False):
            chunks = list(llm.answer_stream("repete 42"))
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), "(reponse locale) repete 42")
```

- [ ] **Step 2 : vérifier que ça échoue**

Run: `cd backend && uv run python manage.py test game.tests.test_llm`
Expected: FAIL — `AttributeError: module 'game.llm' has no attribute 'answer_stream'`.

- [ ] **Step 3 : `_chat` sait streamer**

```python
def _chat(
    model: str,
    messages: list[dict],
    temperature: float,
    json_mode: bool,
    max_tokens: int,
    stream: bool = False,
) -> Iterator[str] | str:
    # `stream=True` returns an iterator of deltas instead of one string, and the three call
    # sites want different things from it: only the play call streams, the judge and the
    # critic must have the WHOLE json before they can parse it, so they keep the string.
    client = OpenAI()
    kwargs = dict(model=model, messages=messages, temperature=temperature, max_tokens=max_tokens)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if stream:
        return (d for d in (c.choices[0].delta.content or "" for c in client.chat.completions.create(stream=True, **kwargs)) if d)
    return client.chat.completions.create(**kwargs).choices[0].message.content.strip()
```

- [ ] **Step 4 : remplacer `answer()`**

```python
def answer_stream(prompt: str) -> Iterator[str]:
    """The play call, as a stream of text deltas. The only way a player can watch it write.

    Never yields an empty delta: the transport drops them, and an empty frame is a round trip
    the player pays for in latency. Same cap as before (PLAY_MAX_TOKENS), same temperature —
    streaming changes when the words arrive, not what they are.
    """
    if not _has_key():
        return iter(_stub_chunks(prompt))
    return _chat(PLAY_MODEL, [{"role": "user", "content": prompt}], 0.7, False, PLAY_MAX_TOKENS, stream=True)
```

et à côté de `_stub_answer` :

```python
def _stub_chunks(prompt: str, words_per_chunk: int = 6) -> list[str]:
    # The stub streams too, deliberately: it is the only way the streaming path is exercised
    # with no key and no bill. Chunked on words, so the dev loop shows text arriving rather
    # than one character per frame — which is not what the real API does.
    text = _stub_answer(prompt)
    words = text.split(" ")
    return [" ".join(words[i : i + words_per_chunk]) + " " for i in range(0, len(words), words_per_chunk)]
```

- [ ] **Step 5 : supprimer `answer()`**

Le remplacer entièrement. Il n'a plus d'appelant après la tâche B3, et le laisser créerait un
chemin de génération que rien n'utilise et que rien ne teste.

- [ ] **Step 6 : les tests de `test_llm.py` qui appellent `answer()`**

Les lignes 87, 101, 197, 201 vérifient le texte **complet** rendu par le vrai chemin non
streamé. Ils passent de `llm.answer("…") == "…"` à
`"".join(llm.answer_stream("…")) == "…"` — et leur double `_completion` doit maintenant
produire un itérable de chunks. Vérifier le double ligne 19 et l'adapter : une completion
streamée est une suite d'objets ayant `.choices[0].delta.content`, pas `.message.content`.

- [ ] **Step 7 : la suite**

Run: `cd backend && uv run python manage.py test game.tests.test_llm`
Expected: PASS. Les autres fichiers échoueront encore (mocks) — c'est la tâche B2.

- [ ] **Step 8 : commit**

```bash
git add backend/game/llm.py backend/game/tests/test_llm.py
git commit -m "feat(llm): the play call streams, and the stub streams with it"
```

### Task B2 : un seul double de test pour les 20 mocks

**Files:**
- Create: `backend/game/tests/support.py`
- Modify: `backend/game/tests/test_play.py`, `test_rating.py`, `test_presence.py`, `test_admin.py`, `test_vote.py`

**Interfaces:**
- Consumes: `llm.answer_stream` (B1).
- Produces: `game.tests.support.answer_chunks(*texts: str) -> Callable[..., Iterator[str]]`.

- [ ] **Step 1 : écrire le double**

```python
"""The test doubles shared by the view tests. One place, because twenty copies of a stub is
twenty places for them to drift apart."""

from collections.abc import Iterator


def answer_chunks(*texts: str):
    """A stand-in for `llm.answer_stream`, used with `patch(..., new=...)`.

    `new=` and not `return_value=`: `return_value` hands every caller the SAME iterator, and
    the second caller of a test that posts twice would read an exhausted generator and see an
    empty answer. A function is re-entered instead.
    """

    def stream(*_args, **_kwargs) -> Iterator[str]:
        yield from texts

    return stream
```

- [ ] **Step 2 : migrer les mocks**

Dans les cinq fichiers, remplacer chaque
`patch("game.llm.answer", return_value="…")` par
`patch("game.llm.answer_stream", new=answer_chunks("…"))`, et
`mock.patch("game.llm.answer", return_value="…")` par
`mock.patch("game.llm.answer_stream", new=answer_chunks("…"))`. Ajouter l'import.

```bash
cd backend && uv run python manage.py test game 2>&1 | tail -20
```

Expected: les 5 fichiers de tests de vues passent de nouveau (ils échouent encore sur les
assertions de corps de réponse — tâche B3 — mais plus sur `AttributeError`/`StopIteration`).

- [ ] **Step 3 : commit**

```bash
git add backend/game/tests
git commit -m "refactor(tests): one stand-in for the streaming play call"
```

### Task B3 : `answer()` répond en SSE

**Files:**
- Modify: `backend/game/views.py:1440-1514`
- Test: `backend/game/tests/support.py`, `backend/game/tests/test_play.py`

**Interfaces:**
- Consumes: `llm.answer_stream` (B1), `llm.score`, `llm.grade_prompt`.
- Produces: `POST /api/rooms/<code>/answer` → `text/event-stream` avec les trames
  `chunk` / `scoring` / `done` / `error`. Et `game.tests.support.read_frames(response)`.

- [ ] **Step 1 : écrire le lecteur de trames et le test qui échoue**

Dans `support.py` :

```python
def read_frames(response) -> list[tuple[str, dict]]:
    """Every frame of a stream response, as (event, data) pairs.

    Walks the body rather than trusting a single read: a StreamingHttpResponse under the test
    client comes back already complete, and a reader that only looked at the first chunk would
    pass on the happy path and fail on the third delta.
    """
    raw = b"".join(response.streaming_content).decode()
    frames = []
    for block in raw.split("\n\n"):
        name, data = "message", ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if data:
            frames.append((name, json.loads(data)))
    return frames
```

Et dans `test_play.py`, à côté de `test_answer_scores_and_advances_when_everyone_answered` :

```python
    @patch("game.llm.grade_prompt", return_value=Verdict(7, "prompt correct"))
    @patch("game.llm.score", return_value=Verdict(8, "tres proche"))
    @patch("game.llm.answer_stream", new=answer_chunks("quarante-", "deux"))
    def test_answer_streams_the_text_then_the_verdict(self, m_score) -> None:
        _, ta = self._join("ana")
        self._play()
        self._online("ana")
        r = self._answer(ta, "repete 42")
        self.assertEqual(r["Content-Type"], "text/event-stream")
        self.assertEqual(
            read_frames(r),
            [
                ("chunk", {"delta": "quarante-"}),
                ("chunk", {"delta": "deux"}),
                ("scoring", {}),
                ("done", {"score": 8, "comment": "tres proche", "round": 0, "truncated": False}),
            ],
        )
        # The judge scores the STORED text, and the stored text is what the player watched:
        # one truth, not a streamed text and a scored text that can differ.
        m_score.assert_called_once_with("repete 42", "quarante-deux", "42")
        self.assertEqual(Answer.objects.get().output, "quarante-deux")

    @patch("game.llm.grade_prompt", return_value=Verdict(7, "prompt correct"))
    @patch("game.llm.score", side_effect=RuntimeError("le juge est tombe"))
    @patch("game.llm.answer_stream", new=answer_chunks("quarante-deux"))
    def test_a_failed_jury_streams_an_error_and_saves_nothing(self) -> None:
        _, ta = self._join("ana")
        self._play()
        self._online("ana")
        frames = read_frames(self._answer(ta, "repete 42"))
        self.assertEqual(frames[-1][0], "error")
        # Nothing persisted: the player is not credited with an answer nobody judged, and the
        # round must not close on it.
        self.assertEqual(Answer.objects.count(), 0)
        self.assertNotIn((self.room.rounds.first().pk, ...), _inflight)  # claim released
```

- [ ] **Step 2 : vérifier que ça échoue**

Run: `cd backend && uv run python manage.py test game.tests.test_play`
Expected: FAIL — le JSON arrive, `read_frames` ne trouve aucune trame `chunk`.

- [ ] **Step 3 : la vue**

Remplacer le corps de `answer()` après les validations (l'argument `-> JsonResponse` devient
`-> HttpResponse`). Tout ce qui précède — phase, token, round, doublon, prompt vide, claim
`_inflight` — **reste synchrone et inchangé**, pour que les 409 restent des 409 JSON.

```python
    def frame(name: str, **payload) -> str:
        return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def work() -> None:
        """The billed work, off the request thread.

        A phone that locks its screen closes this response. The generation has already been
        paid for at that point, and the round closes on the ANSWERS of the players who are
        here — so the answer has to be written even though nobody is left to read it. A
        generator that did the work inline would be closed by the disconnect and the round
        would wait forever for a phone that is asleep.
        """
        try:
            parts: list[str] = []
            for delta in llm.answer_stream(prompt):
                parts.append(delta)
                out.put(frame("chunk", delta=delta))
            output = "".join(parts).strip()
            # The judge runs on the SAME text the player just watched, joined from the same
            # deltas. Not the raw join: `.strip()` is what the non-streaming call did, and a
            # score computed on a text nobody saw is a score about nothing.
            out.put(frame("scoring"))
            verdict = llm.score(prompt, output, rnd.target)
            critique = llm.grade_prompt(prompt, rnd.target)
            with transaction.atomic():
                rnd.answers.create(
                    player=player, prompt=prompt, output=output,
                    score=verdict.score, comment=verdict.comment,
                    quality_llm=critique.score, quality_comment=critique.comment,
                )
            maybe_advance_phase(room)
            out.put(frame("done", score=verdict.score, comment=verdict.comment,
                          round=room.current, truncated=False))
        except Exception:  # noqa: BLE001 — one player's model call, reported to that player
            # Nothing is written, so the player may send again. The composer stays usable on
            # the client precisely because the row does not exist.
            out.put(frame("error", error="La partie a planté côté serveur. Préviens l’animateur."))
        finally:
            _inflight.discard(claim)
            out.put(None)  # the sentinel: the stream ends
            close_old_connections()

    out: queue.Queue = queue.Queue()
    threading.Thread(target=work, daemon=True).start()

    def stream():
        while True:
            item = out.get()
            if item is None:
                return
            yield item

    return StreamingHttpResponse(
        stream(), content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

et les imports : `import queue`, `import threading`,
`from django.db import close_old_connections`.

> **`truncated` :** `True` quand `finish_reason == "length"`. Implémenté dans B1 en exposant
> le motif de fin ; si le chunk OpenAI ne le porte pas dans cette version du SDK, la valeur
> reste `False` et le champ est quand même dans la trame — un contrat qui ne ment pas vaut
> mieux qu'un champ absent. **Ne pas** men's implement ce cas au-delà de `False` sans le
> motif de fin réel sous la main.

- [ ] **Step 4 : `_inflight` et la concurrence**

La claim est prise avant le thread et relâchée dans son `finally`. Un deuxième POST pendant la
génération reçoit `already answered` — inchangé, c'est déjà le comportement.

- [ ] **Step 5 : la suite**

Run: `cd backend && uv run python manage.py test game 2>&1 | tail -20`
Expected: **PASS**, 343+ tests. Les assertions de corps JSON de `test_play.py:99` sont à
adapter : `{"score":…,"comment":…,"output":…}` devient la trame `done`, et `output` n'y est
plus — il est dans les `chunk`. `test_play.py:99` est la seule assertion de ce genre ;
`test_presence.py` et `test_admin.py` vérifient des effets de bord, pas le corps.

- [ ] **Step 6 : commit**

```bash
git add backend/game/views.py backend/game/tests
git commit -m "feat(answer): the verdict streams, and the work survives a phone that locks"
```

### Task B4 : le client SSE côté front

**Files:**
- Modify: `frontend/src/api.ts`
- Test: aucun runner — `bun run build` + `bun run lint` + vérification à l'œil.

**Interfaces:**
- Consumes: les trames de B3.
- Produces: `streamAnswer<T>(path, body, on) : Promise<T>` où `on` reçoit les trames.

- [ ] **Step 1 : écrire la fonction**

```ts
// The answer, as it arrives. `fetch` rather than EventSource for two reasons that are the same
// reason: this is a POST that WRITES (the answer row, the phase, three billed calls), and
// EventSource is a GET that cannot carry a body. A GET that writes would put the player's
// token in a query string, in a proxy log, for a stream that has to be private.
//
// `res.body` is the floor, and it is a real one: iOS Safari 16.4 (March 2023). The check below
// is not defensive coding, it is the honest refusal — a phone that cannot read a stream gets
// told, rather than a button that spins forever.
export async function streamAnswer<T>(
  path: string,
  body: unknown,
  on: { chunk: (delta: string) => void; scoring: () => void },
): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: controller.signal,
  })
  // A refusal is decided BEFORE the stream starts, so it is still a JSON error with its 409.
  // Everything after this line is a 200 whose body is the answer.
  if (!res.ok) throw new ApiError(res.status, ((await res.json().catch(() => ({}))) as { error?: string }).error ?? `HTTP ${res.status}`)
  if (!res.body) throw new ApiError(0, 'stream unsupported')
  ...
}
```

Le reste : `TextDecoder` en `stream: true`, un buffer `let buf = ''` découpé sur `'\n\n'`,
`on.chunk` sur `event: chunk`, `on.scoring()` sur `event: scoring`, `resolve` du `data` de
`event: done`, `reject(new ApiError(0, data.error))` sur `event: error`. Une trame inconnue
est ignorée — elle n'est pas une erreur, et un `throw` dessus tuerait un flux qui marche.

- [ ] **Step 2 : `AbortController`**

Le controller est créé dans la fonction et exposé par le retour, pour que `Game.tsx` puisse
annuler au démontage. Un `fetch` annulé rejette avec une `AbortError` — **distincte** d'une
`ApiError`, et le front doit pouvoir dire « annulé » et non « planté ».

- [ ] **Step 3 : compiler et lint**

Run: `cd frontend && bun run build && bun run lint`
Expected: propre.

- [ ] **Step 4 : commit**

```bash
git add frontend/src/api.ts
git commit -m "feat(api): read the answer as a stream, and refuse a browser that cannot"
```

### Task B5 : le joueur voit la réponse s'écrire

**Files:**
- Modify: `frontend/src/Game.tsx:141-177`, `212-226`

**Interfaces:**
- Consumes: `streamAnswer` (B4), `ModelOutput` (A1).

- [ ] **Step 1 : le câblage**

Dans `send()` : remplacer le `post` par `streamAnswer`, alimenter un état
`live: { text: string; round: number } | null`, appeler `on.scoring` pour passer le libellé du
bouton à « Le jury délibère… ». Le verdict final reste construit à partir de la trame `done`
comme avant, **plus** `result.output = live.text` — donc `ModelOutput` continue d'être appelé
avec `streaming={false}`.

- [ ] **Step 2 : le panneau**

Rendre `live` dans le même `.verdict` que le score final, **sans** le score : un panneau avec
un `0` ou un `—` en note pendant que le texte s'écrit serait un mensonge. Le texte brut, puis
le verdict.

- [ ] **Step 3 : l'erreur**

Une `AbortError` ne dit rien au joueur. Une autre erreur laisse le compositeur utilisable
puisque rien n'a été écrit — **et** `setSent(false)`, sinon le bouton reste « Réponse envoyée »
pour une réponse que le serveur n'a jamais eue.

- [ ] **Step 4 : compiler, lint, et vérifier à l'œil**

Run: `cd frontend && bun run build && bun run lint`, puis une partie réelle.

Expected sans clé API : le texte apparaît **par morceaux** puis se rend en markdown à la fin,
et le commentaire dit bien que c'est une règle locale. Avec `ctrl+C` sur le backend pendant
une génération : le joueur voit « La partie a planté côté serveur », peut renvoyer, et le
round se ferme normalement.

- [ ] **Step 5 : commit**

```bash
git add frontend/src/Game.tsx
git commit -m "feat(game): the answer arrives while it is being written"
```

### Task B6 : le README

**Files:**
- Modify: `README.md`

- [ ] **Step 1 : ce que le README doit dire**

Que la réponse s'écrit à l'écran, que le mode sans clé stream lui aussi (donc qu'on peut voir
la mécanique sans payer), que le jury parle **après** le texte — deux appels distincts, d'où
la pause, et que la pause n'est pas un plantage.

- [ ] **Step 2 : commit**

```bash
git add README.md
git commit -m "docs: the answer is written on screen, and the pause after it is the jury"
```

---

## Le chemin de preuve

**Ce qui doit devenir vrai :** un joueur qui envoie un prompt voit sa réponse s'écrire, la lit
en markdown, et le contrat de manche / note / phase ne bouge pas.

| Claim | Preuve | Limite |
|---|---|---|
| Les 4 trames et leur ordre | `test_answer_streams_the_text_then_the_verdict` | — |
| Le texte lu = le texte noté | `m_score.assert_called_once_with(…, "quarante-deux", …)` + `Answer.objects.get().output` | — |
| Un jury planté n'écrit rien | `test_a_failed_jury_streams_an_error_and_saves_nothing` | — |
| Une déconnexion n'annule pas la note | Test à écrire dans B3 : consuming le générateur à moitié puis `close()`, et vérifier que la ligne `Answer` existe. **Ce test est la preuve directe du choix du thread ; sans lui, le thread est une affirmation.** | Non testé de bout en bout dans un vrai navigateur |
| Pas de HTML injecté | Pas de runner JS : revue de source (`grep -rn dangerouslySetInnerHTML frontend/src` doit être vide) + un prompt réel demandant `<img src=x onerror=…>` affiché **littéralement**. Ajout de vitest = changement structurel pour une preuve seulement : **demandé avant**. | Revue, pas test |
| Les 343 tests existants ne bougent pas | `uv run python manage.py test game` | — |
| Le markdown est lisible au doigt | Vérification à l'œil sur un téléphone, pas sur le bureau | Aucun test de rendu |

## Ce que ce plan ne prouve pas

- Un vrai téléphone iOS 16.3 (sous le plancher `ReadableStream`). Non évité, documenté.
- La reprise après une coupure réseau **pendant** le flux : la réponse est perdue pour ce
  joueur, la manche continue, le résumé la montrera. Choix assumé — un protocole de reprise
  pour un jeu de classe est unendy_cost disproportionate. Non testé.
- Le comportement de `finish_reason` tant que le SDK n'est pas lu (champ `truncated` câblé à
  `False`).