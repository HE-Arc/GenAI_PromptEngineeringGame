import { useEffect, useState } from 'react'
import Join from './Join'
import Lobby from './Lobby'
import Admin from './Admin'
import Game from './Game'
import Results from './Results'
import { ConsoleBar } from './ui'
import { getToken } from './api'
import { useRoom, type Link } from './useRoom'

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

// A room is only meaningful to a player who is in it, and the token is the proof. Someone
// who opens a shared /room link, or who bookmarks one by hand, has none — so instead of
// hanging on a stream they can never join, they get the join form with the code already
// filled in. That also covers the most common wrong-code case before it can cost a reload.
function Gate({ code, onJoined }: { code: string; onJoined: (code: string) => void }) {
  return <Join preset={code} onJoined={onJoined} />
}

// Reached when the stream has failed three times over without ever opening. The browser
// retries on its own and we let it, so this is the state for a room that is not answering at
// all, with a way back instead of an indefinite "Connexion…". It deliberately does not claim
// the room is missing: EventSource cannot read a status code, so all we know is that nothing
// came back, and a server restarting on a laptop looks exactly like a bad code. It never asks
// for a code or a name — reconnecting is the server's job and needs nothing from the player.
function Fault({ code, onRetry, onLeave }: { code: string; onRetry: () => void; onLeave: () => void }) {
  return (
    <div className="deck">
      <ConsoleBar meta={`Partie ${code}`} />
      <main className="screen screen--solo">
        <h1 className="title">La partie ne répond pas</h1>
        <p className="prose prose--dim">
          Rien n’est revenu de la partie <strong>{code}</strong>. Si la machine qui l’héberge
          s’est éteinte ou a redémarré, elle répondra dès qu’elle sera relancée. Sinon,
          vérifie que tu es toujours sur le même réseau.
        </p>
        <div className="actions">
          <button type="button" className="btn btn--signal btn--wide" onClick={onRetry}>
            Réessayer
          </button>
          <button type="button" className="btn btn--ghost btn--wide" onClick={onLeave}>
            Saisir un autre code
          </button>
        </div>
      </main>
    </div>
  )
}

export default function App() {
  const [{ screen, code }, setRoute] = useState(route)
  // The stream is identified by the player's own token, so the server can tell whose phone is
  // on it. Without it every player reads as offline, this one included. The admin passes no
  // token at all and connects anonymously, which is the difference between a missing
  // argument and a null one.
  const token = code ? getToken(code) : null
  const { state, link, retry } = useRoom(code, token)

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

  // No navigation callback: the console never leaves /admin. Every exit is a state reset in
  // Admin, which is why nothing here has to hand it a router.
  if (screen === 'admin') return <Admin />
  if (screen === 'join') return <Join onJoined={(c) => go(`/room/${c}`)} />

  // No token means this phone is not, or was never, in the room. The join form comes back
  // with the code already filled in, which also covers someone following a shared link
  // before they have joined, at no cost.
  if (!token) return <Gate code={code!} onJoined={(c) => go(`/room/${c}`)} />
  if (link === 'failed') return <Fault code={code!} onRetry={retry} onLeave={() => go('/')} />
  if (!state) return <Connecting code={code!} link={link} />
  if (state.status === 'waiting') return <Lobby state={state} link={link} onRetry={retry} onLeave={() => go('/')} />
  if (state.status === 'done') return <Results state={state} link={link} onRetry={retry} onLeave={() => go('/')} />
  return <Game state={state} code={code!} link={link} onRetry={retry} />
}

// The stream is open but no frame has arrived yet. This is the only state that is genuinely a
// wait, and it is bounded: the hook gives up after three misses and routes to `Fault`.
// Deliberately almost empty — the rail already carries the lamp and the word, and a third
// copy of "connecting" is the one thing a screen this short does not need.
function Connecting({ code, link }: { code: string; link: Link }) {
  return (
    <div className="deck">
      <ConsoleBar meta={`Partie ${code}`} link={link} />
      <main className="screen screen--solo">
        <p className="prose prose--dim">Lecture du tableau…</p>
      </main>
    </div>
  )
}
