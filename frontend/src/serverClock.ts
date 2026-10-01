/* The didacticiel's clock, and the only place in this console where the time comes from the
 * server rather than from the phone.
 *
 * The fifteen seconds belong to the ROOM: one question, one window, one instant of revelation for
 * every phone in it. §2 of the cahier says so, and it is why a per-player countdown is not a
 * slower feature but a different feature — the student who answers in two seconds and then
 * watches thirteen of a locked screen.
 *
 * Two ways to get that wrong, and this file exists because of both.
 *
 * Starting fifteen seconds on mount is wrong even when the clocks agree: a phone that opens the
 * page ten seconds after the question appeared would show fifteen and offer four buttons the
 * server has already stopped listening to.
 *
 * Counting from the local clock is worse: a phone whose time is two minutes out would lock on the
 * wrong side of the question — either refusing to open the answers for two minutes, or offering
 * them for two minutes after they closed. `QuizState.server_ms` is sent for precisely this and its
 * comment in `api.ts` says so.
 *
 * So there are two numbers here and they are not interchangeable. The SKEW is the offset between
 * the two clocks, and it is measured from a frame of the server's clock and then frozen — because
 * re-deriving it from an old frame against a fresh `Date.now()` injects exactly the drift the
 * field exists to remove: a room that sits still for fifteen seconds sends no frames at all, and a
 * skew recomputed on every render would then report the server's clock as frozen too. The SAMPLE
 * is the local clock re-read on a timer, and it is re-read rather than incremented, so a phone
 * that slept, a background tab whose timers were throttled to once a minute, and a long render
 * all land on the same instant instead of drifting apart from the room.
 *
 * The interval below therefore decides nothing. It samples, and `open` — the lock, the countdown
 * and the admin's button label all at once — is arithmetic on the server's own deadline at render
 * time. A timer that never fires cannot hold the window open, and one that fires late cannot shut
 * it.
 */

import { useEffect, useLayoutEffect, useState } from 'react'
import type { QuizState } from './api'

// A quarter second. Fast enough that the figure on screen is never a whole second behind the
// room, slow enough that a phone held in one hand for fifteen seconds does not wake its CPU four
// times a second to redraw a number that is only read once a second.
const BEAT_MS = 250

// The two numbers, taken at one instant. Read together or not at all: a skew from one frame and a
// sample from the next would be an answer to a question nobody asked.
type Clock = { skew: number; at: number }

export type QuizWindow = {
  /** Whether the server would still take an answer for this question, judged on the SERVER's
   * clock. False before the first reading arrives, and false from the deadline onward — which is
   * the lock, and the same comparison the countdown, the admin's button and the reveal all use. */
  open: boolean
  /** Milliseconds left on the server's clock. Goes negative once the deadline has passed; every
   * caller checks `open` before it reads this, and nothing else. */
  left: number
}

export function useQuizWindow(quiz: QuizState | null): QuizWindow {
  const serverMs = quiz?.server_ms ?? null
  const [clock, setClock] = useState<Clock | null>(null)

  // A layout effect, and that is the whole reason for it. The skew has to be in place before the
  // browser paints, or the first frame of the one screen in this console that IS a clock is drawn
  // from an uncorrected local time — a phone two minutes out flashing fifteen seconds for one
  // frame of a fifteen-second question. There is no server render here, so nothing warns about
  // reading the wall clock here.
  //
  // The setState inside it is the one this file asks the linter to allow, and the rule's own
  // carve-out is the reason: this is synchronisation with an EXTERNAL SYSTEM — a clock that
  // belongs to another process on another machine — and the render it schedules is the same
  // render the interval below schedules four times a second. The alternative that satisfies the
  // rule is to read `Date.now()` in the render body, which is what the rule is actually about, and
  // it costs two impurity warnings to move a legal read into an illegal one.
  useLayoutEffect(() => {
    if (serverMs === null) return
    // oxlint-disable-next-line react/set-state-in-effect
    setClock({ skew: serverMs - Date.now(), at: Date.now() })
  }, [serverMs])

  // The sample. Keyed on `open` rather than on the clock object, so the timer exists exactly while
  // there is a window to count down and is torn down by the same comparison that locks the
  // choices — the moment the deadline passes, or the moment the stream dies mid-window, this
  // stops and nothing is left running behind a closed question.
  const left = clock !== null && quiz !== null ? quiz.deadline_ms - (clock.at + clock.skew) : 0
  const open = clock !== null && quiz !== null && left > 0
  useEffect(() => {
    if (!open) return
    const t = setInterval(() => setClock((c) => (c && { ...c, at: Date.now() })), BEAT_MS)
    return () => clearInterval(t)
  }, [open])

  return { open, left }
}