import { useEffect, useMemo, useRef, useState } from 'react'
import { ConsoleBar, PlayerName, Readout, RoundTrack, Section } from './ui'
import { countdown, fr, noun } from './format'
import { useQuizWindow } from './serverClock'
import {
  ApiError,
  describe,
  getName,
  getToken,
  post,
  type QuizAnswered,
  type QuizBank,
  type State,
} from './api'
import type { Link } from './useRoom'

/* The didacticiel: ONE question for the whole room, thirty seconds that belong to the server,
 * and a reveal that lands for everyone at the same instant.
 *
 * The screen owns none of the sequence. Which question is on screen is `state.quiz.index`, whether
 * the window is still open is a comparison against the server's own deadline, and whether THIS
 * player has already answered comes from the bank fetch. There is no cursor here, and that is the
 * whole point: a cursor is a browser invention, so a reload used to throw it away, the player came
 * back to question one, answered it again, got the unique-constraint 409, and — because « Suivant »
 * is only drawn once the reveal is on screen and a 409 never produces one — had no way forward at
 * all. Not a bad end to a question. An unreachable end of the didacticiel.
 */

// The letters are on the buttons and not in the text: a class of ten reading the same four rows
// of prose aloud should not also have to hear "A point virgule", and the letter is the one thing
// a screen reader user gets for free from the row's own name. They are also what the reveal
// speaks in — the one fact about a QCM that is unambiguous to a player who cannot see either of
// the two colours.
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
  const [bank, setBank] = useState<QuizBank | null>(null)
  // What this browser has sent, keyed by the question's `order`.
  //
  // `choice: null` is the one case the room cannot resolve for us: the server said this player had
  // already answered, so the lock is known and the choice is not. It is a real state — a phone that
  // reloaded mid-question — and it is why the reveal below has a third verdict rather than two.
  //
  // A map rather than one choice, because the ROOM moves the question and this screen does not. A
  // value keyed to the room's index cannot go stale, and it is what holds the lock in place between
  // the POST and the next frame — the POST is the authority on its own outcome, and the stream is
  // half a second behind a thumb.
  const [answered, setAnswered] = useState<Record<number, number | null>>({})
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const revealRef = useRef<HTMLDivElement>(null)

  const quiz = state.quiz
  // The only clock on this screen, and it is the server's. See `serverClock.ts`: the skew is
  // measured once per frame and frozen, and `open` is a comparison against `deadline_ms` rather
  // than a timer that has been running since mount.
  const { open, left } = useQuizWindow(quiz)

  // Fetched once. An effect because the bank does not arrive with the snapshot and cannot be
  // derived from it; keyed on the room rather than on anything that moves, so a frame arriving
  // every 500ms does not refetch eight questions fifty times a round.
  //
  // The token rides the query string for the same reason it does on the stream: this browser is
  // the player's identity for this room, and a GET has no body to put it in. It buys
  // `answered_orders` — which questions of this room's bank THIS player has already sent — and
  // nothing else, which is a count of the caller's own rows and discloses no other player.
  //
  // `correct` and `explanation` never arrive with a question; they are in the snapshot after the
  // deadline, or in the POST response, and this screen reads neither before `revealed` is true.
  useEffect(() => {
    let gone = false
    void (async () => {
      try {
        const res = await fetch(
          `/api/rooms/${code}/quiz?token=${encodeURIComponent(getToken(code) ?? '')}`,
          { headers: { Accept: 'application/json' } },
        )
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const data = (await res.json()) as QuizBank
        if (gone) return
        setBank(data)
      } catch (e) {
        if (gone) return
        setErr(describe(e))
      }
    })()
    return () => {
      gone = true
    }
  }, [code])

  const questions = bank?.questions ?? null
  const total = questions?.length ?? 0
  const index = quiz?.index ?? 0

  // Which row of the bank the room is showing. Matched on `order` first and by position second,
  // because the two are the same thing for a bank drawn in order and only one of them is right for
  // a bank that is not. The fallback is the difference between a question and a blank panel, and a
  // blank panel on the eighth question of eight is a player who cannot finish.
  const question = questions?.find((q) => q.order === index) ?? questions?.[index] ?? null
  // And what we name it when we answer it. The server looks the question up by `order`, so this is
  // what the POST carries and what `answered_orders` is checked against — never the position.
  const order = question?.order ?? index

    // Three separate facts about whether this player may answer, and they are three because they are
  // three different situations. `inSession` is what this browser just POSTed, `fromBank` is what the
  // server already had when this screen asked, and the lock is their OR.
  //
  // Keeping `fromBank` alone would be wrong in a way that only shows up mid-question: the bank
  // arrives after the first render, so a player who taps an answer in the first half second would
  // see the notice flip from "Réponse envoyée" to "tu avais déjà répondu" the moment their own
  // answer showed up in a fetch that had not even happened yet. Both together, never one.
  const inSession = answered[order] !== undefined
  const picked = inSession ? (answered[order] ?? null) : null
  const fromBank = (bank?.answered_orders ?? []).includes(order)
  const sent = inSession || fromBank
  // The reload: the server had this one and this browser did not. Stable, because `inSession` is
  // false and stays false until the player answers something.
  const lockedBefore = fromBank && !inSession
  // The 409: this browser tried and the server said there was already an attempt.
  const refused = inSession && !fromBank && picked === null

  // Everything this player has answered, from both places it can come from. Only used for the
  // ranking at the end, where the question is no longer "have I done this one" but "have I done
  // them all" — and a count is exactly the right shape for that question, which is why
  // `answered_orders` is a list and this is where it earns that.
  const answeredOrders = useMemo(
    () => new Set([...(bank?.answered_orders ?? []), ...Object.keys(answered).map(Number)]),
    [bank, answered],
  )

  // The reveal. `revealed` is the server's own `now >= deadline` and is the ONLY thing that makes
  // the key and the explanation public, so both are read through it and neither is cached.
  const revealed = quiz?.revealed === true && typeof quiz.correct === 'number'
  const key = revealed && quiz ? quiz.correct : null
  const explanation = revealed && quiz ? quiz.explanation : null
  const right = key !== null && picked !== null && picked === key

  // The ranking, and when it is allowed to arrive. Answering the eighth question is not the end of
  // it: the reveal for that eighth question has not happened yet, and a leaderboard appearing
  // twenty-eight seconds early is a scroll the player did not ask for in the middle of the
  // suspense the whole phase is built on. So it waits for the reveal, and then it is there for as
  // long as the host takes to comment — which is the only moment a player is actually looking
  // for it.
  const finished =
    total > 0 && answeredOrders.size >= total && (revealed || question === null)

  // The lock is the same comparison as the clock and the reveal, so the three can never disagree:
  // either this browser has answered, or the server would refuse the answer now.
  const locked = sent || !open || busy

  // One sentence, and it is the screen's only live region. A pure function of the question's
  // state is what keeps it from being noise: a live region announces on CHANGE, so this string
  // changes on five events per question — the question, the lock, the answer, the window closing,
  // the reveal — and never on the countdown. Which is why the visible notice below is NOT a live
  // region of its own: two regions carrying the same four transitions is a screen that says
  // everything twice to the one reader who cannot see either copy.
  const voice = revealed
    ? picked === null
      ? 'La bonne réponse est affichée.'
      : right
        ? 'Tu as juste.'
        : 'Pas celle-là.'
    : lockedBefore
      ? `Tu avais déjà répondu à la question ${index + 1}. Elle reste verrouillée.`
      : refused
        ? 'Tu avais déjà répondu à celle-ci. La bonne réponse arrive à la fin du chrono.'
        : sent && open
          ? 'Réponse envoyée. La bonne réponse arrive à la fin du chrono.'
          : !open && quiz !== null
            ? `Temps écoulé pour la question ${index + 1}.`
            : `Question ${index + 1} sur ${total}.`

  async function answer(choice: number) {
    if (locked || !question) return
    setBusy(true)
    setErr('')
    try {
      await post<QuizAnswered>(`/api/rooms/${code}/quiz`, {
        token: getToken(code),
        question: order,
        choice,
      })
      // Typed, and then deliberately not read. This response carries the key — `correct_index`
      // and the explanation — and reading it here is exactly the bug this phase exists to avoid:
      // a player who chooses and is told "juste" in the same second has learned nothing and has
      // thirty seconds of screen left to stare at. What the POST buys is the right to stop
      // tapping. What to draw is decided by `state.quiz.revealed`, which the server sets at the
      // deadline and not one millisecond before.
      setAnswered((a) => ({ ...a, [order]: choice }))
    } catch (e) {
      // A 409 here means the server knows an attempt on this question and we did not, which is
      // the reload race the bank fetch cannot cover: the bank was read before the answer landed,
      // or the phone came back after it. There is no cursor to move any more, so there is nothing
      // to skip to — the honest repair is to record the lock and let the reveal say what it says.
      // Matched on the reason as well as the status, because `already answered` is also what the
      // same route returns when the didacticiel has closed under the player, and that one wants
      // the ordinary error line, already translated in `api.ts` for this phase.
      if (e instanceof ApiError && e.status === 409 && e.message === 'already answered') {
        setAnswered((a) => ({ ...a, [order]: null }))
        return
      }
      setErr(describe(e))
    } finally {
      setBusy(false)
    }
  }

  // The explanation is the point of the screen, and the verdict above it is the point of the
  // explanation. A keyboard or screen-reader user has just watched four buttons go dead and a
  // panel appear above them: a change nothing announces. Focus is what says "you have been handed
  // something now", and `block: 'nearest'` is the smallest scroll that will do it, so a short phone
  // is not yanked past the answer it just earned.
  useEffect(() => {
    if (!revealed) return
    const el = revealRef.current
    if (!el) return
    el.scrollIntoView({ block: 'nearest' })
    el.focus()
  }, [revealed])

  // One notice, and it is NOT a live region. Every sentence here is also said by the region
  // above — which is what a screen reader hears — and a second live region carrying the same
  // transitions is a screen that says everything twice. This copy is for the reader looking at
  // the phone: it is in the calm cool colour the console uses for facts about people rather than
  // about faults, and it sits above the question so the first thing a player reads is why they
  // are not being offered four answers.
  const notice = lockedBefore
    ? 'Tu avais déjà répondu à cette question : elle est verrouillée pour toi.'
    : refused
      ? 'Tu avais déjà répondu à celle-ci. La bonne réponse arrive à la fin du chrono.'
      : sent && open
        ? 'Réponse envoyée. La bonne réponse arrive à la fin du chrono.'
        : !open && !revealed && quiz !== null
          ? 'Temps écoulé. La bonne réponse arrive.'
          : ''

  const secs = countdown(left)

  // The class ranking, sorted on the didacticiel's own score. Sorted here rather than asked of
  // the snapshot: `Player.quiz_score` is per player already, and the server sorts the game
  // standings by something else entirely, so the one order this screen needs is not the one
  // that arrives. A stable sort leaves equal scores in join order, and a tie is a tie — no
  // player is handed a place they did not take.
  const myName = getName(code)
  const me = state.players.find((p) => p.name === myName)
  const rows = [...state.players].sort((a, b) => (b.quiz_score ?? 0) - (a.quiz_score ?? 0))
  const waiting = state.players.filter((p) => !p.quiz_done)

  return (
    <div className="deck">
      <ConsoleBar meta={`Partie ${code}`} link={link} onRetry={onRetry} />
      {/* The skip link's target — see the note on `SkipLink` in App.tsx. */}
      <main className="screen screen--narrow" id="main" tabIndex={-1}>
        {/* The one heading on this screen, and the target the phase change moves focus to. It
            says what the screen is rather than which question it is on, because the clock label
            and the panel below already say that and would be heard twice otherwise. */}
        <h1 className="visually-hidden" tabIndex={-1}>
          Didacticiel du prompt engineering
        </h1>

        {/* One live region for the whole screen, mounted from the first frame because a region
            that appears at the same moment as its first message is not reliably announced. It
            carries the question, the answer, the closing of the window and the verdict — never
            the seconds, which change four times a second and would talk over everything else. */}
        <p className="visually-hidden" aria-live="polite">
          {voice}
        </p>

        {!questions && !err && <p className="prose prose--dim">Lecture des questions…</p>}

        {notice && <p className="notice">{notice}</p>}

        {question && quiz && (
          <>
            {/* Eight questions are a sequence before they are eight screens, so they get the
                round's own track: the ticks say "third of eight" and the panel label says it in
                words, which is the same contract `RoundTrack` already has. The index is passed
                raw, 0-based, because `RoundTrack` compares it against its own 0-based ticks —
                handing it `index + 1` lit the second tick while the panel said "Question 1". */}
            <RoundTrack current={index} total={total} />

            {/* The shared clock. Read off `deadline_ms` against the server's own clock, so a
                phone that opens this page ten seconds late shows twenty seconds rather than thirty
                and a phone whose time is two minutes out locks on the same side of the question
                as everybody else. When the window is shut the number is replaced by the word,
                because "0" running down into minus one is not a state the room is ever in. */}
            <p className="qclock" data-state={open ? (secs > 5 ? 'open' : 'soon') : 'closed'}>
              {open ? (
                <>
                  <span className="qclock__n num">{secs}</span>
                  <span className="qclock__t">
                    {secs > 1 ? 'secondes pour répondre' : 'seconde pour répondre'}
                  </span>
                </>
              ) : (
                <span className="qclock__t qclock__t--closed">
                  {revealed ? 'Fenêtre fermée' : 'Révélation…'}
                </span>
              )}
            </p>

            <Readout label={`Question ${index + 1} sur ${total}`} value={question.prompt} />
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
                standing up, one-handed, in a room, eight times in a row. `disabled` rather than
                removed once answered, so the list a player just chose from stays on screen where
                their thumb is, and the verdict lands on it rather than replacing it. */}
            <div className="choices">
              {question.choices.map((text, i) => {
                const isKey = key !== null && i === key
                const isYours = i === picked
                // The missed row is drawn only once there is a verdict to miss: before the reveal
                // the player's own row is marked by nothing at all, which is the entire feature.
                const missed = isYours && key !== null && !isKey
                return (
                  <button
                    key={text}
                    type="button"
                    className={`btn choice${isKey ? ' choice--right' : ''}${missed ? ' choice--miss' : ''}${isYours && key !== null ? ' choice--yours' : ''}`}
                    disabled={locked}
                    onClick={() => void answer(i)}
                  >
                    <span className="choice__k" aria-hidden="true">
                      {LETTERS[i] ?? ''}
                    </span>
                    <span className="choice__t">{text}</span>
                    {/* Said three ways, and none of them is the colour: the mark is a shape, the
                        word is a sentence, and the tag above them is where the console puts what
                        it wants read across a room. `--good` and `--fault` are reinforcement for
                        the reader who has both of them, which is not everyone in a bright
                        classroom on a phone held at arm's length. */}
                    {isKey && (
                      <span className="choice__tag">
                        <span className="choice__mark" aria-hidden="true">
                          ✓
                        </span>
                        Bonne réponse
                      </span>
                    )}
                    {missed && (
                      <span className="choice__tag">
                        <span className="choice__mark" aria-hidden="true">
                          ✗
                        </span>
                        Ta réponse
                      </span>
                    )}
                    {/* At most one tag per row, and never the same word twice. On the row the
                        player got right there is nothing left to say beyond "Bonne réponse":
                        there is only one green row on the screen, so it IS their row, and the
                        verdict below says so in words. Squeezing a second tag in there instead
                        cost the answer itself three wrapped lines on a 390px phone. */}
                  </button>
                )
              })}
            </div>

            {revealed && (
              <>
                {/* The verdict, in words, before the reason. Three sentences because there are
                    three truths: the player was right, the player was wrong, or the server had an
                    attempt this screen did not know about. The middle one names both letters, so
                    the sentence is complete without reading a single colour on the page — and the
                    third takes no colour at all, because nothing about it is right or wrong. */}
                <div
                  className={`revealnote${
                    right ? ' revealnote--good' : picked === null ? '' : ' revealnote--miss'
                  }`}
                  ref={revealRef}
                  tabIndex={-1}
                >
                  {picked === null ? (
                    <>
                      <span className="revealnote__t">Tu avais déjà répondu à celle-ci</span>
                      <span className="revealnote__s">
                        La bonne réponse était {key !== null ? LETTERS[key] : ''}.
                      </span>
                    </>
                  ) : right ? (
                    <>
                      <span className="revealnote__t">Tu as juste</span>
                      <span className="revealnote__s">
                        C’est bien la réponse {LETTERS[picked]} que tu avais choisie.
                      </span>
                    </>
                  ) : (
                    <>
                      <span className="revealnote__t">Pas celle-là</span>
                      <span className="revealnote__s">
                        Tu avais choisi la {LETTERS[picked]}, la bonne réponse était la{' '}
                        {key !== null ? LETTERS[key] : ''}.
                      </span>
                    </>
                  )}
                </div>

                {explanation && (
                  <div className="explain">
                    <p className="label">Pourquoi</p>
                    <p className="prose">{explanation}</p>
                  </div>
                )}
              </>
            )}

            {err && (
              <p className="err err--inline" role="alert">
                {err}
              </p>
            )}
          </>
        )}

        {/* The eight-question ranking, and only the didacticiel's. It is here rather than on a
            separate screen because the player has just been told what they got right and the
            question they actually have is "was that good". It is deliberately not the game's
            leaderboard and not `Board`: the didacticiel score never enters the final ranking, so
            drawing it with the instrument that draws the standings would be drawing the same
            object twice in two places with two different meanings.
            *
            It appears UNDER the question rather than replacing it, which the local cursor used to
            do. There is no cursor now, and the room does not stop talking about the eighth answer
            because a player has finished the eighth question — the explanation they just earned
            is still the thing they are reading. */}
        {finished && (
          <>
            <div className="qboard">
              <p className="qboard__mine">
                <span className="qboard__score num">{fr(me?.quiz_score ?? null)}</span>
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
                    <PlayerName
                      name={p.name}
                      isMe={p.name === myName}
                      className={p.name === myName ? 'board__name board__name--me' : 'board__name'}
                    />
                    <span className="qboard__pts num">{fr(p.quiz_score)}</span>
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
                ? 'Tout le monde a terminé. L’animateur ouvre la partie.'
                : `En attente de ${noun(waiting.length, 'joueur', 'joueurs')}.`}
            </p>
          </>
        )}
      </main>
    </div>
  )
}