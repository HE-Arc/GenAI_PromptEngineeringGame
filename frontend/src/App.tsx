import { useEffect, useState } from 'react'
import Join from './Join'
import Lobby from './Lobby'
import Admin from './Admin'
import Game from './Game'
import Results from './Results'
import { useRoom } from './useRoom'

// Real paths, not hash routes. `/admin` and `/room/<CODE>` are what an admin bookmarks,
// types and reads aloud, and they are what a server has to be able to serve — which is the
// shape Django hosting would need. Vite's SPA fallback answers both with index.html, and
// the dev proxy still claims /api before the fallback ever runs.
function route() {
  const path = location.pathname.replace(/^\/+|\/+$/g, '')
  const [head, tail] = path.split('/')
  if (head === 'admin') return { screen: 'admin' as const, code: null }
  if (head === 'room' && tail) return { screen: 'room' as const, code: tail.toUpperCase() }
  return { screen: 'join' as const, code: null }
}

export default function App() {
  const [{ screen, code }, setRoute] = useState(route)
  const { state, connected } = useRoom(code)

  useEffect(() => {
    const on = () => setRoute(route())
    addEventListener('popstate', on)
    return () => removeEventListener('popstate', on)
  }, [])

  const go = (next: string) => {
    // pushState, not a bare `location =` assignment: that would hit the server, so every
    // screen change would become a full reload.
    history.pushState(null, '', next)
    setRoute(route())
  }

  if (screen === 'admin') return <Admin go={go} />
  if (screen === 'join') return <Join onJoined={(c) => go(`/room/${c}`)} />
  if (!state) return <div className="card muted">{connected ? 'Chargement...' : 'Connexion...'}</div>
  if (state.status === 'waiting') return <Lobby state={state} />
  if (state.status === 'done') return <Results state={state} />
  return <Game state={state} code={code!} />
}
