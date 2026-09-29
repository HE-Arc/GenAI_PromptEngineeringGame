import { useEffect, useState, type FormEvent } from 'react'
import { getName, getToken, post, type Player, type State } from './api'

type Result = { score: number; comment: string; output: string; round: number }

export default function Game({ state, code }: { state: State; code: string }) {
  const [prompt, setPrompt] = useState('')
  const [result, setResult] = useState<Result | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  // A prompt belongs to the target it was written against, and an error to the round that
  // caused it, so both are cleared when the round moves on. The result is not: the push that
  // closes the round lands after this player's own POST, so clearing it took away the last
  // player's score, comment and output within a second of earning them. It is kept instead and
  // labelled with the round it belongs to, which is what stops the misreading this reset
  // was originally for — an unlabelled note under a different round's target.
  useEffect(() => {
    setPrompt('')
    setErr('')
  }, [state.current])

  const me: Player | undefined = state.players.find((p) => p.name === getName(code))
  const alreadyDone = me?.answered ?? false

  async function send(e: FormEvent) {
    e.preventDefault()
    setErr('')
    setBusy(true)
    try {
      const res = await post<Omit<Result, 'round'>>(`/api/rooms/${code}/answer`, { token: getToken(code), prompt })
      setResult({ ...res, round: state.current })
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
              <p className="score">
                Note : {result.score}/10 <span className="muted">(manche {result.round + 1})</span>
              </p>
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
