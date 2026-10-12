import type { State } from './api'

// One answer frame off the socket, in the shape the screens already consume. `error`
// is transport-level (refusals the POST used to carry as HTTP statuses); the three
// others are the stream the POST used to carry as SSE frames.
export type AnswerEvent =
  | { kind: 'chunk'; player: string; delta: string }
  | { kind: 'scoring'; player: string }
  | { kind: 'done'; player: string; score: number; feedback: string; truncated: boolean }
  | { kind: 'error'; code: string; message: string }

export interface RoomWsHandlers {
  onSnapshot: (state: State) => void
  onAnswer: (event: AnswerEvent) => void
  onOpen: () => void
  // Any frame at all, pongs included: proof the socket is alive even when the
  // room itself is quiet.
  onAlive: () => void
  // Closed but retrying: the readout goes stale, not dead.
  onDown: () => void
  // Closed and not retrying: bad token or unknown room, retrying cannot fix it.
  onFailed: () => void
}

export interface RoomWs {
  answerStart: (prompt: string) => void
  close: () => void
}

// The reconnect ladder, in the order it is climbed: half a second, one, two, then
// doubling to a ten-second ceiling where it stays. Short first because the common
// case is a network flap, not an outage, and a phone that changed cells should not
// stare at a stale board for ten seconds before its first retry.
const BACKOFFS_MS = [500, 1_000, 2_000, 4_000, 8_000, 10_000]
// The server holds no per-connection timer, so a silent half-open socket is found
// only by its own keepalive going unanswered. Twenty seconds is far below the
// snapshot watchdog in useRoom, so the socket is the thing that notices first.
const PING_EVERY_MS = 20_000
// A patch burst (join storm, rating sweep) resyncs once, not once per frame: the
// server answers every hello with a full snapshot, and ten hellos in a second buy
// ten identical snapshots at the cost of ten snapshot builds the hub cached anyway.
const PATCH_RESYNC_DEBOUNCE_MS = 500

function wsUrl(code: string, token: string): string {
  // Same host, same /api prefix as the REST calls, so the dev proxy and any
  // deployment route it together with everything else: no second origin to configure.
  const http = new URL(`/api/rooms/${code}/ws?token=${encodeURIComponent(token)}`, location.href)
  http.protocol = http.protocol === 'https:' ? 'wss:' : 'ws:'
  return http.toString()
}

export function connectRoomWs(code: string, token: string, handlers: RoomWsHandlers): RoomWs {
  let ws: WebSocket | null = null
  let closed = false
  let failed = false
  let tries = 0
  let retryTimer: number | null = null
  let pingTimer: number | null = null
  let resyncTimer: number | null = null
  // Highest snapshot seq seen on the current socket. The hello reply and the
  // room broadcast travel different server paths, so an older snapshot can
  // land after a newer one; applying it would roll the board back. Reset per
  // socket because a restarted server counts from 1 again.
  let lastSeq = 0

  function stopTimers() {
    if (retryTimer !== null) window.clearTimeout(retryTimer)
    if (pingTimer !== null) window.clearInterval(pingTimer)
    if (resyncTimer !== null) window.clearTimeout(resyncTimer)
    retryTimer = pingTimer = resyncTimer = null
  }

  function send(raw: string) {
    if (ws?.readyState === WebSocket.OPEN) ws.send(raw)
  }

  function hello() {
    send(JSON.stringify({ type: 'hello', token }))
  }

  // A patch names what changed but carries no state; v1 ignores the names and
  // re-asks for the snapshot the server already cached, debounced so a burst of
  // writes costs one resync. A snapshot arriving on its own cancels the ask.
  function resyncSoon() {
    if (closed || failed || resyncTimer !== null) return
    resyncTimer = window.setTimeout(() => {
      resyncTimer = null
      hello()
    }, PATCH_RESYNC_DEBOUNCE_MS)
  }

  function connect() {
    if (closed || failed) return
    let sock: WebSocket
    try {
      sock = new WebSocket(wsUrl(code, token))
    } catch {
      schedule()
      return
    }
    ws = sock

    sock.onopen = () => {
      tries = 0
      lastSeq = 0
      hello()
      if (pingTimer !== null) window.clearInterval(pingTimer)
      pingTimer = window.setInterval(() => send(JSON.stringify({ type: 'ping' })), PING_EVERY_MS)
      handlers.onOpen()
    }

    sock.onmessage = (e) => {
      let msg: Record<string, unknown>
      try {
        msg = JSON.parse(typeof e.data === 'string' ? e.data : '') as Record<string, unknown>
      } catch {
        return
      }
      handlers.onAlive()
      switch (msg.type) {
        case 'snapshot': {
          if (resyncTimer !== null) {
            window.clearTimeout(resyncTimer)
            resyncTimer = null
          }
          const seq = typeof msg.seq === 'number' ? msg.seq : 0
          if (seq < lastSeq) break
          lastSeq = seq
          handlers.onSnapshot(msg.state as State)
          break
        }
        case 'patch':
          resyncSoon()
          break
        case 'answer.chunk':
          handlers.onAnswer({ kind: 'chunk', player: msg.player as string, delta: msg.delta as string })
          break
        case 'answer.scoring':
          handlers.onAnswer({ kind: 'scoring', player: msg.player as string })
          break
        case 'answer.done':
          handlers.onAnswer({
            kind: 'done',
            player: msg.player as string,
            score: msg.score as number,
            feedback: msg.feedback as string,
            // The token cap cut the answer short: the verdict screen says so.
            truncated: msg.truncated === true,
          })
          break
        case 'pong':
          break
        case 'error': {
          const errCode = msg.code as string
          // The server closes the socket after these two, so reconnecting would
          // only replay the refusal: mark first, the close handler then stays quiet.
          if (errCode === 'bad_token' || errCode === 'unknown_room') {
            failed = true
            handlers.onFailed()
          } else {
            handlers.onAnswer({ kind: 'error', code: errCode, message: msg.message as string })
          }
          break
        }
        default:
          break
      }
    }

    sock.onerror = () => {
      // The close event follows and owns the retry decision; handling it here
      // too would schedule two reconnects for one failure.
      sock.close()
    }

    sock.onclose = () => {
      if (ws === sock) ws = null
      if (closed || failed) return
      if (pingTimer !== null) {
        window.clearInterval(pingTimer)
        pingTimer = null
      }
      handlers.onDown()
      schedule()
    }
  }

  // The ladder is climbed, never jumped: the index survives across attempts and
  // resets only on a successful open, so a server that is really down is asked
  // at most every ten seconds rather than hammered at half-second intervals.
  function schedule() {
    if (closed || failed) return
    const wait = BACKOFFS_MS[Math.min(tries, BACKOFFS_MS.length - 1)]
    tries += 1
    if (retryTimer !== null) window.clearTimeout(retryTimer)
    retryTimer = window.setTimeout(connect, wait)
  }

  connect()

  return {
    answerStart: (prompt: string) => send(JSON.stringify({ type: 'answer.start', prompt })),
    close: () => {
      closed = true
      stopTimers()
      ws?.close()
      ws = null
    },
  }
}
