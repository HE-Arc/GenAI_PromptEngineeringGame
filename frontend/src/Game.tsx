import { useEffect, useState, type FormEvent } from 'react'
import { getName, getToken, post, type Player, type State } from './api'

type Result = { score: number; comment: string; output: string }

export default function Game({ state, code }: { state: State; code: string }) {
  const [prompt, setPrompt] = useState('')
  const [result, setResult] = useState<Result | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  // A note belongs to the round that earned it, and a prompt belongs to the target it was
  // written against. Without this reset, round 3's score and comment stay on screen under
  // the round-4 target, unlabelled, and read as though they graded the current prompt; and a
  // half-written draft carries into a round it cannot possibly answer — submitted there, it
  // is judged against the wrong target and that score lands in the ranking under the
  // player's name. Same for a stale 409 left in the error slot.
  useEffect(() => {
    setPrompt('')
    setResult(null)
    setErr('')
  }, [state.current])

  const me: Player | undefined = state.players.find((p) => p.name === getName(code))
  const alreadyDone = me?.answered ?? false

  async function send(e: FormEvent) {
    e.preventDefault()
    setErr('')
    setBusy(true)
    try {
      setResult(await post<Result>(`/api/rooms/${code}/answer`, { token: getToken(code), prompt }))
      setPrompt('')
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'envoi failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card">
      <h1>
        Manche {state.current + 1}/{state.total} <span className="muted">Room {state.code}</span>
      </h1>
      <div className="cols">
        <div>
          <p className="muted">Repone a atteindre</p>
          <p className="target">{state.target ?? '—'}</p>
          <form onSubmit={send} className="rows">
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Ton prompt, en anglais de preference"
              disabled={alreadyDone}
            />
            {err && <p className="err">{err}</p>}
            <button disabled={busy || alreadyDone || !prompt.trim()}>
              {alreadyDone ? 'Reponse envoyee' : busy ? 'Le modele reflechit...' : 'Envoyer'}
            </button>
          </form>
          {result && (
            <div className="rows">
              <p className="score">Note : {result.score}/10</p>
              <p className="muted">{result.comment}</p>
              <pre className="output">{result.output}</pre>
            </div>
          )}
        </div>
        <div className="rows">
          <h2>Classement</h2>
          {state.players.map((p, i) => (
            <div className="row" key={p.name}>
              <span>
                {i + 1}. {p.name}
                {p.name === getName(code) ? ' (toi)' : ''}
              </span>
              <span className="score">{p.score}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
