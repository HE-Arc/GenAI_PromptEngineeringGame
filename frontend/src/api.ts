// The contract with backend/game/views.py:room_state, key for key. Snake_case is kept as the
// server emits it — this repo does no key mapping, and renaming here would only move the
// translation to the one file that is supposed to describe the wire format.

export type Phase = 'waiting' | 'quiz' | 'playing' | 'rating' | 'summary' | 'done'

// The didacticiel, as the room sees it. ROOM-level, not per-viewer: the snapshot is one
// payload for every phone and the admin screen, so it cannot say who is reading. The three
// clocks matter:
//
// - `deadline_ms` is when the server stops accepting answers for the current question. The
//   thirty seconds are SHARED: one question at a time for the whole class, so everyone
//   sees the same question and the reveal lands at the same instant for everyone.
// - `server_ms` is the server's own clock at that moment, sent so a client can correct for
//   skew against its own. A phone whose clock is two minutes out would otherwise count down
//   from the wrong instant and lock the wrong side of the question.
// - `revealed` is `now >= deadline`, and it is the ONLY thing that makes `correct` and
//   `explanation` public. Before it, they are null: a key shipped to the client is a QCM the
//   player can read in devtools instead of playing.
//
// The admin, not this block, decides when the NEXT question opens. A clock that advances the
// game on its own takes the teacher's commentary away from them.
export interface QuizState {
  index: number
  total: number
  deadline_ms: number
  server_ms: number
  revealed: boolean
  correct: number | null
  explanation: string | null
  // How many of the room's online players have answered the current question, and the room's
  // best QCM score. A teacher watching the room does not need per-player detail here, and the
  // per-player score is already on `Player.quiz_score`.
  done: number
  online_count: number
  score: number
}

// The one prompt on screen, rated one at a time. `author` is null under anonymity while
// `author_label` is always something to display: a screen must never branch on anonymity to
// decide whether it has a label to draw.
export interface RevealState {
  index: number
  count: number
  prompt: string
  author: string | null
  author_label: string
  rated: number
}

// One line of the round synthesis, shown once anonymity is lifted. `quality_llm`,
// `quality_peers` and `quality` are all here so the screen can show the bound at work rather
// than an unexplained verdict.
//
// The three numbers are meant to be CHECKABLE against each other: `quality` is `quality_peers`
// held within ±3 of `quality_llm`, and the screen says so under the table. If the server rounds
// the two by different rules, that sentence becomes false in front of the class, so the server
// rounds them the same way. `quality_comment` is the critic's own reason for `quality_llm`, and
// it is the surface that admits when there is no real model behind the number.
export interface SummaryRow {
  author: string
  prompt: string
  output: string
  proximity: number
  quality_llm: number
  quality_peers: number
  quality: number
  quality_comment: string
  round_score: number
}

// The class's own bar on the current step. `needed` rather than `quorum` because the server
// sends it under that name: it is a number of players, not a score, and this payload has no
// other float.
export type ReadyState = {
  // Which discussion this is: `quiz:3`, `rating:2:0`, `summary:2`. Shown nowhere — it is here so
  // a screen can tell « my vote was for the step on screen » from « my vote is stale ».
  step: string
  count: number
  needed: number
  mine: boolean
}

// One question of the didacticiel, as the GET endpoint returns it: no `correct`, no
// `explanation`. Those two travel in the POST response that reveals the answer, so a player
// with devtools open cannot read the key off the bank.
export interface Question {
  order: number
  prompt: string
  choices: string[]
}

// The bank, fetched once, plus what THIS player has already answered.
//
// `answered_orders` is a list of question orders, not a count, and the distinction is the
// whole reason this type is not a number. The question on screen belongs to the ROOM now, so
// "how many have I done" cannot answer "have I done THIS one": a player who answers the first
// question and then looks away has one answered, and is looking at the fifth, which they have
// not done. A count cannot tell those apart, and a client that assumed it would lock a player
// out of a question they never saw.
export interface QuizBank {
  questions: Question[]
  answered_orders: number[]
}

// The reveal, in the response that answers it.
//
// `correct_index` is what the screen cannot work without, and it is why this is a separate
// field from `correct`. `correct` alone only says whether the player's own choice was right,
// and the client is never sent the key — that is the whole point of the QCM — so without the
// index the reveal could not mark which of the four was right, which is the part of a Kahoot
// that teaches. A client that compared `correct` against a choice index would silently never
// highlight anything.
//
// `explanation` belongs to the exact row the player was shown, which is why the server stores
// it rather than reading it from the bank at answer time — an edited bank cannot
// retroactively rewrite the reason a past game gave.
export interface QuizAnswered {
  correct: boolean
  correct_index: number
  explanation: string
  score: number
}

// A submitted rating. Deliberately empty beyond `ok`: the rating screen's progress comes from
// the `reveal` block in the SSE snapshot, so the POST confirms the write and the next frame
// carries the state. Sending the progress here too would give the client two truths to keep in
// step, and the snapshot is the one everybody already trusts.
export interface Rated {
  ok: true
}

export type Player = {
  name: string
  // Both are null during the rating phase when the game is anonymous. The server blanks them
  // on purpose: a player who reads their own score, sees it missing from the column, and
  // notices which revealed line is theirs defeats the whole feature. Null here, so a screen
  // is forced to handle "no score yet" and cannot quietly print NaN.
  score: number | null
  round_score: number | null
  answered: boolean
  // Whether this player's phone is on the stream right now. Distinct from being in the game:
  // someone whose screen locked is still in the roster and still counted in the score, and
  // only stops being `online`.
  online: boolean
  // The didacticiel's score. Carried into `score` raw — one correct answer is one point —
  // and kept as a separate field so screens can show the two halves apart.
  quiz_score: number | null
  quiz_done: boolean
  // The two dimensions behind `score`, summed over the rounds that have been settled. Null
  // before the player's first settled round, or during the rating phase alongside `score`.
  //
  // They are in the payload for a teaching reason, not a decorative one: "proximité 42,
  // qualité 58, total 50" tells a player their prompts WORK and are badly BUILT, which is the
  // whole lesson. Collapse it to one number and that distinction disappears behind a score out
  // of 100 that reads like a grade.
  proximity_total: number | null
  quality_total: number | null
  // No `prompt`, `output` or `comment`: the server stops sending them during the rating phase,
  // and keeping them in the type would tell whoever writes the next screen that they are
  // always there.
}

export type State = {
  code: string
  phase: Phase
  current: number
  total: number
  target: string | null
  // The picture an exercise refers to ("générez cette image"), or null.
  target_image: string | null
  // The clock of the phase on screen — writing a prompt, rating one — or null when nothing is
  // timed. Same two clocks as `QuizState`, read through `useQuizWindow`.
  timer: { deadline_ms: number; server_ms: number } | null
  // Fixed for the whole game, so a screen can read it once instead of inferring anonymity from
  // whether a name happens to be present.
  anonymous: boolean
  players: Player[]
  // How many of `players` are connected. Never use it in place of `players.length`: the two
  // answer different questions and the game depends on the difference.
  online_count: number
  quiz: QuizState | null
  reveal: RevealState | null
  summary: { round: number; rows: SummaryRow[] } | null
  // The class's own bar on the step the room is discussing, and `null` everywhere else — four of
  // the six phases, plus a phase that has no step behind it. Unconditional, for the reason the
  // three blocks above are: a client that tests for a key's existence has to know the phase to do
  // it, and this is the block that lets a screen say nothing about phases at all.
  ready: ReadyState | null
}

// The cross-rating scale, declared here because this file is where the room-state contract
// lives, and named as the server names it — `SCALE_MIN, SCALE_MAX = 1, 10` at
// backend/game/prompts.py:30 — so the two halves of one rule are greppable from either end. It
// was written out in three places on this side, and a client that drifts from the bound the
// server enforces is a client that offers a note the server will refuse.
//
// The two numbers are not interchangeable and neither is the default: 1 is the lowest note a
// player may give, while the score a round hands out is read on a 0-10 scale.
export const SCALE_MIN = 1
export const SCALE_MAX = 10

// The players whose phones are actually on the stream. Split out in one place because the
// answer meter and the roster must agree on it, and a progress indicator that can never fill
// is worse than no indicator.
export const present = (state: State) => state.players.filter((p) => p.online)
export const absent = (state: State) => state.players.filter((p) => !p.online)

// Carries the status so a caller can tell a room that does not exist from a name that is
// already taken: both arrive as a thrown Error, and only one of them is the player's fault.
export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

export const tokenKey = (code: string) => `jeu-genai:${code}`
export const getToken = (code: string) => localStorage.getItem(tokenKey(code))
export const setToken = (code: string, t: string) => localStorage.setItem(tokenKey(code), t)
export const nameKey = (code: string) => `jeu-genai:${code}:name`
export const getName = (code: string) => localStorage.getItem(nameKey(code)) ?? ''

// One room as the console's room list returns it: what an admin chooses between, and nothing
// else — no players, no quiz window, no standings, because a row that carried the whole snapshot
// would be a console opened thirty times over.
//
// `phase` is the same raw string `State.phase` is, not a French label, so the list and the
// console call a phase by one name and the row and the run-of-show cannot disagree. `current`
// is 0-based for the same reason: it is the number `State` already uses, and a row that counted
// from one would be the one place on the screen where the round is numbered differently.
//
// `players` is a COUNT here and a list in `State`, under the same name the server sends it. The
// two are not interchangeable and neither is derivable from the other: `online_count` is how many
// of those phones are on the stream right now, and a room whose players have all gone is exactly
// the one an admin needs to notice from across a room.
export interface RoomSummary {
  code: string
  phase: Phase
  current: number
  total: number
  online_count: number
  players: number
  tutorial: boolean
  anonymous: boolean
}

// An HTTP response becomes a value or an ApiError, in one place, because the two verbs below it
// must raise the same error for the same status: a caller cannot tell a GET apart from a POST by
// how it fails, and one that could is one that eventually handles them differently.
async function unwrap<T>(res: Response): Promise<T> {
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new ApiError(res.status, (data as { error?: string }).error ?? `HTTP ${res.status}`)
  return data as T
}

// What `POST /answer` answers with: the judge's verdict, and whether the model's own cap cut
// the answer short. `output` is NOT here — the client keeps the text it accumulated from the
// `chunk` frames, which is the same text the server stored and the judge scored, so a streamed
// answer and a scored answer cannot drift apart.
export interface Answered {
  score: number
  comment: string
  round: number
  truncated: boolean
}

// A cancel, and not a failure. `fetch` rejects an aborted request with a `DOMException` named
// `AbortError`, which is not an `ApiError` and must never be shown to a player as one: leaving
// the screen is not the server falling over.
export function isAbort(e: unknown): boolean {
  return e instanceof DOMException && e.name === 'AbortError'
}

// The `error` frame, and the sentence the server chose for it. A separate class from `ApiError`
// because it must reach the player VERBATIM: it is not a refusal `describe` can translate, and
// `describe` would replace it with a generic line that says less than the server just said.
export class StreamError extends ApiError {
  constructor(message: string) {
    // 500 is what the frame means: the round failed, not the player. It only matters because
    // `describe` reads the status, and `Game` bypasses `describe` for this class entirely.
    super(500, message || 'La partie a planté côté serveur. Préviens l’animateur.')
    this.name = 'StreamError'
  }
}

// The answer, as it arrives. `fetch` rather than EventSource for two reasons that are the same
// reason: this is a POST that WRITES (the answer row, the phase, three billed calls), and
// EventSource is a GET that cannot carry a body. A GET that writes would put the player's
// token in a query string, in a proxy log, for a stream that has to be private.
//
// `res.body` is the floor, and it is a real one: iOS Safari 16.4 (March 2023), which is also
// the floor Vite 8's default build target puts this app on. The check below is not defensive
// coding, it is the honest refusal — a phone that cannot read a stream gets told, rather than a
// button that spins forever.
export async function streamAnswer(
  path: string,
  body: unknown,
  on: { chunk: (delta: string) => void; scoring: () => void },
  signal: AbortSignal,
): Promise<Answered> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  // A refusal is decided BEFORE the stream starts, so it is still a JSON error with its 409 —
  // exactly what `unwrap` would have thrown, so `describe` keeps translating it. Everything
  // after this line is a 200 whose body is the answer.
  if (!res.ok) {
    const data = (await res.json().catch(() => ({}))) as { error?: string }
    throw new ApiError(res.status, data.error ?? `HTTP ${res.status}`)
  }
  if (!res.body) throw new ApiError(0, 'stream unsupported')

  const reader = res.body.getReader()
  // A `TextDecoderStream` would be the obvious tool and it is iOS 14.5; the manual buffer is one
  // object and one loop. `stream: true` per call is what makes a multi-byte character split
  // across two reads come back whole instead of as two replacement characters — it belongs on
  // `decode`, not on the constructor, which is why the decoder is built with the label alone.
  const decoder = new TextDecoder('utf-8')
  // Frames are separated by a blank line and a frame can be split across two reads at any byte,
  // so whatever follows the last blank line is carried into the next read. Every frame the
  // server writes ends with its own `\n\n`, so nothing is left in the buffer at the end — the
  // server terminating without a `done` is what the throw below is for.
  let buf = ''

  // One block of SSE. `data` lines are concatenated because the format allows a frame to split
  // its payload across several of them, and `JSON.parse` on either half would not survive it.
  function frame(block: string) {
    let name = 'message'
    let data = ''
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) name = line.slice(6).trim()
      else if (line.startsWith('data:')) data += line.slice(5).trim()
    }
    return { name, data }
  }

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let cut = buf.indexOf('\n\n')
    while (cut !== -1) {
      const { name, data } = frame(buf.slice(0, cut))
      buf = buf.slice(cut + 2)
      // Dispatched on the event name alone: `scoring` carries an empty object, and a frame whose
      // data line was dropped in transit must still get its name acted on. An unknown name is
      // ignored, not thrown — it is not an error, and throwing on one would kill a stream that
      // is working perfectly well.
      if (name === 'chunk' && data) on.chunk((JSON.parse(data) as { delta: string }).delta)
      else if (name === 'scoring') on.scoring()
      else if (name === 'done') return JSON.parse(data) as Answered
      else if (name === 'error') throw new StreamError(((JSON.parse(data) as { error: string }).error) || '')
      cut = buf.indexOf('\n\n')
    }
  }
  // The stream ended without a verdict: no `error` frame and no `done`. The server closed it,
  // which is a failure of the round and not of the player, so it is reported as one.
  throw new StreamError('La partie a planté côté serveur. Préviens l’animateur.')
}

export async function post<T>(path: string, body?: unknown): Promise<T> {
  return unwrap<T>(
    await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body ?? {}),
    }),
  )
}

// GET is POST without the body, and it does not take one: a request that had to send `{}` to ask
// a question is a request whose shape says the answer is optional. Not exported — the only
// caller is the function below it, and a general verb on this file is a verb nobody has to keep
// honest until the second caller arrives.
async function get<T>(path: string): Promise<T> {
  return unwrap<T>(await fetch(path, { headers: { Accept: 'application/json' } }))
}

// Every room the console can go back to. No code, no token, no argument: this is the one request
// on the admin side that is answered before the password has been typed, because the screen that
// asks for the password is also the screen that draws the list — a list behind a password the
// admin does not have typed yet is a list they cannot see while they type it, and the admin who
// is reopening a game they ran this morning knows the password already.
export async function listRooms(): Promise<RoomSummary[]> {
  const data = await get<{ rooms: RoomSummary[] }>('/api/admin/rooms')
  return data.rooms
}

// « On peut passer. » One player's vote on the step the room is already in.
//
// The client never names the step: a payload that could name it is a payload that could vote on a
// discussion that has not started, and the count would then hold a vote the room never asked for.
// So the body is the token and nothing else, and what to draw comes back from the next snapshot —
// the POST only has to say whether it was written, which is why this returns nothing.
export async function postReady(code: string, token: string): Promise<void> {
  await post(`/api/rooms/${code}/ready`, { token })
}

// The server answers in English, the room speaks French, and the two must not be allowed to
// meet. These are every message backend/game/views.py can return, translated into something a
// first-time player can act on rather than a status code. Kept in one place so the six
// screens cannot drift apart on wording.
const REASONS: Record<string, string> = {
  'name required': 'Choisis un pseudo.',
  'name taken': 'Ce pseudo est déjà pris. Choisis-en un autre.',
  'game already started': "La partie a déjà commencé. L’animateur doit en créer une nouvelle.",
  'already answered': 'Tu as déjà répondu pour cette manche.',
  // Two refusals specific to the timed didacticiel. They are 409s, so without these they would
  // land on the generic "Impossible pour le moment. Réessaie dans un instant." — which is the
  // wrong advice here: the window has closed and retrying cannot reopen it. A player tapping at
  // the buzzer needs to be told the room moved on, not to try again.
  "time's up": 'C’est fini pour cette question, la suivante arrive.',
  'not the current question': 'La classe est déjà passée à une autre question.',
  'already rated': 'Tu as déjà noté ce prompt. Le suivant arrive.',
  'not playing': "La partie n’est pas encore démarrée.",
  'quiz not open': 'Le didacticiel est fermé. L’animateur fait avancer la partie.',
  'not rating': 'La notation est fermée. L’animateur fait avancer la partie.',
  'your own prompt': 'Ce prompt est le tien : tu ne peux pas le noter.',
  // The three refusals of the ready bar, and the first two are the reason the generic line below
  // is wrong here rather than merely blunt. `already ready` comes back to a player whose vote WAS
  // recorded — the second tap of a thumb — and `nothing to discuss` to a phone that woke up on the
  // next step; both are told « Réessaie dans un instant » by the fallback, which is advice for a
  // request that failed, not for one the server has already had. The third is the author of the
  // prompt on screen, who is not a player of this bar at all and needs to be told why.
  'already ready': 'Tu as déjà dit que tu étais prêt.',
  'nothing to discuss': 'La classe est déjà passée à la suite : il n’y a plus rien à discuter.',
  'you wrote this one': 'Ce prompt, c’est toi qui l’as écrit : personne ne note le sien.',
  'score 1-10': 'Choisis une note entre 1 et 10.',
  'prompt required': 'Écris un prompt avant de l’envoyer.',
  'prompt too long': 'Ton prompt est trop long (500 caractères maximum).',
  'bad token': 'Cette partie a peut-être été relancée. Reprends le code depuis l’accueil.',
  'bad password': 'Mot de passe incorrect.',
  'admin only': 'Session expirée. Reconnecte-toi.',
}

export function describe(e: unknown): string {
  if (!(e instanceof ApiError)) return 'Le serveur ne répond pas. Vérifie le réseau local.'
  if (e.status === 404) return 'Cette partie n’existe pas. Vérifie le code.'
  const known = REASONS[e.message]
  if (known) return known
  if (e.status === 403) return 'Accès refusé à cette partie.'
  if (e.status === 409) return 'Impossible pour le moment. Réessaie dans un instant.'
  if (e.status >= 500) return 'La partie a planté côté serveur. Préviens l’animateur.'
  return e.message
}
