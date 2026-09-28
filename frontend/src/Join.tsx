import { useState, type FormEvent } from 'react'
import { post, setToken } from './api'

export default function Join({ onJoined }: { onJoined: (code: string) => void }) {
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  async function go(e: FormEvent) {
    e.preventDefault()
    setErr('')
    setBusy(true)
    try {
      const c = code.trim().toUpperCase()
      const res = await post<{ token: string }>(`/api/rooms/${c}/join`, { name })
      setToken(c, res.token)
      onJoined(c)
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'join failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="card" onSubmit={go}>
      <h1>Jeu GenAi</h1>
      <p className="muted">Devine la reponse du modele en ecrivant le bon prompt.</p>
      <label>
        Code de la partie
        <input
          value={code}
          onChange={(e) => setCode(e.target.value.toUpperCase())}
          maxLength={8}
          placeholder="ABCD"
          autoFocus
        />
      </label>
      <label>
        Ton pseudo
        <input value={name} onChange={(e) => setName(e.target.value)} maxLength={32} placeholder="ana" />
      </label>
      {err && <p className="err">{err}</p>}
      <button disabled={busy || !code || !name}>{busy ? '...' : 'Rejoindre'}</button>
    </form>
  )
}
