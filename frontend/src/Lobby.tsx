import type { State } from './api'

export default function Lobby({ state }: { state: State }) {
  return (
    <div className="card">
      <h1>Room {state.code}</h1>
      <p className="muted">En attente du lancement par l admin...</p>
      <ul className="players">
        {state.players.map((p) => (
          <li key={p.name}>{p.name}</li>
        ))}
      </ul>
    </div>
  )
}
