import { useEffect, useRef, useState } from 'react'
import { ConsoleBar, Readout, RoundTrack, Section } from './ui'
import {
  ApiError,
  describe,
  getName,
  getToken,
  post,
  type Question,
  type QuizAnswered,
  type QuizBank,
  type State,
} from './api'
import type { Link } from './useRoom'

/* The didacticiel: one question on screen at a time, the answer revealed the moment it is
 * given, and the eight-question ranking at the end. */

// The bank is fetched once from its own endpoint, which is right — eight questions and their
// order are drawn per room, and the snapshot is rebuilt every 500ms. `correct` never arrives
// with a question, so nothing here can spoil the next one: the answer comes back from the POST
// that answers it, which is the only copy of it the client ever holds. The shapes live in
// `api.ts` beside the rest of the wire contract, not here.
//
// `answered` is declared here rather than in `api.ts` because that file is the frozen half of the
// wire contract and this field is landing in it separately. `QuizBank` plus the one field is
// still a named shape: `questions` is checked, so a change to the bank still fails to compile
// here, which is the whole reason for typing a response instead of casting the payload loose.
type ResumableBank = QuizBank & {
  // How many of THIS player's questions are already in the database, counted by the server. It
  // is that player's own history and nothing else: no other player is named, counted or implied
  // by it, so the endpoint stays exactly as unauthenticated as it was — the token identifies
  // whose count to report, it does not unlock anything.
  answered: number
}

// The letters are on the buttons and not in the text: a class of ten reading the same four
// rows of prose aloud should not also have to hear "A point virgule", and the letter is the one
// thing a screen reader user gets for free from the row's own name.
const LETTERS = ['A', 'B', 'C', 'D']

export default function Quiz({
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
  const [questions, setQuestions] = useState<Question[] | null>(null)
  // Which question is on screen, and what has already been answered on it.
  //
  // It starts at whatever the server says this player has already answered, not at 0, and that
  // is the whole fix for the locked player. The cursor used to be a pure browser invention, so a
  // reload threw it away: the player came back to question 0, answered it again, hit the unique
  // constraint, got a 409, and — because « Suivant » is only rendered once the reveal is on
  // screen and a 409 never produces one — had no way forward at all. Not a bad end to a
  // question: an unreachable end of the didacticiel, and with it a player who can never post
  // eight attempts, a room whose automatic opening never fires, and a ranking §2 promises them.
  //
  // The server is the authority on it because it is the only party that knows what is in the
  // database. The cursor stays local once set, because advancing is a re-render and asking the
  // server after every question would put a round trip between a button and the next question.
  const [at, setAt] = useState(0)
  const [picked, setPicked] = useState<number | null>(null)
  // `correct_index`, not `correct`: the reveal has to point at the good choice, and the two
  // only differ on a wrong answer — which is exactly when the screen most needs to be right.
  const [why, setWhy] = useState<QuizAnswered | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  // Said once, after the cursor has jumped forward over a question the player cannot re-take.
  // Separate from `err` because it is not a fault: nothing failed, the server simply knows more
  // than the screen did, and the copy has to sound like that rather than like an error.
  const [resumed, setResumed] = useState('')
  // The running tally, from this screen's own POSTs. The snapshot carries it too, but the
  // stream is up to half a second behind a button press and the ranking at the end of the quiz
  // is the one number a player is looking at when they reach it.
  const [score, setScore] = useState<number | null>(null)
  const revealRef = useRef<HTMLDivElement>(null)

  // Fetched once. An effect because the bank does not arrive with the snapshot and cannot be
  // derived from it; keyed on the room rather than on anything that moves, so a frame arriving
  // every 500ms does not refetch eight questions fifty times a round.
  //
  // The token rides the query string for the same reason it does on the stream: this browser is
  // the player's identity for this room, and a GET has no body to put it in.
  useEffect(() => {
    let gone = false
    void (async () => {
      try {
        const res = await fetch(
          `/api/rooms/${code}/quiz?token=${encodeURIComponent(getToken(code) ?? '')}`,
          { headers: { Accept: 'application/json' } },
        )
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const data = (await res.json()) as ResumableBank
        if (gone) return
        setQuestions(data.questions)
        // Clamped to the bank rather than trusted raw. The count is this player's and the
        // questions are this room's, so it cannot exceed the length — but a cursor is an index
        // into an array, and an index one past the end renders a blank screen instead of the
        // ranking. A clamp turns any disagreement between the two into the finished screen,
        // which is where a player who has answered everything belongs anyway.
        //
        // `Number.isFinite` before the clamp, not after, because `Math.min(undefined, 8)` is
        // `NaN` rather than 0 and a `NaN` index makes `questions[at]` undefined — which would
        // blank the screen entirely instead of starting at question 1. A server that has not
        // sent the field yet therefore behaves like the one that sends zero, which is the only
        // sensible thing for a resume to degrade to.
        const done = Number.isFinite(data.answered)
          ? Math.max(0, Math.min(data.answered, data.questions.length))
          : 0
        if (done > 0) {
          setAt(done)
          setResumed(
            done >= data.questions.length
              ? 'Tu avais déjà terminé le didacticiel. Voici le classement.'
              : `Reprise à la question ${done + 1} : les ${done} précédentes sont déjà validées.`,
          )
        }
      } catch (e) {
        if (!gone) setErr(e instanceof Error ? e.message : 'quiz indisponible')
      }
    })()
    return () => {
      gone = true
    }
  }, [code])

  const total = questions?.length ?? 0
  const question = questions?.[at] ?? null
  const finished = questions !== null && at >= total
  const answered = why !== null

  // The explanation is the point of the screen. A keyboard or screen-reader user has just
  // activated a button and the next thing that happens is that four buttons go dead and a
  // paragraph appears above them — a change nothing announces. Focus is what says "you are
  // handed something now", and `block: 'nearest'` is the smallest scroll that will do it, so a
  // short phone is not yanked past the answer it just earned.
  useEffect(() => {
    if (!why) return
    revealRef.current?.scrollIntoView({ block: 'nearest' })
    revealRef.current?.focus()
  }, [why])

  async function answer(choice: number) {
    if (answered || busy) return
    setBusy(true)
    setErr('')
    try {
      const res = await post<QuizAnswered>(
        `/api/rooms/${code}/quiz`,
        { token: getToken(code), question: at, choice },
      )
      setPicked(choice)
      setWhy(res)
      setScore(res.score)
    } catch (e) {
      // A 409 here is the server saying this player already has an attempt on THIS question,
      // which after a reload is the whole bug: the screen thinks it is on an unanswered
      // question, the database says otherwise, and only one of the two can move. It has to be
      // the screen.
      //
      // Matched on the status AND the reason rather than on the status alone. `already answered`
      // is also what the same route returns when the didacticiel has closed under the player, and
      // jumping the cursor forward in that case would walk them off the end of the bank and
      // strand them somewhere worse than where they started. Any other 409 keeps the ordinary
      // error line, and `quiz not open` is already translated in `api.ts` for exactly this phase.
      if (e instanceof ApiError && e.status === 409 && e.message === 'already answered') {
        skip()
        return
      }
      setErr(describe(e))
    } finally {
      setBusy(false)
    }
  }

  // Forward one question, saying why. Shared by the resume and by the 409 so the two can never
  // disagree about where "the next question" is, and clamped at `total` rather than at the last
  // question: a 409 on the eighth means this player has already answered the eighth, and the
  // only place left to send them is the ranking, which is exactly where the spec puts a player
  // who has finished. Clamping one earlier would leave them tapping a button that 409s forever.
  function skip() {
    setAt((a) => Math.min(a + 1, total))
    setPicked(null)
    setWhy(null)
    setErr('')
    setResumed('Tu avais déjà répondu à celle-là. On continue à la suivante.')
  }

  function next() {
    setAt((a) => a + 1)
    setPicked(null)
    setWhy(null)
    setErr('')
    // Said once, then cleared: it describes the question the player was on when they arrived,
    // and carrying it onto the eighth would be a note about a question that is now off screen.
    setResumed('')
  }

  // The class ranking, sorted on the didacticiel's own score. Sorted here rather than asked of
  // the snapshot: `Player.quiz_score` is per player already, and the server sorts the game
  // standings by something else entirely, so the one order this screen needs is not the one
  // that arrives. A stable sort leaves equal scores in join order, and a tie is a tie — no
  // player is handed a place they did not take.
  const myName = getName(code)
  const rows = [...state.players].sort((a, b) => (b.quiz_score ?? 0) - (a.quiz_score ?? 0))
  const waiting = state.players.filter((p) => !p.quiz_done)

  return (
    <div className="deck">
      <ConsoleBar meta={`Partie ${code}`} link={link} onRetry={onRetry} />
      {/* The skip link's target — see the note on `SkipLink` in App.tsx. */}
      <main className="screen screen--narrow" id="main" tabIndex={-1}>
        {/* The one heading on this screen, and the target the phase change moves focus to. It
            says what the screen is rather than which question it is on, because the panel
            label below already says that and would be heard twice otherwise. */}
        <h1 className="visually-hidden" tabIndex={-1}>
          Didacticiel du prompt engineering
        </h1>

        {/* One live region for the whole screen, mounted from the first frame because a region
            that appears at the same moment as its first message is not reliably announced. It
            carries the progress when nothing has been answered yet and the verdict once there
            is one — the reveal is the part of this screen that happens to the player rather
            than to the room, so it is the part worth a voice. */}
        <p className="visually-hidden" aria-live="polite">
        {why
          ? why.correct_index === picked
            ? 'Bonne réponse.'
            : 'Réponse incorrecte.'
          : question
            ? `Question ${at + 1} sur ${total}.`
            : ''}
        </p>

        {!questions && !err && <p className="prose prose--dim">Lecture des questions…</p>}

        {/* One line, in the calm colour the console uses for facts about people rather than
            faults, because that is what this is: a statement about where the player is, not a
            complaint. It is `role="status"` rather than `role="alert"` for the same reason — an
            alert interrupts, and being told you have resumed is not an interruption. It sits
            above the question, where the first thing a player reads is an explanation of why
            they are not on question 1. Rendered even with no question on screen, because the
            case that most needs saying is the one where the cursor has just run off the end. */}
        {resumed && (
          <p className="notice" role="status">
            {resumed}
          </p>
        )}

        {question && (
          <>
            {/* Eight questions are a sequence before they are eight screens, so they get the
                round's own track: the ticks say "third of eight" and the panel label says it
                in words, which is the same contract `RoundTrack` already has. */}
            <RoundTrack current={at + 1} total={total} />
            <Readout label={`Question ${at + 1} sur ${total}`} value={question.prompt} />
          </>
        )}

        {err && !question && (
          <p className="err" role="alert">
            Le didacticiel ne répond pas. Préviens l’animateur.
          </p>
        )}

        {question && (
          <>
            {/* Four full-width buttons, never two columns and never a dropdown: this is played
                standing up, one-handed, in a room, ten times in a row. `disabled` rather than
                removed once answered, so the list a player just chose from stays on screen
                where their thumb is, and the verdict lands on it rather than replacing it. */}
            <div className="choices">
              {question.choices.map((text, i) => {
                const right = why !== null && i === why.correct_index
                const yours = why !== null && i === picked
                const missed = yours && !right
                return (
                  <button
                    key={text}
                    type="button"
                    className={`btn choice${right ? ' choice--right' : ''}${missed ? ' choice--miss' : ''}${yours && right ? ' choice--yours' : ''}`}
                    disabled={answered || busy}
                    onClick={() => void answer(i)}
                  >
                    <span className="choice__k" aria-hidden="true">
                      {LETTERS[i] ?? ''}
                    </span>
                    <span className="choice__t">{text}</span>
                    {/* Said in words, not by colour: sodium for the right answer and the fault
                        red for the missed one is a pair a player who cannot separate those two
                        hues would otherwise have to guess at. The label is the whole signal and
                        the colour is only reinforcement. */}
                    {right && <span className="choice__tag">Bonne réponse</span>}
                    {missed && <span className="choice__tag">Ta réponse</span>}
                    {yours && right && <span className="choice__tag">Ta réponse</span>}
                  </button>
                )
              })}
            </div>

            {why && (
              <div className="explain" ref={revealRef} tabIndex={-1}>
                <p className="label">Pourquoi</p>
                <p className="prose">{why.explanation}</p>
              </div>
            )}

            {err && (
              <p className="err err--inline" role="alert">
                {err}
              </p>
            )}

            {answered && (
              <button type="button" className="btn btn--signal btn--wide" onClick={next}>
                {at + 1 >= total ? 'Voir le classement' : 'Question suivante'}
              </button>
            )}
          </>
        )}

        {/* The eight-question ranking, and only the didacticiel's. It is here rather than on a
            separate screen because the player has just been told what they got right and the
            question they actually have is "was that good". It is deliberately not the game's
            leaderboard and not `Board`: the didacticiel score never enters the final ranking,
            so drawing it with the instrument that draws the standings would be drawing the same
            object twice in two places with two different meanings. */}
        {finished && (
          <>
            <div className="qboard">
              <p className="qboard__mine">
                <span className="qboard__score num">{score ?? state.quiz?.score ?? 0}</span>
                <span className="qboard__den">bonnes réponses sur {total}</span>
              </p>
              <p className="prose prose--sm prose--dim">
                Ce score n’entre pas dans le classement de la partie : il dit ce que tu savais
                avant de commencer, pas ce que tu écriras.
              </p>
            </div>

            <Section title="Classement du didacticiel">
              <div className="qboard__list">
                {rows.map((p, i) => (
                  <div className="qboard__row" key={p.name}>
                    <span className="board__rank">{i + 1}</span>
                    <span className={p.name === myName ? 'board__name board__name--me' : 'board__name'}>
                      {p.name}
                      {p.name === myName && <span className="board__tag">toi</span>}
                    </span>
                    <span className="qboard__pts num">{p.quiz_score ?? 0}</span>
                    <span className="qboard__of num">/ {total}</span>
                  </div>
                ))}
              </div>
            </Section>

            {/* The gate is still closed, and this is the sentence that says so instead of
                leaving a player who has finished to wonder whether the game has started. It is
                the same count the room is waiting on, so it can never be more or less true than
                the game is. */}
            <p className="prose prose--sm prose--cool">
              {waiting.length === 0
                ? 'Tout le monde a terminé. La partie démarre…'
                : `En attente de ${waiting.length} ${waiting.length > 1 ? 'joueurs' : 'joueur'}.`}
            </p>
          </>
        )}
      </main>
    </div>
  )
}