import type { State } from './api'

export default function Results({ state }: { state: State }) {
  const [first] = state.players
  return (
    <div className="card">
      <h1>Partie terminee</h1>
      {first ? (
        <p className="target">
          🏆 {first.name} - {first.score} pts
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
