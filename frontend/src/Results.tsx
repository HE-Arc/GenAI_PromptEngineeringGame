import { Board, ConsoleBar, Section, type BoardColumn } from './ui'
import { getName, type Player, type State } from './api'
import type { Link } from './useRoom'

/* The final standings. Four columns — player, proximity, quality, total — because the game now
 * scores two things and a single total cannot say which of them a player was good at. */

// The two dimension totals are not in the contract: `Player` carries the final score and the
// current round's, and nothing else, while the final board is specified as four columns. They
// are read out of the payload anyway and printed as "—" when absent, rather than the board
// being quietly drawn with two columns. A zero would be a score nobody earned; a blank cell in a
// column of numbers reads as a missing value and invites the inference the columns exist to
// stop. The day the server adds them this file changes not at all.
type Measured = Player & { proximity?: number | null; quality?: number | null }

const COLUMNS: BoardColumn[] = [
  { label: 'Proximité', value: (p) => (p as Measured).proximity ?? null },
  { label: 'Qualité', value: (p) => (p as Measured).quality ?? null },
]

export default function Results({
  state,
  link,
  onRetry,
  onLeave,
}: {
  state: State
  link: Link
  onRetry: () => void
  onLeave: () => void
}) {
  const [first] = state.players
  // A tie is a tie: the server's sort picks a display order (join order among equals), which
  // is not a reason to hand one of the tied players a trophy the other did not lose.
  const winners = first === undefined ? [] : state.players.filter((p) => p.score === first.score)
  // `score` is null outside a resolved round, and the last screen of a game can therefore find a
  // player with no total at all — a player who never reached a scored round. It is read as zero
  // and the winner's sentence says nothing about points when there is nothing to count, rather
  // than announcing "l'emporte avec null".
  const points = first?.score ?? 0

  // The winner is a sentence, not a slab. The illuminated panel is spent on the target and
  // the room code, and it is worth more for that: a player who has just seen it ten times
  // stops seeing it. Here the board itself carries the result, with the leading row in
  // sodium, so nothing extra has to be invented to announce it.
  const verdict =
    points === 0
      ? 'Aucun tour n’a été noté dans cette partie.'
      : winners.length === 1
        ? `${winners[0].name} l’emporte avec ${points} ${points > 1 ? 'points' : 'point'}.`
        : `Égalité entre ${winners.map((p) => p.name).join(', ')}, avec ${points} ${points > 1 ? 'points' : 'point'}.`

  return (
    <div className="deck">
      <ConsoleBar meta={`Partie ${state.code}`} link={link} onRetry={onRetry} />
      {/* The skip link's target — see the note on `SkipLink` in App.tsx. */}
      <main className="screen screen--split" id="main" tabIndex={-1}>
        <div>
          {/* `tabIndex` so the route-change effect in App.tsx can land focus here. This is
              the last screen of a game, so it is the one place a round ending swaps the whole
              view out from under a keyboard user; without a focusable target the swap is
              silent, and focus is left on a control that is no longer in the document. Every
              other screen in the console already carries this on its heading. */}
          <h1 className="title" tabIndex={-1}>
            Partie terminée
          </h1>
          <p className="prose prose--dim">{winners.length ? verdict : 'Aucun joueur dans la partie.'}</p>
        </div>

        {/* The winner is a sentence and the standings are the evidence, so on a wide screen
            they sit side by side with the board given a column of its own to stretch into. */}
        <div className="railcol">
          <Section title="Classement final">
            <Board players={state.players} myName={getName(state.code)} columns={COLUMNS} />
          </Section>
        </div>

        <div className="foot">
          <span className="foot__code">Partie {state.code}</span>
          <button type="button" className="btn btn--ghost foot__end" onClick={onLeave}>
            Retour à l’accueil
          </button>
        </div>
      </main>
    </div>
  )
}
