import type { CSSProperties } from 'react'
import { Avatar, Board, Confetti, ConsoleBar, Section } from './ui'
import { fr, nounOf } from './format'
import { getName, type State } from './api'
import type { Link } from './useRoom'

export default function Results({
  state,
  link,
  onRetry,
  onLeave,
}: {
  state: State
  link: Link
  onRetry: () => void
  onLeave: () => void
}) {
  const [first] = state.players
  // A tie is a tie: the server's sort picks a display order (join order among equals), which
  // is not a reason to hand one of the tied players a trophy the other did not lose.
  const winners = first === undefined ? [] : state.players.filter((p) => p.score === first.score)
  // `score` is null outside a resolved round, and the last screen of a game can therefore find a
  // player with no total at all — a player who never reached a scored round. It is read as zero
  // and the winner's sentence says nothing about points when there is nothing to count, rather
  // than announcing "l'emporte avec null".
  const points = first?.score ?? 0

  // The winner is a sentence, not a slab. The illuminated panel is spent on the target and
  // the room code, and it is worth more for that: a player who has just seen it ten times
  // stops seeing it. Here the board itself carries the result, with the leading row in
  // sodium, so nothing extra has to be invented to announce it.
  //
  // The count is printed by `fr` and the noun asked of `nounOf`, because these are the two
  // sentences that already own the count in another shape.
  const point = nounOf(points, 'point', 'points')
  const verdict =
    points === 0
      ? 'Aucune manche n’a été notée dans cette partie.'
      : winners.length === 1
        ? `${winners[0].name} l’emporte avec ${fr(points)} ${point}.`
        : `Égalité entre ${winners.map((p) => p.name).join(', ')}, avec ${fr(points)} ${point}.`

  return (
    <div className="deck">
      <ConsoleBar meta={`Partie ${state.code}`} link={link} onRetry={onRetry} />
      {/* The skip link's target — see the note on `SkipLink` in App.tsx. */}
      <main className="screen screen--split" id="main" tabIndex={-1}>
        <div className="finale">
          {winners.length > 0 && points > 0 && <Confetti pieces={110} />}
          {/* `tabIndex` so the route-change effect in App.tsx can land focus here. This is
              the last screen of a game, so it is the one place a round ending swaps the whole
              view out from under a keyboard user; without a focusable target the swap is
              silent, and focus is left on a control that is no longer in the document. Every
              other screen in the console already carries this on its heading. */}
          <h1 className="title" tabIndex={-1}>
            Partie terminée
          </h1>
          <p className="prose finale__lead">{winners.length ? verdict : 'Aucun joueur dans la partie.'}</p>
          {/* The podium: the top three, second-first-third, the way a podium stands. Decorative —
              the sentence above and the board below carry the same result in words. Places come
              from equal scores, so a tie shares a step instead of being split by join order. */}
          {points > 0 && (
            <ol className="podium" aria-hidden="true">
              {[1, 0, 2].map((i) => {
                const p = state.players[i]
                if (!p) return <li key={i} className="podium__slot podium__slot--empty" />
                const place = state.players.findIndex((q) => q.score === p.score) + 1
                return (
                  <li key={p.name} className="podium__slot" data-place={place} style={{ '--i': i } as CSSProperties}>
                    {place === 1 && <span className="podium__crown">👑</span>}
                    <Avatar name={p.name} size="lg" />
                    <span className="podium__name">{p.name}</span>
                    <span className="podium__pts num">{fr(p.score)}</span>
                    <span className="podium__step">
                      <span className="podium__place num">{place}</span>
                    </span>
                  </li>
                )
              })}
            </ol>
          )}
        </div>

        {/* The winner is a sentence and the standings are the evidence, so on a wide screen
            they sit side by side with the board given a column of its own to stretch into. */}
        <div className="railcol">
          <Section title="Classement final">
            <Board players={state.players} myName={getName(state.code)} />
          </Section>
        </div>

        <div className="foot">
          <span className="foot__code">Partie {state.code}</span>
          <button type="button" className="btn btn--ghost foot__end" onClick={onLeave}>
            Retour à l’accueil
          </button>
        </div>
      </main>
    </div>
  )
}
