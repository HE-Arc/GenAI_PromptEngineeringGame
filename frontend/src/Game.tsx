import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { AnswerMeter, Board, ConsoleBar, PlayerName, PresenceDots, PhaseClock, Readout, ScoreScale, Section, Step } from './ui'
import { fr, noun } from './format'
import {
  absent,
  describe,
  getName,
  getToken,
  isAbort,
  present,
  StreamError,
  streamAnswer,
  type Player,
  type State,
} from './api'
import ModelOutput from './ModelOutput'
import type { Link } from './useRoom'

type Result = { score: number; comment: string; output: string; round: number; truncated: boolean }

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
  // The score has to be somewhere the player is already looking. Two refs: the composer,
  // which is holding the keyboard, and the verdict, which is what the submit earned.
  const areaRef = useRef<HTMLTextAreaElement>(null)
  const verdictRef = useRef<HTMLDivElement>(null)
  // True the moment the POST has answered, before the stream has said so. See `send`.
  const [sent, setSent] = useState(false)
  // The answer as it is being written, and then the verdict. Two pieces of one thing rather
  // than two, because the player's question — « where is my note » — has one answer, and a
  // screen that could show either would have to say which half is on.
  const [live, setLive] = useState<{ text: string; scoring: boolean } | null>(null)
  // Aborting the stream is not a failure and must not be reported as one: unmounting, or the
  // player leaving, closes the request mid-answer. Kept in a ref because it is never rendered.
  const abortRef = useRef<AbortController | null>(null)

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
    setSent(false)
    setLive(null)
  }

  const myName = getName(code)
  const me: Player | undefined = state.players.find((p) => p.name === myName)
  // The round is written, revealed and rated: the player is reading numbers, not composing.
  // Derived from the phase rather than from `answered`, because "this player already sent" is
  // true at the end of every round and says nothing about whether the round is still open.
  const scored = state.phase === 'summary'
  // This player's own send, OR the stream. The stream alone is the event that put the score
  // below the fold: the button label is the only feedback still inside the viewport after a
  // submit, and it sat unchanged for the length of a model call, waiting on an SSE frame that
  // this same POST has already made unnecessary. The POST is the authority on its own outcome.
  const alreadyDone = sent || (me?.answered ?? false)

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
  // `busy` is in the first test, not the second: a player who has pressed send is waiting on
  // the jury, not being told to answer again. Before the stream existed the POST only came
  // back at the very end, so this line had ten seconds of nothing to be wrong about.
  const caption = (() => {
    if (!alreadyDone && !busy) return left === 1 ? 'À toi de répondre' : 'Réponses envoyées'
    return left === 0 ? 'Tout le monde a répondu' : `En attente de ${noun(left, 'joueur', 'joueurs')}`
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

  // What the button is waiting for. Two waits and two words: the pause after the text is two
  // more billed calls, so a button frozen on « le modèle réfléchit » while the jury reads is a
  // button telling the player the model is still working when it stopped two seconds ago.
  const submitLabel = alreadyDone
    ? 'Réponse envoyée'
    : busy
      ? live?.scoring
        ? 'Le jury délibère…'
        : 'Le modèle réfléchit…'
      : stale
        ? 'En attente de la reconnexion…'
        : 'Envoyer au modèle'

  function submitOnChord(e: KeyboardEvent<HTMLFormElement>) {
    if (e.key !== 'Enter' || !(e.metaKey || e.ctrlKey)) return
    // Same refusals as the button, so a shortcut can never send what a click would not.
    if (busy || alreadyDone || stale || !prompt.trim()) return
    e.preventDefault()
    e.currentTarget.requestSubmit()
  }

  async function send(e: FormEvent) {
    e.preventDefault()
    setErr('')
    setBusy(true)
    setLive({ text: '', scoring: false })
    const ac = new AbortController()
    abortRef.current = ac
    // Accumulated here rather than read back off `live`: a state value captured by this closure
    // is the value it had when the send started, so reading it after the awaits would hand the
    // verdict an empty answer.
    let text = ''
    let scoring = false
    try {
      const res = await streamAnswer(
        `/api/rooms/${code}/answer`,
        { token: getToken(code), prompt },
        {
          chunk: (delta) => {
            text += delta
            setLive({ text, scoring })
          },
          scoring: () => {
            scoring = true
            setLive({ text, scoring })
          },
        },
        ac.signal,
      )
      // The text the player watched, not a second copy from the payload: the server stored the
      // same string and the judge scored the same string, so one truth reaches the screen.
      setResult({ score: res.score, comment: res.comment, output: text, round: res.round, truncated: res.truncated })
      setLive(null)
      // Only the verdict earns the round its answer. Not the first chunk: the row does not exist
      // until the `done` frame, and a composer disabled on the strength of a delta would lock a
      // player out of a round the server has not yet accepted — and lock them out of retrying
      // if the jury then falls over.
      setSent(true)
      setPrompt('')
    } catch (e) {
      // An abort says nothing to the player: the screen went away, or the player did. There is
      // no error line to write and no state to undo, because a cancel writes nothing server-side
      // either — the generation keeps running and the round closes on the answers that do land.
      if (!isAbort(e)) {
        setErr(e instanceof StreamError ? e.message : describe(e))
        // Nothing was persisted, so the composer must stay usable and must stop claiming the
        // answer is sent — otherwise a failed send locks the player out of a round they never
        // answered, and there is nothing on screen saying why.
        setSent(false)
        setLive(null)
      }
    } finally {
      abortRef.current = null
      setBusy(false)
    }
  }

  // Cancel an answer still being written when this screen goes away. `busy` is deliberately not
  // in the deps: the effect must fire on unmount only, and a re-run on every send would abort
  // the request it was mounted for.
  useEffect(() => () => abortRef.current?.abort(), [])

  // The score lands where the eye already is, which takes three things that all used to be
  // missing. The verdict now sits above the composer in the document rather than under it; the
  // composer gives the keyboard back, because iOS keeps it up across a type="submit" and the
  // bottom 400px of the document stay covered; and the verdict is then brought into the visual
  // viewport by the smallest scroll that will do it, so a short phone does not get yanked
  // past the score it just earned.
  //
  // In an effect keyed on the result rather than at the end of `send`, because the node it
  // acts on does not exist yet at the point the promise resolves — React has not committed the
  // render that creates it.
  useEffect(() => {
    if (!result) return
    areaRef.current?.blur()
    const v = verdictRef.current
    if (!v) return
    v.scrollIntoView({ block: 'nearest' })
    // Focus as well as scroll: `block: 'nearest'` moves the page, focus is what tells a
    // screen reader the player has just been handed something.
    v.focus()
  }, [result])

  return (
    <div className="deck">
      <ConsoleBar meta={`Partie ${code}`} link={link} onRetry={onRetry} />
      {/* The skip link's target — see the note on `SkipLink` in App.tsx. */}
      <main className={`screen screen--split${stale ? ' screen--stale' : ''}`} id="main" tabIndex={-1}>
        <Step current={state.current} total={state.total}>
          {!scored && <PhaseClock timer={state.timer} what="pour écrire ton prompt" />}
        </Step>

        {/* The only heading on this screen, and the target a route change moves focus to. The
            round track above says the same thing visibly, so this one says what the *screen*
            is rather than which round it is on — otherwise a player arriving mid-game would
            hear the number twice and nothing about what they are meant to do. It also follows
            the phase, because on a summary screen there is nothing to answer: announcing
            "Répondre à la manche 3" above a board of scores and no field would tell a
            screen-reader user to go and do something this screen is not for. */}
        <h1 className="visually-hidden" tabIndex={-1}>
          {scored ? `Résumé de la manche ${state.current + 1}` : `Répondre à la manche ${state.current + 1}`}
        </h1>

        {notice && <p className="notice">{notice}</p>}

        {/* One permanent live region for the whole screen, always in the tree even when it has
            nothing to say. A live region that is mounted at the same moment as its first
            message is not reliably announced — the region has to already exist for the change
            to be a change — so this one carries the empty string until a score lands. */}
        <p className="visually-hidden" aria-live="polite">
          {result ? `Note obtenue : ${result.score} sur 10, à la manche ${result.round + 1}.` : ''}
        </p>

        {/* The score, and it is above the target and above the field rather than under them.
            Under, it rendered at roughly y690 with a long target, and the visual viewport with
            the keyboard up is about 400px: the one thing the player had just earned, invisible,
            with no scroll, no focus move and nothing to say so. Above, it is in the first
            screenful. It keeps its `manche N` label because it survives a round change. */}
        {/* The answer being written, in the same panel as the verdict it becomes and in the same
            place in the document, so the text never jumps. Deliberately WITHOUT a score: a `0`
            or a `—` in the score slot while the text arrives would be a number nobody gave, and
            the slot is the loudest thing on the screen. */}
        {live && (
          <div className="verdict" ref={verdictRef} tabIndex={-1}>
            <div>
              <p className="label">
                {live.scoring ? 'Le jury délibère…' : 'Le modèle a répondu'}
              </p>
              <ModelOutput text={live.text} streaming={!live.scoring} />
            </div>
          </div>
        )}

        {result && (
          <div className="verdict" ref={verdictRef} tabIndex={-1}>
            <div className="verdict__top">
              <span className="verdict__score num">{result.score}</span>
              <span className="verdict__den">sur 10</span>
              <span className="verdict__round">manche {result.round + 1}</span>
            </div>
            <ScoreScale score={result.score} />
            <p className="prose prose--sm">{result.comment}</p>
            <div>
              <p className="label">Le modèle a répondu</p>
              <ModelOutput text={result.output} />
              {/* Said here because the truncation is a fact about the answer, not about the
                  game: the note below was given for a text that stops mid-sentence, and a class
                  reading a bad score deserves to know the answer is why. */}
              {result.truncated && (
                <p className="prose prose--sm prose--dim">
                  La réponse s’arrête ici : le modèle a atteint sa limite de longueur. C’est ce
                  texte, coupé à cet endroit, que le jury a noté.
                </p>
              )}
            </div>
          </div>
        )}

        {/* The synthesis of the round that has just been scored, above the target rather than
            on a screen of its own. It stays here because the player is still in the middle of
            writing ten prompts: what came of the last one is the reading they do while the next
            one waits, not a different activity that deserves its own tab.
            Every figure is drawn, including the two the bound is made of. The critic's score and
            the peers' average are the only reason the retained quality is the number it is, and
            a round synthesis that printed the verdict without its inputs would be asking a class
            to accept an arithmetic result they cannot check.
             *
             * Every figure goes through `fr`, because §7's note_tour is a real division and the
             * server stopped pretending otherwise: a round of proximity 7 and quality 6 is 6,5, and
             * a board that printed "6.5" would be the only Americanism in a French room. `fr` also
             * prints the em-dash for an absent `quality_peers` — a round where nobody rated — so a
             * cell never shows a zero that nobody earned. */}
        {scored && state.summary && (
          <section className="tally">
            <h2 className="label">Ta note de la manche {state.summary.round + 1}</h2>
            {(() => {
              const mine = state.summary!.rows.find((row) => row.author === myName)
              if (!mine) return <p className="prose prose--dim">Tu n’as pas répondu à cette manche.</p>
              return (
                <div className="tally__row tally__row--in" key={mine.author}>
                  <div className="tally__head">
                    <PlayerName
                      name={mine.author}
                      isMe={true}
                      className="board__name board__name--me"
                    />
                    <span className="tally__round num">
                      {fr(mine.round_score)}
                      <span className="tally__roundden">/ 10</span>
                    </span>
                  </div>
                  <pre className="slot tally__prompt">{mine.prompt}</pre>
                  <details className="tally__more">
                    <summary className="tally__morebtn">Voir la réponse du modèle</summary>
                    <ModelOutput text={mine.output} />
                  </details>
                </div>
              )
            })()}
          </section>
        )}

        {/* The next round is the admin's button alone: the class has no vote on a summary. */}
        {scored && (
          <p className="prose prose--sm prose--dim" role="status">
            L’admin lance la manche suivante.
          </p>
        )}

        {/* The target and the field it is written against, kept as one block: on a phone
            turned sideways they sit side by side, on a phone held upright they are the first
            two things on the screen, and on a desktop they head the column. A player is
            always composing against the words they can actually see.

            The summary phase is the one place there is a target and no field. The round is
            closed — it has been written, revealed and rated — so a live composer under it
            could only ever fail, and for a player who has already sent it reads as a second
            prompt of the round they are still credited for. What the player does during a
            summary is read the numbers and argue about them, so that is what the space says. */}
        <div className="playgrid">
          {/* The one illuminated panel. The label says what the words below are for, which
              is the piece the old "Reponse a atteindre" never did: this is not a response to
              match, it is the answer you have to make the model produce. */}
          <Readout
            label={
              scored
                ? `La cible de la manche ${state.current + 1}`
                : 'Le modèle doit répondre'
            }
            value={state.target ?? '—'}
            image={state.target_image}
          />

          {!scored && (
            <>
              {/* Two taps are not a keyboard, and on a desktop a textarea you can only leave by
                  reaching for the mouse is a textarea half-used. Cmd/Ctrl+Enter submits; a bare
                  Enter does not, because a prompt is instruction text and often wants line
                  breaks. The chord is also the guard the shortcut needs: it cannot be hit by
                  accident while a thumb or a hand is mid-word, which a bare-Enter submit could. */}
              <form className="form" onSubmit={send} onKeyDown={submitOnChord}>
                <div className="field">
                  <label className="label" htmlFor="prompt">
                    Ton prompt
                  </label>
                  <textarea
                    id="prompt"
                    ref={areaRef}
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
                    // The message is a sentence about this field, so the field points at it. The
                    // prompt is never cleared on a failure — it is the work, and a rejected
                    // submission that ate it would be the real defect.
                    aria-invalid={err ? true : undefined}
                    aria-describedby={err ? 'prompt-err' : undefined}
                  />
                </div>
                {err && (
                  <p className="err err--inline" id="prompt-err" role="alert">
                    {err}
                  </p>
                )}
                <button
                  type="submit"
                  className="btn btn--signal btn--wide"
                  disabled={busy || alreadyDone || stale || !prompt.trim()}
                >
                  {submitLabel}
                </button>
                <p className="hint">
                  <kbd>Ctrl</kbd>/<kbd>⌘</kbd> + <kbd>Entrée</kbd> pour envoyer
                </p>
              </form>
            </>
          )}

          {scored && (
            <p className="prose prose--dim">
              La manche {state.current + 1} est commentée. L’animateur ouvre la suivante quand
              la classe a fini.
            </p>
          )}
        </div>

        {/* The wait after submitting used to be nothing at all: a disabled button and a dead
            screen for the several seconds the two model calls take. This is the thing that
            fills instead, and it is the same condition the round closes on — which is also why
            it is the reason not to dawdle. `mine` is not gated on having submitted: a segment
            now means "this person is on the stream", so the sodium one has to say "that one is
            me" whether or not I have hit send, and the fill still carries the answer. */}
        <AnswerMeter lit={lit} total={here.length} mine={mine} caption={caption} />
        {/* Anonymous presence derived from the meter itself: no names, no backend field.
            Shown only while some answered and some are still writing. */}
        {!scored && lit.length > 0 && left > 0 && (
          <PresenceDots label={`${noun(left, 'joueur écrit', 'joueurs écrivent')}…`} />
        )}

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

        <div className="railcol">
          {/* Rule one, as a render decision rather than as a rule: during the rating phase the
              standings are not drawn. The server blanks every score then, so there is nothing to
              draw — but "nothing to draw" is exactly how a bug looks on a screen, so the absence
              has to be deliberate here rather than left to fall out of a null, and this is the
              only place in the console that decides not to render a board it could render. */}
          {state.phase !== 'rating' && (
            <Section title="Classement">
              <Board players={state.players} myName={myName} />
            </Section>
          )}
        </div>
      </main>
    </div>
  )
}
