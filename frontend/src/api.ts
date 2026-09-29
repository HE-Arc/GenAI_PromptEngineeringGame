export type Player = {
  name: string
  score: number
  answered: boolean
  // Whether this player's phone is on the stream right now. Distinct from being in the game:
  // someone whose screen locked is still in the roster and still counted in the score, and
  // only stops being `online`.
  online: boolean
  prompt: string | null
  output: string | null
  comment: string | null
  round_score: number | null
}

export type State = {
  code: string
  status: 'waiting' | 'playing' | 'done'
  current: number
  total: number
  target: string | null
  players: Player[]
  // How many of `players` are connected. Never use it in place of `players.length`: the two
  // answer different questions and the game depends on the difference.
  online_count: number
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
