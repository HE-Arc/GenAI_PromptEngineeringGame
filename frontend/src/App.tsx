import { useEffect, useState } from 'react'
import Join from './Join'
import Lobby from './Lobby'
import Admin from './Admin'
import Game from './Game'
import Results from './Results'
import { useRoom } from './useRoom'

function route() {
  const h = location.hash.replace(/^#\/?/, '')
  const [head, tail] = h.split('/')
  if (head === 'admin') return { screen: 'admin' as const, code: null }
  if (head === 'room' && tail) return { screen: 'room' as const, code: tail.toUpperCase() }
  return { screen: 'join' as const, code: null }
}

export default function App() {
  const [{ screen, code }, setRoute] = useState(route)
  const { state, connected } = useRoom(code)

  useEffect(() => {
    const on = () => setRoute(route())
    addEventListener('hashchange', on)
    return () => removeEventListener('hashchange', on)
  }, [])

  const go = (next: string) => {
    location.hash = next
  }

  if (screen === 'admin') return <Admin go={go} />
  if (screen === 'join') return <Join onJoined={(c) => go(`/room/${c}`)} />
  if (!state) return <div className="card muted">{connected ? 'Chargement...' : 'Connexion...'}</div>
  if (state.status === 'waiting') return <Lobby state={state} />
  if (state.status === 'done') return <Results state={state} />
  return <Game state={state} code={code!} />
}
