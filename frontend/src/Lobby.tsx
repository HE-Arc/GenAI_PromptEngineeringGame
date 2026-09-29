import { useState } from 'react'
import { ConsoleBar, Readout, Roster } from './ui'
import { getName, type State } from './api'
import type { Link } from './useRoom'

export default function Lobby({
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
  const [shared, setShared] = useState<'no' | 'yes'>('no')

  // The code is spoken across a room, not tapped. Sharing hands the whole sentence over
  // WhatsApp when the phone allows it, and falls back to a copy when it does not.
  async function share() {
    const text = `Rejoins la partie Jeu GenAi : http://${location.host}/room/${state.code}`
    try {
      if (navigator.share) await navigator.share({ text, url: `${location.origin}/room/${state.code}` })
      else await navigator.clipboard.writeText(text)
      setShared('yes')
    } catch {
      // A share sheet the player dismissed, or a clipboard the browser refused. Neither is
      // worth an error line: the code is on screen either way, and that is the whole job.
      setShared('no')
    }
  }

  return (
    <div className="deck">
      <ConsoleBar link={link} onRetry={onRetry} />
      <main className="screen">
        <Readout label="Code de la partie" value={state.code} code />

        {/* The second thing a player checks, right after the code: is everyone actually here?
            The number is the one on the stream, not the size of the roster — a phone asleep
            in a pocket is a player who is in the game and not in the room. */}
        <Roster players={state.players} online={state.online_count} myName={getName(state.code)} />
        <p className="prose prose--dim">
          Lis ce code à voix haute pour que les autres te rejoignent, puis garde l’écran ouvert :
          la partie démarre ici.
        </p>

        <div className="actions">
          <button type="button" className="btn btn--signal btn--wide" onClick={() => void share()}>
            {shared === 'yes' ? 'Code copié' : 'Partager le code'}
          </button>
        </div>

        <div className="foot">
          <span className="foot__code">En attente du démarrage par l’animateur.</span>
          <button type="button" className="btn btn--ghost foot__end" onClick={onLeave}>
            Quitter la partie
          </button>
        </div>
      </main>
    </div>
  )
}
