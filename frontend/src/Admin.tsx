import { useState, type FormEvent } from 'react'
import { post } from './api'
import { useAdminRoom } from './adminRoom'

export default function Admin({ go }: { go: (h: string) => void }) {
  const [password, setPassword] = useState('')
  const [code, setCode] = useState<string | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const { state } = useAdminRoom(code)

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

  if (!code)
    return (
      <form
        className="card"
        onSubmit={(e: FormEvent) => {
          e.preventDefault()
          void run(async () => {
            await post('/api/admin/login', { password })
            const { code: c } = await post<{ code: string }>('/api/admin/rooms')
            setCode(c)
          })
        }}
      >
        <h1>Admin</h1>
        <label>
          Mot de passe
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoFocus />
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
        <button className="ghost" onClick={() => go('/')}>
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
