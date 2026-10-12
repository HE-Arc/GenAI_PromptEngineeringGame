import { useRef, useState, type FormEvent } from 'react'
import { ApiError, describe, nameKey, post, setToken } from './api'

// Which of the two fields the player actually has to change. The server is the only thing
// that knows, and it says so twice over: a status of 404 is the code, a refusal to accept the
// name is the name. A network failure is neither, and gets neither marked — putting an
// "invalid" ring on two inputs the player filled in correctly would be the form accusing them
// for a failure of the network.
function blame(e: unknown): 'code' | 'name' | null {
  if (!(e instanceof ApiError)) return null
  if (e.status === 404) return 'code'
  if (e.message === 'name required' || e.message === 'name taken') return 'name'
  return null
}

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
  const [bad, setBad] = useState<'code' | 'name' | null>(null)
  const [busy, setBusy] = useState(false)
  const nameRef = useRef<HTMLInputElement>(null)

  // Deliberately not on mount. This screen is typed on a phone held in one hand, and
  // raising the keyboard at load hides the field you are typing into and the button you are
  // about to press. Only a code read off the address is skipped, because it cannot be edited:
  // a code the player typed stays theirs to correct, and the name can be typed first.
  function skipPresetCode() {
    if (preset !== undefined) nameRef.current?.focus()
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
      setBad(blame(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="deck">
      {/* The skip link's target — see the note on `SkipLink` in App.tsx. */}
      <main className="screen screen--solo" id="main" tabIndex={-1}>
        <div className="hero">
          <span className="hero__badge" aria-hidden="true">✦</span>
          <h1 className="title title--hero" tabIndex={-1}>Jeu GenAi</h1>
          <p className="hero__tag">Le jeu du prompt parfait</p>
        </div>
        <p className="prose prose--dim hero__lead">
          Le modèle doit répondre par une chose bien précise. À toi d’écrire le prompt qui
          l’y amène. Un jury note la ressemblance sur 10, et les dix manches se jouent entre
          vous.
        </p>
        <form className="form card" onSubmit={go}>
          <div className="field">
            <label className="label" htmlFor="join-code">
              Code de la partie
            </label>
            {/* Four letters from an alphabet with no O and no I, typed one-handed, often
                with autocorrect and autocapitalise fighting back. The mobile keyboard is
                asked for explicitly rather than left to guess from the input type. */}
            <input
              id="join-code"
              className="input input--code"
              value={code}
              onChange={(e) => setCode(e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, ''))}
              onFocus={skipPresetCode}
              maxLength={4}
              placeholder="K7TQ"
              inputMode="text"
              autoCapitalize="characters"
              autoComplete="off"
              autoCorrect="off"
              spellCheck={false}
              readOnly={preset !== undefined}
              aria-required="true"
              aria-invalid={bad === 'code' ? true : undefined}
              aria-describedby={bad === 'code' ? 'join-err' : 'join-help'}
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
              maxLength={32}
              placeholder="Ana"
              autoComplete="nickname"
              autoCapitalize="words"
              aria-required="true"
              aria-invalid={bad === 'name' ? true : undefined}
              aria-describedby={bad === 'name' ? 'join-err' : undefined}
            />
          </div>
          {/* The message is about one of the two fields, so it is linked to that field and
              the field is marked, rather than sitting in the middle of the form as a paragraph
              about nothing. `role="alert"` because it arrives without a page change and without
              anything moving on the screen — the player has just been told no and would
              otherwise have to go looking for why. Neither field is ever cleared: the code was
              typed, or was read off the address bar, and the name is the player's own. */}
          {err && (
            <p className="err" id="join-err" role="alert">
              {err}
            </p>
          )}
          {/* The button below is dead until both fields are filled, and a disabled control
              that does not say why is the one form element that gives a player nothing to act
              on — it just looks broken. Stated permanently rather than shown and hidden on
              each keystroke, so nothing on the phone reflows under the thumb mid-word. The
              `<p className="label">` the code field already uses is this form's own idiom for
              a line of help, and it is used rather than a new element invented. */}
          <p className="label">Le code et le pseudo sont tous les deux nécessaires.</p>
          <button type="submit" className="btn btn--signal btn--wide" disabled={busy || !code || !name.trim()}>
            {busy ? 'Connexion…' : 'Rejoindre la partie'}
          </button>
        </form>
      </main>
    </div>
  )
}
