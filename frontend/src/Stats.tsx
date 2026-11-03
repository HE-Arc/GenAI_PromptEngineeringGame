import { useEffect, useState, type FormEvent } from 'react'
import { ConsoleBar } from './ui'
import { ApiError, describe, getStats, post, type Phase, type StatsData } from './api'

// Local label maps: this page is the only one that names a phase or a call kind in the
// aggregate, and the console's own table is private to Admin.
const PHASE_NAME: Record<Phase, string> = {
  waiting: 'En attente',
  quiz: 'Didacticiel',
  playing: 'Rédaction',
  rating: 'Notation croisée',
  summary: 'Résumé de manche',
  done: 'Partie terminée',
}

const KIND_NAME: Record<string, string> = {
  play: 'Réponse',
  judge: 'Note de la réponse',
  critic: 'Note du prompt',
  image: 'Image',
}

const int = (n: number) => n.toLocaleString('fr-FR')
const one = (n: number | null) =>
  n === null ? '—' : n.toLocaleString('fr-FR', { minimumFractionDigits: 1, maximumFractionDigits: 1 })

function uptime(seconds: number): string {
  const min = Math.floor(seconds / 60)
  if (min >= 60) return `${Math.floor(min / 60)} h ${min % 60} min`
  return min > 0 ? `${min} min` : `${seconds} s`
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <li className="stats__row">
      <span>{label}</span>
      <span className="num">{value}</span>
    </li>
  )
}

// Same gate as the admin console: one probe on mount, then the password form. No polling —
// the page is read, not watched, and the button refetches.
export default function Stats() {
  const [signedIn, setSignedIn] = useState<boolean | null>(null)
  const [stats, setStats] = useState<StatsData | null>(null)
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let gone = false
    void (async () => {
      try {
        const data = await getStats()
        if (gone) return
        setStats(data)
        setSignedIn(true)
      } catch {
        // Any failure paints the gate, as in Admin: the press then shows the real message.
        if (!gone) setSignedIn(false)
      }
    })()
    return () => {
      gone = true
    }
  }, [])

  async function login(e: FormEvent) {
    e.preventDefault()
    setErr('')
    setBusy(true)
    try {
      await post('/api/admin/login', { password })
      setStats(await getStats())
      setSignedIn(true)
    } catch (e) {
      setErr(describe(e))
    } finally {
      setBusy(false)
    }
  }

  async function refresh() {
    setErr('')
    setBusy(true)
    try {
      setStats(await getStats())
    } catch (e) {
      // A 403 means the session expired: back to the gate rather than a sentence.
      if (e instanceof ApiError && e.status === 403) setSignedIn(false)
      else setErr(describe(e))
    } finally {
      setBusy(false)
    }
  }

  if (signedIn === null)
    return (
      <div className="deck">
        <ConsoleBar />
        <main className="screen screen--solo" id="main" tabIndex={-1}>
          <h1 className="title" tabIndex={-1}>Statistiques</h1>
          <p className="prose prose--dim">Lecture des statistiques…</p>
        </main>
      </div>
    )

  if (!signedIn || !stats)
    return (
      <div className="deck">
        <ConsoleBar />
        <main className="screen screen--solo" id="main" tabIndex={-1}>
          <h1 className="title" tabIndex={-1}>Statistiques</h1>
          <p className="prose prose--dim">Le mot de passe de l’animation donne accès aux chiffres de la session.</p>
          <form className="form" onSubmit={(e) => void login(e)}>
            <div className="field">
              <label className="label" htmlFor="stats-password">
                Mot de passe
              </label>
              <input
                id="stats-password"
                className="input"
                type="text"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                aria-required="true"
              />
            </div>
            {err && (
              <p className="err" role="alert">
                {err}
              </p>
            )}
            <button type="submit" className="btn btn--signal btn--wide" disabled={busy || !password}>
              {busy ? 'Ouverture…' : 'Ouvrir les statistiques'}
            </button>
          </form>
        </main>
      </div>
    )

  const { tokens, rooms, answers, ratings, quiz } = stats
  const success = quiz.attempts > 0 ? `${Math.round((quiz.correct / quiz.attempts) * 100)} %` : '—'

  return (
    <div className="deck">
      <ConsoleBar meta="Statistiques" />
      <main className="screen" id="main" tabIndex={-1}>
        <h1 className="title" tabIndex={-1}>Statistiques</h1>
        <p className="prose prose--dim">
          Consommation du modèle depuis le début, et activité des parties en cours.
        </p>
        <div>
          <button type="button" className="btn btn--ghost" disabled={busy} onClick={() => void refresh()}>
            Rafraîchir
          </button>
        </div>
        {err && <p className="prose prose--sm prose--dim">{err}</p>}

        <div className="stats">
          <section className="card">
            <h2 className="label">Tokens consommés</h2>
            <ul className="stats__rows">
              <Row label="Appels" value={int(tokens.calls)} />
              <Row label="Tokens d’entrée" value={int(tokens.input)} />
              <Row label="Tokens de sortie" value={int(tokens.output)} />
              <Row
                label="24 dernières heures (entrée / sortie)"
                value={`${int(tokens.last_24h.input)} / ${int(tokens.last_24h.output)}`}
              />
            </ul>
            {tokens.by_kind.length > 0 && (
              <ul className="stats__rows">
                {tokens.by_kind.map((k) => (
                  <li className="stats__row" key={k.kind}>
                    <span>{KIND_NAME[k.kind] ?? k.kind}</span>
                    <span className="num">{int(k.calls)} appels</span>
                    <span className="num">{int(k.input)} ent.</span>
                    <span className="num">{int(k.output)} sort.</span>
                  </li>
                ))}
              </ul>
            )}
            <p className="prose prose--sm prose--dim">
              Sortie inclut les tokens de raisonnement. Les réponses interrompues ne sont pas comptées.
            </p>
          </section>

          <section className="card">
            <h2 className="label">Parties</h2>
            <p className="stats__fig num">{int(rooms.total)}</p>
            <ul className="stats__rows">
              {rooms.by_phase.map((p) => (
                <Row key={p.phase} label={PHASE_NAME[p.phase] ?? p.phase} value={int(p.count)} />
              ))}
            </ul>
            <p className="prose prose--sm prose--dim">
              Les parties terminées sont supprimées après un temps d’inactivité : ce sont les parties
              actuellement présentes.
            </p>
          </section>

          <section className="card">
            <h2 className="label">Joueurs</h2>
            <p className="stats__fig num">{int(stats.players)}</p>
          </section>

          <section className="card">
            <h2 className="label">Réponses</h2>
            <p className="stats__fig num">{int(answers.total)}</p>
            <ul className="stats__rows">
              <Row label="Essais" value={int(answers.attempts)} />
              <Row
                label="Longueur moyenne du prompt"
                value={answers.avg_prompt_chars === null ? '—' : `${int(Math.round(answers.avg_prompt_chars))} car.`}
              />
              <Row label="Note finale moyenne" value={one(answers.avg_note)} />
            </ul>
          </section>

          <section className="card">
            <h2 className="label">Notation croisée</h2>
            <ul className="stats__rows">
              <Row label="Notes données" value={int(ratings.total)} />
              <Row label="Moyenne" value={one(ratings.avg)} />
            </ul>
          </section>

          <section className="card">
            <h2 className="label">Didacticiel</h2>
            <ul className="stats__rows">
              <Row label="Essais" value={int(quiz.attempts)} />
              <Row label="Bonnes réponses" value={int(quiz.correct)} />
              <Row label="Réussite" value={success} />
            </ul>
          </section>

          <section className="card">
            <h2 className="label">Images générées</h2>
            <p className="stats__fig num">{int(stats.images)}</p>
          </section>

          <section className="card">
            <h2 className="label">Serveur</h2>
            <ul className="stats__rows">
              <Row label="Démarré depuis" value={uptime(stats.uptime_seconds)} />
            </ul>
          </section>
        </div>
      </main>
    </div>
  )
}
