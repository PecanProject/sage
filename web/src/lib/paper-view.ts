import { useEffect, useEffectEvent, useRef } from "react"
import { toast } from "sonner"
import { $api, queryClient } from "@/api/client"
import { endedAlert, isLive, itemCount, type RunInfo } from "@/lib/format"
import { type Box, buildItems, type Item } from "@/lib/items"
import { paperState } from "@/lib/paper-state"
import type { PaperSearch } from "@/router"

type Boxes = Record<string, Box>

/** Margin order: the items with no place in the paper first, then the rest as they come down the pages. */
function marginOrder(items: Item[], boxes: Boxes): Item[] {
  const unplaced: Item[] = []
  const placed: { item: Item; page: number; y: number }[] = []
  for (const item of items) {
    const b = item.primary ? boxes[item.primary] : undefined
    if (b) placed.push({ item, page: b.page, y: b.bbox[1] ?? 0 })
    else unplaced.push(item)
  }
  placed.sort((a, b) => a.page - b.page || a.y - b.y)
  return [...unplaced, ...placed.map((p) => p.item)]
}

const paperPath = (paperId: string) => ({ params: { path: { paper_id: paperId } } })
const paperRuns = (paperId: string) => ({ params: { query: { paper_id: paperId } } })
const runPath = (runId: string) => ({ params: { path: { run_id: runId } } })

/** Starts every request the Paper screen makes, and resolves once the summary has loaded or failed. */
export async function loadPaper(paperId: string, run: string | undefined) {
  void queryClient.prefetchQuery($api.queryOptions("get", "/api/runs", paperRuns(paperId)))
  if (run) prefetchRun(run)
  const summary = await queryClient
    .ensureQueryData($api.queryOptions("get", "/api/papers/{paper_id}", paperPath(paperId)))
    .catch(() => null)
  if (run || !summary) return
  if (summary.data_run) prefetchRun(summary.data_run.id)
  else
    void queryClient.prefetchQuery(
      $api.queryOptions("get", "/api/papers/{paper_id}/document", paperPath(paperId)),
    )
}

function prefetchRun(runId: string) {
  void queryClient.prefetchQuery($api.queryOptions("get", "/api/runs/{run_id}/records", runPath(runId)))
  void queryClient.prefetchQuery($api.queryOptions("get", "/api/runs/{run_id}/queue", runPath(runId)))
  void queryClient.prefetchQuery($api.queryOptions("get", "/api/runs/{run_id}/document", runPath(runId)))
}

/** The paper and its extractions. `useLibraryChanges` fetches them again whenever their files change. */
function usePaperQueries(paperId: string) {
  const summary = $api.useQuery("get", "/api/papers/{paper_id}", paperPath(paperId))
  const runs = $api.useQuery("get", "/api/runs", paperRuns(paperId))
  return { summary, runs: runs.data ?? [], runsLoaded: runs.data !== undefined }
}

function useDocument(paperId: string, runId: string | null, summaryLoaded: boolean) {
  const paperDoc = $api.useQuery("get", "/api/papers/{paper_id}/document", paperPath(paperId), {
    enabled: summaryLoaded && runId === null,
  })
  const runDoc = $api.useQuery("get", "/api/runs/{run_id}/document", runPath(runId ?? ""), {
    enabled: runId !== null,
  })
  return runId === null ? paperDoc.data : runDoc.data
}

/** One extraction's data items, in margin order. */
function useItems(runId: string | null) {
  const path = runPath(runId ?? "")
  const on = { enabled: runId !== null }
  const records = $api.useQuery("get", "/api/runs/{run_id}/records", path, on)
  const queue = $api.useQuery("get", "/api/runs/{run_id}/queue", path, on)
  const boxes: Boxes = records.data?.boxes ?? {}
  const items = records.data && queue.data ? marginOrder(buildItems(records.data, queue.data), boxes) : []
  return { items, boxes, loading: runId !== null && (records.isLoading || queue.isLoading) }
}

/** The run the screen shows: the URL's, once it is known to be this paper's, so a link to another paper's run never
 * loads its data; else the one that stands for the paper. */
function pickRun(runs: RunInfo[] | null, wanted: string | undefined, dataRun: RunInfo | null) {
  if (wanted === undefined)
    return { picked: null, viewId: dataRun?.id ?? null, missing: null, waiting: false }
  const picked = runs?.find((r) => r.id === wanted) ?? null
  return {
    picked,
    viewId: picked?.id ?? null,
    missing: runs && !picked ? wanted : null,
    waiting: runs === null,
  }
}

/** Everything the Paper screen shows, from the server and the URL. Nothing here is copied into state. */
export function usePaperView(paperId: string, search: PaperSearch) {
  const { summary, runs, runsLoaded } = usePaperQueries(paperId)
  const dataRun = summary.data?.data_run ?? null
  const latest = summary.data?.latest_run ?? null
  const { picked, viewId, missing, waiting } = pickRun(runsLoaded ? runs : null, search.run, dataRun)
  const { items, boxes, loading } = useItems(viewId)
  const doc = useDocument(paperId, viewId, summary.data !== undefined)
  return {
    summary,
    runs,
    doc,
    state: summary.data ? paperState(summary.data) : null,
    dataRun,
    viewId,
    /** An extraction picked from History, when it isn't the one that stands for the paper. */
    older: picked?.id === dataRun?.id ? null : picked,
    items,
    boxes,
    loading: summary.isLoading || waiting || loading,
    /** The `run` the URL names when this paper has no such extraction. */
    missingRun: missing,
    latest,
    /** The run the details drawer opens on: the picked one, else the live or latest one. */
    drawerRun: picked ?? latest ?? dataRun,
  }
}

/** Say when a live extraction ends, once, and hand back its outcome. */
export function useRunFinished(
  latest: RunInfo | null,
  onFinished: (run: RunInfo) => void,
  onCarryOn: (run: RunInfo) => void,
) {
  const watching = useRef<string | null>(null)
  const seen = useEffectEvent((run: RunInfo) => {
    if (isLive(run)) {
      watching.current = run.id
      return
    }
    if (watching.current !== run.id) return
    watching.current = null
    // A resumed run keeps its id, so whatever was cached for it before it ran again is stale.
    void queryClient.invalidateQueries({ queryKey: ["get", "/api/runs/{run_id}/records", runPath(run.id)] })
    void queryClient.invalidateQueries({ queryKey: ["get", "/api/runs/{run_id}/queue", runPath(run.id)] })
    const alert = endedAlert(run)
    if (alert === null) toast(`Extraction finished: ${itemCount(run.counts?.records ?? 0)}`)
    // Only a failure is an error: a stop the user asked for, or a limit, is a warning.
    else
      (run.status === "failed" || run.error ? toast.error : toast.warning)(alert.title, {
        description: alert.reason,
        action: { label: alert.action, onClick: () => onCarryOn(run) },
      })
    onFinished(run)
  })
  useEffect(() => {
    if (latest) seen(latest)
  }, [latest])
}
