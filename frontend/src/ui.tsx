import type { CSSProperties, ReactNode } from 'react'
import { linkWord, type Link } from './useRoom'
import type { Player } from './api'

/* The instrument panel. Every screen is built from these, which is what makes six screens
 * read as one device: the same lamp, the same track, the same meter, the same board. */

// How a link is coloured. 'down' is a phone that slept, changed network or walked out of
// range: the stream reopens by itself within seconds, so it is stated in the calm cool colour
// and never dressed as a fault. Only a room that genuinely is not there gets the red.
const LINK_CLASS: Record<Link, string> = {
  idle: '',
  connecting: 'status--pending',
  live: '',
  down: 'status--pending',
  failed: 'status--fault',
}

export function StatusLamp({ link, onRetry }: { link: Link; onRetry?: () => void }) {
  if (link === 'idle') return null
  const fault = link === 'failed'
  return (
    // Two elements, not one. The layout box stays a plain span so `.status` keeps owning the
    // flex, the right-hand margin and the colour, and the live region shrinks to the two
    // things that are actually a status: the lamp and the word.
    //
    // The button used to live inside the <output>, which is an anti-pattern rather than a
    // style question: a live region announces its whole subtree when it changes, so a
    // focusable control inside it got re-announced as part of the connection message on every
    // flap, and the announcement described something the player cannot do. As a sibling it
    // inherits the fault colour and the rail's type through the wrapper, unchanged.
    <span className={`status ${LINK_CLASS[link]}`}>
      {/* <output> carries an implicit role=status, so this is a polite live region: the lamp
          change is announced without interrupting whatever the player is doing. */}
      <output>
        <span className={`lamp lamp--${link}`} aria-hidden="true" /> {linkWord(link)}
      </output>
      {/* A manual retry only where nothing is coming on its own. Offering it for a blip the
          watchdog is already fixing would train a player to tap a button that does not need
          tapping. */}
      {fault && onRetry && (
        <button type="button" className="status__retry" onClick={onRetry}>
          Réessayer
        </button>
      )}
    </span>
  )
}

export function ConsoleBar({
  meta,
  link,
  onRetry,
}: {
  meta?: string
  link?: Link
  onRetry?: () => void
}) {
  return (
    <header className="railbar">
      <span className="railbar__mark">Jeu GenAi</span>
      {meta && <span className="railbar__meta">{meta}</span>}
      {link && <StatusLamp link={link} onRetry={onRetry} />}
    </header>
  )
}

// Rounds are the one thing in this game that is genuinely a sequence, so they are the one
// thing that gets a numbered track. The written "Manche 3 sur 10" stays in the accessibility
// tree and the ticks are decoration on top of it, not the only way to read the round.
//
// The tick's key is its own position, which is the whole identity a segment of a fixed track
// has: it never moves, and it is not the same object twice.
export function RoundTrack({ current, total }: { current: number; total: number }) {
  return (
    <div className="track" aria-hidden="true">
      {Array.from({ length: total }, (_, i) => (
        <span
          key={i}
          className={`track__tick${i < current ? ' track__tick--done' : i === current ? ' track__tick--now' : ''}`}
        />
      ))}
    </div>
  )
}

export function Step({ current, total }: { current: number; total: number }) {
  return (
    <div className="stepwrap">
      <p className="step">
        <span className="step__n">Manche {current + 1}</span>
        <span className="step__of">sur {total}</span>
      </p>
      <RoundTrack current={current} total={total} />
    </div>
  )
}

// The lamps, shared by every thing that shows them. `lit` holds the indices that are lit;
// `mine` is the caller's own index, which is the one lamp that burns sodium: the accent means
// "yours" everywhere it appears, so "one of these is me" comes free.
//
// A fixed-length row of segments whose key is its own position, which is the whole identity a
// segment has: it never moves, and it is not the same object twice. They are hidden from
// assistive tech because the number beside them always says the same thing in words.
function Lamps({
  lit,
  total,
  mine,
}: {
  lit: number[]
  total: number
  mine: number
}) {
  return (
    <div className="meter" aria-hidden="true">
      {Array.from({ length: total }, (_, i) => (
        <span
          key={i}
          className={`meter__seg${i === mine ? ' meter__seg--me' : lit.includes(i) ? ' meter__seg--on' : ''}`}
        />
      ))}
    </div>
  )
}

// How many people are actually here, out of how many are in the game. The number on screen is
// the *online* one, because the question a player asks on arrival is "is everyone here" and
// not "does the roster contain everyone who has ever joined". The roster total sits under it
// rather than beside it, so the honest reading — five of seven, two phones asleep — is the
// one you get by default, and neither number can be mistaken for the other.
//
// The lamps under it are one per person in the game, lit for the ones on the stream. An unlit
// lamp is a player, not an empty slot: the row is exactly as long as the roster, so a gap is
// always somebody.
export function Roster({ players, online, myName }: { players: Player[]; online: number; myName: string }) {
  const lit = players.flatMap((p, i) => (p.online ? [i] : []))
  const mine = players.findIndex((p) => p.online && p.name === myName)
  return (
    <div>
      {/* The roster moves on every arrival, every departure and every reconnect, all of it
          pushed by SSE and none of it in response to anything the player did — so the eye has
          nothing to catch it on and a screen reader has nothing at all. The echo says the same
          sentence the panel says, in the panel's own terms. `polite`, because a player who is
          mid-word does not want a reconnect to talk over them. */}
      <p className="visually-hidden" aria-live="polite">
        {online} {online > 1 ? 'joueurs connectés' : 'joueur connecté'} sur {players.length} dans
        la pièce.
      </p>
      <p className="roster">
        <span className="roster__n num">{online}</span>
        <span className="roster__unit">
          {online > 1 ? 'joueurs connectés' : 'joueur connecté'}
        </span>
      </p>
      <p className="roster__total">sur {players.length} dans la pièce</p>
      <div className="roster__meter">
        <Lamps lit={lit} total={players.length} mine={mine} />
      </div>
    </div>
  )
}

// The named roster, for the one screen a laptop looks at. The host has a decision to make —
// force the round on, or wait — and it turns on knowing *who* is unreachable, not how many.
export function RosterList({ players, myName }: { players: Player[]; myName: string }) {
  return (
    <div className="rosterlist">
      {players.map((p) => (
        <div className="rosterlist__row" key={p.name}>
          <span className={`lamp lamp--${p.online ? 'on' : 'off'}`} aria-hidden="true" />
          <span className="rosterlist__name">
            {p.name}
            {p.name === myName && <span className="board__tag">toi</span>}
          </span>
          {/* Said in words as well as shown by an unlit lamp: colour alone would leave this
              invisible to a player who cannot separate the two hues. */}
          {!p.online && <span className="rosterlist__away">hors ligne</span>}
        </div>
      ))}
    </div>
  )
}

// One lamp per *present* player, for the question asked during a round: who has sent theirs
// in yet. On a phone this is the only feedback loop a waiting player has, so it is live, and
// it is the one thing on the screen that moves.
//
// The count deliberately excludes players who are not on the stream. A segment for someone
// whose phone is asleep can never fill, and a progress indicator that cannot reach the end
// stops being information and becomes a fault report — the player would sit there reading
// "4 sur 5" and believe the round was about to move. Callers report the excluded players
// separately, in words, because the round genuinely is still blocked on them.
export function AnswerMeter({
  lit,
  total,
  mine,
  caption,
}: {
  lit: number[]
  total: number
  mine: number
  caption: string
}) {
  if (total === 0) return null
  return (
    <div>
      {/* The meter is the one thing on the screen that moves during a round, and it moves
          without the player having done anything: the round closing is an event, and an event
          that is only a set of lamps is invisible to anyone not looking at them. The caption
          already carries the meaning in words — it is the same sentence the meter is drawn
          from — so the echo repeats it and adds the fraction, which is the part the lamps
          cannot state. */}
      <p className="visually-hidden" aria-live="polite">
        {caption}. {lit.length} sur {total}.
      </p>
      <p className="meterrow">
        <span className="label">{caption}</span>
        <span className="meterrow__count">
          {lit.length} sur {total}
        </span>
      </p>
      <Lamps lit={lit} total={total} mine={mine} />
    </div>
  )
}

// Three typographic tiers for one panel, chosen by the length of the target.
//
// What varies is not a font size, it is the *kind* of thing being read. A four-character
// answer is a label to be illuminated across a room; a recipe is a specification to be read
// closely. Sizing one like the other is what breaks a panel — a paragraph set at display size
// is not typography, it is a wall, and it pushes the composer off the phone. So the panel's
// chassis never changes and only the letterform steps down to meet the content:
//
//   word    a target the room could shout across a room. Display face, full size, the panel
//           at its thumb-high best. Unchanged from the phone-first build.
//   phrase  a title, a riddle's first line. Still the display face, still readable at
//           distance, one step down so a 90-character line does not eat the screen.
//   text    several sentences, and the tier that decides whether this panel works at all.
//           Big Shoulders is about 30% narrower than Barlow at the same size, so the
//           paragraph can stay in the display face, be a quarter larger than the body text
//           around it, and still take six lines where it took seven — a tier step for free
//           rather than a cost. It also keeps the whole panel in one letterform, which is
//           what "one instrument" means when the face itself never has to change.
//           The trade is that it is one step quieter than a big display face and no louder
//           than the composer, so the tier also grows a 2px sodium rule on the panel: size
//           and family are the part of the hierarchy that survives being compared with the
//           textarea underneath, and the rule is the part that survives being looked at.
//           The measure is what stops the condensed face running the full width of the panel
//           — 26rem on a phone, 44rem on a desktop, so lines stay sentences.
//
// The chassis — border, gradient, bevels, scanlines, glow, label — is identical in all three
// tiers, which is what keeps this one panel and not three.
//
// The thresholds are where the content changes kind, not round numbers. The room code is
// four characters, so it is always `word`.
const TIERS = [
  { max: 42, size: 'word' },
  { max: 120, size: 'phrase' },
  { max: Number.POSITIVE_INFINITY, size: 'text' },
]

export function Readout({ label, value, code }: { label: string; value: string; code?: boolean }) {
  const size = TIERS.find((t) => value.length <= t.max)!.size
  return (
    // The tier rides on the panel, not on the text, so every rule below can be written as
    // `.readout[data-size=…] .readout__value` and win on specificity rather than on
    // whichever order the stylesheet happens to be in.
    <div className="readout" data-size={size}>
      <span className="readout__label">{label}</span>
      <p className={`readout__value${code ? ' readout__value--code' : ''}`}>{value}</p>
    </div>
  )
}

// A range, because the console draws two of these. The score scale is 0-10 and has been since
// the Game screen; the cross-rating scale is 1-10, and a ten-tick bar that started one notch in
// would say "you have scored nothing" before a single note had been given. Defaults keep every
// existing call site on the 0-10 reading without touching it, which is the whole argument for
// extending this component instead of writing a second scale beside it: one instrument, two
// calibrations, and the two cannot drift apart visually because there is only one set of rules.
export function ScoreScale({ score, min = 0, max = 10 }: { score: number; min?: number; max?: number }) {
  const lit = Math.max(min, Math.min(max, Math.round(score)))
  // Hidden for the same reason as the meter: the score is written out in full beside it, and
  // this exists to show how big that number is, which is a thing words cannot do.
  return (
    <div className="scale" aria-hidden="true">
      {Array.from({ length: max - min }, (_, i) => (
        <span
          key={i}
          className={`scale__tick${min + i < lit ? ' scale__tick--on' : ''}`}
          style={{ '--i': i } as CSSProperties}
        />
      ))}
    </div>
  )
}

// The measured columns the final standings are specified as — proximity and quality alongside
// the total — carried as functions of the player rather than as a fixed list, so a caller that
// has one number does not have to invent the other, and a caller with neither keeps the two
// column board every other screen in the console already draws.
export type BoardColumn = { label: string; value: (p: Player) => number | null }

// A null score is not a zero and it must never render as a blank. The server blanks every score
// during the rating phase on purpose, and a cell that draws nothing in a column of numbers reads
// as a missing value, which is the inference the phase exists to prevent — so the cell says "—".
function Dash({ value }: { value: number | null }) {
  return <span className="board__pts num">{value ?? '—'}</span>
}

// Rank alone tells you nothing when you are fifth of seven. The bar is drawn against the
// leader, so the row answers "how far off the top am I" and the leading row is the only one
// at full sodium.
//
// The bar goes away when there are measured columns, and that is not a loss of information: it
// was a single comparison against the leader drawn as a length, and three columns of digits are
// that same comparison stated exactly, for each dimension separately. One bar across a row of
// three numbers would only ever be about the total, and would invite reading it as if it were
// about all three.
export function Board({
  players,
  myName,
  columns = [],
}: {
  players: Player[]
  myName: string
  columns?: BoardColumn[]
}) {
  const top = Math.max(...players.map((p) => p.score ?? 0), 0)
  return (
    <div
      className={`board${columns.length ? ' board--cols' : ''}`}
      style={
        columns.length
          ? { gridTemplateColumns: `1.4rem minmax(0, 1fr) repeat(${columns.length + 1}, auto)` }
          : undefined
      }
    >
      {/* A header row, and only when there is more than one number to name. With a single
          column there is nothing to disambiguate: the only number in a row has been the total
          since the Game screen, and a heading over it would say nothing a reader did not
          already know. */}
      {columns.length > 0 && (
        <div className="board__head">
          <span />
          <span className="board__headname">Joueur</span>
          {columns.map((c) => (
            <span key={c.label}>{c.label}</span>
          ))}
          <span>Total</span>
        </div>
      )}
      {players.map((p, i) => (
        <div className="board__row" key={p.name}>
          <span className="board__rank">{i + 1}</span>
          <span className={p.name === myName ? 'board__name board__name--me' : 'board__name'}>
            {p.name}
            {p.name === myName && <span className="board__tag">toi</span>}
          </span>
          {columns.map((c) => (
            <span className="board__cell" key={c.label}>
              {c.value(p) ?? '—'}
            </span>
          ))}
          <Dash value={p.score} />
          {!columns.length && (
            <span className="board__bar" aria-hidden="true">
              <span
                className={`board__fill${i === 0 ? ' board__fill--top' : ''}`}
                style={{ width: `${top > 0 ? ((p.score ?? 0) / top) * 100 : 100}%` }}
              />
            </span>
          )}
        </div>
      ))}
    </div>
  )
}

// The one destructive control in the console. A rule above it and real space below the primary
// action, so a thumb reaching for "next round" in a room where someone is shouting cannot land
// on "erase the game in progress". Quiet until it is armed, then the fault colour and a label
// that says what pressing it will do — two taps, because none of these can be taken back.
//
// The armed state is either one confirm or a set of choices, never both, and the type says so: a
// component that took both would let a call site pass a confirm that nothing can ever reach, and
// a destructive control whose fallback is unreachable is a control nobody can audit. A restart is
// the case that needs the choice — "with the didacticiel" or "without" — and it gets one arming
// and a second tap rather than two armed buttons a thumb picks between in a noisy room.
//
// All the state stays with the caller, which is the only thing that knows what "armed" means
// here: an arming is only valid for the exact room state it was armed against.
type DangerProps = {
  label: string
  armedLabel: string
  confirming: boolean
  disabled?: boolean
  onArm: () => void
} & (
  | { choices: ReactNode; onConfirm?: never }
  | { choices?: never; onConfirm: () => void }
)

export function DangerButton(props: DangerProps) {
  const { label, armedLabel, confirming, disabled, onArm } = props
  const confirm = props.onConfirm
  return (
    <div className="guard">
      {confirming && props.choices ? (
        props.choices
      ) : (
        <button
          type="button"
          className={`btn btn--wide${confirming ? ' btn--danger' : ' btn--ghost'}`}
          disabled={disabled}
          onClick={() => (confirming && confirm ? confirm() : onArm())}
        >
          {confirming ? armedLabel : label}
        </button>
      )}
    </div>
  )
}

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rule">
      <h2 className="label">{title}</h2>
      {children}
    </section>
  )
}
