import { useEffect, useRef, useState, type FormEvent } from 'react'
import { post } from './api'
import { useRoom } from './useRoom'

// The admin's own room code, kept so a reload does not orphan a running game. Nothing on this
// screen ever accepts a typed code: the server generates it, and this only ever restores the
// generated one, so two rooms can never collide because a person typed the same code twice.
// It is written only after the room has answered us (see the effect below), so it is always a
// code that exists.
const roomKey = 'jeu-genai:admin-room'

export default function Admin({ go }: { go: (h: string) => void }) {
  const [password, setPassword] = useState('')
  const [code, setCode] = useState<string | null>(() => localStorage.getItem(roomKey))
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const { state } = useRoom(code)
  const verified = useRef(false)

  async function run<T>(fn: () => Promise<T>): Promise<T | undefined> {
    setErr('')
    setBusy(true)
    try {
      return await fn()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'erreur')
      return undefined
    } finally {
      setBusy(false)
    }
  }

  // A code is persisted only once the room has answered us, never on entry: a stored code
  // must name a room that exists. Persisting an unverified one would leave a dead screen that
  // every reload of /admin returns to, with a Demarrer button that cannot work. A ref rather
  // than state, so this costs one write per code instead of one per SSE frame.
  useEffect(() => {
    if (state && code && !verified.current) {
      verified.current = true
      localStorage.setItem(roomKey, code)
    }
  }, [state, code])

  function enter(c: string) {
    verified.current = false
    setCode(c)
  }

  if (!code)
    return (
      <form
        className="card"
        onSubmit={(e: FormEvent) => {
          e.preventDefault()
          void run(async () => {
            await post('/api/admin/login', { password })
            const { code: c } = await post<{ code: string }>('/api/admin/rooms')
            enter(c)
          })
        }}
      >
        <h1>Admin</h1>
        <label>
          Mot de passe
          {/* Visible rather than masked, on purpose: the admin is reading their own password
              back off their own screen before starting a game, and a masked field gives no way
              to spot a typo until it is silently rejected. */}
          <input type="text" value={password} onChange={(e) => setPassword(e.target.value)} autoFocus />
        </label>
        {err && <p className="err">{err}</p>}
        <button disabled={busy || !password}>{busy ? '...' : 'Creer une partie'}</button>
      </form>
    )

  const running = state ? state.status !== 'waiting' : false
  return (
    <div className="card">
      <h1>Room {code}</h1>
      {err && <p className="err">{err}</p>}
      <p className="target">{code}</p>
      <p className="muted">
        Manche {state ? state.current + 1 : 1}/{state?.total ?? 10} - {state?.status ?? 'loading'}
      </p>
      <div className="rows">
        {!running ? (
          <button
            disabled={busy}
            onClick={() =>
              void run(async () => {
                await post(`/api/admin/rooms/${code}/start`)
              })
            }
          >
            Demarrer
          </button>
        ) : (
          <button
            disabled={busy || state?.status === 'done'}
            onClick={() =>
              void run(async () => {
                await post(`/api/admin/rooms/${code}/next`)
              })
            }
          >
            Manche suivante
          </button>
        )}
        <button
          className="ghost"
          onClick={() => {
            // A new game means a new code: leaving the old one persisted would send the next
            // reload back into the room this button just abandoned.
            localStorage.removeItem(roomKey)
            go('/')
          }}
        >
          Nouvelle partie
        </button>
      </div>
      <ul className="players">
        {(state?.players ?? []).map((p) => (
          <li key={p.name}>
            {p.name} - {p.score} pts {p.answered ? '' : '(en attente)'}
          </li>
        ))}
      </ul>
    </div>
  )
}
