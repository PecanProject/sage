import { cn } from "@/lib/utils"

const CONTEXT_CHARS = 90
const SENTENCE_REACH_CHARS = 220
const sentences = new Intl.Segmenter(undefined, { granularity: "sentence" })

function excerptBounds(text: string, at: number, end: number) {
  const parts = [...sentences.segment(text)]
  const first = parts.findLast((s) => s.index <= at)
  const last = parts.find((s) => s.index + s.segment.trimEnd().length >= end)
  const start = first?.index ?? 0
  const stop = last ? last.index + last.segment.trimEnd().length : text.length
  const whole = { from: at - start <= SENTENCE_REACH_CHARS, to: stop - end <= SENTENCE_REACH_CHARS }
  const from = whole.from ? start : at - CONTEXT_CHARS
  const to = whole.to ? stop : end + CONTEXT_CHARS
  return { from, to, before: from > 0 && !whole.from, after: to < text.length && !whole.to }
}

export function Quote({
  quote,
  context,
  status,
}: {
  quote: string
  context: string | undefined
  status: string
}) {
  const at = context ? context.toLowerCase().indexOf(quote.toLowerCase()) : -1
  const tint = status === "verified" ? "bg-good-tint" : "bg-check-tint"
  if (!context || at < 0) return <mark className={cn("rounded-mark px-0.5 text-ink", tint)}>{quote}</mark>
  const end = at + quote.length
  const cut = excerptBounds(context, at, end)
  return (
    <>
      {cut.before ? "…" : null}
      {context.slice(cut.from, at)}
      <mark className={cn("rounded-mark px-0.5 text-ink", tint)}>{context.slice(at, end)}</mark>
      {context.slice(end, cut.to)}
      {cut.after ? "…" : null}
    </>
  )
}
