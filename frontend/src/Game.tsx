import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { AnswerMeter, AutoArea, Board, ConsoleBar, Criteria, PlayerName, PresenceDots, PhaseClock, Readout, Section, Step } from './ui'
import { noun } from './format'
import {
  absent,
  answerStart,
  describe,
  getName,
  present,
  wsRefusal,
  type Player,
  type State,
} from './api'
import ModelOutput from './ModelOutput'
import { useQuizWindow } from './serverClock'
import { buzz, reduced } from './fx'
import type { AnswerFeed, Link } from './useRoom'

// Same bound as the server's MAX_PROMPT_CHARS (server/src/ws/mod.rs).
const MAX_PROMPT_CHARS = 4096

type Result = { output: string; round: number; truncated: boolean }

// The material of an exercise without its instruction: every target opens with the
// instruction paragraph, then a blank line, then the data to work on. Empty when the exercise
// is the instruction alone (nothing to paste into a prompt).
function exerciseContent(target: string | null): string {
  if (!target) return ''
  const cut = target.indexOf('\n\n')
  return cut === -1 ? '' : target.slice(cut + 2).trim()
}

export default function Game({
  state,
  code,
  link,
  onRetry,
  answer,
  sendPrompt,
}: {
  state: State
  code: string
  link: Link
  onRetry: () => void
  answer: AnswerFeed | null
  sendPrompt: (prompt: string) => void
}) {
  const [prompt, setPrompt] = useState('')
  // Conversation rounds: one draft per prompt slot, so later steps can be written ahead.
  const [drafts, setDrafts] = useState<string[]>([])
  // Slots this screen has sent, counted locally: the `done` frame can land before the snapshot
  // that carries the new turn, and the slot must not offer a second send in that gap.
  const [sentCount, setSentCount] = useState(0)
  const [result, setResult] = useState<Result | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  // The score has to be somewhere the player is already looking. Two refs: the composer,
  // which is holding the keyboard, and the verdict, which is what the submit earned.
  const areaRef = useRef<HTMLTextAreaElement>(null)
  const verdictRef = useRef<HTMLDivElement>(null)
  // True the moment the `done` frame lands, before the next snapshot says so. See `send`.
  const [sent, setSent] = useState(false)
  // The answer as it is being written, and then the verdict. Two pieces of one thing rather
  // than two, because the player's question — « where is my note » — has one answer, and a
  // screen that could show either would have to say which half is on.
  const [live, setLive] = useState<{ text: string } | null>(null)
  // The verdict this screen is waiting on over the socket. The frames arrive on the room
  // feed the hook owns, not on the send call, so the waiter parks the resolve/reject pair
  // here and the effect below feeds it as frames land. One at a time: the composer is
  // disabled while busy, so a second send cannot overwrite the first.
  const waiterRef = useRef<{
    player: string
    text: string
    resolve: (v: { text: string; truncated: boolean }) => void
    reject: (e: unknown) => void
  } | null>(null)

  // A prompt belongs to the target it was written against, and an error to the round that
  // caused it, so both are cleared when the round moves on. This used to be an effect keyed
  // on `state.current`; it is a render-time reset now, because an effect that only exists to
  // setState is a second render that React then has to justify, and the lint rightly calls
  // it out. The round is the trigger, and a render is where React resets state on a trigger.
  //
  // The result is normally kept. The push that closes the round lands after this player's
  // own answer, so clearing it took away the last player's score, comment and output within a
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
    setDrafts([])
    setSentCount(0)
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
  // This player's own send, OR the snapshot. The snapshot alone is the event that put the
  // score below the fold: the button label is the only feedback still inside the viewport
  // after a submit, and it sat unchanged for the length of a model call. The `done` frame
  // is the authority on its own outcome.
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

  // Rule: stale is exactly `link !== 'live'`, and the banner shows exactly while stale
  // is true. A phone that slept comes back to a board from two rounds ago, still able to send. The
  // prompt would be written against a target that is no longer on screen and would land on
  // whatever round is live now, so the answer is refused rather than silently misfiled. The
  // stream reopens by itself within a few seconds and the button comes back.
  const stale = link !== 'live'
  // The round is over but answers are still arriving: nothing new can be sent.
  const ending = !scored && !!state.waiting_answers

  // What the wait is actually waiting on, in one sentence. `left` counts this player in — they
  // are present and on the stream — so being the only one left is `left === 1` while the box
  // is still empty. This is also the exact condition the server closes a round on, so the
  // line is never more or less true than the game is.
  // `busy` is in the first test, not the second: a player who has pressed send is waiting on
  // the jury, not being told to answer again.
  // "Réponses envoyées" is shown only once this player has sent (alreadyDone): before that
  // the meter counts other players, so the caption is the wait ("En attente des autres…").
  const caption = (() => {
    if (alreadyDone) return left === 0 ? 'Tout le monde a répondu' : 'Réponses envoyées'
    return 'En attente des autres…'
  })()

  // Zero answers is not a zero on the meter: the counter reports progress, while this
  // reports that nothing has started yet, so it gets its own sentence.
  const noAnswerYet = !scored && lit.length === 0

  // One notice, never two, and the most specific thing we know wins. Both cases are the same
  // thing to a player — what you are reading may not be the room as it is now — so they share
  // one place on the screen, directly under the round, which is where someone who has just
  // woken up looks first. The server's room error is the most specific of the three and takes
  // the slot: it names a submit that just failed for the whole room, and the other two only say
  // what the screen may be showing.
  // Rule for the two reconnect messages: `stale` means the feed is down right now (what is on
  // screen may be old), while `missed` means the feed came back on a later manche (rounds were
  // skipped while the screen was off). Two distinct sentences because they are two facts.
  const notice = state.error
    ? state.error
    : ending
      ? busy
        ? 'La manche se termine : ta réponse arrive, la suite attend qu’elle soit prête.'
        : 'La manche se termine : on attend les dernières réponses du modèle avant de continuer…'
      : missed
      ? `La manche est passée de ${missed.from + 1} à ${missed.to + 1} pendant que l’écran était éteint. Ton prompt n’a pas été envoyé.`
      : stale
        ? 'Reconnexion en cours — ce que tu lis peut dater de la manche précédente.'
        : ''

  // The server refuses anything past MAX_PROMPT_CHARS, so the composer enforces the same
  // bound before sending: a submit the server would reject is a billed round-trip for nothing.
  // A conversation round: sends add to the chain instead of replacing the last prompt.
  const conv = state.conversation
  // Older servers send no turns; an absent list is an empty conversation.
  const turns = state.my_turns ?? []
  // The slot the next send fills; equal to max_attempts once the conversation is complete.
  const cur = Math.max(turns.length, sentCount)
  // What the next send carries: the current slot's draft, or the single field.
  const text = conv ? (drafts[cur] ?? '') : prompt
  const setDraft = (i: number, v: string) =>
    setDrafts((d) => {
      const next = [...d]
      next[i] = v
      return next
    })
  const tooLong = text.trim().length > MAX_PROMPT_CHARS
  // The round's attempt cap (3, or 2 on an image round): past it the server refuses, and the
  // screen stops offering a send.
  const attemptsLeft = Math.max(0, state.max_attempts - state.my_attempts)
  const outOfAttempts = attemptsLeft === 0
  // What the button is waiting for. The pause after the text is only the
  // model's answer stream closing; grading is peer grading, so « le modèle
  // réfléchit » clears as soon as the answer landed — no further model call.
  // Sending again is allowed while the round is open: the last attempt is the one kept.
  const submitLabel = busy
    ? 'Le modèle réfléchit…'
    : stale
      ? 'En attente de la reconnexion…'
      : ending
        ? 'La manche se termine…'
        : conv
        ? outOfAttempts
          ? 'Conversation terminée'
          : `Envoyer le prompt ${cur + 1}/${state.max_attempts}`
        : outOfAttempts
          ? 'Plus d’essai pour cette manche'
          : alreadyDone
            ? `Envoyer un nouvel essai (${state.my_attempts + 1}/${state.max_attempts})`
            : 'Envoyer au modèle'

  function submitOnChord(e: KeyboardEvent<HTMLFormElement>) {
    if (e.key !== 'Enter' || !(e.metaKey || e.ctrlKey)) return
    // Same refusals as the button, so a shortcut can never send what a click would not.
    if (busy || stale || ending || outOfAttempts || !text.trim() || tooLong) return
    e.preventDefault()
    e.currentTarget.requestSubmit()
  }

  async function send(e?: FormEvent) {
    e?.preventDefault()
    // Re-checked here, not only on the button: the bound is also enforced by `maxLength`,
    // but a value set past it must fail closed rather than ride a send the server rejects.
    if (tooLong) {
      setErr(`Ton prompt est trop long (${MAX_PROMPT_CHARS} caractères maximum).`)
      return
    }
    setErr('')
    buzz()
    setBusy(true)
    setLive({ text: '' })
    // One frame on the live socket, and the verdict comes back as frames on the
    // same feed. The text the player watched is the verdict's text: the server
    // stored the same string, so one truth reaches the screen. The `done` frame carries no round — the round is the
    // one the prompt was sent on — and the socket never truncates mid-frame.
    const roundNow = state.current
    const slot = cur
    try {
      const done = await new Promise<{ text: string; truncated: boolean }>(
        (resolve, reject) => {
          // Armed before the send: the server may answer faster than React re-renders,
          // and a frame landing before the waiter exists would wait forever.
          waiterRef.current = { player: myName, text: '', resolve, reject }
          answerStart(sendPrompt, text)
        },
      )
      setResult({ output: done.text, round: roundNow, truncated: done.truncated })
      setLive(null)
      // Only the verdict earns the round its answer. Not the first chunk: the row does not exist
      // until the `done` frame, and a composer disabled on the strength of a delta would lock a
      // player out of a round the server has not yet accepted — and lock them out of retrying
      // if the jury then falls over.
      // The prompt stays in the field: the next attempt is usually a correction of this one.
      // In a conversation the sent slot locks and the next one takes the keyboard.
      if (conv) setSentCount(slot + 1)
      setSent(true)
    } catch (e) {
      // A refusal off the socket lands here through the waiter: nothing was
      // persisted, so the composer stays usable instead of locking the player
      // out of a round they never answered.
      setErr(describe(e))
      // Nothing was persisted, so the composer must stay usable and must stop claiming the
      // answer is sent — otherwise a failed send locks the player out of a round they never
      // answered, and there is nothing on screen saying why.
      setSent(false)
      setLive(null)
    } finally {
      setBusy(false)
    }
  }

  // The socket's answer frames, dealt to the waiting send. Chunks accumulate here rather
  // than read back off `live`: a state value captured by the send closure is the value it
  // had when the send started. Frames for other players are the room's business, not this
  // screen's — the server sends chunks only to the author's sockets and admins; the player check
  // below stays as a guard.
  // A refusal is always this screen's own: the server answers it on the sender's connection,
  // never on the broadcast, so it needs no player check.
  //
  // Every frame past the cursor is replayed, not just the newest: several frames can land
  // between two renders, and skipping any of them cut words out of the answer on screen.
  const answerSeq = answer?.seq ?? 0
  const seenRef = useRef(answerSeq)
  useEffect(() => {
    if (!answer) return
    const first = answer.seq - answer.events.length
    const fresh = answer.events.slice(Math.max(0, seenRef.current - first))
    seenRef.current = answer.seq
    for (const ev of fresh) {
      const w = waiterRef.current
      if (!w) return
      if (ev.kind === 'error') {
        waiterRef.current = null
        w.reject(wsRefusal(ev.code, ev.message))
        return
      }
      if (ev.player !== w.player) continue
      if (ev.kind === 'chunk') {
        w.text += ev.delta
      } else if (ev.kind === 'done') {
        waiterRef.current = null
        w.resolve({ text: w.text, truncated: ev.truncated })
        return
      }
    }
    const w = waiterRef.current
    if (w) setLive({ text: w.text })
    // `answer` carries its own seq; depending on the object would miss repeat frames.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [answerSeq])

  // Dropping a waiter whose screen went away: the generation keeps running
  // server-side and the round closes on the answers that do land, so there is
  // nothing to cancel, only a promise nobody will read. `busy` is deliberately
  // not in the deps: the effect must fire on unmount only.
  useEffect(
    () => () => {
      waiterRef.current = null
    },
    [],
  )

  // Time is nearly up: the draft goes out by itself, once per round, on the same skew-corrected
  // clock as the countdown. The server takes it up to a short grace after the deadline.
  const clock = useQuizWindow(state.timer)
  const autoRound = useRef(-1)
  const nearEnd = clock.open && clock.left <= 1000
  useEffect(() => {
    if (!nearEnd || scored || state.phase !== 'playing' || autoRound.current === state.current) return
    autoRound.current = state.current
    if (alreadyDone || busy || stale || ending || outOfAttempts || tooLong || !text.trim()) return
    void send()
    // Fires on the edge into the last second only; `send` reads the render's own values.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nearEnd])

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
    // 'start': the score heads the panel, so its top is what the player must see first.
    v.scrollIntoView({ block: 'start', behavior: reduced() ? 'auto' : 'smooth' })
    // Focus as well as scroll: `block: 'nearest'` moves the page, focus is what tells a
    // screen reader the player has just been handed something.
    v.focus()
  }, [result])

  // The answer panel opens above the composer, out of sight once the player has scrolled down
  // to write: bring its top into view as soon as the model starts, so the text is read where it
  // is being written. Keyed on the panel opening, not on each chunk, so reading is never yanked.
  const liveOpen = live !== null
  useEffect(() => {
    if (!liveOpen) return
    areaRef.current?.blur()
    verdictRef.current?.scrollIntoView({ block: 'start', behavior: reduced() ? 'auto' : 'smooth' })
  }, [liveOpen])

  // The field the next send is written in: the only one on a plain round, the current slot of
  // a conversation.
  const composer = (
    <>
      {/* Two taps are not a keyboard, and on a desktop a textarea you can only leave by
          reaching for the mouse is a textarea half-used. Cmd/Ctrl+Enter submits; a bare
          Enter does not, because a prompt is instruction text and often wants line
          breaks. The chord is also the guard the shortcut needs: it cannot be hit by
          accident while a thumb or a hand is mid-word, which a bare-Enter submit could. */}
      <form className="form" onSubmit={send} onKeyDown={submitOnChord}>
        <div className="field">
          <label className="label" htmlFor="prompt">
            {conv ? `Prompt ${cur + 1} sur ${state.max_attempts}` : 'Ton prompt'}
          </label>
          <AutoArea
            id="prompt"
            ref={areaRef}
            className="area"
            value={text}
            onChange={(e) => (conv ? setDraft(cur, e.target.value) : setPrompt(e.target.value))}
            placeholder={
              conv
                ? cur === 0
                  ? 'Commence par donner le contexte et les informations utiles…'
                  : 'Ton prompt suivant, dans la même conversation…'
                : 'Décris la scène, le ton, la longueur…'
            }
            maxLength={MAX_PROMPT_CHARS}
            // The prompt is a piece of instruction, not prose. Autocorrect and the
            // spellchecker are both actively hostile to it: they "fix" the deliberate
            // misspellings and the invented vocabulary that make a model produce
            // something specific, and they do it while the player is mid-word.
            autoCapitalize="sentences"
            autoCorrect="off"
            spellCheck={false}
            disabled={busy}
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
          disabled={busy || stale || ending || outOfAttempts || !text.trim() || tooLong}
        >
          {submitLabel}
        </button>
        <p className="hint">
          {text.length} / {MAX_PROMPT_CHARS}
        </p>
        <p className="hint">
          <kbd>Ctrl</kbd>/<kbd>⌘</kbd> + <kbd>Entrée</kbd> pour envoyer
        </p>
      </form>
    </>
  )

  // The answer being written, then the verdict it becomes: one place in the document, so the
  // text never jumps. Placed at the top of a plain round, inside the slots of a conversation.
  const livePanel = live && (
    <div className="verdict verdict--live" ref={verdictRef} tabIndex={-1}>
              <div>
                <p className="label verdict__livelabel">
                  <span className="spinner" aria-hidden="true" />
                  Le modèle écrit…
                </p>
                <ModelOutput text={live.text} streaming />
              </div>
            </div>
  )
  const resultPanel = result && (
    <div className="verdict verdict--done" ref={verdictRef} tabIndex={-1}>
              <div className="verdict__top">
                <span className="verdict__round">manche {result.round + 1}</span>
              </div>
              {!scored && result.round === state.current && (
                <p className="prose prose--sm prose--dim">
                  {conv
                    ? outOfAttempts
                      ? `Tu as envoyé tes ${state.max_attempts} prompts : c’est toute la conversation qui sera notée.`
                      : `Continue la conversation (${noun(attemptsLeft, 'prompt restant', 'prompts restants')}) : le modèle se souvient des échanges précédents, et c’est toute la conversation qui est notée.`
                    : outOfAttempts
                      ? `Tu as utilisé tes ${state.max_attempts} essais : c’est ton dernier prompt qui sera noté.`
                      : `Tu peux corriger ton prompt et réessayer (${noun(attemptsLeft, 'essai restant', 'essais restants')}) : seul ton dernier essai compte.`}
                </p>
              )}
              {/* A conversation's answers are read in its slots, in order, not here. */}
              {!conv && <div>
                <p className="label">Le modèle a répondu</p>
                <ModelOutput text={result.output} />
                {/* Said here because the truncation is a fact about the answer, not about the
                    game: the class grades a text that stops mid-sentence. */}
                {result.truncated && (
                  <p className="prose prose--sm prose--dim">
                    La réponse s’arrête ici : le modèle a atteint sa limite de longueur. C’est ce
                    texte, coupé à cet endroit, que les autres joueurs noteront.
                  </p>
                )}
              </div>}
            </div>
  )

  return (
    <div className="deck">
      <ConsoleBar meta={`Partie ${code}`} link={link} onRetry={onRetry} />
      {/* The skip link's target — see the note on `SkipLink` in App.tsx. */}
      <main className={`screen screen--split screen--calm${stale ? ' screen--stale' : ''}`} id="main" tabIndex={-1}>
        <Step current={state.current} total={state.total}>
          {/* The seconds are the server's, read through useQuizWindow: this screen never
              counts down from the phone's own clock. */}
          {!scored && (
            <span data-testid="time-left">
              <PhaseClock timer={state.timer} seconds={state.round_seconds} what="pour écrire ton prompt" />
            </span>
          )}
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

        {/* The score, and it is above the target and above the field rather than under them.
            Under, it rendered at roughly y690 with a long target, and the visual viewport with
            the keyboard up is about 400px: the one thing the player had just earned, invisible,
            with no scroll, no focus move and nothing to say so. Above, it is in the first
            screenful. It keeps its `manche N` label because it survives a round change. */}
        {/* The answer being written, in the same panel as the verdict it becomes and in the same
            place in the document, so the text never jumps. Deliberately WITHOUT a score: a `0`
            or a `—` in the score slot while the text arrives would be a number nobody gave, and
            the slot is the loudest thing on the screen. */}
        {/* The play column: everything the player reads and does, in one box, so the wide layout can
            put it on the left and the standings on the right. As loose children of the screen grid
            they filled whichever cell was free next — on a desktop the meter took the right-hand
            column, the standings fell under the composer, and a score on screen pushed the target
            into the narrow column. */}
        <div className="maincol">
          {/* A conversation draws these inside its slots, beside the prompt they answer. */}
          {(!conv || scored) && livePanel}
          {(!conv || scored) && resultPanel}

          {/* The synthesis of the round that has just been scored, above the target rather than
              on a screen of its own: what came of the last prompt is the reading the player does
              while the next one waits. One number and nothing else: the class's average grade. */}
          {scored && state.summary && (
            <section className="tally">
              <h2 className="label">Ta note de la manche {state.summary.round + 1}</h2>
              {/* An empty round still has its summary: the collective sentence when nobody
                  answered, the personal one when the reader is the one who did not. */}
              {state.summary.rows.length === 0 ? (
                <p className="prose prose--dim">Personne n’a répondu à cette manche.</p>
              ) : (() => {
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
                    </div>
                    {/* A round nobody graded has no score. */}
                    <p className="prose" role="status">
                      Note finale :{' '}
                      {/* The number and its unit are one nowrap unit, and the space between them
                          is a real text node: JSX drops whitespace that sits between two tags on
                          different lines, which is what rendered "6sur 10". */}
                      <span className="num tally__round">
                        {mine.round_score === null ? (
                          'Non noté'
                        ) : (
                          <>
                            {mine.round_score}
                            <span className="tally__roundden">{' '}sur 10</span>
                          </>
                        )}
                      </span>
                      .
                    </p>
                  </div>
                )
              })()}
              {/* Beside the note, the grid it was given against: the player reads why. */}
              <Criteria items={state.target_criteria} />
            </section>
          )}

          {/* The next round is the animator's button alone: the class has no vote on a summary. */}
          {scored && !state.auto_mode && (
            <p className="prose prose--sm prose--dim" role="status">
              L’animateur lance la manche suivante.
            </p>
          )}
          {scored && state.auto_mode && (
            <p className="prose prose--sm prose--dim" role="status">
              Mode automatique : la suite s’enchaîne seule.
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
            {/* Keyed by round so an opened tip closes when the next exercise arrives. */}
            {/* The exercise only while writing: the summary shows the note, not the instructions. */}
            {!scored && (
              <Readout
                key={state.current}
                label="Le modèle doit répondre"
                value={state.target ?? '—'}
                image={state.target_image}
                consigne={!!state.target}
                tip={state.target_tip}
                copy={exerciseContent(state.target) || null}
              />
            )}

            {/* Every slot of a conversation on screen from the start, so the player sees at once
                that the exercise takes several prompts. Sent slots lock and show the model's
                answer; the next one sends; the later ones can be drafted ahead. */}
            {!scored && conv && (
              <section className="thread" aria-label="Ta conversation avec le modèle">
                <p className="prose prose--sm prose--dim">
                  Cet exercice se fait en {state.max_attempts} prompts envoyés l’un après l’autre :
                  chaque prompt continue la même conversation, et le modèle se souvient des
                  échanges précédents. C’est toute la conversation qui est notée.
                </p>
                {Array.from({ length: state.max_attempts }, (_, i) => {
                  const done = turns[i]
                  if (done)
                    return (
                      <div className="thread__turn" key={i}>
                        <p className="label">Prompt {i + 1} · envoyé</p>
                        <pre className="slot">{done.prompt}</pre>
                        <p className="label">Le modèle a répondu</p>
                        <ModelOutput text={done.output} />
                        {i === turns.length - 1 && resultPanel}
                      </div>
                    )
                  if (i === cur) return <div className="thread__turn" key={i}>{composer}{livePanel}</div>
                  // Sent this session but the snapshot with its turn has not landed yet.
                  if (i < cur)
                    return (
                      <div className="thread__turn" key={i}>
                        <p className="label">Prompt {i + 1} · envoyé</p>
                        <pre className="slot">{drafts[i]}</pre>
                        {i === cur - 1 && resultPanel}
                      </div>
                    )
                  return (
                    <div className="thread__turn thread__turn--later" key={i}>
                      <label className="label" htmlFor={`prompt-${i}`}>
                        Prompt {i + 1} sur {state.max_attempts} · à envoyer après le prompt {i}
                      </label>
                      <AutoArea
                        id={`prompt-${i}`}
                        className="area"
                        value={drafts[i] ?? ''}
                        onChange={(e) => setDraft(i, e.target.value)}
                        placeholder={i === 1 ? 'Par exemple : demander une synthèse…' : i === 2 ? 'Par exemple : demander un test d’entraînement…' : 'Une relance, une correction…'}
                        maxLength={MAX_PROMPT_CHARS}
                        autoCapitalize="sentences"
                        autoCorrect="off"
                        spellCheck={false}
                      />
                    </div>
                  )
                })}
              </section>
            )}

            {!scored && !conv && composer}

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
          {/* No answer yet gets its own sentence, above the meter: the counter says how far
              the round has come, this says it has not started. */}
          {noAnswerYet && (
            <p className="prose prose--sm prose--dim" role="status">Aucune réponse pour l’instant…</p>
          )}
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
        </div>

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
