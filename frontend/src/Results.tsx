import { Board, ConsoleBar, Section } from './ui'
import { getName, type State } from './api'
import type { Link } from './useRoom'

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
  const points = first === undefined ? 0 : first.score

  // The winner is a sentence, not a slab. The illuminated panel is spent on the target and
  // the room code, and it is worth more for that: a player who has just seen it ten times
  // stops seeing it. Here the board itself carries the result, with the leading row in
  // sodium, so nothing extra has to be invented to announce it.
  const verdict =
    winners.length === 1
      ? `${winners[0].name} l’emporte avec ${points} ${points > 1 ? 'points' : 'point'}.`
      : `Égalité entre ${winners.map((p) => p.name).join(', ')}, avec ${points} ${points > 1 ? 'points' : 'point'}.`

  return (
    <div className="deck">
      <ConsoleBar meta={`Partie ${state.code}`} link={link} onRetry={onRetry} />
      <main className="screen">
        <div>
          <h1 className="title">Partie terminée</h1>
          <p className="prose prose--dim">{winners.length ? verdict : 'Aucun joueur dans la partie.'}</p>
        </div>

        <Section title="Classement final">
          <Board players={state.players} myName={getName(state.code)} />
        </Section>

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
