import { useState, type CSSProperties } from 'react'
import { AnswerMeter, ConsoleBar, PhaseClock, RoundTrack, ScoreScale } from './ui'
import { ApiError, describe, getName, getToken, post, SCALE_MAX, SCALE_MIN, type State } from './api'
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
  // The note this screen just tapped, ahead of the snapshot that confirms it. The server's
  // `reveal.my_score` is the truth (it survives a reconnect); this only bridges the round-trip.
  const [picked, setPicked] = useState<number | null>(null)
  // The reveal the local state belongs to: a new prompt clears the note, the refusal and the
  // error, at render rather than in an effect, so prompt four never wears prompt three's note.
  const [at, setAt] = useState<number | null>(null)
  // Set by the server's refusal: `rate` derives the target from the reveal, so a 409 on
  // "your own prompt" is the backstop for a frame that predates `reveal.mine`.
  const [refused, setRefused] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const reveal = state.reveal
  const myName = getName(code)

  if (reveal && reveal.index !== at) {
    setAt(reveal.index)
    setPicked(null)
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

  // Bound once after the null check, because the render below and the POST inside `rate` are
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

  // What this player has given the prompt on screen: the tap in flight, else the server's record.
  const shown = picked ?? reveal.my_score
  const sent = shown !== null
  const lit = Array.from({ length: Math.min(reveal.rated, reveal.count) }, (_, i) => i)

  // One tap is the note: no confirm button. Another tap replaces it (the server upserts), as
  // long as the prompt is still on screen.
  async function rate(n: number) {
    if (busy || mine) return
    buzz(8)
    const before = picked
    setPicked(n)
    setBusy(true)
    setErr('')
    try {
      // The index pins the note to the prompt on screen: a tap that lands as the reveal moves
      // is refused rather than scored against a prompt this player has not read.
      await post(`/api/rooms/${code}/rate`, { token: getToken(code), score: n, index: promptAt })
    } catch (e) {
      setPicked(before)
      if (e instanceof ApiError && e.message === 'your own prompt') {
        setRefused(true)
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
          <p className="label">La cible de la manche</p>
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
          <div className="form">
            <div className="raterow">
              <span className="label" id="rate-label">
                Conformité aux règles du prompt engineering
              </span>
              <span className="raterow__value num" aria-hidden="true">
                {shown ?? '—'}
              </span>
            </div>

            {/* Ten buttons rather than a slider: a thumb hits one of ten cells far better than it
                places a mark between two integers. Each tap sends the note; the pressed one is the
                note the server holds. */}
            <div className="rates" role="group" aria-labelledby="rate-label">
              {Array.from({ length: SCALE_MAX - SCALE_MIN + 1 }, (_, i) => i + SCALE_MIN).map((n) => (
                <button
                  key={n}
                  type="button"
                  className={`btn rate${shown === n ? ' rate--on' : ''}`}
                  // A warm-to-green ramp along the scale, so the grid reads as low-to-high at a glance.
                  style={{ '--h': Math.round(((n - SCALE_MIN) / (SCALE_MAX - SCALE_MIN)) * 130) } as CSSProperties}
                  aria-pressed={shown === n}
                  disabled={busy}
                  onClick={() => void rate(n)}
                >
                  {n}
                </button>
              ))}
            </div>
            <div className="rateends">
              <span className="label">à revoir</span>
              <span className="label">excellent</span>
            </div>

            {shown !== null && <ScoreScale score={shown} min={SCALE_MIN} />}

            {err && (
              <p className="err err--inline" role="alert">
                {err}
              </p>
            )}

            <p className="hint" role="status">
              {sent ? 'Note enregistrée. Touche une autre note pour la changer.' : 'Touche une note pour la donner.'}
            </p>
          </div>
        )}

        {/* The one instrument on this screen, and the same one the players and the host are
            looking at: it says how far the room has got on this prompt. Reused rather than
            redrawn because the room already knows what it looks like, and `AnswerMeter` already
            carries the live region that says the fraction in words.
            Unit note: here the meter counts prompts of the manche ("Prompts notés"), while the
            Game screen counts players ("Réponses envoyées"). Two units, two captions. */}
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