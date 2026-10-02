import { useEffect, useRef, useState, type FormEvent } from 'react'
import { AnswerMeter, Board, ConsoleBar, DangerButton, Readout, Roster, RosterList, Step } from './ui'
import { countdown, noun } from './format'
import { useQuizWindow } from './serverClock'
import { absent, ApiError, describe, listRooms, post, present, type Phase, type QuizState, type RoomSummary } from './api'
import { useRoom } from './useRoom'

// The admin's own room code, kept so a reload does not orphan a running game. Nothing on this
// screen ever accepts a typed code: the server generates it, and this only ever restores the
// generated one, so two rooms can never collide because a person typed the same code twice.
// It is written only after the room has answered us (see the effect below), so it is always a
// code that exists.
const roomKey = 'jeu-genai:admin-room'

// How often the list of rooms is re-read while the console has no room open.
//
// Five seconds, and not the console's own half second: that stream carries the game a host is
// standing in front of a class about, and a number that moves has to move with it. This list is
// read between rooms — an admin glances at it to decide which code to go back into — and what it
// carries moves on human timescales: someone joins, someone leaves, a phase advances because a
// human pressed a button. One second would be twelve times the requests for a difference no eye
// can act on; five is short enough that a row is never the reason a tap went to the wrong room,
// and it is one request every five seconds on a screen nobody is even looking at.
//
// A timer rather than a fetch on every render, because every render would be a request per
// render, and this screen re-renders on each keystroke in the password field. And a timer that
// lives in an effect keyed on nothing, so it is torn down the moment the component leaves the
// screen: a console with a room open must not be polling for rooms it is not showing.
const ROOMS_EVERY_MS = 5_000

// What the phase is CALLED, in the room's own French. Six phases, and the name is the first thing
// on the run-of-show block because it is the answer to the only question a host standing in front
// of a class has: where are we. `connecting` is in here with the six because a console that has
// not received a frame yet is a state the block has to be able to draw, and naming it is more
// honest than drawing an empty instrument.
const PHASE_NAME: Record<Phase | 'connecting', string> = {
  connecting: 'Connexion',
  waiting: 'En attente',
  quiz: 'Didacticiel',
  playing: 'Rédaction',
  rating: 'Notation croisée',
  summary: 'Résumé de manche',
  done: 'Partie terminée',
}

// What the one advance button does in each phase, written as data for the same reason the
// server's own transition table is: six phases, and a phase that is missing from the table has
// no advance at all, which is the honest answer for `done` and for the lobby. The labels name
// what will happen next rather than repeating the phase, because the phase is a word from the
// contract and a button labelled `rating` tells an admin nothing they can act on.
//
// The didacticiel is in here and not in the spec's list of four, and it carries the only label in
// the console that is not a constant. One button, three jobs, all of them read off the snapshot:
// §2 gives the host the power to close a question that is still open ("the same button closes the
// question in progress when it is pressed before the deadline, to move faster once the class has
// understood"), then the next question after its reveal, then the game after the eighth. Which
// one it is depends on whether the window is shut and on the index, both of which are in the
// snapshot — so the label is derived here rather than stored, because a button label a host has
// to keep in step with a state machine is a label that will be wrong during the round the host
// most needs to trust it.
function advanceLabel(phase: Phase, quiz: QuizState | null, windowClosed: boolean): string | null {
  switch (phase) {
    case 'waiting':
      return 'Lancer la partie'
    case 'quiz':
      if (!quiz) return null
      if (!windowClosed) return 'Fermer la question'
      return quiz.index + 1 >= quiz.total ? 'Ouvrir la partie' : 'Question suivante'
    case 'playing':
      return 'Ouvrir la notation'
    case 'rating':
      return 'Prompt suivant'
    case 'summary':
      return 'Tour suivant'
    case 'done':
      return null
  }
}

// One block of plain French about where the game is. Four parts and no more: what the phase is
// called, one sentence about the state of it, the counts, and what the host can do about it.
//
// The last part is the reason the block exists. A teacher in front of a class cannot read meters;
// they need to be told the step and what to press. Everything it says is already in the snapshot,
// which is the point — there is no second truth to keep in step with the room.
type Note = {
  /** One sentence about the phase. Also, on its own, the screen's live-region announcement, so
   * it must never carry the countdown: a value that changes four times a second inside a live
   * region talks over everything else on the page. */
  state: string
  /** The countdown, in the visible block only. */
  timer?: string
  /** The counts, in the visible block only. */
  figures?: string
  /** What the host can do about it. */
  hint?: string
}

// The one screen that is never held in a hand, so it is the one screen that may put the
// cursor in a field for you: a laptop on a table, a password the admin is about to type.
function useMountFocus<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  useEffect(() => {
    ref.current?.focus()
  }, [])
  return ref
}

// The two relaunch targets, drawn together because they are always offered together: after a
// finished game, where a relaunch is the obvious thing to do and "with the didacticiel" is the
// primary action, and behind the armed confirm of a running one, where both are the ghost half of
// a destructive pair. Same two buttons, same order, same strings — the only difference between
// the two sites is which of them is primary, so that is the only thing this component takes.
//
// The comment on the caller says why the pair is one action at all; what is left out of it is
// only that the pair was written down twice.
function RelaunchButtons({
  busy,
  primary,
  onRestart,
}: {
  busy: boolean
  primary: boolean
  onRestart: (withTutorial: boolean) => void
}) {
  return (
    <>
      <button
        type="button"
        className={`btn ${primary ? 'btn--signal' : 'btn--ghost'} btn--wide`}
        disabled={busy}
        onClick={() => onRestart(true)}
      >
        Relancer avec le didacticiel
      </button>
      <button
        type="button"
        className="btn btn--ghost btn--wide"
        disabled={busy}
        onClick={() => onRestart(false)}
      >
        Relancer sans didacticiel
      </button>
    </>
  )
}

// The rooms this console can go back into, and the only place in the console that is not the
// console.
//
// One component and not inline in the screen below it, for the same reason the run-of-show is a
// component: it owns a timer, and a timer owned by a screen's body is a timer whose cleanup is
// one forgotten branch away from a console that polls for rooms while an audience is watching.
//
// FOUR states, and the first one is not an error. `GET /api/admin/rooms` is behind the same
// `require_admin` as everything else on this console, so it answers 403 until a password has been
// submitted — which is the state the very first paint of this screen is in, for an admin who has
// not yet been asked to do anything. Printing a fault there would be a broken screen telling
// someone they have a problem, before they have been given anything to press. So a 403 is read as
// what it is — « nobody has signed in here yet » — and the list renders nothing at all: not the
// rows, not a heading, not a message. The password field below already says everything there is
// to say about it.
//
// This GET is therefore the session check, and that is deliberate rather than a shortcut: the
// list appearing IS the evidence the session is alive. The obvious-looking alternative is to
// open the endpoint and let the list paint, then gate it — and that would publish every room's
// code, to every phase of every game, to anyone who typed /admin. There is nothing to unpublish
// afterwards, so the session is the precondition and not a decoration on top.
//
// The other three states are about the list itself: still loading, empty, or failed. None of
// them hides the create form underneath. A list that failed to load is the one that could: an
// admin whose request timed out still has to be able to open a game, and a screen that shows
// only an error tells them the console is broken when the only thing that failed is the
// convenience on top of it. So the error is a line under the list and the password field stays
// exactly where it was.
//
// The phase shown is the one from `PHASE_NAME` and not a wording written here: the list and the
// console call a phase by one name, and a second table is a second place for the six phases and
// their French to drift apart.
function RoomsList({ onOpen }: { onOpen: (code: string) => void }) {
  const [rooms, setRooms] = useState<RoomSummary[] | null>(null)
  const [err, setErr] = useState('')
  // Set by the 403 and cleared by any other answer, so it is a statement about the LAST reply and
  // not a latch: a session that dies mid-session has to be able to come back, and the next tick
  // is what says whether it did.
  const [signedOut, setSignedOut] = useState(false)

  useEffect(() => {
    // `gone` rather than a cancel token because there is nothing to do with a response that
    // arrives after the screen does: the unmounted list throws it away, and the one request
    // still in flight at teardown is at most one list of rooms nobody is looking at.
    let gone = false
    const read = async () => {
      try {
        const next = await listRooms()
        if (gone) return
        setRooms(next)
        setErr('')
        setSignedOut(false)
      } catch (e) {
        if (gone) return
        // The status is already on the thrown `ApiError` and is read there rather than by
        // asking again: a second probe to find out whether this was a 403 would be a request
        // whose only purpose is to classify another request, and the endpoint that answers it
        // is the same one that just said no.
        if (e instanceof ApiError && e.status === 403) {
          setSignedOut(true)
          setErr('')
          return
        }
        setSignedOut(false)
        // Not `describe(e)`: that is the player's phrasing of a failure they can act on — «
        // Réessaie dans un instant », « Cette partie n’existe pas » — and neither is true of a
        // list an admin did not ask for by name. The screen says what is true here and stops
        // there.
        setErr('La liste des parties ne répond pas.')
      }
    }
    void read()
    // Kept running across the 403 rather than stopped on it. It costs one refused request per
    // five seconds on a screen nobody can act on, and it is what lets the list appear by
    // itself if the session is re-established in another tab while this one sits on the
    // password field — which is the only way that can happen, since this screen's own submit
    // signs in and creates a room in one press, which unmounts the list entirely.
    const beat = setInterval(() => void read(), ROOMS_EVERY_MS)
    return () => {
      gone = true
      clearInterval(beat)
    }
  }, [])

  // Nothing at all, including the heading: a « Parties ouvertes » with no parties under it is a
  // promise the screen is not making yet, and a gap where the list will be is a better thing to
  // leave the password form than a sentence saying there is nothing.
  if (signedOut) return null

  return (
    <section className="roomslist">
      <h2 className="label">Parties ouvertes</h2>

      {rooms === null && !err && (
        <p className="prose prose--sm prose--dim">Lecture des parties en cours…</p>
      )}

      {/* The only two shapes the list can be in when it has never arrived, and they are told
          apart by whether we have anything to show. `rooms === null` alone would also cover
          "the last poll failed over a list we already had", and blanking four good rows to
          print one line of trouble is a worse answer than keeping them and saying they may be
          behind. */}
      {rooms === null && err && <p className="notice">{err}</p>}

      {rooms !== null && rooms.length === 0 && (
        <p className="notice">
          Aucune partie ouverte. La création d’une partie est juste en dessous.
        </p>
      )}

      {rooms !== null && rooms.length > 0 && (
        <ul className="roomslist__rows">
          {rooms.map((r) => (
            <li key={r.code}>
              {/* A button and not a row with a handler on it: this is the only control on the
                  screen that chooses which game an admin is about to run in front of a class,
                  and a div would cost it the keyboard, the focus ring and the announced role
                  for nothing in return. `enter` is the same path a created room takes, so a
                  room that was opened from here and a room that was just made arrive
                  identically — including the "persist the code once the room has answered"
                  effect, which is the only thing that writes the key back. */}
              <button type="button" className="btn btn--wide roomslist__row" onClick={() => onOpen(r.code)}>
                <span className="roomslist__code">{r.code}</span>
                <span className="roomslist__where">
                  <span className="roomslist__phase">{PHASE_NAME[r.phase]}</span>
                  {/* `current` is 0-based on the wire and this counts from one, like every
                      other round number in the console — and says so only when there is a
                      round, because "manche 1 sur 0" is a number about nothing. */}
                  {r.total > 0 && (
                    <span className="roomslist__round">
                      Manche {r.current + 1} sur {r.total}
                    </span>
                  )}
                </span>
                {/* Both counts, and they are two questions. How many phones have joined the
                    room is what an admin expects to see; how many are connected right now is
                    what tells them the class has gone home, and one number cannot answer the
                    second without hiding the first. Dimmed, not hidden, when they disagree —
                    a room of nine with one phone left is still a room of nine. */}
                <span className="roomslist__figs">
                  {noun(r.players, 'joueur', 'joueurs')}
                  <span className="roomslist__now">
                    {noun(r.online_count, 'joueur connecté', 'joueurs connectés')}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* Under the rows and never over them, so a poll that fails while a list is on screen
          reads as what it is: the list you are looking at may be a few seconds behind, not
          gone. */}
      {rooms !== null && rooms.length > 0 && err && (
        <p className="prose prose--sm prose--dim">{err}</p>
      )}
    </section>
  )
}

export default function Admin() {
  const [password, setPassword] = useState('')
  const [code, setCode] = useState<string | null>(() => localStorage.getItem(roomKey))
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  // The two flags a room is born with, and the only two moments the screen ever offers them:
  // the spec fixes both for the whole game, so after creation there is nowhere to put a control
  // for them that would not be a lie. Both default to on. A didacticiel a host has to remember
  // to ask for is a didacticiel half the class skips, and anonymity is the feature the whole
  // rating phase is built to preserve.
  const [tutorial, setTutorial] = useState(true)
  const [anonymous, setAnonymous] = useState(true)
  // A confirm only holds until anything about the room changes, and says which action it is
  // confirming: two of the destructive controls now arm the same way, and an arm that did not
  // carry its action could confirm "throw the room away" after the host had reached for the
  // other one. Keying it on the room's current identity means a round advancing under a
  // disarmed button cannot leave a live "Confirmer" waiting for a second tap, and a restart
  // that fails keeps the confirm up so the admin can try again deliberately.
  //
  // One action left, so the state is the key and not a `{key, action}` pair: the arm was
  // carrying which control was armed so that two arms could not be confused for one another,
  // and with the relaunch as the only control left to arm there is nothing to confuse. The key
  // is the part that survives a phase moving underneath it, which is why this is now a bare
  // string rather than `null`.
  const [armed, setArmed] = useState<string | null>(null)
  const { state, link, retry } = useRoom(code)
  // The same clock the players are watching, on the same terms: the server's deadline against the
  // server's clock, so the host's "12 secondes" and the class's twelve seconds are the same
  // twelve seconds. A host counting on their own phone is the one version of this that was always
  // going to be a second late.
  //
  // Called here, with the other hooks and above the two early returns below, because that is where
  // a hook has to be: `!code` and `link === 'failed'` both render whole screens of their own, and
  // a hook after them would be called on some renders and not others.
  const quizWindow = useQuizWindow(state?.quiz ?? null)
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
        {/* The skip link's target — see the note on `SkipLink` in App.tsx. Each of this
            component's three branches is a whole screen of its own and renders alone. */}
        <main className="screen screen--solo" id="main" tabIndex={-1}>
          <h1 className="title" tabIndex={-1}>Console d’animation</h1>
          <p className="prose prose--dim">
            Une partie se crée ici. Le code sera affiché à l’écran pour que tu le donnes au
            groupe.
          </p>

          {/* Above the flags and above the form, not below them. A host arriving at a console
              with four games already on the server is looking for the one they left an hour
              ago, and everything on this screen below this line creates rather than reopens —
              putting the list at the bottom would make the common case a scroll past a password
              field they do not need. The create form is not demoted by being second: it is
              still one screen, one scroll, no navigation between the two. */}
          <RoomsList onOpen={enter} />
          {/* Two flags, above the password and above the button that creates the party they
              configure, and outside the form: they are not part of signing in, and a control
              inside the form would create the room the moment it was touched. Position matters
              more than it looks — a flag below "Créer une partie" is a flag nobody reads, because
              the host pressed the button before seeing it and the party is already fixed.
              Native checkboxes with the console's accent, rather than buttons drawn to look like
              checkboxes: the whole cost of that choice is that the box cannot be styled to match
              the panel, and what it buys is a real control with real keyboard, real focus and a
              real checked state, which three divs would each have to re-implement. */}
          <div className="checks">
            <label className="check">
              <input
                type="checkbox"
                className="check__box"
                checked={tutorial}
                onChange={(e) => setTutorial(e.target.checked)}
              />
              <span className="check__text">
                Avec le didacticiel
                <span className="check__hint">
                  Huit questions avant la première manche. Personne n’écrit de prompt avant d’y
                  avoir répondu.
                </span>
              </span>
            </label>
            <label className="check">
              <input
                type="checkbox"
                className="check__box"
                checked={anonymous}
                onChange={(e) => setAnonymous(e.target.checked)}
              />
              <span className="check__text">
                Anonyme pendant la notation
                <span className="check__hint">
                  Les noms sont masqués tant que les prompts sont notés. Ils reviennent au résumé
                  de la manche.
                </span>
              </span>
            </label>
            <p className="prose prose--sm prose--dim">
              Les deux sont réglés pour toute la partie. Une relance peut repartir avec ou sans
              didacticiel.
            </p>
          </div>

          <form
            className="form"
            onSubmit={(e: FormEvent) => {
              e.preventDefault()
              void run(async () => {
                await post('/api/admin/login', { password })
                // Both flags ride the creation POST and nowhere else. They are the room's own
                // fields, named as the server names them, so the payload is readable in one
                // place rather than translated on the way out.
                const { code: c } = await post<{ code: string }>('/api/admin/rooms', {
                  tutorial,
                  anonymous,
                })
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

  // `connecting` is not a phase: it is the absence of a snapshot, and it stands in for the
  // lobby only because the lobby is the first thing that happens. A union of the real phases and
  // that one sentinel keeps `advanceLabel`'s exhaustiveness checked at this lookup rather than
  // silently falling through to no button at all.
  const phase: Phase | 'connecting' = state?.phase ?? 'connecting'
  const players = state?.players ?? []
  const here = state ? present(state) : []
  const gone = state ? absent(state) : []
  // Present players only, exactly as the players' own meter counts them. A segment for a
  // phone that is asleep can never fill, and the host would be reading a bar that is stuck
  // short with no way to tell that from a player who is just slow.
  const lit = here.flatMap((p, i) => (p.answered ? [i] : []))
  const left = here.length - lit.length
  // The room, but only once the two questions the right-hand column asks have been answered:
  // is there a snapshot, and does it have rounds to number. Both blocks down there ride on it,
  // and it was typed out four times for two blocks — four chances for the round counter and the
  // standings to disagree about whether there is a round to show.
  const round = state !== null && state.total > 0 ? state : null
  // The phase is part of the arm key as well as the round: an advance moves the phase, and a
  // confirm must never survive it.
  const roomKeyNow = `${code}:${phase}:${state?.current ?? 0}`
  const arm = () => setArmed(roomKeyNow)
  const armedNow = armed === roomKeyNow

  // The window's state, on the host's screen as well as the players'. `revealed` is the server's
  // own word and it only turns on the next frame; the comparison turns at the deadline itself, so
  // the OR is what lets a host press "Fermer la question" the instant the class has finished
  // talking rather than up to half a second later.
  const quizState = state?.quiz ?? null
  const quizClosed = quizState ? quizState.revealed || !quizWindow.open : true
  // `null` means there is no honest button: before the first frame the console does not know
  // whether the room is in the lobby or mid-game, and a disabled "Lancer la partie" would be
  // wrong half the time. `done` is the other null, and that one is honest.
  const advance = phase === 'connecting' ? null : advanceLabel(phase, quizState, quizClosed)

  // Where the game is, in one block. Every figure is read off the snapshot and nothing is stored,
  // so the block cannot disagree with the instruments below it — which is the failure it is
  // replacing, where six numbers on a screen each meant something and the host had to reconcile
  // them while a class waited.
  const note: Note = (() => {
    switch (phase) {
      case 'connecting':
        return { state: 'Lecture de la partie…' }
      case 'waiting':
        return {
          state: 'La partie n’a pas encore commencé.',
          figures: `${noun(state?.online_count ?? 0, 'joueur connecté', 'joueurs connectés')} sur ${players.length} dans la pièce.`,
          hint: 'Donne le code à la classe, puis lance la partie.',
        }
      case 'quiz': {
        const q = state?.quiz
        if (!q) return { state: 'Le didacticiel n’est pas encore ouvert.' }
        return {
          state: quizClosed
            ? 'La bonne réponse est affichée, la classe peut commenter.'
            : 'Les trente secondes sont ouvertes à toute la classe.',
          timer: quizClosed
            ? undefined
            : `${noun(countdown(quizWindow.left), 'seconde', 'secondes')} pour répondre`,
          figures: `${noun(q.done, 'joueur a répondu', 'joueurs ont répondu')} sur ${noun(q.online_count, 'joueur connecté', 'joueurs connectés')}.`,
          hint: quizClosed
            ? 'La classe passe à la suite quand la majorité est prête. Tu peux forcer.'
            : 'Tu peux refermer la question avant la fin si la classe a compris.',
        }
      }
      case 'playing':
        return {
          state:
            state && state.total > 0
              ? `Manche ${state.current + 1} sur ${state.total}.`
              : 'La première manche est ouverte.',
          figures:
            here.length === 0
              ? 'Aucun joueur connecté.'
              : `${noun(lit.length, 'joueur a envoyé', 'joueurs ont envoyé')} sur ${noun(here.length, 'joueur connecté', 'joueurs connectés')}.`,
          // Both facts at once when both are true. A host who has just been told the round is
          // ready to close and who then notices two unlit lamps in the roster needs to know that
          // those two phones are not the reason — and the offline line is the one that stops a
          // host forcing the round early to work around a problem that is not there.
          hint: [
            here.length > 0 && left === 0 ? 'Tu peux ouvrir la notation.' : '',
            gone.length > 0
              ? `${noun(gone.length, 'joueur est hors ligne', 'joueurs sont hors ligne')} — la manche avance sans attendre.`
              : '',
          ]
            .filter(Boolean)
            .join(' '),
        }
      case 'rating': {
        const r = state?.reveal
        if (!r) return { state: 'La notation n’a pas encore commencé.' }
        return {
          state: `Prompt ${r.index + 1} sur ${r.count}.`,
          figures: `${noun(r.rated, 'joueur a noté', 'joueurs ont noté')} ce prompt.`,
          // The sentence the board cannot say for itself. §4 hides every score for the whole
          // rating phase, so a host glancing at a console with no standings on it has no way to
          // tell a deliberate absence from a broken screen — and "is it broken" is exactly the
          // thought that makes someone press the button.
          hint: 'Les scores et les autres prompts sont masqués : c’est voulu.',
        }
      }
      case 'summary': {
        const total = state?.total ?? 0
        const round = (state?.current ?? 0) + 1
        const last = total > 0 && round >= total
        return {
          state:
            total > 0
              ? `La manche ${round} sur ${total} est commentée.`
              : 'La manche est commentée.',
          // The one phase that never moves on its own, so it reads as an invitation. "En attente"
          // with nothing after it would be the console's version of a stall, and this is the
          // moment in a game where the host is most likely to be talking and least likely to be
          // watching a screen.
          hint: last
            ? 'C’est la dernière : lance le classement final quand la classe a fini.'
            : 'La partie attend ton bouton pour la manche suivante.',
        }
      }
      case 'done':
        return {
          state: 'Les manches sont jouées.',
          hint: 'Le classement final est affiché. Tu peux relancer avec les mêmes joueurs.',
        }
    }
  })()

  // Throws away every round and every answer, keeps the players and their tokens, and puts the
  // room back in the lobby. Nobody has to rejoin and nobody loses their place. The flag is sent
  // rather than remembered: the spec fixes it for the whole game, and a relaunch is exactly the
  // moment a host is allowed to choose the other one.
  const restart = (withTutorial: boolean) =>
    run(async () => {
      await post(`/api/admin/rooms/${code}/restart`, { tutorial: withTutorial })
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
  // the watchdog and closes the EventSource) before the new body early-runs. `state` falls
  // to null with it, because the hook only hands back a snapshot whose code still matches — so
  // the "persist a code once the room has answered" effect cannot re-write the key it was just
  // asked to forget.
  //
  // Added after the room list, and it corrects the first line: this does NOT throw the room
  // away. Nothing in this function leaves the browser — no delete endpoint exists in this API —
  // so the room stays in the database and comes back in the list, with its players and its
  // rounds intact. What it throws away is this tab's claim to it: the stored code, the stream,
  // and the console's own state. A button labelled « Abandonner cette partie » was therefore
  // promising a destruction that does not happen, and the confirm in front of it was asking a
  // host to agree to a lie before they were allowed to look at another room. The one caller
  // now says « Retour à la liste », which is the whole of what happens.
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

  // The code in localStorage outlives the room behind it: the database was reset, or the
  // machine running the party was restarted. Without this, every reload of /admin returns to
  // a console watching a room that is not there, with a start button that cannot work. The
  // recovery is to throw the stale code away and open a new one.
  if (link === 'failed') {
    return (
      <div className="deck">
        <ConsoleBar meta="Console" link={link} onRetry={retry} />
        <main className="screen screen--solo" id="main" tabIndex={-1}>
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
      <main className="screen" id="main" tabIndex={-1}>
        <div className="split split--console">
          <div className="stack">
            {/* Where the game is. Above the instruments rather than beside them, and before the
                code, because it is the one thing a host needs before every other thing on this
                screen: which step the room is on, and what pressing the button will do about it.
                On a phone — which is where a teacher actually holds this — it is the first thing
                under the rail and the only part of the console that reads as one sentence.

                Its own live region announces the phase and the state and nothing else. The
                countdown and the counts are inside this block and change several times a second
                and several times a question respectively; putting either in the region would have
                a screen reader talking over the class for the whole didacticiel. */}
            <div className="runshow" data-live={quizClosed ? undefined : 'true'}>
              <p className="visually-hidden" aria-live="polite">
                {`${PHASE_NAME[phase]}. ${note.state}`}
              </p>
              <h2 className="runshow__phase">{PHASE_NAME[phase]}</h2>
              <p className="runshow__lead">
                {note.state}
                {/* A space, not a separator. The state sentence ends in a full stop, so anything
                    between the two would land between two sentences that are already separate —
                    and the colour is what tells the figure out anyway. */}
                {note.timer && <span className="runshow__timer"> {note.timer}</span>}
              </p>
              {note.figures && <p className="runshow__figs">{note.figures}</p>}
              {note.hint && <p className="runshow__hint">{note.hint}</p>}
            </div>

            <Readout label="Code de la partie" value={code} code />

            <Roster players={players} online={state?.online_count ?? 0} myName="" />

{/* The quiz phase counts the players who have finished the didacticiel, because
                that is what is holding the gate; the rating phase counts the players who have
                scored the prompt on screen, which is the only question the host has to answer
                between "Prompt suivant" presses. */}
            {phase === 'quiz' && (
              <AnswerMeter
                lit={players.flatMap((p, i) => (p.quiz_done ? [i] : []))}
                total={players.length}
                mine={-1}
                caption="Ont terminé le didacticiel"
              />
            )}
            {phase === 'playing' && (
              <AnswerMeter
                lit={lit}
                total={here.length}
                mine={-1}
                caption={
                  left === 0
                    ? 'Tout le monde a répondu'
                    : `En attente de ${noun(left, 'joueur', 'joueurs')}`
                }
              />
            )}
            {phase === 'rating' && state?.reveal && (
              <>
                <p className="prose prose--sm">
                  Prompt {state.reveal.index + 1} sur {state.reveal.count}.
                  {state.reveal.author !== null ? ` Écrit par ${state.reveal.author}.` : ''}
                </p>
                <AnswerMeter
                  lit={Array.from({ length: Math.min(state.reveal.rated, state.reveal.count) }, (_, i) => i)}
                  total={state.reveal.count}
                  mine={-1}
                  caption="Prompts notés"
                />
              </>
            )}

            {/* Three states, three jobs, and the buttons follow them rather than the other way
                round. In the lobby there is nothing to undo and nothing to change, so there is
                exactly one button. Mid-game the same players must be able to go again, and that
                is destructive, so it sits below the primary action behind a confirm. When the
                game is over, a restart is no longer destructive — it is the obvious thing to do
                — so it becomes the primary, and starting a new room with a new code comes back
                as the secondary.

                One advance button, and its label is derived from the snapshot rather than stored:
                the room already says where it is and how far through the didacticiel it is, and a
                label the host has to keep in step with a state machine is a label that will be
                wrong during the round the host most needs to trust it. */}
            <div className="actions">
              {advance && (
                <button
                  type="button"
                  className="btn btn--signal btn--wide"
                  disabled={busy}
                  onClick={() =>
                    void run(async () => {
                      // `start` for an empty room and `next` for everything after it. The lobby
                      // is the one phase where the transition is a different endpoint rather than
                      // the same one resolving differently, and it is the only one that has one.
                      const path =
                        phase === 'waiting'
                          ? `/api/admin/rooms/${code}/start`
                          : `/api/admin/rooms/${code}/next`
                      await post(path)
                    })
                  }
                >
                  {advance}
                </button>
              )}

              {/* Until the first frame lands the console cannot know whether the room is in the
                  lobby or mid-game, so there is no honest button to show, and that beats a
                  disabled "Lancer la partie" which would be wrong half the time. The sentence
                  itself is the run-of-show's job now — it opens on « Connexion · Lecture de la
                  partie… » — so this block is simply empty until there is something to press,
                  which is what an empty action area is supposed to mean. */}

              {/* Both relaunch targets, offered by one action. "With the didacticiel" is the one
                  pressed by default because a course that has just taught the material is the
                  case the spec is about; "without" is the practice run, and it exists because a
                  class that has already done it once must be able to do it again without sitting
                  through eight questions they have seen. Two armed confirms instead of one
                  would be the alternative, and a thumb reaching for either of them in a noisy
                  room picks the other. */}
              {phase === 'done' ? (
                <RelaunchButtons busy={busy} primary onRestart={restart} />
              ) : (
                phase !== 'waiting' &&
                phase !== 'connecting' && (
                  <DangerButton
                    label="Relancer avec les mêmes joueurs"
                    armedLabel="Confirmer : effacer la partie en cours"
                    confirming={armedNow}
                    disabled={busy}
                    choices={<RelaunchButtons busy={busy} primary={false} onRestart={restart} />}
                    onArm={arm}
                  />
                )
              )}

              {/* The one way out, and the only control in this console that merely leaves.
                  Until the room list existed the console held exactly one room — the one in
                  this tab's localStorage — so the only way back to the lobby was a control
                  labelled « Abandonner cette partie », behind a confirm that said it would
                  abandon the partie.

                  It never did, and that is the whole reason the control is gone rather than
                  relabelled. `abandon()` is four lines of local state — removeItem, a ref, a
                  `setCode`, a cleared error — and no request leaves the browser: there is no
                  delete endpoint in this API at all, and the one destructive call that does
                  exist (`restart`) wipes the rounds and keeps the room. So a confirm reading
                  « Confirmer : abandonner la partie ABCD » was guarding a move that could
                  lose nothing, and it was asking a host who merely wanted to look at another
                  room to type the word "abandon" first. Worse, the room it claimed to abandon
                  survived, and came back in the list.

                  What replaces it is the same function with a label that says what it does:
                  the room keeps running, its players keep their scores, the code is forgotten
                  in this tab, and the list is on the other side. Nothing is confirmed because
                  nothing is at stake — and the relaunch above stays armed precisely because
                  that one really does throw eight rounds of answers away. A console where the
                  only confirm is the one that matters is a console where a confirm means
                  something.

                  In the lobby this is also the control the old one was for. The room it was
                  written against — created by mistake, nobody in it — is still here, still one
                  press, and the thing it was really offering (a way out of a room you did not
                  want) is what this button is. What is no longer offered is the fiction that
                  the room will be gone afterwards.

                  A ghost button rather than `btn--signal`: sodium is the console's "armed and
                  live" colour and it is spent on the one action that moves the game forward.
                  Going back is not that. */}
              <button type="button" className="btn btn--ghost btn--wide" onClick={abandon}>
                Retour à la liste
              </button>
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
            {/* The standings, in the two phases where there is one to stand in. Not in the lobby, where
                every score is zero and ten full bars say less than the list of names beside them;
                not during the didacticiel or the rating, where the server blanks every score and
                the board would draw ten em dashes — one thing the host cannot use, when what
                they need is where the room actually is. The round counter rides on the same
                condition: `total` is 0 in the lobby, and "Manche 1 sur 0" is a number about
                nothing. The named roster takes both of the phases the board skips, because the
                host's decision there is whether to force the room on and that turns on knowing
                *who* is unreachable rather than how many. */}
            {round && phase === 'playing' && <Step current={round.current} total={round.total} />}
            {round && phase === 'playing' && <Board players={players} myName="" />}
            {round && phase === 'summary' && <Step current={round.current} total={round.total} />}
            {round && phase === 'summary' && <Board players={players} myName="" />}
            {(phase === 'waiting' || phase === 'quiz' || phase === 'rating') &&
              players.length > 0 && <RosterList players={players} myName="" />}
          </div>
        </div>
      </main>
    </div>
  )
}
