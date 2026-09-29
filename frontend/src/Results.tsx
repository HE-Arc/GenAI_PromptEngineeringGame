import type { State } from './api'

export default function Results({ state }: { state: State }) {
  const [first] = state.players
  // A tie is a tie: the server's sort picks a display order (join order among equals), which
  // is not a reason to hand one of the tied players a trophy the other did not lose.
  const winners = first === undefined ? [] : state.players.filter((p) => p.score === first.score)
  return (
    <div className="card">
      <h1>Partie terminee</h1>
      {winners.length === 1 ? (
        <p className="target">
          🏆 {winners[0].name} - {winners[0].score} pts
        </p>
      ) : winners.length > 1 ? (
        <p className="target">
          🏆 Ex aequo : {winners.map((p) => p.name).join(', ')} - {winners[0].score} pts
        </p>
      ) : (
        <p className="muted">Aucun joueur.</p>
      )}
      <div className="rows">
        {state.players.map((p, i) => (
          <div className="row" key={p.name}>
            <span>
              {i + 1}. {p.name}
            </span>
            <span className="score">{p.score}</span>
          </div>
        ))}
      </div>
      <p className="muted">Room {state.code}</p>
    </div>
  )
}
