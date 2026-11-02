// A model asked for a "schéma" often draws one in text (`/|`, `A-----C`) and forgets the code
// fence. Markdown then folds the lines and collapses the spaces the drawing is made of. This finds
// such drawings outside existing fences and fences them, so they render monospaced and intact.

// A stroke of a drawing: a slash, a pipe, or a run of three dashes/underscores/equals.
const STROKE = /[/\\|]|[-_=]{3,}/
// Markdown structures that use the same characters and must stay markdown.
const TABLE_ROW = /^\s*\|.*\|\s*$/
const LIST_OR_QUOTE = /^\s*([-*+]\s|\d+[.)]\s|>)/
const FENCE = /^\s*(```|~~~)/

function letters(line: string): number {
  return (line.match(/\p{L}/gu) ?? []).length
}

// A line that looks drawn: it has a stroke and is mostly not words.
function isArt(line: string): boolean {
  if (!STROKE.test(line) || TABLE_ROW.test(line) || LIST_OR_QUOTE.test(line)) return false
  const ink = line.replace(/\s/g, '').length
  return line.length <= 100 && letters(line) <= ink * 0.5
}

// A label beside a drawing ("  B", "h ?"): too short to be prose.
function isLabel(line: string): boolean {
  const ink = line.replace(/\s/g, '')
  return ink.length > 0 && ink.length <= 4
}

export function fenceAsciiArt(text: string): string {
  const lines = text.split('\n')
  const out: string[] = []
  let inFence = false
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (FENCE.test(line)) inFence = !inFence
    if (inFence || !(isArt(line) || isLabel(line))) {
      out.push(line)
      i++
      continue
    }
    // Grow the block over drawn lines and short labels.
    let j = i
    let strokes = 0
    while (j < lines.length && !FENCE.test(lines[j]) && (isArt(lines[j]) || isLabel(lines[j]))) {
      if (isArt(lines[j])) strokes++
      j++
    }
    // Two drawn lines at least: one slash in a sentence is not a drawing.
    if (strokes >= 2 && j - i >= 3) {
      out.push('```', ...lines.slice(i, j), '```')
    } else {
      out.push(...lines.slice(i, j))
    }
    i = j
  }
  return out.join('\n')
}
