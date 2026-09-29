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
    // <output> carries an implicit role=status, so this is a polite live region: the lamp
    // change is announced without interrupting whatever the player is doing.
    <output className={`status ${LINK_CLASS[link]}`}>
      <span className={`lamp lamp--${link}`} aria-hidden="true" />
      {linkWord(link)}
      {/* A manual retry only where nothing is coming on its own. Offering it for a blip the
          watchdog is already fixing would train a player to tap a button that does not need
          tapping. */}
      {fault && onRetry && (
        <>
          {' '}
          <button type="button" className="status__retry" onClick={onRetry}>
            Réessayer
          </button>
        </>
      )}
    </output>
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

// The illuminated panel. It is the loudest thing on the page, and it is used in exactly three
// places, all of which are the same job: show the room something it must read at a glance or
// read out loud. The target, and the code, twice.
export function Readout({ label, value, code }: { label: string; value: string; code?: boolean }) {
  return (
    <div className="readout">
      <span className="readout__label">{label}</span>
      <p className={`readout__value${code ? ' readout__value--code' : ''}`}>{value}</p>
    </div>
  )
}

export function ScoreScale({ score }: { score: number }) {
  const lit = Math.max(0, Math.min(10, Math.round(score)))
  // Hidden for the same reason as the meter: the score is written out in full beside it, and
  // this exists to show how big that number is, which is a thing words cannot do.
  return (
    <div className="scale" aria-hidden="true">
      {Array.from({ length: 10 }, (_, i) => (
        <span
          key={i}
          className={`scale__tick${i < lit ? ' scale__tick--on' : ''}`}
          style={{ '--i': i } as CSSProperties}
        />
      ))}
    </div>
  )
}

// Rank alone tells you nothing when you are fifth of seven. The bar is drawn against the
// leader, so the row answers "how far off the top am I" and the leading row is the only one
// at full sodium.
export function Board({ players, myName }: { players: Player[]; myName: string }) {
  const top = Math.max(...players.map((p) => p.score), 0)
  return (
    <div className="board">
      {players.map((p, i) => (
        <div className="board__row" key={p.name}>
          <span className="board__rank">{i + 1}</span>
          <span className={p.name === myName ? 'board__name board__name--me' : 'board__name'}>
            {p.name}
            {p.name === myName && <span className="board__tag">toi</span>}
          </span>
          <span className="board__pts num">{p.score}</span>
          <span className="board__bar" aria-hidden="true">
            <span
              className={`board__fill${i === 0 ? ' board__fill--top' : ''}`}
              style={{ width: `${top > 0 ? (p.score / top) * 100 : 100}%` }}
            />
          </span>
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
// All the state stays with the caller, which is the only thing that knows what "armed" means
// here: an arming is only valid for the exact room state it was armed against.
export function DangerButton({
  label,
  armedLabel,
  confirming,
  disabled,
  onArm,
  onConfirm,
}: {
  label: string
  armedLabel: string
  confirming: boolean
  disabled?: boolean
  onArm: () => void
  onConfirm: () => void
}) {
  return (
    <div className="guard">
      <button
        type="button"
        className={`btn btn--wide${confirming ? ' btn--danger' : ' btn--ghost'}`}
        disabled={disabled}
        onClick={() => (confirming ? onConfirm() : onArm())}
      >
        {confirming ? armedLabel : label}
      </button>
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
