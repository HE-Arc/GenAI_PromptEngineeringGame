import { useId, useState, type CSSProperties, type ReactNode } from 'react'
import { fr, noun, nounOf } from './format'
import { linkWord, type Link } from './useRoom'
import { SCALE_MAX, type Player, type ReadyState } from './api'

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
        {noun(online, 'joueur connecté', 'joueurs connectés')} sur {players.length} dans la pièce.
      </p>
      <p className="roster">
        <span className="roster__n num">{online}</span>
        {/* The count is the sibling above, set as a figure, so this is the noun alone: the same
            rule `noun` applies, asked for the word rather than the pair. */}
        <span className="roster__unit">
          {nounOf(online, 'joueur connecté', 'joueurs connectés')}
        </span>
      </p>
      <p className="roster__total">sur {players.length} dans la pièce</p>
      <div className="roster__meter">
        <Lamps lit={lit} total={players.length} mine={mine} />
      </div>
    </div>
  )
}

// A player's name, plus the "toi" badge when the name is the reader's own. It is one component
// because the badge is the same mark everywhere it appears — the accent means "yours" across the
// whole console — and a copy of it is a place where that meaning can drift.
//
// `className` is passed rather than derived: the roster list sets the name in a row of its own
// and never flags it, while the board, the tally and the didacticiel standings all set the same
// base class and add the `--me` modifier. Those are different rules about the same box, so the
// box stays the caller's and only the badge is shared.
export function PlayerName({
  name,
  isMe,
  className,
}: {
  name: string
  isMe: boolean
  className: string
}) {
  return (
    <span className={className}>
      {name}
      {isMe && <span className="board__tag">toi</span>}
    </span>
  )
}

// The named roster, for the one screen a laptop looks at. The host has a decision to make —
// force the round on, or wait — and it turns on knowing *who* is unreachable, not how many.
export function RosterList({ players, myName }: { players: Player[]; myName: string }) {
  // Whether the figures have earned a column, asked of the SNAPSHOT rather than of the phase,
  // and that is the whole argument for it.
  //
  // `score` is `settled + open_round` summed over a player's answers, so in `waiting` and in
  // `quiz` it is a hard 0 for everybody — not "no score yet", which is the null, but a real
  // zero produced by the arithmetic over an empty set. A column of those is twenty identical
  // figures: it says nothing the host did not already know, and it spends the console's figure
  // face and the reader's attention on a comparison that cannot exist yet. The obvious-looking
  // implementation is one line that always draws the column, and it prints twenty zeros on the
  // one screen in the game where the host's next move is a guess about who is listening.
  //
  // Once a single figure differs, the column earns its place and the zeros IN it become
  // meaning rather than noise: a player who answered and earned nothing is genuinely 0, and
  // the host needs to see that against a leader rather than infer it from a missing digit. So
  // the test is "is there anything here to compare", not "has the game started" — the two are
  // not the same question, and the snapshot is the only one this component can be given:
  // `Admin` calls this in three phases with `players` and nothing else, and adding a prop it
  // does not pass would only move the decision somewhere it cannot be trusted.
  //
  // The rating phase is answered by the same rule rather than by a special case. The server
  // blanks every score there at once (`views.py`: `shown = None if hiding else total`), so
  // there is nothing to compare and no column — which is the console's other answer for that
  // phase, and the reason `Admin` draws a named roster and no standings while it lasts.
  const scored = players.some((p) => p.score !== null && p.score !== 0)
  return (
    <div className="rosterlist">
      {players.map((p) => (
        <div className="rosterlist__row" key={p.name}>
          <span className={`lamp lamp--${p.online ? 'on' : 'off'}`} aria-hidden="true" />
          <PlayerName name={p.name} isMe={p.name === myName} className="rosterlist__name" />
          {/* The ranking total, after the name: the only number in this row, printed by `fr`.
              That is the whole null story here, and it is worth being exact about it — the dash
              `fr` prints for a null is NOT reached in this component today, because the
              all-blank case is caught one level up and the column is simply not drawn, the same
              answer the console gives the rest of the rating phase. It still goes through `fr`
              rather than through `p.score ?? 0` so that the guarantee is structural rather than
              editorial: a roster row can only ever print what every other board in the console
              prints, and the one-line version that looks obviously right — `?? 0` — is the line
              that hands the ranking a zero nobody earned, which is the exact inference the
              rating phase exists to prevent. Re-derive `scored` later and this stays true for
              free; the moment somebody writes a fallback, it stops being true everywhere.
              It is `score` and never `quiz_score`: the didacticiel's figure is deliberately kept
              out of the ranking, and it is already drawn where it belongs, under its own heading,
              with its own sentence saying it does not count. A roster carrying both would be a
              second surface making the one distinction this game refuses to blur. */}
          {scored && <span className="rosterlist__score num">{fr(p.score)}</span>}
          {/* Said in words as well as shown by an unlit lamp: colour alone would leave this
              invisible to a player who cannot separate the two hues. It sits in its own track
              rather than pushed to the edge by a margin, because a row that ends with the word
              and a row that ends with the figure would otherwise draw the figures at two
              different x positions. */}
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
export function ScoreScale({ score, min = 0, max = SCALE_MAX }: { score: number; min?: number; max?: number }) {
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
// `fr` owns that dash, so no caller here can forget it and print a zero instead.
function Dash({ value }: { value: number | null }) {
  return <span className="board__pts num">{fr(value)}</span>
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
          <PlayerName
            name={p.name}
            isMe={p.name === myName}
            className={p.name === myName ? 'board__name board__name--me' : 'board__name'}
          />
          {/* The dimension columns, through the same formatter as the total. `proximity_total`
              and `quality_total` are sums over settled rounds, and a round is a mean of two
              out-of-ten numbers, so both are as fractional as the rounds that made them. */}
          {columns.map((c) => (
            <span className="board__cell" key={c.label}>
              {fr(c.value(p))}
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

// The class's own bar on a discussion: one instruction, one wide button, the count under it. ONE
// component for the four screens that discuss, and the reason is not tidiness — the rule, the
// wording and the three disabled states are the same in all four, and four copies are four places
// for « Prêt » and the sentence under it to drift apart while the server keeps one rule.
//
// It renders itself away when there is no discussion rather than being handed a flag: `ready` is
// null outside a step, so the screen that mounts this never has to know the phase it is in.

// WHAT THE CLASS IS TALKING ABOUT, keyed on the FAMILY of `ready.step` and stored as a TUPLE.
//
// The tuple is the point, and a single string cannot hold it. The imperative is a different KIND of
// thing from the sentence — it is the word that has to arrive from across the room, set heavier and
// larger, while the sentence is prose read at normal pace — so it needs two fields to become two
// elements. One string would mean parsing an emphasis marker out of it at render time and building
// the span from the string, which is the one thing this console does not do with copy. And
// `readonly [string, string]` is checked: a fifth entry cannot come with a missing verb or a
// third field, where `Record<string, string>` would have said nothing.
//
// Keyed on the family — the part before the colon — because the cursor in `ready.step` is an
// index: `quiz:3` must read as the didacticiel whatever question it is, and a key per index would
// be eight sentences of which four can never be right.
//
// Derived rather than passed in, because `current_step` already names the discussion in the payload
// the bar already reads: the wording costs no prop, so a fifth screen cannot forget it.
const DISCUSSION: Record<string, readonly [string, string]> = {
  // The class has arrived and is deciding to start, so the thing to settle out loud is presence,
  // not the game — the roster above the bar is the same fact drawn, and the bar is where the
  // decision is made.
  lobby: ['Dites', 'à voix haute si tout le monde est là.'],
  // The one screen where the discussion is not « what did you think » but « why ». The explanation
  // is on screen precisely so a student can argue it to a neighbour, and a bar that said
  // « comparez vos idées » here would spend the strongest moment of the didacticiel on the
  // vaguest possible prompt.
  quiz: ['Expliquez', 'à un voisin pourquoi c’est la bonne réponse.'],
  // PRESENT tense, « ce que vous pensez », and it is not a style choice: the bar is up from the
  // moment the prompt appears, so a « ce que vous avez pensé » asserts a judgement this player has
  // not made yet, and a « la note que vous avez donnée » asserts one they may be structurally
  // barred from making — `Rating` swaps the scale for `.ownprompt` on the prompt's author, and
  // anonymous mode bars one prompt per player the same way. An opinion is available to all three
  // populations on every frame the bar is visible; a score is available to one of them.
  rating: ['Dites', 'à voix haute ce que vous pensez de ce prompt.'],
  // The summary is the one step with a number on screen that is still PROVISIONAL, so the round's
  // scores are the thing worth arguing about rather than the prompt that produced them.
  summary: ['Dites', 'à voix haute ce que vous pensez des notes du tour.'],
}
// A real sentence rather than a hole, on the same argument as the server's `_PHASE_ADVANCE`: a
// step family added there must not paint an empty paragraph above the button.
const DEFAULT_DISCUSSION: readonly [string, string] = [
  'Discutez',
  'de ce qui vient d’être montré à l’écran.',
]

export function ReadyBar({ ready, onReady }: { ready: ReadyState | null; onReady: () => Promise<boolean> }) {
  // The POST is the authority on its own outcome and the stream is half a second behind a thumb,
  // so this flag is the window the server's `already ready` lives in: without it, a thumb that
  // taps twice sends two votes, and the second one paints a failure under a vote the server
  // recorded — on a screen that a class is looking at.
  //
  // It IS an optimistic « I have voted » flag now, and the only reason it can be is the contract:
  // it is set before the await, kept when the promise answers true and cleared when it answers
  // false. Under the old `void` callback this was impossible — the three screens report a
  // refusal by writing to their own error line and RESOLVING, so this component could not tell a
  // success from a refusal and so had no way to undo the flag it had set. `ready.mine` is still
  // the durable lock and still arrives with the next frame; this only covers the gap.
  //
  // What it must not become is the defect the designer refused earlier: a vote that only ever
  // clears on a refusal strands a player. `mine` is late by design, but it is not guaranteed —
  // if the stream dies after the vote it may never arrive at all, and a flag keyed to nothing
  // would leave « Prêt » permanently disabled on a button whose only repair is a tap. So the flag
  // holds the STEP it was cast for and is read only against `ready.step`: the first render after
  // the room moves on already sees a flag that does not apply — no frame anywhere paints a stale
  // « Prêt » — and the comparison below drops it, so it cannot answer for a step the room later
  // returns to. `ready.step` going null, which is every phase with no discussion, drops it on the
  // same argument. And the vote itself is not what strands the player there: `useRoom`'s watchdog
  // reopens the stream within seconds and the room pushes the whole snapshot on connect, which
  // carries `mine: true` for the very vote this flag is standing in for.
  //
  // No timer expires it. A vote that succeeded stays voted, and un-setting the flag on a delay
  // would resurrect the exact confusion it exists to close — a live « Je suis prêt » over a vote
  // the server already holds.
  //
  // Local to this control on purpose: the screens' `busy` also disables the four answers, the
  // Noter button and Envoyer, and a vote in flight must not do that.
  const [optimistic, setOptimistic] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // `useId` over a literal: four mounts, one document lifetime, and a hand-written id is an id a
  // second mount would collide with.
  const askId = useId()
  // Dropped by a comparison during render rather than by an effect: React re-runs this component
  // the moment `ready` moves and never paints the stale value, so no frame exists in which the
  // flag is read for a discussion this phone has left — while an effect would put one there. It
  // is above the early return because the flag has to be dropped on the way out of a step as
  // well as on the way into the next one, and `ready` is null outside every discussion.
  const step = ready?.step ?? null
  if (optimistic !== null && optimistic !== step) setOptimistic(null)
  if (!ready) return null
  const there = ready.count >= ready.needed
  // What the label and the colour say, and it is BROADER than `mine` on purpose. Once the class
  // has crossed the bar this player's own tap is refused too, so a button still reading « Je suis
  // prêt » would be inviting a tap the server turns down. `settled` is the situation; the counter
  // underneath says which of the two produced it.
  //
  // Three inputs rather than two, and the third is the flag above — a vote in flight and a vote
  // the stream has not caught up with are the same fact to the player looking at the phone, and
  // the label, the ghost class and `disabled` all read this one line, so all three stay together.
  const settled = ready.mine || there || optimistic === ready.step
  const [verb, sentence] = DISCUSSION[ready.step.split(':')[0]] ?? DEFAULT_DISCUSSION
  return (
    <div className="ready">
      {/* THE INSTRUCTION GOES ABOVE THE BUTTON, and the reason is the order the reader already
          has. `aria-describedby` reads what it points at when the control takes focus, and the eye
          reaches a full-width sodium slab before a line of text under it — so a description placed
          after the button is one every player has already acted before reading, which is the exact
          failure this bar was asked to fix. The count stays below, because the count is about the
          button (« how many of us have pressed it ») and this is about the room.
          `aria-describedby` and NOT a live region, which is the other half of the same decision. A
          live region announces on every change; this sentence changes once per step while the count
          changes several times inside one step, so folding it into the polite region would re-read
          the instruction on every vote or need a guard to stay quiet. The count below is the only
          live region on the bar, and it is quiet until another phone presses — which a player
          cannot otherwise perceive at all until the room jumps. */}
      <p className="ready__ask" id={askId}>
        <b className="ready__verb">{verb}</b> {sentence}
      </p>
      {/* Disabled on all three, and none of them is a mistake: a player who has voted does not
          vote again, a player whose own vote carried the class is not handed a second one to
          press, and a vote still travelling is not sent twice. Ghost is the console's own
          treatment for an action already taken, so it is borrowed here rather than invented.
          The label says the state and never the rule — « the connected phones, halved and rounded
          up » is the server's arithmetic, and a student should not have to do it to know whether
          to wait. */}
      <button
        type="button"
        aria-describedby={askId}
        className={`btn${settled ? ' btn--ghost' : ' btn--signal'} btn--wide`}
        onClick={async () => {
          setBusy(true)
          setOptimistic(ready.step)
          try {
            // A rejection is a refusal too. The contract says the three screens answer false
            // rather than throw, but a flag that outlives a thrown vote is the strand this whole
            // mechanism exists to prevent, so the two are treated as one thing.
            if (!(await onReady().catch(() => false))) setOptimistic(null)
          } finally {
            setBusy(false)
          }
        }}
        disabled={settled || busy}
      >
        {settled ? 'Prêt' : 'Je suis prêt'}
      </button>
      {/* Live, because this is the only thing on the bar that moves without the player doing
          anything: other phones pressing, which a player cannot otherwise perceive at all until
          the room jumps. `polite`, so it never talks over the screen's own live region — and it
          carries the sentence AND the fraction, so the change it announces is the whole of what
          the row above it draws. « La classe passe à la suite. » is the one string here that is
          true while `settled` is true for somebody else's reason, and it stays in this region for
          that reason — a player reads it as the state of the room, not as an invitation that
          outlived the thing it invited.
          The noun goes through `nounOf` like every other count in the console: « 1 sur 2 prêts »
          is wrong French on a phone a teacher is reading from the front of the room, and the
          rule that fixes it already exists in `format.ts`. */}
      <p className="prose prose--sm" aria-live="polite">
        {there
          ? 'La classe passe à la suite.'
          : `${ready.count} sur ${ready.needed} ${nounOf(ready.count, 'prêt', 'prêts')}.`}
      </p>
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
