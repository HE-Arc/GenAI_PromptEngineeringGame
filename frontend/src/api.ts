export type Player = {
  name: string
  score: number
  answered: boolean
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
}

export const tokenKey = (code: string) => `jeu-genai:${code}`
export const getToken = (code: string) => localStorage.getItem(tokenKey(code))
export const setToken = (code: string, t: string) => localStorage.setItem(tokenKey(code), t)

export async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error((data as { error?: string }).error ?? `HTTP ${res.status}`)
  return data as T
}
