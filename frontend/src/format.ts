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
