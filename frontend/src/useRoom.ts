import { useCallback, useEffect, useState } from 'react'
import type { State } from './api'

/* What the event stream is doing, as far as the browser can tell. A phone on a LAN drops
 * Wi-Fi silently, sleeps mid-round, and comes back to a frozen board: the stream stops,
 * nothing on screen changes, and the player goes on believing a stale round is the live one.
 * This is the state that stops that. */
export type Link = 'idle' | 'connecting' | 'live' | 'down' | 'failed'

// A phone that sleeps, changes network or walks out of range flaps. That is not a failure and
// must never be dressed as one: the reconnection reads in the calm cool colour, and nothing
// here asserts anything the browser cannot actually see. EventSource cannot read a status
// code, so three silent failures mean only "this is not answering" — never "this room does
// not exist", which would be a guess dressed as a fact.
const LINK_WORDS: Record<Link, string> = {
  idle: '',
  connecting: 'Connexion…',
  live: 'En direct',
  down: 'Reconnexion…',
  failed: 'Partie sans réponse',
}

export const linkWord = (link: Link) => LINK_WORDS[link]

// A room that does not exist answers 404 on the stream, and EventSource cannot read a status
// code, so the only honest signal is that the stream never opened. The browser has already
// retried three times by then, and a phone retrying forever against a bad code is the exact
// "Connexion..." dead end this replaces. After the third miss we stop believing and say so.
const MISSES_BEFORE_FAILED = 3

// The server pings every half second, but those are SSE comment lines and the browser throws
// them away: `onmessage` fires only when the room actually changes. So "the socket is dead"
// and "the room is idle" look the same from in here, and only one of them is worth acting on.
const WATCH_EVERY_MS = 4_000
// A room cannot sit still for this long in the middle of ten rounds and a live leaderboard,
// so silence this long means a half-open socket: the server is gone and the browser will
// never find out, because an SSE GET never writes anything that would fail. Long enough that
// a quiet lobby before the admin presses start costs one socket a minute, which is nothing.
const SILENCE_IS_DEATH_MS = 60_000

export function useRoom(code: string | null, token?: string | null) {
  // The snapshot carries the code it was fetched for, so a route change to another room
  // cannot briefly show the previous room's state. That keeps the reset out of the effect
  // and out of a setState-in-effect.
  const [snap, setSnap] = useState<{ code: string; state: State } | null>(null)
  const [link, setLink] = useState<Link>('idle')
  const [attempt, setAttempt] = useState(0)

  // `token === undefined` is an anonymous stream: the admin, who is not a player. A player
  // whose token is not in this browser yet passes `null` and does not connect at all, because
  // an unattributed stream is worse than no stream — the server would report every player,
  // including the one looking at the screen, as offline.
  const anonymous = token === undefined

  useEffect(() => {
    if (!code) return
    if (!anonymous && !token) return
    // A local const, not the parameter: narrowing does not survive into the callbacks below.
    const room = code
    // The token is how the server knows which player this phone is, and therefore who is
    // online. It rides in the query string, which Django routes past: everything after `?` is
    // ignored, so the stream endpoint is unchanged and the attribution is a query parameter.
    const who = anonymous ? '' : `&token=${encodeURIComponent(token)}`
    let es: EventSource | null = null
    let opened = false
    let dead = false
    let misses = 0
    let lastBeat = Date.now()
    // The counter rides in the query string too, rather than sitting in the dependency list as
    // a value the effect never reads. It buys two real things: the effect has a genuine
    // dependency, and a retry cannot be answered by a connection already given up on.
    let gen = attempt

    const open = () => {
      es?.close()
      const s = new EventSource(`/api/rooms/${room}/events?r=${gen}${who}`)
      s.onopen = () => {
        opened = true
        dead = false
        misses = 0
        lastBeat = Date.now()
        setLink('live')
      }
      s.onmessage = (e) => {
        lastBeat = Date.now()
        setSnap({ code: room, state: JSON.parse(e.data) as State })
        setLink('live')
      }
      s.onerror = () => {
        // Once open, the browser is already reconnecting on its own and the player only
        // needs to be told the readout is stale. Before that, a miss is a bad code, not a
        // bad moment.
        if (opened) {
          setLink('down')
          return
        }
        misses += 1
        if (misses >= MISSES_BEFORE_FAILED) dead = true
        setLink(dead ? 'failed' : 'connecting')
      }
      es = s
    }

    open()

    // The phone slept. Its timers stopped, the socket died underneath it, and the browser
    // notices the dead connection on its own schedule, if at all — and its schedule is a
    // backoff that grows every attempt, so the player can sit on a stale round for a minute
    // after a screen lock. So this does not wait to be told, and opens a new stream itself
    // instead of leaving the recovery to that backoff. It costs nothing to be wrong: the view
    // pushes the entire room snapshot on every connect, so a needless reopen just re-reads
    // the state the player already had.
    //
    // The timer is throttled while the tab is hidden, which is the right way round. There is
    // nobody looking at a hidden tab, and the first tick after the screen comes back is
    // always long overdue, so a phone that slept through a round rejoins on its next beat.
    const watch = setInterval(() => {
      if (dead) return
      // The browser already knows when it has lost the socket: it moves to CONNECTING and
      // starts retrying. Going straight to a fresh attempt skips the backoff it has grown.
      const broken = es?.readyState !== EventSource.OPEN
      // Or it does not, which is the half-open case only silence can catch.
      const silent = Date.now() - lastBeat > SILENCE_IS_DEATH_MS
      if (!broken && !silent) return
      gen += 1
      lastBeat = Date.now()
      setLink('connecting')
      open()
    }, WATCH_EVERY_MS)

    return () => {
      clearInterval(watch)
      es?.close()
    }
  }, [code, token, attempt, anonymous])

  // Called from a click, never from the effect, so re-opening the stream is an event rather
  // than a cascade.
  const retry = useCallback(() => {
    setLink('connecting')
    setAttempt((a) => a + 1)
  }, [])

  const fresh = snap !== null && snap.code === code
  return {
    state: fresh ? snap.state : null,
    // 'connecting' is derived, not stored: nothing has to write it when the room changes.
    link: link === 'idle' && code ? 'connecting' : link,
    retry,
  }
}
