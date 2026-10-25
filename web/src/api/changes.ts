import { partialMatchKey, type QueryKey, useQueryClient } from "@tanstack/react-query"
import { useEffect } from "react"
import type { Schemas } from "./client"

type Change = Schemas["Change"]

const paperPath = (paperId: string) => ({ params: { path: { paper_id: paperId } } })
const runPath = (runId: string) => ({ params: { path: { run_id: runId } } })

/** The `$api` queries that show what a change touched, as key prefixes. */
function touched(c: Change): QueryKey[] {
  const paper: QueryKey[] = [
    ["get", "/api/papers"],
    ["get", "/api/papers/{paper_id}", paperPath(c.paper_id)],
  ]
  const runId = c.run_id
  const run = (...paths: string[]): QueryKey[] =>
    runId ? paths.map((p) => ["get", `/api/runs/{run_id}${p}`, runPath(runId)]) : []
  switch (c.what) {
    case "library":
      return paper
    case "paper":
      return [
        ...paper,
        ["get", "/api/papers/{paper_id}/document", paperPath(c.paper_id)],
        ...run("/records", "/queue", "/document"),
      ]
    case "files":
      // Only an open run compiles the files as they are; an ended one shows what it settled.
      return runId ? [...paper, ...run("/records", "/queue")] : []
    case "reviews":
      return [
        ...paper,
        ["get", "/api/papers/{paper_id}/reviews", paperPath(c.paper_id)],
        ["get", "/api/runs/{run_id}/queue"],
      ]
    case "run":
      return [
        ...paper,
        ["get", "/api/runs", { params: { query: { paper_id: c.paper_id } } }],
        ...run("", "/records", "/queue", "/document"),
      ]
    case "log":
      return paper
  }
}

const RETRY_MS = 2000

/** Opens /api/changes and hands each batch to `onChanges`, and calls `onMissed` once a dropped connection is back.
 * EventSource retries a dropped connection itself but gives up on an error response, such as a proxy's while the
 * server restarts, so then it is opened again here. Returns the function that stops it. */
function follow(onChanges: (batch: Change[]) => void, onMissed: () => void): () => void {
  let source: EventSource | null = null
  let retry: ReturnType<typeof setTimeout> | undefined
  let missed = false
  const open = () => {
    const s = new EventSource("/api/changes")
    source = s
    s.onmessage = (m: MessageEvent<string>) => {
      const batch: unknown = JSON.parse(m.data)
      if (Array.isArray(batch)) onChanges(batch as Change[])
    }
    s.onopen = () => {
      if (missed) onMissed()
      missed = false
    }
    s.onerror = () => {
      missed = true
      if (s.readyState === EventSource.CLOSED) retry = setTimeout(open, RETRY_MS)
    }
  }
  open()
  return () => {
    clearTimeout(retry)
    source?.close()
  }
}

/** Follows every change in the library, so a screen shows what an agent, a run or another window wrote as soon as it
 * lands. What changed while the connection was down is unknown, so then everything is fetched again. */
export function useLibraryChanges() {
  const queryClient = useQueryClient()
  useEffect(
    () =>
      follow(
        (batch) => {
          const keys = batch.flatMap(touched)
          void queryClient.invalidateQueries({
            predicate: (q) => keys.some((k) => partialMatchKey(q.queryKey, k)),
          })
        },
        () => void queryClient.invalidateQueries(),
      ),
    [queryClient],
  )
}
