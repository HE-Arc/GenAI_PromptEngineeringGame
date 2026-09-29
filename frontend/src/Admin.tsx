import { useEffect, useRef, useState, type FormEvent } from 'react'
import { post } from './api'
import { useRoom } from './useRoom'

// The admin's own room code, kept so a reload does not orphan a running game. It is written
// only once the room has answered us — see the effect below — so it is always a code that
// exists, never a typo the admin typed.
const roomKey = 'jeu-genai:admin-room'

export default function Admin({ go }: { go: (h: string) => void }) {
  const [password, setPassword] = useState('')
  const [code, setCode] = useState<string | null>(() => localStorage.getItem(roomKey))
  const [existing, setExisting] = useState('')
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

  // A code is not persisted on entry, only once the room has answered us. `/api/admin/login`
  // proves the password, not the room: a typo'd code 404s on the EventSource, `state` stays
  // null, and the screen would sit on "Manche 1/10 - loading" with a working Demarrer button
  // — and every reload of #/admin would land back on that same dead screen. A ref rather than
  // state, so this costs one write per code instead of one per SSE frame.
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
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoFocus />
        </label>
        <p className="muted">Deja une partie en cours ?</p>
        <label>
          Code de la partie
          <input
            value={existing}
            onChange={(e) => setExisting(e.target.value.toUpperCase())}
            maxLength={8}
            placeholder="ABCD"
          />
        </label>
        {err && <p className="err">{err}</p>}
        <div className="rows">
          <button disabled={busy || !password}>{busy ? '...' : 'Creer une partie'}</button>
          <button
            type="button"
            className="ghost"
            disabled={busy || !existing}
            onClick={() => void run(async () => {
              await post('/api/admin/login', { password })
              enter(existing.trim())
            })}
          >
            Rejoindre la partie
          </button>
        </div>
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
