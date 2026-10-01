// The contract with backend/game/views.py:room_state, key for key. Snake_case is kept as the
// server emits it — this repo does no key mapping, and renaming here would only move the
// translation to the one file that is supposed to describe the wire format.

export type Phase = 'waiting' | 'quiz' | 'playing' | 'rating' | 'summary' | 'done'

// Progress through the didacticiel, for the player the snapshot was built for. Null outside
// the quiz phase: the gate is closed, and a client that renders a "2/8" over a game it is not
// playing is a client showing a progress bar for nothing.
export interface QuizState {
  total: number
  done: number
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
export interface SummaryRow {
  author: string
  prompt: string
  output: string
  proximity: number
  quality_llm: number
  quality_peers: number
  quality: number
  round_score: number
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
  // The didacticiel's score. It never reaches the final ranking, which is why it is a separate
  // field rather than folded into `score`.
  quiz_score: number | null
  quiz_done: boolean
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
}

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

export async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new ApiError(res.status, (data as { error?: string }).error ?? `HTTP ${res.status}`)
  return data as T
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
  'not playing': "La partie n’est pas encore démarrée.",
  'quiz not open': 'Le didacticiel est fermé. L’animateur fait avancer la partie.',
  'not rating': 'La notation est fermée. L’animateur fait avancer la partie.',
  'your own prompt': 'Ce prompt est le tien : tu ne peux pas le noter.',
  'score 1-10': 'Choisis une note entre 1 et 10.',
  'prompt required': 'Écris un prompt avant de l’envoyer.',
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
