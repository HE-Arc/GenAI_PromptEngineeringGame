/* Game feel: the small effects that are not components. Its own module for the reason `format.ts`
 * gives — a file exporting both hooks and components breaks Fast Refresh. */

import { useEffect, useState } from 'react'

const reduced = () =>
  typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches

// A short tap on phones that support it (Android). iOS Safari has no vibration API, so this is a
// no-op there rather than an error.
export function buzz(pattern: number | number[] = 12) {
  try {
    navigator.vibrate?.(pattern)
  } catch {
    /* a refused vibration is not worth a fault */
  }
}

// A score that ticks up to its value instead of appearing, so the reveal is a moment. Snaps
// straight to the value under reduced motion.
export function useCountUp(target: number, ms = 700): number {
  const [shown, setShown] = useState(() => (reduced() ? target : 0))
  useEffect(() => {
    if (reduced()) return
    let raf = 0
    const t0 = performance.now()
    const step = (t: number) => {
      const k = Math.min(1, (t - t0) / ms)
      // ease-out cubic: fast start, gentle landing on the number
      setShown(Math.round(target * (1 - (1 - k) ** 3)))
      if (k < 1) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [target, ms])
  // Reduced motion never animates, so the value is the target itself rather than a state copy.
  return reduced() ? target : shown
}

// The length of a server window, which the snapshot does not carry: the largest remaining time
// seen for one deadline is the window's length to within one frame. Adjusted during render, keyed
// on the deadline, so the next question or round starts its own ring.
export function useWindowLength(deadline: number | null, left: number): number {
  const [seen, setSeen] = useState<{ deadline: number | null; max: number }>({ deadline, max: left })
  if (seen.deadline !== deadline) setSeen({ deadline, max: left })
  else if (left > seen.max) setSeen({ deadline, max: left })
  return seen.deadline === deadline ? Math.max(seen.max, left) : left
}

// Deterministic hue per player name, so a player keeps the same colour on every phone.
export function hueOf(name: string): number {
  let h = 0
  for (const ch of name) h = (h * 31 + ch.codePointAt(0)!) >>> 0
  return h % 360
}

export function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  if (parts.length === 0) return '?'
  const first = [...parts[0]][0] ?? '?'
  const second = parts.length > 1 ? ([...parts[1]][0] ?? '') : ''
  return (first + second).toUpperCase()
}
