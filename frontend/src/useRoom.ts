import { useCallback, useEffect, useRef, useState } from 'react'
import type { State } from './api'
import { connectRoomWs, type AnswerEvent, type RoomWs } from './ws'

/* What the room feed is doing, as far as the browser can tell. A phone on a LAN drops
 * Wi-Fi silently, sleeps mid-round, and comes back to a frozen board: the feed stops,
 * nothing on screen changes, and the player goes on believing a stale round is the live one.
 * This is the state that stops that. */
export type Link = 'idle' | 'connecting' | 'live' | 'down' | 'failed'

// A phone that sleeps, changes network or walks out of range flaps. That is not a failure and
// must never be dressed as one: the reconnection reads in the calm cool colour, and nothing
// here asserts anything the browser cannot actually see.
const LINK_WORDS: Record<Link, string> = {
  idle: '',
  connecting: 'Connexion…',
  live: 'En direct',
  down: 'Reconnexion…',
  failed: 'Partie sans réponse',
}

export const linkWord = (link: Link) => LINK_WORDS[link]

// A room that does not exist never answers, and neither transport can read a refusal off
// a socket that never opened, so the only honest signal is that nothing ever opened. The
// browser has already retried three times by then, and a phone retrying forever against a
// bad code is the exact "Connexion..." dead end this replaces. After the third miss we
// stop believing and say so.
const MISSES_BEFORE_FAILED = 3

// The server ticks only when the room changes, so "the socket is dead" and "the room is
// idle" look the same from in here, and only one of them is worth acting on.
const WATCH_EVERY_MS = 4_000
// A room cannot sit still for this long in the middle of ten rounds and a live leaderboard,
// so silence this long means a half-open socket: the server is gone and the browser will
// never find out, because neither transport writes anything that would fail on a dead read.
// Long enough that a quiet lobby before the admin presses start costs one socket a minute,
// which is nothing.
const SILENCE_IS_DEATH_MS = 60_000

// Rollback hatch: with VITE_WS_DISABLED=1 the room rides the Django SSE stream and the
// POST answer exactly as before the migration, so a broken socket layer is one env var
// away from the old path with no code change.
const WS_DISABLED = import.meta.env.VITE_WS_DISABLED === '1'

// The answer stream, as the room broadcasts it. One event per frame plus a sequence
// number, because two chunks for the same player carry identical shapes and a screen
// awaiting a verdict must wake once per frame rather than once per distinct value.
export interface AnswerFeed {
  event: AnswerEvent
  seq: number
}

export function useRoom(code: string | null, token?: string | null) {
  // The snapshot carries the code it was fetched for, so a route change to another room
  // cannot briefly show the previous room's state. That keeps the reset out of the effect
  // and out of a setState-in-effect.
  const [snap, setSnap] = useState<{ code: string; state: State } | null>(null)
  const [link, setLink] = useState<Link>('idle')
  const [attempt, setAttempt] = useState(0)
  const [feed, setFeed] = useState<AnswerFeed | null>(null)
  const [wsUp, setWsUp] = useState(false)
  // The live socket, owned by the effect below. A ref rather than state: the screens
  // never render it, they only speak on it, and a state would re-render the room on
  // every reconnect for a value nothing draws.
  const connRef = useRef<RoomWs | null>(null)

  // `token === undefined` is an anonymous feed: the admin, who is not a player. A player
  // whose token is not in this browser yet passes `null` and does not connect at all, because
  // an unattributed feed is worse than no feed — the server would report every player,
  // including the one looking at the screen, as offline. Anonymous stays on SSE: the admin
  // authenticates by session cookie, the socket only knows tokens, and the console never
  // holds an admin_key to offer it.
  const anonymous = token === undefined
  const useWs = !WS_DISABLED && !anonymous && !!token

  useEffect(() => {
    if (!code) return
    if (!anonymous && !token) return
    // A local const, not the parameter: narrowing does not survive into the callbacks below.
    const room = code
    // The counter rides in the effect deps as a value the reconnect reads. It buys two real
    // things: the effect has a genuine dependency, and a retry cannot be answered by a
    // connection already given up on.
    const gen = attempt

    if (useWs) {
      // The token is how the server knows which player this phone is, and therefore who is
      // online. It rides the hello frame, which the socket answers with a snapshot, so the
      // first frame after a reconnect is the whole room rather than a delta against state
      // the player may have missed while asleep.
      const who = token as string
      let lastBeat = Date.now()
      let misses = 0
      let dead = false
      let opened = false
      // No setLink here: the callers that re-open (retry, silence watch) already set
      // 'connecting' before the attempt bump, and the first mount derives it from 'idle'.
      const conn = connectRoomWs(room, who, {
        onSnapshot: (state) => {
          if (dead) return
          opened = true
          misses = 0
          lastBeat = Date.now()
          setSnap({ code: room, state })
          setLink('live')
          setWsUp(true)
        },
        onAnswer: (event) => {
          lastBeat = Date.now()
          // A refusal names no player and belongs to the sender alone, but it still
          // proves the socket is alive: only the verdict waiter reads it, the link stays.
          if (event.kind === 'error' && (event.code === 'bad_token' || event.code === 'unknown_room')) {
            dead = true
            setLink('failed')
            setWsUp(false)
            return
          }
          setFeed((f) => ({ event, seq: (f?.seq ?? 0) + 1 }))
        },
        onOpen: () => {
          lastBeat = Date.now()
        },
        onDown: () => {
          // A flap, not a verdict: the socket is already climbing its own backoff
          // ladder, and the player only needs to be told the readout is stale.
          if (!opened) {
            misses += 1
            if (misses >= MISSES_BEFORE_FAILED) dead = true
            setLink(dead ? 'failed' : 'connecting')
          } else {
            setLink('down')
          }
          setWsUp(false)
        },
        onFailed: () => {
          dead = true
          setLink('failed')
          setWsUp(false)
        },
      })

      // Same half-open watch as the SSE path below: the keepalive belongs to the socket,
      // but a phone that slept through it wakes to timers that stopped, and the first tick
      // after the screen comes back is always long overdue. Reopening is cheap — the server
      // answers hello with the whole snapshot — so a needless reconnect just re-reads the
      // state the player already had.
      const watch = setInterval(() => {
        if (dead) return
        if (Date.now() - lastBeat <= SILENCE_IS_DEATH_MS) return
        lastBeat = Date.now()
        setLink('connecting')
        setWsUp(false)
        conn.close()
        // Reconnecting is a new connection, not a revived one: the attempt bump tears
        // this effect down and rebuilds it, so no two sockets can ever be open at once.
        setAttempt((a) => (a === gen ? a + 1 : a))
      }, WATCH_EVERY_MS)

      connRef.current = conn
      return () => {
        clearInterval(watch)
        conn.close()
        if (connRef.current === conn) connRef.current = null
      }
    }

    // The fallback path: the pre-migration SSE stream, kept verbatim so the rollback env
    // restores exactly the behaviour it replaces rather than an approximation of it. The
    // token rides in the query string, which Django routes past: everything after `?` is
    // ignored, so the stream endpoint is unchanged and the attribution is a query parameter.
    const who = anonymous ? '' : `&token=${encodeURIComponent(token as string)}`
    let es: EventSource | null = null
    let opened = false
    let dead = false
    let misses = 0
    let lastBeat = Date.now()

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
      lastBeat = Date.now()
      setLink('connecting')
      open()
    }, WATCH_EVERY_MS)

    return () => {
      clearInterval(watch)
      es?.close()
    }
  }, [code, token, attempt, anonymous, useWs])

  // Called from a click, never from the effect, so re-opening the feed is an event rather
  // than a cascade.
  const retry = useCallback(() => {
    setLink('connecting')
    setAttempt((a) => a + 1)
  }, [])

  // The socket's own voice, stable across renders: it speaks on whatever connection the
  // effect currently holds, or nowhere if there is none. The Game screen checks `wsLive`
  // before calling — a prompt sent on a dead socket would be answered by nobody.
  const sendPrompt = useCallback((prompt: string) => {
    connRef.current?.answerStart(prompt)
  }, [])

  const fresh = snap !== null && snap.code === code
  return {
    state: fresh ? snap.state : null,
    // 'connecting' is derived, not stored: nothing has to write it when the room changes.
    link: link === 'idle' && code ? 'connecting' : link,
    retry,
    // The answer channel of the live socket: the Game screen falls back to the POST
    // stream whenever there is no socket to speak on.
    answer: feed,
    sendPrompt,
    wsLive: WS_DISABLED ? false : wsUp,
  }
}
