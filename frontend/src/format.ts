/* How a number reaches the screen. Its own module and not part of `ui.tsx` because that file is
 * all components, and a module that exports both components and functions breaks React Fast
 * Refresh: editing one of them reloads the page instead of swapping the component, which in a
 * console someone is using to run a live classroom is the difference between a two-second round
 * trip and losing the room. */

// ONE WAY TO PRINT A SCORE, because the server stopped rounding. §7's note_tour is a real
// division — (proximité + qualité) / 2 — so a round of 7 and 6 settles on 6.5 and a sum over
// ten rounds is a sum of halves. Two things then have to be true at once on a screen written in
// French: a fraction reads with a comma, and a whole number reads as a whole number. "6.5" is
// an Americanism in a room in Lyon, and "6,0" is a lie about a precision the arithmetic does not
// have — a class checking the bound printed under the summary would be checking it against a
// number claiming to be more exact than it is, and "why is it 6,0" is a question with no good
// answer.
//
// `Intl` gives both for free, and `maximumFractionDigits` with no `minimum` is exactly the
// "no trailing ,0" rule: a ceiling on the fraction, never a floor on it. Two digits rather than
// one because the total is a sum of halves — ten rounds of x,5 land on a .0 and never need the
// second, but a screen that rounds a real number for the sake of a tidy column is the same lie
// in the other direction.
//
// Constructed once at module scope because a formatter is a lookup table, and building one per
// cell of a nine-row summary on every frame is work the browser has no reason to do twice.
//
// The null case is HERE rather than at each call site, because it is one decision the console
// already makes everywhere else: an absent score is a dash and never a zero. `Intl` prints null
// as "0" without complaint, and a zero in a column of scores is a score nobody earned — the
// exact reading the em-dash exists to refuse. One function, one rule, no call site able to
// forget it.
const FR = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 2 })

export const fr = (n: number | null): string => (n === null ? '—' : FR.format(n))

// A count with its noun attached, because "3 joueur" is the kind of thing a screen is believed
// rather than read.
//
// It lives here and not in any one screen because the rule was being written out again at every
// site that shows a count: the run-of-show says five of these in six sentences, the roster panel
// says it twice, a meter caption and a countdown say it once each, and the final verdict says it
// twice more. One rule about French plurals, in one file, next to the formatter that owns the
// other half of how a number reaches the screen.
export const noun = (n: number, one: string, many: string) => `${n} ${n > 1 ? many : one}`

// The same rule without the count, for the two sentences that already own the count in another
// shape: one draws it in a sibling span because it is set as a figure, and one has already printed
// it through `fr`. Neither can hand the count to `noun` — one would print the number twice, the
// other would print it in the American notation `fr` exists to keep off the screen.
export const nounOf = (n: number, one: string, many: string) => (n > 1 ? many : one)

// The shared countdown, in whole seconds.
//
// Rounded UP, and that is the only decision in it. `Math.floor` would show 0 for the last whole
// second of a thirty-second window and then sit on 0 for another second, so the room reads "0"
// while the server is still accepting answers — a clock that lies about the deadline on the one
// question where the deadline is the whole point. Ceil gives 1 until the last millisecond is gone
// and then stops, because `open` is false by then and nothing draws the number at all.
//
// Never negative either: it is a figure for a phone in front of a class, and "-3" is not a state
// any of them is in. The negative side of the comparison belongs to `useQuizWindow`, which owns
// the clock; this only formats what is left to spend.
export const countdown = (ms: number): number => Math.max(0, Math.ceil(ms / 1000))
