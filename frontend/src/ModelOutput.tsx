// The model's answer, read. One component for the player's verdict and for the round summary:
// the same text deserves the same rendering, and two callers assembling their own markdown
// diverge at the first forgotten tag.
//
// `streaming` is the only difference between the two modes, and it lives in the API rather
// than in the caller because the reason is a single one: a markdown document interrupted in
// the middle of a `**` is not a document. So it is drawn raw while it is being written, and
// rendered once it is finished — one switch, not a flicker per frame.
//
// No raw HTML is rendered: `react-markdown` escapes what comes from the source, so a player
// whose prompt asks for `<img onerror>` gets the text of the tag, not a tag. That is the whole
// reason for this library rather than an innerHTML assignment over a plain `<div>`.
import Markdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

// Every component receives a `node` prop carrying the hast element. It MUST be stripped before
// spreading onto a DOM node, or React warns once per element per render. This is the only reason
// the map below exists rather than the four lines of CSS the task looks like it needs.
const BLOCKS: Components = {
  p: ({ node: _node, ...props }) => <p {...props} />,
  ul: ({ node: _node, ...props }) => <ul {...props} />,
  ol: ({ node: _node, ...props }) => <ol {...props} />,
  li: ({ node: _node, ...props }) => <li {...props} />,
  blockquote: ({ node: _node, ...props }) => <blockquote {...props} />,
  h1: ({ node: _node, ...props }) => <h1 {...props} />,
  h2: ({ node: _node, ...props }) => <h2 {...props} />,
  h3: ({ node: _node, ...props }) => <h3 {...props} />,
  h4: ({ node: _node, ...props }) => <h4 {...props} />,
  pre: ({ node: _node, ...props }) => <pre {...props} />,
  table: ({ node: _node, ...props }) => <table {...props} />,
}

export default function ModelOutput({ text, streaming = false }: { text: string; streaming?: boolean }) {
  if (streaming) return <pre className="slot md md--raw">{text}</pre>
  return (
    <div className="md">
      {/* No `skipHtml`: the default escapes a raw tag into visible text, and a player who asked
          the model for `<img onerror>` should SEE the tag it emitted. Dropping it would hide the
          very thing worth reading. `urlTransform` is left at its default, which strips
          `javascript:` URLs — do not override it. */}
      <Markdown remarkPlugins={[remarkGfm]} components={BLOCKS}>{text}</Markdown>
    </div>
  )
}