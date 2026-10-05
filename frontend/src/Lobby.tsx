import { useState } from 'react'
import { ConsoleBar, Readout, ReadyBar, Roster } from './ui'
import { describe, getName, getToken, postReady, type State } from './api'
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
              Lis ce code à voix haute pour que les autres te rejoignent. Quand tout le monde est
              là, appuie sur « Je suis prêt » : c’est la classe qui démarre la partie.
            </p>

            <div className="actions">
              <button type="button" className="btn btn--signal btn--wide" onClick={() => void share()}>
                {shared === 'copied' ? 'Code copié' : shared === 'sent' ? 'Code partagé' : 'Partager le code'}
              </button>
            </div>

            {/* Said here and not in the rail: this is the answer to the button that was just
                pressed, so it belongs under that button. `.err--inline` rather than `.err` for
                the same reason the form's own error is inline — it is one line against a
                control, not a fault panel.
                One line for both controls on this column, which is the point of not inventing a
                second kind of message when the ready vote arrived: a refused share paints under
                the share button and a refused vote paints immediately above the bar, and a
                player who has just pressed either of them reads the same sentence in the same
                place on the same screen. */}
            {err && <p className="err err--inline" role="alert">{err}</p>}

            {/* The class's own vote on starting, the same bar the three screens that discuss
                draw. At the foot of this column rather than in the rail under the roster: the
                rail is where the room's *information* goes — who is here, and the count the bar
                itself draws — and a second thing to press down there would put the one decision
                of the whole lobby in the same compartment as « Quitter la partie », behind the
                same rule. A button that starts a game for thirty people reads as a setting for
                the phone it is on, and the destructive action's own border would be the only
                thing telling the class otherwise.
                Here it is the last thing the player does, and that is the order the first minute
                goes in: read the code, send it, say you are ready. It also holds its place on a
                phone, where the two columns stack and the roster is the one part of this screen
                that grows without limit — under it, « Je suis prêt » would be the last thing
                below a thirty-name fold instead of the third thing on the way in.
                Nothing here tests the phase: the bar renders itself away while `state.ready` is
                null, so the sentence above is a promise the screen keeps only once the server
                publishes a discussion for `waiting` — which is the server's decision and not
                something this screen may second-guess by branching on `state.phase`.
                No live region is added around it either. `ReadyBar` already carries the count in
                its own polite region, because other phones pressing is the one thing on this
                bar that moves without this player doing anything; a second region saying the
                same words would read it out twice, and the roster's « n joueurs connectés » is
                a different fact — who is in the room, not who has voted — so the two do not
                collide. */}
            <ReadyBar
              ready={state.ready}
              onReady={async () => {
                try {
                  await postReady(state.code, getToken(state.code) ?? '')
                  // `true` means the server has the vote. The bar cannot tell a success from a
                  // refusal on its own — this screen reports a refusal by writing to `setErr`
                  // and RESOLVING — so this answer is what lets it close its own double-tap
                  // window instead of painting a refusal under a vote the room already holds.
                  return true
                } catch (e) {
                  // `describe`, not a fixed sentence: the refusals are different facts with
                  // different repairs, and « the vote failed » is none of them. A vote the server
                  // already had and a room that has already started both arrive here as a 409
                  // with a reason attached.
                  setErr(describe(e))
                  return false
                }
              }}
            />
          </div>
        </div>

        <div className="railcol">
          {/* No names and no points on this screen, on purpose, and the reason is that both are
              the wrong shape for the one minute this screen owns: read the code, send it, say
              you are ready.
              THE POINTS. `score` is `settled + open_round` summed over a player's answers, and
              this screen only exists in `waiting` — a room with no rounds and no answers yet —
              so every one of them is a hard 0 produced by arithmetic over an empty set. It is
              a real score, not a missing one, which is exactly why it cannot be dressed up as
              « — » either: the dash means the server is withholding, and nothing is being
              withheld here. So the two obvious-looking moves are both wrong: a column of twenty
              zeros says nothing and, worse, trains twenty phones to look past that figure
              before the first point of the game exists; and a labelled « points » column is the
              same twenty zeros wearing a sign. The number arrives where it can mean something —
              the board, from the first settled round — which is one screen away and not one
              state away.
              THE NAMES. The roster list exists on this console because the HOST has a decision
              a name goes into: force the round on, or wait for this person. A player has no
              such decision here — their only lever is their own ready vote, which the bar above
              already draws — so a named roster would be information with no question behind it.
              It would also be the expensive kind: the compact count and lamp meter is the
              screen's answer to "is everyone here", and swapping it for a list that grows with
              every arrival puts « Je suis prêt » back underneath a thirty-name fold, which is
              the exact placement the bar's own comment was written to prevent.
              Nothing is lost by waiting: the player still finds their own name, flagged « toi »,
              on the board the moment the game starts. */}
          {/* The number is the one on the stream, not the size of the roster — a phone asleep
              in a pocket is a player who is in the game and not in the room. */}
          <Roster players={state.players} online={state.online_count} myName={getName(state.code)} />

          <div className="foot">
            {/* The one line on this screen that names who starts the game, and it now names two
                of them. It used to say the animator and was the only sentence in the console
                claiming a job the class could not do; leaving « l’animateur » as the only subject
                would now be the opposite lie — a player would sit on « Je suis prêt » believing it was
                decoration while the bar above them counts the room down. Both, because both are
                true: the host can still press start, and that is worth saying out loud for the
                player who would rather not wait for the room. */}
            <span className="foot__code">En attente de démarrage : l’animateur ou toute la classe peuvent lancer la partie.</span>
            <button type="button" className="btn btn--ghost foot__end" onClick={onLeave}>
              Quitter la partie
            </button>
          </div>
        </div>
      </main>
    </div>
  )
}
