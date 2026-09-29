import { useEffect, useRef, useState, type FormEvent } from 'react'
import { AnswerMeter, Board, ConsoleBar, DangerButton, Readout, Roster, RosterList, Step } from './ui'
import { absent, describe, post, present } from './api'
import { useRoom } from './useRoom'

// The admin's own room code, kept so a reload does not orphan a running game. Nothing on this
// screen ever accepts a typed code: the server generates it, and this only ever restores the
// generated one, so two rooms can never collide because a person typed the same code twice.
// It is written only after the room has answered us (see the effect below), so it is always a
// code that exists.
const roomKey = 'jeu-genai:admin-room'

// The one screen that is never held in a hand, so it is the one screen that may put the
// cursor in a field for you: a laptop on a table, a password the admin is about to type.
function useMountFocus<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  useEffect(() => {
    ref.current?.focus()
  }, [])
  return ref
}

export default function Admin() {
  const [password, setPassword] = useState('')
  const [code, setCode] = useState<string | null>(() => localStorage.getItem(roomKey))
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  // A confirm only holds until anything about the room changes. Keying it on the room's
  // current identity means a round advancing under a disarmed button cannot leave a live
  // "Confirmer" waiting for a second tap, and a restart that fails keeps the confirm up so
  // the admin can try again deliberately.
  const [armed, setArmed] = useState<{ key: string } | null>(null)
  const { state, link, retry } = useRoom(code)
  const verified = useRef(false)
  const passwordRef = useMountFocus<HTMLInputElement>()

  async function run<T>(fn: () => Promise<T>): Promise<T | undefined> {
    setErr('')
    setBusy(true)
    try {
      return await fn()
    } catch (e) {
      setErr(describe(e))
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
      <div className="deck">
        <ConsoleBar />
        <main className="screen screen--solo">
          <h1 className="title" tabIndex={-1}>Console d’animation</h1>
          <p className="prose prose--dim">
            Une partie se crée ici. Le code sera affiché à l’écran pour que tu le donnes au
            groupe.
          </p>
          <form
            className="form"
            onSubmit={(e: FormEvent) => {
              e.preventDefault()
              void run(async () => {
                await post('/api/admin/login', { password })
                const { code: c } = await post<{ code: string }>('/api/admin/rooms')
                enter(c)
              })
            }}
          >
            <div className="field">
              <label className="label" htmlFor="admin-password">
                Mot de passe
              </label>
              {/* Visible rather than masked, on purpose: the admin is reading their own password
                  back off their own screen before starting a game, and a masked field gives no way
                  to spot a typo until it is silently rejected. */}
              <input
                id="admin-password"
                ref={passwordRef}
                className="input"
                type="text"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                aria-required="true"
                aria-invalid={err ? true : undefined}
                aria-describedby={err ? 'admin-login-err' : undefined}
              />
            </div>
            {/* The one field on this form, so the message is unambiguously about it: marked,
                linked, and announced on arrival. The typed password is kept — an admin who
                mistyped a nine-character password should retype one character, not nine. */}
            {err && (
              <p className="err" id="admin-login-err" role="alert">
                {err}
              </p>
            )}
            <button
              type="submit"
              className="btn btn--signal btn--wide"
              disabled={busy || !password}
            >
              {busy ? 'Ouverture…' : 'Créer une partie'}
            </button>
          </form>
        </main>
      </div>
    )

  const status = state?.status ?? 'connecting'
  const players = state?.players ?? []
  const here = state ? present(state) : []
  const gone = state ? absent(state) : []
  // Present players only, exactly as the players' own meter counts them. A segment for a
  // phone that is asleep can never fill, and the host would be reading a bar that is stuck
  // short with no way to tell that from a player who is just slow.
  const lit = here.flatMap((p, i) => (p.answered ? [i] : []))
  const left = here.length - lit.length
  const roomKeyNow = `${code}:${status}:${state?.current ?? 0}`
  const confirming = armed?.key === roomKeyNow

  const restart = () =>
    run(async () => {
      // Throws away every round and every answer, keeps the players and their tokens, and
      // puts the room back in the lobby. Nobody has to rejoin and nobody loses their place.
      await post(`/api/admin/rooms/${code}/restart`)
    })

  // Throwing the room away. No navigation: the admin is already on /admin, and the component's
  // own state is the only thing actually holding the room, so resetting it is the whole of
  // abandoning. `go('/admin')` would look like the obvious move and is quietly wrong — the path
  // does not change, React keeps this component mounted at the same position, `code` never
  // re-reads localStorage, and the stale code survives. Leaving the route does remount, which
  // is why the old version worked at all, and why it worked by accident: `go('/')` dropped the
  // admin on the *player* join form, inviting them to join a game as a player.
  //
  // `setCode(null)` is the branch that renders the password form, and it is also what tears the
  // stream down: the hook's effect deps change, React runs the previous cleanup (which clears
  // the watchdog and closes the EventSource) before the new body early-returns. `state` falls
  // to null with it, because the hook only hands back a snapshot whose code still matches — so
  // the "persist a code once the room has answered" effect cannot re-write the key it was just
  // asked to forget.
  const abandon = () => {
    localStorage.removeItem(roomKey)
    // Reset with the rest, so re-entering a new room is not judged by a flag left over from
    // the abandoned one.
    verified.current = false
    setCode(null)
    // Nothing to report about a room that is gone, and the form being revealed is a different
    // form from the one the error belongs to.
    setErr('')
    setArmed(null)
  }

  // Arming a destructive control is only ever valid for the exact room state it was armed
  // against. The room's status is part of that key, so a confirm can never leak from one state
  // into the other, and any change to the room disarms it without an effect to do it.
  const arm = () => setArmed({ key: roomKeyNow })

  // The code in localStorage outlives the room behind it: the database was reset, or the
  // machine running the party was restarted. Without this, every reload of /admin returns to
  // a console watching a room that is not there, with a start button that cannot work. The
  // recovery is to throw the stale code away and open a new one.
  if (link === 'failed') {
    return (
      <div className="deck">
        <ConsoleBar meta="Console" link={link} onRetry={retry} />
        <main className="screen screen--solo">
          <h1 className="title" tabIndex={-1}>Cette partie n’existe plus</h1>
          <p className="prose prose--dim">
            Le code <strong>{code}</strong> ne mène plus à aucune partie. La machine qui
            héberge la partie a peut-être redémarré, ou la base a été remise à zéro.
          </p>
          <div className="actions">
            <button type="button" className="btn btn--signal btn--wide" onClick={retry}>
              Réessayer
            </button>
            <button
              type="button"
              className="btn btn--ghost btn--wide"
              onClick={() => {
                localStorage.removeItem(roomKey)
                verified.current = false
                setCode(null)
                setPassword('')
              }}
            >
              Ouvrir une nouvelle partie
            </button>
          </div>
        </main>
      </div>
    )
  }

  return (
    <div className="deck deck--wide">
      <ConsoleBar meta="Console" link={link} onRetry={retry} />
      <main className="screen">
        <div className="split split--console">
          <div className="stack">
            <Readout label="Code de la partie" value={code} code />

            <Roster players={players} online={state?.online_count ?? 0} myName="" />

            {/* The one thing the admin cannot read off the players' screens and cannot ask
                for over a noisy room: who is still holding up the round, and who has gone. It
                is the same meter the players are watching, which is the point — the host and
                the room are looking at one instrument. */}
            {status === 'playing' && (
              <>
                <AnswerMeter
                  lit={lit}
                  total={here.length}
                  mine={-1}
                  caption={
                    left === 0
                      ? 'Tout le monde a répondu'
                      : `En attente de ${left} ${left > 1 ? 'joueurs' : 'joueur'}`
                  }
                />
                {/* The operationally important fact, and the opposite of the one a host
                    assumes: a round closes on the players who are here, so somebody dropping
                    out does not need "Manche suivante". Saying it prevents the host from
                    forcing rounds early to work around a problem that is not there. */}
                {gone.length > 0 && (
                  <p className="prose prose--sm prose--cool">
                    {gone.length === 1
                      ? 'Un joueur est hors ligne'
                      : `${gone.length} joueurs sont hors ligne`}{' '}
                    — la manche avance quand même.
                  </p>
                )}
              </>
            )}

            {/* Three states, three jobs, and the buttons follow them rather than the other way
                round. In the lobby there is nothing to undo and nothing to change, so there is
                exactly one button. Mid-game the same players must be able to go again, and that
                is destructive, so it sits below the primary action behind a confirm. When the
                game is over, a restart is no longer destructive — it is the obvious thing to do
                — so it becomes the primary, and starting a new room with a new code comes back
                as the secondary. */}
            <div className="actions">
              {status === 'waiting' && (
                <>
                  <button
                    type="button"
                    className="btn btn--signal btn--wide"
                    disabled={busy}
                    onClick={() =>
                      void run(async () => {
                        await post(`/api/admin/rooms/${code}/start`)
                      })
                    }
                  >
                    Démarrer la partie
                  </button>
                  {/* A room created by mistake, with nobody in it, used to be a dead end: the
                      only control was the one that starts the game, and starting it put an
                      empty room in front of it. This is the way back out. It makes nothing —
                      it just forgets the code, so the next visit to /admin asks for the
                      password again. */}
                  <DangerButton
                    label="Abandonner cette partie"
                    armedLabel={`Confirmer : abandonner la partie ${code}`}
                    confirming={confirming}
                    disabled={busy}
                    onArm={arm}
                    onConfirm={abandon}
                  />
                </>
              )}

              {status === 'playing' && (
                <>
                  <button
                    type="button"
                    className="btn btn--signal btn--wide"
                    disabled={busy}
                    onClick={() =>
                      void run(async () => {
                        await post(`/api/admin/rooms/${code}/next`)
                      })
                    }
                  >
                    Manche suivante
                  </button>
                  <DangerButton
                    label="Recommencer avec les mêmes joueurs"
                    armedLabel="Confirmer : effacer la partie en cours"
                    confirming={confirming}
                    disabled={busy}
                    onArm={arm}
                    onConfirm={() => void restart()}
                  />
                </>
              )}

              {status === 'done' && (
                <>
                  <button
                    type="button"
                    className="btn btn--signal btn--wide"
                    disabled={busy}
                    onClick={() => void restart()}
                  >
                    Rejouer avec les mêmes joueurs
                  </button>
                  <DangerButton
                    label="Nouvelle partie"
                    armedLabel="Confirmer : quitter cette partie"
                    confirming={confirming}
                    disabled={busy}
                    onArm={arm}
                    onConfirm={abandon}
                  />
                </>
              )}

              {/* Until the first frame lands the admin cannot know whether the room is in the
                  lobby or mid-game, so there is no honest button to show. Saying so beats a
                  disabled "Démarrer" that would be wrong half the time. */}
              {status === 'connecting' && (
                <p className="prose prose--dim">Lecture de la partie…</p>
              )}
            </div>

            {/* Beside the controls rather than pinned to the top of <main>. It used to be at
                the top, which in the two-column console is some 400px above the button that
                caused it and a full column away from it: the admin pressed a button, nothing
                visibly happened, and the reason was somewhere the eye had just left. There is
                no field to mark here — these are actions, not inputs — so the alert is the
                whole of it, and it is simply next to the thing it is about. */}
            {err && (
              <p className="err err--inline" role="alert">
                {err}
              </p>
            )}
          </div>

          <div className="stack">
            {/* No counter in the lobby: `total` is 0 there because no round exists yet, and
                "Manche 1 sur 0" is a number about nothing. The lobby is the roster and the
                code, and the round counter belongs to a game that has started. Nor a
                leaderboard: with every score at zero every bar is full, which draws ten
                identical bars and says less than the list of names it sits next to. */}
            {state && state.total > 0 && <Step current={state.current} total={state.total} />}
            {state && state.total > 0 && <Board players={players} myName="" />}
            {/* The host's decision is whether to force the round on, and that turns on knowing
                *who* is unreachable rather than how many. A phone on a lock screen is the
                ordinary case, which is why this is a list of people and not an error. */}
            {state && state.total === 0 && players.length > 0 && (
              <RosterList players={players} myName="" />
            )}
          </div>
        </div>
      </main>
    </div>
  )
}
