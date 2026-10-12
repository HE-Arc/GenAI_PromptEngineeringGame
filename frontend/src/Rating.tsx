import { useState, type CSSProperties, type FormEvent, type KeyboardEvent } from 'react'
import { AnswerMeter, ConsoleBar, PhaseClock, ReadyBar, RoundTrack, ScoreScale } from './ui'
import { ApiError, describe, getName, getToken, post, postReady, SCALE_MAX, SCALE_MIN, type State } from './api'
import type { Link } from './useRoom'
import { buzz } from './fx'
import { TargetText } from './ModelOutput'

/* The cross-rating screen. The most constrained surface in the console, and the constraints are
 * the feature: a prompt, the target it was written against, and one number to give. */

export default function Rating({
  state,
  code,
  link,
  onRetry,
}: {
  state: State
  code: string
  link: Link
  onRetry: () => void
}) {
  const [picked, setPicked] = useState<number | null>(null)
  // The reveal this player has already scored. The stream moves the reveal on when the room is
  // done, not when this player is done, so the screen has to know which prompt its button would
  // rate — a phone that sleeps through the change would otherwise send a second opinion on the
  // next prompt it sees.
  const [seen, setSeen] = useState<number | null>(null)
  // Set by the server's refusal. The only signal the client gets about its own prompt, and it is
  // a real one: `rate` derives the target from the reveal, so a 409 on "your own prompt" cannot
  // be provoked by naming something else.
  const [refused, setRefused] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const reveal = state.reveal
  const myName = getName(code)

  // A fresh prompt clears the note, the refusal and the error. Done at render rather than in an
  // effect: the reveal index is the trigger and a render is where React resets state on a
  // trigger, and a player arriving at prompt four of nine would otherwise be looking at prompt
  // three's half-filled button.
  //
  // It has to be keyed on the one binding that both outcomes set. A refusal sets `seen` too —
  // without it, the refusal outlived the prompt that caused it and the next one came up wearing
  // a panel saying "this one is yours" about somebody else's work.
  if (reveal && seen !== null && reveal.index !== seen) {
    setPicked(null)
    setSeen(null)
    setRefused(false)
    setErr('')
  }

  if (!reveal)
    return (
      <div className="deck">
        <ConsoleBar meta={`Partie ${code}`} link={link} onRetry={onRetry} />
        <main className="screen screen--solo" id="main" tabIndex={-1}>
          <h1 className="visually-hidden" tabIndex={-1}>
            Notation croisée
          </h1>
          <p className="prose prose--dim">La pièce attend le premier prompt.</p>
        </main>
      </div>
    )

  // Bound once after the null check, because the render below and the POST inside `send` are
  // the two places that need the revealed prompt's position, and a narrowed `reveal` does not
  // survive into a closure: TypeScript keeps the narrowing for the straight-line render and
  // drops it inside the callback. One name for the prompt on screen, used by both.
  const { index: promptAt } = reveal

  // Rule three, decided by the server. `reveal.mine` is the exact answer for the prompt on
  // screen and it works under anonymity, where the author's name is null and the client cannot
  // tell which reveal is its own. So the panel replaces the scale on the first frame, before the
  // player can spend a tap on a button the server would refuse. `refused` stays as the backstop
  // for a stale frame — an old snapshot that predates the server learning this player answered.
  const mine = reveal.mine || refused

  // Rule three, when the game IS anonymous. Nobody can be named, so the client cannot say *which*
  // prompt is the player's — but it can say that one of them is, and that is derived rather than
  // guessed: a player who wrote a prompt this round owns exactly one of the round's prompts, and
  // nobody rates their own. So a player who has sent one is told, every time, that one line of
  // this round is going to be refused. A player who has not sent one owns nothing in this round
  // and is not told anything.
  //
  // Kept even though `reveal.mine` now flags the exact prompt: this notice is the ADVANCE warning
  // that appears on every prompt of the round, including the ones before the player's own. It
  // tells them a refusal is coming before they see it, rather than only when it arrives.
  const named = reveal.author !== null && reveal.author === myName
  const iWrote = state.players.some((p) => p.name === myName && p.answered)
  const oneIsMine = !named && iWrote

  const sent = seen !== null && seen === reveal.index
  const lit = Array.from({ length: Math.min(reveal.rated, reveal.count) }, (_, i) => i)

  function submitOnChord(e: KeyboardEvent<HTMLFormElement>) {
    if (e.key !== 'Enter' || !(e.metaKey || e.ctrlKey)) return
    // Same refusals as the button, so a shortcut can never send what a click would not.
    if (busy || sent || mine || picked === null) return
    e.preventDefault()
    e.currentTarget.requestSubmit()
  }

  async function send(e: FormEvent) {
    e.preventDefault()
    if (picked === null || busy || sent || mine) return
    setBusy(true)
    setErr('')
    try {
      // The index pins the vote to the prompt on screen: a tap that lands as the reveal moves
      // is refused rather than scored against a prompt this player has not read.
      await post(`/api/rooms/${code}/rate`, { token: getToken(code), score: picked, index: promptAt })
      // The POST is the authority on its own outcome, exactly as it is on the Game screen: the
      // reveal moves for the room when everyone has scored, which is a frame away, and a
      // player who has just scored should not be able to score twice while it travels.
      setSeen(promptAt)
    } catch (e) {
      // A refusal is a state of the screen, not a message about a failure: the panel that
      // replaces the scale is the same one a named game draws without asking. It is recorded
      // against this prompt so it is cleared with it.
      if (e instanceof ApiError && e.message === 'your own prompt') {
        setRefused(true)
        setSeen(promptAt)
        // And stop here, as the Quiz screen stops on its 409: `refused` replaces the form with
        // the panel, and the form is the only thing on this screen that draws `err`. An error
        // written on this branch is an error nobody reads, and one that survives into the next
        // prompt if the reset above ever stopped clearing it.
        return
      }
      setErr(describe(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="deck">
      <ConsoleBar meta={`Partie ${code}`} link={link} onRetry={onRetry} />
      {/* The skip link's target — see the note on `SkipLink` in App.tsx. */}
      <main className="screen screen--narrow" id="main" tabIndex={-1}>
        {/* The one heading, and the focus target the phase change lands on. It says which prompt
            of the round is in front of the player, because that is the only navigation this
            screen has: there is no list to go back to, by design. */}
        <h1 className="visually-hidden" tabIndex={-1}>
          Prompt {reveal.index + 1} sur {reveal.count}
        </h1>

        {/* Mounted from the first frame, as everywhere else: a live region that appears with
            its first message is not reliably announced. This one carries the thing the player
            cannot see happening — the reveal moving under them while they rated. */}
        <p className="visually-hidden" aria-live="polite">
          {sent ? `Noté. Prompt ${reveal.index + 1} sur ${reveal.count}.` : ''}
        </p>

        {/* A round's prompts are a sequence, so they get the round's own track. Nothing else on
            this screen is a sequence, and this is the only thing on it that moves. */}
        <RoundTrack current={reveal.index + 1} total={reveal.count} />

        <PhaseClock timer={state.timer} what="pour noter ce prompt" />

        {/* The target, read-only, above the prompt it is being judged against. It is not
            decoration: you cannot tell whether a prompt asks for the right thing without knowing
            what was asked, and the panel is the console's own way of drawing a fixed fact. */}
        <div className="ratehead">
          <p className="label">La cible du tour</p>
          <TargetText className="ratehead__target" text={state.target ?? '—'} />
          {state.target_image && (
            <img className="ratehead__image" src={state.target_image} alt="Image de l’exercice" />
          )}
        </div>

        <div className="rateline">
          {/* `author_label` and never `author`: under anonymity the name is null and the screen
              would then have no label to draw at all, which is a branch on anonymity that exists
              only to decide whether there is anything to show. The label is the same shape in
              both modes, so the prompt is presented the same way in both. */}
          <span className="board__tag rateline__who">{reveal.author_label}</span>
          <span className="rateline__what">
            {mine ? 'Ton prompt' : 'Prompt à noter'}
          </span>
        </div>

        {/* The prompt, and nothing around it. No model output, no score, no author when the game
            hides authors, and no other prompt of the round: a player who saw two of them at once
            could rank them against each other, which is the competition the phase exists to
            refuse. */}
        <p className="prompt">{reveal.prompt}</p>

        {mine ? (
          /* Cool rather than the fault red: a rule of the game is not a malfunction, and this
             screen has no business looking like something is broken. It replaces the scale
             rather than sitting under it — a grid of ten live-looking buttons a player may not
             use is the control actively lying about what it will do. */
          <p className="ownprompt">
            Ce prompt est le tien, et personne ne note le sien. La manche continue sans ta note.
          </p>
        ) : (
          <form className="form" onSubmit={send} onKeyDown={submitOnChord}>
            <div className="raterow">
              <span className="label" id="rate-label">
                Conformité aux règles du prompt engineering
              </span>
              <span className="raterow__value num" aria-hidden="true">
                {picked ?? '—'}
              </span>
            </div>

            {/* Ten buttons rather than a slider, and this is the one control in the console where
                that matters most. A slider asks a thumb standing up to place a mark between two
                integers; a grid of ten 48px cells asks it to hit one of ten, which is a thing a
                thumb is good at. Ten rows of one would be unusable, so it is five by two.
                The marks are toggles rather than a radio group because the choice is not committed
                until "Noter" is pressed — a radio group claims a selection the moment it is
                touched, and here a mis-tap is one more tap to fix and nothing else. */}
            <div className="rates" role="group" aria-labelledby="rate-label">
              {Array.from({ length: SCALE_MAX - SCALE_MIN + 1 }, (_, i) => i + SCALE_MIN).map((n) => (
                <button
                  key={n}
                  type="button"
                  className={`btn rate${picked === n ? ' rate--on' : ''}`}
                  // A warm-to-green ramp along the scale, so the grid reads as low-to-high at a glance.
                  style={{ '--h': Math.round(((n - SCALE_MIN) / (SCALE_MAX - SCALE_MIN)) * 130) } as CSSProperties}
                  aria-pressed={picked === n}
                  disabled={busy || sent}
                  onClick={() => {
                    buzz(8)
                    setPicked(n)
                  }}
                >
                  {n}
                </button>
              ))}
            </div>
            {/* The scale is 1 to 10 and a bare "1" means nothing to someone who has not been
                told what a 7 is. Two ends are named rather than ten stops: a label under every
                cell would put a second word on top of the one a player is trying to read. */}
            <div className="rateends">
              <span className="label">à revoir</span>
              <span className="label">excellent</span>
            </div>

            {/* The chosen note drawn as a scale, under the grid the thumb is on rather than
                beside the meter further down. The buttons each carry their own digit, so this
                is decoration: it exists because "how big is a 7" is a thing words cannot do.
                Calibrated 1-10 and not 0-10, because the lowest note a player may give is 1 and
                a scale that lit all ten ticks for a 10 is right while a scale that lit nothing
                for a 1 would be a lie. */}
            {picked !== null && <ScoreScale score={picked} min={SCALE_MIN} />}

            {err && (
              <p className="err err--inline" role="alert">
                {err}
              </p>
            )}

            <button
              type="submit"
              className="btn btn--signal btn--wide"
              disabled={busy || sent || picked === null}
            >
              {busy ? 'Envoi…' : sent ? 'Noté' : 'Noter ce prompt'}
            </button>
            <p className="hint">
              <kbd>Ctrl</kbd>/<kbd>⌘</kbd> + <kbd>Entrée</kbd> pour noter
            </p>
          </form>
        )}

        {/* The end of the discussion about THIS prompt, under the scale that scored it and
            outside the form that sent the note. Outside, and not as a second submit inside:
            `Ctrl/⌘ + Entrée` is bound to the rating form, and a vote reachable by the same
            chord would be a vote a player could cast while believing they were noting — and the
            note is not undoable from here.
            It sits under the scale rather than beside it because the order is the argument:
            the note is this player's job and finishes first, then the room talks about it and
            says so. */}
        <ReadyBar
          ready={state.ready}
          onReady={async () => {
            try {
              await postReady(code, getToken(code) ?? '')
              // `true` means the server has the vote. The bar cannot tell a success from a
              // refusal on its own — the screens report a refusal by writing to `setErr` and
              // RESOLVING — so this answer is what lets it close its own double-tap window
              // instead of painting a refusal under a vote the room already holds.
              return true
            } catch (e) {
              // `describe`, not a fixed sentence: the refusals are two different facts with two
              // different repairs, and « the vote failed » is neither of them. A vote the server
              // already had, and a phone that woke up on the next prompt, both arrive here as a
              // 409 with a reason attached.
              setErr(describe(e))
              return false
            }
          }}
        />

        {/* The one instrument on this screen, and the same one the players and the host are
            looking at: it says how far the room has got on this prompt. Reused rather than
            redrawn because the room already knows what it looks like, and `AnswerMeter` already
            carries the live region that says the fraction in words. */}
        <AnswerMeter
          lit={lit}
          total={reveal.count}
          mine={-1}
          caption={sent ? 'Tu as noté, en attente des autres' : 'Prompts notés'}
        />

        {/* Rule one, stated to the player rather than left as a hole in the screen. During the
            rating phase every score in the snapshot is null and no prompt is sent but this one,
            so a player who goes looking for the leaderboard or for the round's other prompts
            finds nothing — and a screen that hides something without saying why reads as broken.
            This is the sentence that says the absence is the design. */}
        <p className="prose prose--sm prose--cool">
          Pendant la notation, ni les scores ni les autres prompts de la manche ne sont
          affichés.
        </p>
        {/* Rule three told in advance, in the calm cool colour as well: it is the one thing the
            player is about to be refused, and saying it here rather than at the 409 is the
            difference between a rule the room knows and an error each player discovers alone. */}
        {oneIsMine && (
          <p className="prose prose--sm prose--cool">
            Un des prompts de cette manche est le tien : celui-là ne peut pas être noté.
          </p>
        )}
      </main>
    </div>
  )
}