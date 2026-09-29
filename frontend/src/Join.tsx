import { useRef, useState, type FormEvent } from 'react'
import { ConsoleBar } from './ui'
import { describe, nameKey, post, setToken } from './api'

export default function Join({
  preset,
  onJoined,
}: {
  // Arriving on /room/<CODE> without a token means the code is already known and only the
  // name is missing, so the field comes pre-filled and read-only rather than making the
  // player retype four letters they can already see.
  preset?: string
  onJoined: (code: string) => void
}) {
  const [code, setCode] = useState(preset ?? '')
  const [name, setName] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const nameRef = useRef<HTMLInputElement>(null)
  const codeRef = useRef<HTMLInputElement>(null)

  // Deliberately not on mount. This screen is typed on a phone held in one hand, and
  // raising the keyboard at load hides the field you are typing into and the button you are
  // about to press. Focus is taken on the first thing this form can actually accept instead.
  function start() {
    if (code) nameRef.current?.focus()
    else codeRef.current?.focus()
  }

  async function go(e: FormEvent) {
    e.preventDefault()
    setErr('')
    setBusy(true)
    try {
      const c = code.trim().toUpperCase()
      const res = await post<{ token: string; name: string }>(`/api/rooms/${c}/join`, { name })
      setToken(c, res.token)
      localStorage.setItem(nameKey(c), res.name)
      onJoined(c)
    } catch (err) {
      setErr(describe(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="deck">
      <ConsoleBar />
      <main className="screen screen--solo">
        <h1 className="title">Jeu GenAi</h1>
        <p className="prose prose--dim">
          Le modèle doit répondre par une chose bien précise. À toi d’écrire le prompt qui
          l’y amène. Un jury note la ressemblance sur 10, et les dix manches se jouent entre
          vous.
        </p>
        <form className="form" onSubmit={go}>
          <div className="field">
            <label className="label" htmlFor="join-code">
              Code de la partie
            </label>
            {/* Four letters from an alphabet with no O and no I, typed one-handed, often
                with autocorrect and autocapitalise fighting back. The mobile keyboard is
                asked for explicitly rather than left to guess from the input type. */}
            <input
              id="join-code"
              ref={codeRef}
              className="input input--code"
              value={code}
              onChange={(e) => setCode(e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, ''))}
              onFocus={start}
              maxLength={4}
              placeholder="K7TQ"
              inputMode="text"
              autoCapitalize="characters"
              autoComplete="off"
              autoCorrect="off"
              spellCheck={false}
              readOnly={preset !== undefined}
              aria-describedby="join-help"
            />
            <p className="label" id="join-help">
              {preset ? 'Code repris de l’adresse.' : 'Quatre lettres données par l’animateur.'}
            </p>
          </div>
          <div className="field">
            <label className="label" htmlFor="join-name">
              Ton pseudo
            </label>
            <input
              id="join-name"
              ref={nameRef}
              className="input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onFocus={start}
              maxLength={32}
              placeholder="Ana"
              autoComplete="nickname"
              autoCapitalize="words"
            />
          </div>
          {err && <p className="err">{err}</p>}
          <button type="submit" className="btn btn--signal btn--wide" disabled={busy || !code || !name.trim()}>
            {busy ? 'Connexion…' : 'Rejoindre la partie'}
          </button>
        </form>
      </main>
    </div>
  )
}
