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
  // Which branch actually did the work. A share sheet sends the sentence to someone else and
  // the button is not claiming otherwise; a copy puts it on this phone's clipboard. The old
  // single 'yes' said "Code copié" on the share path too, which is a claim the browser had not
  // made.
  const [shared, setShared] = useState<'no' | 'sent' | 'copied'>('no')
  const [err, setErr] = useState('')

  // The documented deployment is http://<IP-LAN>:5173, and plain http on a LAN address is not
  // a secure context. Both `navigator.share` and `navigator.clipboard` are secure-context APIs
  // and on that origin both are simply undefined — so the documented way of playing this game
  // is the one way its only button cannot work, and the failure landed in a bare catch and was
  // reported to the player as nothing at all.
  //
  // `execCommand('copy')` is deprecated and unfashionable and is also the only copy that is
  // still there on an insecure origin, which is the only copy this screen can rely on.
  function copyLegacy(text: string) {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    // Off-screen rather than display:none, because a textarea that is not laid out cannot be
    // selected and the selection is the copy. Pinned to the middle of the viewport rather than
    // the top of the document, because iOS scrolls a focused field into view and would
    // otherwise jump the page to a textarea parked at 0,0.
    ta.style.cssText = 'position:fixed;top:50%;left:50%;width:1px;height:1px;padding:0;border:0;opacity:0;'
    // Selecting the textarea takes the focus, and letting it go to <body> on cleanup would drop
    // a keyboard player out of the tab order on a control they had just pressed.
    const back = document.activeElement as HTMLElement | null
    document.body.appendChild(ta)
    let ok = false
    try {
      ta.select()
      ta.setSelectionRange(0, text.length)
      ok = document.execCommand('copy')
    } finally {
      ta.remove()
      back?.focus()
    }
    if (!ok) throw new Error('copy refused')
  }

  // The code is spoken across a room, not tapped. Sharing hands the whole sentence over
  // WhatsApp when the phone allows it, and falls back to a copy when it does not.
  async function share() {
    const url = `${location.origin}/room/${state.code}`
    const text = `Rejoins la partie Jeu GenAi : ${url}`
    setErr('')
    try {
      if (navigator.share) {
        // `text` already contains the full address. Passing `url` as well is what produced
        // "Rejoins la partie : http://… — http://…" in the sheet on every phone that showed
        // the two fields at once.
        await navigator.share({ text })
        setShared('sent')
        return
      }
      if (navigator.clipboard) await navigator.clipboard.writeText(text)
      else copyLegacy(text)
      setShared('copied')
    } catch (e) {
      // A share sheet the player dismissed is them saying no, not a fault, and reporting it
      // would train them to distrust the button. Anything else is the screen's only control
      // having done nothing, which used to be swallowed into a silent reset of the label — a
      // defect the player had no way to report, because nothing on screen had changed.
      if (e instanceof DOMException && e.name === 'AbortError') return
      setErr('Partage impossible sur ce navigateur. Le code reste affiché ici, recopie-le à la main.')
      setShared('no')
    }
  }

  return (
    <div className="deck">
      <ConsoleBar link={link} onRetry={onRetry} />
      {/* The skip link's target — see the note on `SkipLink` in App.tsx. */}
      <main className="screen screen--split" id="main" tabIndex={-1}>
        {/* This screen had no heading at all, which left a route change with nothing to focus
            and nothing to name the screen by. Hidden rather than drawn: the code panel is the
            first thing on the screen and an <h1> above it would only say the same thing
            louder. Its job is to exist — as the document's heading, and as the target focus
            lands on when the player arrives. */}
        <h1 className="visually-hidden" tabIndex={-1}>
          Lobby de la partie {state.code}
        </h1>
        {/* The two things a player checks on arrival, and neither is subordinate to the other:
            the code they read out loud, and whether everyone is actually here. They stack on
            a phone because there is no room; on a wide screen they are two columns. */}
        <div className="playgrid">
          <Readout label="Code de la partie" value={state.code} code />

          <div className="stack">
            <p className="prose prose--dim">
              Lis ce code à voix haute pour que les autres te rejoignent, puis garde l’écran
              ouvert : la partie démarre ici.
            </p>

            <div className="actions">
              <button type="button" className="btn btn--signal btn--wide" onClick={() => void share()}>
                {shared === 'copied' ? 'Code copié' : shared === 'sent' ? 'Code partagé' : 'Partager le code'}
              </button>
            </div>

            {/* Said here and not in the rail: this is the answer to the button that was just
                pressed, so it belongs under that button. `.err--inline` rather than `.err` for
                the same reason the form's own error is inline — it is one line against a
                control, not a fault panel. */}
            {err && <p className="err err--inline" role="alert">{err}</p>}
          </div>
        </div>

        <div className="railcol">
          {/* The number is the one on the stream, not the size of the roster — a phone asleep
              in a pocket is a player who is in the game and not in the room. */}
          <Roster players={state.players} online={state.online_count} myName={getName(state.code)} />

          <div className="foot">
            <span className="foot__code">En attente du démarrage par l’animateur.</span>
            <button type="button" className="btn btn--ghost foot__end" onClick={onLeave}>
              Quitter la partie
            </button>
          </div>
        </div>
      </main>
    </div>
  )
}
