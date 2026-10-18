import type { Schemas } from "@/api/client"
import type { RunEvent } from "@/api/events"

export type Usage = Schemas["Usage"]
type CacheMiss = Schemas["CacheMiss"]

// pydantic-ai's `input_tokens` already counts cache reads and writes (docs/design/05-prompt-caching.md).
export type InputSplit = { read: number; write: number; fresh: number; input: number }

export function split(u: Usage): InputSplit {
  const read = u.cache_read_tokens
  const write = u.cache_write_tokens
  return { read, write, fresh: Math.max(0, u.input_tokens - read - write), input: u.input_tokens }
}

const ZERO: Usage = {
  requests: 0,
  input_tokens: 0,
  output_tokens: 0,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  cost_usd: 0,
}

export const used = (u: Usage | undefined): Usage => u ?? ZERO

export function sum(a: Usage, b: Usage): Usage {
  return {
    requests: a.requests + b.requests,
    input_tokens: a.input_tokens + b.input_tokens,
    output_tokens: a.output_tokens + b.output_tokens,
    cache_read_tokens: a.cache_read_tokens + b.cache_read_tokens,
    cache_write_tokens: a.cache_write_tokens + b.cache_write_tokens,
    cost_usd: a.cost_usd + b.cost_usd,
  }
}

export type TurnUsage = { key: string; turn: number; usage: Usage; miss: CacheMiss | null }

export function turnUsage(events: RunEvent[]): TurnUsage[] {
  return events
    .flatMap((e, i) =>
      e.type === "usage" ? [{ key: `${i}`, usage: e.request, miss: e.cache_miss ?? null }] : [],
    )
    .map((t, n) => ({ ...t, turn: n + 1 }))
}

export const MISS_REASON: Record<CacheMiss["reason"], string> = {
  model_changed: "the model changed",
  system_changed: "the instructions changed",
  tools_changed: "the tools changed",
  messages_changed: "earlier messages changed",
  previous_message_not_found: "the previous request wasn't found",
  unavailable: "the cache was unavailable",
  pending: "the cache was still being written",
}

const compactFormat = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 })
const wholeFormat = new Intl.NumberFormat()

export const compact = (n: number) => compactFormat.format(n)
export const whole = (n: number) => wholeFormat.format(n)
export const dollars = (usd: number) => `$${usd.toFixed(4)}`
export const percent = (part: number, of: number) => (of > 0 ? `${Math.round((part / of) * 100)}%` : "–")
