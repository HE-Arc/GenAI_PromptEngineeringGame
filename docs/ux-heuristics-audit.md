# UX heuristic audit

Date: 2026-11-06. Scope: player flow (Join, Lobby, Quiz, Game, Rating, Results) and admin console copy.
Method: Krug's laws and Nielsen's 10 heuristics, severity 0-4. Code review only, no user test.

## Score: 7/10

Quick diagnostic:

| Check | Result |
|---|---|
| Site and page identifiable | Partial: most screen titles are visually hidden (#7) |
| Main action obvious | Pass |
| Navigation clear | Pass (linear flow, server-driven) |
| Search | N/A |
| System status visible | Pass: reconnect notices, live regions, spinners, timers |
| Error messages helpful | Partial: raw server text can leak (#3) |
| Undo / back | Pass: re-tap changes a grade, confirm dialogs name irreversibility |
| Works without hover | Pass, except the clock is pointer-only to move (#6) |
| Controls labelled | Pass |
| Nothing makes you think "huh?" | Fail: copy contradicts the scoring rules (#1, #2) |

Strengths worth keeping: one primary action per screen, rating by single tap with re-tap to change,
the stale/missed-round notices, the own-prompt rule explained before it happens, confirm modals that
state "Impossible de revenir en arrière".

## Findings

### 1. Copy describes a scoring model that no longer exists. Severity 3
Heuristic: match with the real world, Krug law 1.
- `frontend/src/Admin.tsx:910`: the "Passer la notation" dialog says unrated prompts "gardent la note
  de l'IA". AI grading was removed (BUGFIXES task 3). An animator who skips rating is told the wrong
  outcome.
- `frontend/src/Join.tsx:69-71`: "Un jury note la ressemblance sur 10, et toi aussi tu joues les dix
  manches." Peers grade against criteria, not "ressemblance"; "jury" and "toi aussi" are unclear.

Fix: Admin: "Les prompts pas encore notés restent sans note." Join: "Le modèle doit produire un
résultat précis. À toi d'écrire le prompt qui l'y amène. Les autres joueurs notent ton prompt sur 10."

### 2. Summary screen contradicts itself in auto mode. Severity 2
Heuristic: consistency. `frontend/src/Game.tsx:704-709` always says "L'animateur ouvre la suivante",
while `:614-618` says "Mode automatique : la suite s'enchaîne seule." Both render together, and the
first also repeats `:609-613` in manual mode.
Fix: delete the `scored &&` paragraph at `:704-709`; the status line above covers both modes.

### 3. Unknown server errors show raw English text. Severity 2
Heuristic: error recovery. `frontend/src/api.ts` `describe()` ends with `return e.message`. Any
reason missing from `REASONS` (for example a 429 from the rate limiter, unverified) reaches a French
player as English.
Fix: handle 429 ("Trop de tentatives. Réessaie dans quelques secondes.") and return a generic
French sentence instead of `e.message`.

### 4. Repeated text. Severity 2
Krug law 3 (get rid of half the words).
- Quiz: the scoring rule "Chaque bonne réponse vaut 1 point… seul le contenu compte" shows above
  every question (`Quiz.tsx:315-320`) and again in the final board (`:548-551`). Keep it once, on the
  first question or the final board.
- Lobby: "L'animateur lance la partie quand tout le monde est là" (`Lobby.tsx:76-77`) and
  "seul l'animateur peut lancer la partie" (`:101`). Keep one.
- Lobby: `Roster` and `PlayerCloud` (`:96-98`) both show who is present.
- Conversation rounds: the "toute la conversation est notée" rule appears in the thread intro
  (`Game.tsx:653-657`) and again in the result panel (`:488-491`).

### 5. Composer hints are noise on phones. Severity 2
Heuristic: minimalist design. `Game.tsx:458-463` always shows "0 / 4096" and the Ctrl/⌘ + Entrée hint.
The limit is rarely near and phones have no Ctrl key.
Fix: show the counter only past ~80 % of `MAX_PROMPT_CHARS`; hide the shortcut hint under
`@media (pointer: coarse)`.

### 6. Floating clock can cover controls. Severity 2
Heuristic: user control. `ui.tsx` `PhaseClock` is `position: fixed`, movable only by pointer drag, and
its position persists across screens. A clock parked over the rating grid or the submit button on one
screen stays there on the next. No keyboard move and no reset.
Fix: double-click (and Enter when focused) resets to the default corner; or clamp it away from the
bottom action zone.

### 7. Screen titles are hidden. Severity 1
Trunk test, "what page is this?". Lobby, Quiz, Game and Rating headings are `visually-hidden`. The
panel labels mostly compensate, but the jump from writing to rating has no visible title.
Fix: a small visible phase label in the `ConsoleBar` meta (for example "Partie K7TQ · Notation").

### 8. Logo leaves a running game without warning. Severity 1
Heuristic: error prevention. The `ConsoleBar` logo navigates to `/` mid-round. The token survives, so
Back recovers, but nothing says so.
Fix: on room screens, render the logo as plain text, or confirm before leaving.

### 9. Small copy slips. Severity 1
- `Game.tsx:176`: "Réponses envoyées" for the player's own single answer; use "Réponse envoyée".
- `Lobby.tsx:82`: "Partager le code" shares a link; use "Partager le lien".

## Path to 10/10
Fix #1-#3 (score 9), then #4-#6 (score 10). #7-#9 are polish. Validate with 3-5 students in one
real session: watch the Join screen, the first rating, and the summary in auto mode.
