import { useEffect, useRef, useState } from 'react'
import Join from './Join'
import Lobby from './Lobby'
import Admin from './Admin'
import Game from './Game'
import Quiz from './Quiz'
import Rating from './Rating'
import Results from './Results'
import { ConsoleBar } from './ui'
import { getToken, type State } from './api'
import { useRoom, type Link } from './useRoom'

// Real paths, not hash routes. `/admin` and `/room/<CODE>` are what an admin bookmarks,
// types and reads aloud, and they are what a server has to be able to serve — which is the
// shape Django hosting would need. Vite's SPA fallback answers both with index.html, and
// the dev proxy still claims /api before the fallback ever runs.
function route() {
  const path = location.pathname.replace(/^\/+|\/+$/g, '')
  const [head, tail] = path.split('/')
  if (head === 'admin') return { screen: 'admin' as const, code: null }
  if (head === 'room' && tail) return { screen: 'room' as const, code: tail.toUpperCase() }
  return { screen: 'join' as const, code: null }
}

// A room is only meaningful to a player who is in it, and the token is the proof. Someone
// who opens a shared /room link, or who bookmarks one by hand, has none — so instead of
// hanging on a stream they can never join, they get the join form with the code already
// filled in. That also covers the most common wrong-code case before it can cost a reload.
function Gate({ code, onJoined }: { code: string; onJoined: (code: string) => void }) {
  return <Join preset={code} onJoined={onJoined} />
}

// The one control that is on every screen and belongs to none of them, so it lives here
// rather than in the six components it has to precede. It is `position: fixed`, which means
// it does not need a wrapper and does not disturb the grid of whatever it is rendered
// alongside — and it is rendered as the first sibling in each branch below, so it is the
// first tab stop on every screen, which is the entire reason a skip link exists.
//
// The target is resolved at activation rather than left to the `#main` fragment, and that is
// all the handler has to do: every `<main>` in the console authors `id="main"` and
// `tabIndex={-1}` in its own markup, so the element is already in the document and already
// focusable by the time anyone can activate this. The id and the tabindex used to be stamped
// on at click time, which meant the console mutated its own markup on the one interaction that
// is supposed to be a pure accessibility affordance — and left the link's target unresolvable
// to anything else that looked for it, including the DOM before the first click.
function SkipLink() {
  return (
    <a
      className="skiplink"
      href="#main"
      onClick={(e) => {
        e.preventDefault()
        document.querySelector<HTMLElement>('main')?.focus()
      }}
    >
      Aller au contenu
    </a>
  )
}

// Reached when the stream has failed three times over without ever opening. The browser
// retries on its own and we let it, so this is the state for a room that is not answering at
// all, with a way back instead of an indefinite "Connexion…". It deliberately does not claim
// the room is missing: EventSource cannot read a status code, so all we know is that nothing
// came back, and a server restarting on a laptop looks exactly like a bad code. It never asks
// for a code or a name — reconnecting is the server's job and needs nothing from the player.
function Fault({ code, onRetry, onLeave }: { code: string; onRetry: () => void; onLeave: () => void }) {
  return (
    <div className="deck">
      <ConsoleBar meta={`Partie ${code}`} />
      {/* The skip link's target, authored rather than stamped: `id` so the link resolves and
          `tabIndex` so `<main>` can hold focus at all — a focusable-by-default element is not
          a focusable one. The id is unique because there is one `<main>` per screen. */}
      <main className="screen screen--solo" id="main" tabIndex={-1}>
        <h1 className="title" tabIndex={-1}>La partie ne répond pas</h1>
        <p className="prose prose--dim">
          Rien n’est revenu de la partie <strong>{code}</strong>. Si la machine qui l’héberge
          s’est éteinte ou a redémarré, elle répondra dès qu’elle sera relancée. Sinon,
          vérifie que tu es toujours sur le même réseau.
        </p>
        <div className="actions">
          <button type="button" className="btn btn--signal btn--wide" onClick={onRetry}>
            Réessayer
          </button>
          <button type="button" className="btn btn--ghost btn--wide" onClick={onLeave}>
            Saisir un autre code
          </button>
        </div>
      </main>
    </div>
  )
}

// The one list of screens in the console, and it carries what each screen needs to draw itself.
// The screen is a discriminated field rather than a bare word because the alternative was a
// second ladder of conditions below, re-testing the same five facts and handing a narrowed `state`
// or a `code!` to each component in a hand-written order. Here the resolver below decides it and
// the switch renders it, so there is one list to keep right.
//
// `code` is a string on every case that has a room — the resolver reached them by testing it — and
// `state` is a snapshot on every case that has one, which is why no case needs an assertion.
type View =
  | { screen: 'admin' }
  | { screen: 'join' }
  | { screen: 'gate'; code: string }
  | { screen: 'fault'; code: string }
  | { screen: 'connecting'; code: string }
  | { screen: 'lobby'; state: State }
  | { screen: 'results'; state: State }
  | { screen: 'quiz'; state: State; code: string }
  | { screen: 'rating'; state: State; code: string }
  | { screen: 'game'; state: State; code: string }

export default function App() {
  const [{ screen, code }, setRoute] = useState(route)

  // The stream is identified by the player's own token, so the server can tell whose phone is
  // on it. Without it every player reads as offline, this one included. The admin passes no
  // token at all and connects anonymously, which is the difference between a missing
  // argument and a null one.
  const token = code ? getToken(code) : null
  const { state, link, retry, answer, sendPrompt, wsLive } = useRoom(code, token)

  // The screen, resolved once. The URL is not enough: a round ending moves Game -> Results
  // without the address changing at all, which is the largest swap in the game and the one
  // where a player most needs to be told.
  //
  // `quiz` and `rating` are screens of their own and `summary` is not: the synthesis of a round
  // that has just been scored stays on the play screen with the composer, because the player is
  // still in the middle of writing ten prompts and the round they are about to be handed is the
  // next thing they need. A separate tab would put the standings and the work in two places and
  // make the second look like a summary of the game rather than of the last round.
  const view: View =
    screen === 'admin'
      ? { screen: 'admin' }
      : !code
        ? { screen: 'join' }
        : !token
          ? { screen: 'gate', code }
          : link === 'failed'
            ? { screen: 'fault', code }
            : !state
              ? { screen: 'connecting', code }
              : state.phase === 'waiting'
                ? { screen: 'lobby', state }
                : state.phase === 'done'
                  ? { screen: 'results', state }
                  : state.phase === 'quiz'
                    ? { screen: 'quiz', state, code }
                    : state.phase === 'rating'
                      ? { screen: 'rating', state, code }
                      : { screen: 'game', state, code }

  // The screen changed and nothing else about the page did, so a keyboard or screen-reader
  // user was given a different screen with no signal whatsoever: the same URL bar, the same
  // chrome, and focus sitting wherever the last control left it — quite possibly on a button
  // that is no longer in the document. Moving focus to the new screen's heading is the
  // smallest thing that says "you are here now", and it also puts the top of the new screen
  // in view, which a page swapped in under a scrolled position otherwise would not.
  //
  // The heading is found rather than threaded down as a ref: it lives in five components, two
  // of which are rendered by other components in turn, and a ref per screen would be five
  // props and five effects to say one thing. One query on one screen change is not the place
  // to be precious about it.
  //
  // Two transitions are deliberately not announced. The first resolution of a screen is a
  // page load, which the browser has already announced, and the admin console puts the
  // cursor in the password on mount and would not give it back. "Connecting" is the wait
  // *into* a screen rather than a screen, so arriving in a room does not drag focus onto a
  // heading that was hidden a moment ago — which also keeps the skip link the first thing a
  // Tab reaches, instead of the second.
  const seen = useRef<string | null>(null)
  useEffect(() => {
    const from = seen.current
    seen.current = view.screen
    if (
      from === null ||
      from === view.screen ||
      from === 'connecting' ||
      view.screen === 'connecting'
    )
      return
    const h = document.querySelector<HTMLElement>('.screen h1')
    if (h) h.focus()
  }, [view.screen])

  useEffect(() => {
    const on = () => setRoute(route())
    addEventListener('popstate', on)
    return () => removeEventListener('popstate', on)
  }, [])

  const go = (next: string) => {
    // pushState, not a bare `location =` assignment: that would hit the server, so every
    // screen change would become a full reload.
    history.pushState(null, '', next)
    setRoute(route())
  }

  // No navigation callback: the console never leaves /admin. Every exit is a state reset in
  // Admin, which is why nothing here has to hand it a router.
  //
  // One switch on the screen resolved above, rather than a ladder re-testing the same five
  // conditions: two hand-maintained lists of the same question could disagree about which screen
  // a player was looking at, and only an assertion that they agreed was checking. Each case is
  // exactly the return it replaced, in the same order, with the same components and the same
  // props; `code` and `state` are carried by `View`, so nothing here asserts them.
  switch (view.screen) {
    case 'admin':
      return (
        <>
          <SkipLink />
          <Admin />
        </>
      )
    case 'join':
      return (
        <>
          <SkipLink />
          <Join onJoined={(c) => go(`/room/${c}`)} />
        </>
      )
    // No token means this phone is not, or was never, in the room. The join form comes back
    // with the code already filled in, which also covers someone following a shared link
    // before they have joined, at no cost.
    case 'gate':
      return (
        <>
          <SkipLink />
          <Gate code={view.code} onJoined={(c) => go(`/room/${c}`)} />
        </>
      )
    case 'fault':
      return (
        <>
          <SkipLink />
          <Fault code={view.code} onRetry={retry} onLeave={() => go('/')} />
        </>
      )
    case 'connecting':
      return (
        <>
          <SkipLink />
          <Connecting code={view.code} link={link} />
        </>
      )
    case 'lobby':
      return (
        <>
          <SkipLink />
          <Lobby state={view.state} link={link} onRetry={retry} onLeave={() => go('/')} />
        </>
      )
    case 'results':
      return (
        <>
          <SkipLink />
          <Results state={view.state} link={link} onRetry={retry} onLeave={() => go('/')} />
        </>
      )
    // Both of these are gated screens the phase switch opens and closes. The eight questions and
    // the nine prompts are short, deliberate and single-purpose: a composer half-written on the
    // play screen has to be somewhere to go while the didacticiel runs, and unmounting this
    // screen is where its local question index and the note in flight go.
    case 'quiz':
      return (
        <>
          <SkipLink />
          <Quiz state={view.state} code={view.code} link={link} onRetry={retry} />
        </>
      )
    case 'rating':
      return (
        <>
          <SkipLink />
          <Rating state={view.state} code={view.code} link={link} onRetry={retry} />
        </>
      )
    case 'game':
      return (
        <>
          <SkipLink />
          <Game
            state={view.state}
            code={view.code}
            link={link}
            onRetry={retry}
            answer={answer}
            sendPrompt={sendPrompt}
            wsLive={wsLive}
          />
        </>
      )
  }
}

// The stream is open but no frame has arrived yet. This is the only state that is genuinely a
// wait, and it is bounded: the hook gives up after three misses and routes to `Fault`.
// Deliberately almost empty — the rail already carries the lamp and the word, and a third
// copy of "connecting" is the one thing a screen this short does not need.
function Connecting({ code, link }: { code: string; link: Link }) {
  return (
    <div className="deck">
      <ConsoleBar meta={`Partie ${code}`} link={link} />
      <main className="screen screen--solo" id="main" tabIndex={-1}>
        <p className="prose prose--dim">Lecture du tableau…</p>
      </main>
    </div>
  )
}
