import { useQueryClient } from "@tanstack/react-query"
import { useEffect, useState } from "react"
import type { Schemas } from "./client"

export type RunEvent = Schemas["EventPage"]["events"][number]

// The stream names each SSE message after its event type, and EventSource only delivers names it listens for.
// The record makes tsc fail when sage/events.py gains a type this list lacks.
const EVENT_TYPES = Object.keys({
  "run.started": true,
  "stage.started": true,
  "stage.finished": true,
  "llm.request": true,
  "llm.thinking": true,
  "llm.text": true,
  "llm.tool_call": true,
  "llm.tool_result": true,
  "check.failed": true,
  "record.settled": true,
  usage: true,
  note: true,
  "run.finished": true,
  "run.error": true,
} satisfies Record<RunEvent["type"], true>)

/** A run's events over SSE, replayed from the start. After a dropped connection EventSource reconnects on its own
 * and the server resumes from the last batch it marked with an id. A new `attempt` reopens the stream: a resume, or
 * the first start of a run whose worker died while it was queued. */
export function useRunEvents(runId: string, attempt: string): RunEvent[] {
  const queryClient = useQueryClient()
  const stream = `${runId}:${attempt}`
  const [got, setGot] = useState<{ stream: string; events: RunEvent[] }>({ stream, events: [] })
  useEffect(() => {
    const source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/stream`)
    let pending: RunEvent[] = []
    let frame = 0
    const flush = () => {
      const batch = pending
      pending = []
      frame = 0
      setGot((g) => ({ stream, events: g.stream === stream ? [...g.events, ...batch] : batch }))
    }
    const push = (m: MessageEvent<string>) => {
      pending.push(JSON.parse(m.data) as RunEvent)
      if (!frame) frame = requestAnimationFrame(flush)
    }
    for (const t of EVENT_TYPES) source.addEventListener(t, push)
    source.addEventListener("end", () => {
      source.close()
      void queryClient.invalidateQueries()
    })
    return () => {
      source.close()
      cancelAnimationFrame(frame)
    }
  }, [runId, stream, queryClient])
  return got.stream === stream ? got.events : []
}
