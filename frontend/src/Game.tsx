import { useState, type FormEvent } from 'react'
import { AnswerMeter, Board, ConsoleBar, Readout, ScoreScale, Section, Step } from './ui'
import { absent, describe, getName, getToken, post, present, type Player, type State } from './api'
import type { Link } from './useRoom'

type Result = { score: number; comment: string; output: string; round: number }

export default function Game({
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
  const [prompt, setPrompt] = useState('')
  const [result, setResult] = useState<Result | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  // A prompt belongs to the target it was written against, and an error to the round that
  // caused it, so both are cleared when the round moves on. This used to be an effect keyed
  // on `state.current`; it is a render-time reset now, because an effect that only exists to
  // setState is a second render that React then has to justify, and the lint rightly calls
  // it out. The round is the trigger, and a render is where React resets state on a trigger.
  //
  // The result is normally kept. The push that closes the round lands after this player's
  // own POST, so clearing it took away the last player's score, comment and output within a
  // second of earning them. It is kept instead and labelled with the round it belongs to,
  // which is what stops the misreading the reset was originally for — an unlabelled note
  // sitting under a different round's target.
  //
  // Kept, but not across a restart. A restart throws away every round and every answer and
  // starts a new game in the same room with the same people, so `current` goes *backwards* —
  // `next_round` only ever counts up, so a decrease can only mean a new game. A score from
  // the old game sitting under a new game's target is the exact misreading the label exists
  // to prevent, so a backwards jump is the one transition that clears the result and the
  // busy flag.
  //
  // The one case the counter cannot catch is a restart during round one, where it goes from
  // 0 to 0. That is covered by the routing above: a restart puts the room back in the lobby,
  // App swaps this screen for the lobby one, and every piece of local state goes with it. The
  // check is here so the rule holds for the restarts that *are* observable here rather than
  // relying on that unmount to catch all of them.
  const [round, setRound] = useState(state.current)
  // Set when a draft is lost to a round change the player was not there to see. Held as the
  // jump itself rather than a flag, because "the manche is passed from 3 to 5" is the only
  // sentence that tells a player who woke up on round 5 what happened to the round 3 they
  // remember. Cleared by the next round change, so it is said once and then gets out of the
  // way.
  const [missed, setMissed] = useState<{ from: number; to: number } | null>(null)
  // True while the stream is not live, meaning the room may move on without this screen
  // seeing it do so. Adjusted during render rather than in an effect so the reconnect and
  // the round that overtook it are seen as one event, which is the event being detected, and
  // so that no setState-in-effect has to justify a second render.
  const [away, setAway] = useState(false)
  if (link !== 'live' && !away) setAway(true)
  // A reconnect that lands on the round we were already on missed nothing, and there is
  // nothing to say about it.
  if (link === 'live' && away && round === state.current) setAway(false)

  if (round !== state.current) {
    if (away && state.current > round) setMissed({ from: round, to: state.current })
    setAway(false)
    setRound(state.current)
    if (state.current < round) {
      setResult(null)
      setBusy(false)
    }
    setPrompt('')
    setErr('')
  }

  const myName = getName(code)
  const me: Player | undefined = state.players.find((p) => p.name === myName)
  const alreadyDone = me?.answered ?? false

  // The meter counts players who are present, so it can actually finish; the ones who are not
  // get said out loud underneath instead. This is not a display preference: the round closes
  // when every *present* player has answered, so counting present players here is the same
  // rule the screen is reporting on, and a segment for a sleeping phone would be a bar that
  // can never reach the end.
  const here = present(state)
  const gone = absent(state)
  const mine = here.findIndex((p) => p.name === myName)
  const lit = here.flatMap((p, i) => (p.answered ? [i] : []))
  const left = here.length - lit.length

  // A phone that slept comes back to a board from two rounds ago, still able to send. The
  // prompt would be written against a target that is no longer on screen and would land on
  // whatever round is live now, so the answer is refused rather than silently misfiled. The
  // stream reopens by itself within a few seconds and the button comes back.
  const stale = link !== 'live'

  // What the wait is actually waiting on, in one sentence. `left` counts this player in — they
  // are present and on the stream — so being the only one left is `left === 1` while the box
  // is still empty. This is also the exact condition the server closes a round on, so the
  // line is never more or less true than the game is.
  const caption = (() => {
    if (!alreadyDone) return left === 1 ? 'À toi de répondre' : 'Réponses envoyées'
    return left === 0 ? 'Tout le monde a répondu' : `En attente de ${left} ${left > 1 ? 'joueurs' : 'joueur'}`
  })()

  // One notice, never two, and the most specific thing we know wins. Both cases are the same
  // thing to a player — what you are reading may not be the room as it is now — so they share
  // one place on the screen, directly under the round, which is where someone who has just
  // woken up looks first.
  const notice = missed
    ? `La manche est passée de ${missed.from + 1} à ${missed.to + 1} pendant que l’écran était éteint. Ton prompt n’a pas été envoyé.`
    : stale
      ? 'Reconnexion en cours — ce que tu lis peut dater de la manche précédente.'
      : ''

  async function send(e: FormEvent) {
    e.preventDefault()
    setErr('')
    setBusy(true)
    try {
      const res = await post<Omit<Result, 'round'>>(`/api/rooms/${code}/answer`, { token: getToken(code), prompt })
      setResult({ ...res, round: state.current })
      setPrompt('')
    } catch (e) {
      setErr(describe(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="deck">
      <ConsoleBar meta={`Partie ${code}`} link={link} onRetry={onRetry} />
      <main className={`screen${stale ? ' screen--stale' : ''}`}>
        <Step current={state.current} total={state.total} />

        {notice && <p className="notice">{notice}</p>}

        {/* The target and the field it is written against, kept as one block: on a phone
            turned sideways they sit side by side, and on a phone held upright they are the
            first two things on the screen, so a player is always composing against the words
            they can actually see. */}
        <div className="playgrid">
          {/* The one illuminated panel. The label says what the words below are for, which
              is the piece the old "Reponse a atteindre" never did: this is not a response to
              match, it is the answer you have to make the model produce. */}
          <Readout label="Le modèle doit répondre" value={state.target ?? '—'} />

          <form className="form" onSubmit={send}>
            <div className="field">
              <label className="label" htmlFor="prompt">
                Ton prompt
              </label>
              <textarea
                id="prompt"
                className="area"
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder="Décris la scène, le ton, la longueur…"
                maxLength={2000}
                // The prompt is a piece of instruction, not prose. Autocorrect and the
                // spellchecker are both actively hostile to it: they "fix" the deliberate
                // misspellings and the invented vocabulary that make a model produce
                // something specific, and they do it while the player is mid-word.
                autoCapitalize="sentences"
                autoCorrect="off"
                spellCheck={false}
                disabled={alreadyDone}
              />
            </div>
            {err && <p className="err err--inline">{err}</p>}
            <button
              type="submit"
              className="btn btn--signal btn--wide"
              disabled={busy || alreadyDone || stale || !prompt.trim()}
            >
              {alreadyDone
                ? 'Réponse envoyée'
                : busy
                  ? 'Le modèle réfléchit…'
                  : stale
                    ? 'En attente de la reconnexion…'
                    : 'Envoyer au modèle'}
            </button>
          </form>
        </div>

        {/* The wait after submitting used to be nothing at all: a disabled button and a dead
            screen for the several seconds the two model calls take. This is the thing that
            fills instead, and it is the same condition the round closes on — which is also why
            it is the reason not to dawdle. `mine` is not gated on having submitted: a segment
            now means "this person is on the stream", so the sodium one has to say "that one is
            me" whether or not I have hit send, and the fill still carries the answer. */}
        <AnswerMeter lit={lit} total={here.length} mine={mine} caption={caption} />

        {/* Stated plainly and without alarm, because it is usually a lock screen and not a
            problem. The second half is the part that matters: a round closes on the answers of
            the players who are here, so somebody dropping out does not hold the game up — and
            if they come back they can still answer the round that is open. */}
        {gone.length > 0 && (
          <p className="prose prose--sm prose--cool">
            {gone.length === 1 ? 'Un joueur est hors ligne' : `${gone.length} joueurs sont hors ligne`}{' '}
            — la manche avance sans attendre.
          </p>
        )}

        {result && (
          <div className="verdict">
            <div className="verdict__top">
              <span className="verdict__score num">{result.score}</span>
              <span className="verdict__den">sur 10</span>
              <span className="verdict__round">manche {result.round + 1}</span>
            </div>
            <ScoreScale score={result.score} />
            <p className="prose prose--sm">{result.comment}</p>
            <div>
              <p className="label">Le modèle a répondu</p>
              <pre className="slot">{result.output}</pre>
            </div>
          </div>
        )}

        <Section title="Classement">
          <Board players={state.players} myName={myName} />
        </Section>
      </main>
    </div>
  )
}
