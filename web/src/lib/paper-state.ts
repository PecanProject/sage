import { useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { $api, errorMessage, type Schemas } from "@/api/client"
import { isLive, type LiveStep, liveStep, type RunInfo } from "@/lib/format"
import { useSettingsRoute } from "@/lib/settings-route"

export type Summary = Schemas["PaperSummary"]

/** Where a paper stands, from `PaperSummary` alone, so Home, the Paper screen and the CLI agree. */
export type PaperState =
  | { kind: "live"; run: RunInfo; step: LiveStep }
  | { kind: "none" }
  | { kind: "failed"; run: RunInfo }
  | { kind: "data"; run: RunInfo; toCheck: number; looksGood: number; warn: RunInfo | null }

export function paperState(s: Summary): PaperState {
  const latest = s.latest_run ?? null
  if (latest && isLive(latest)) return { kind: "live", run: latest, step: liveStep(s.live_stage) }
  const data = s.data_run ?? null
  if (data === null) return latest ? { kind: "failed", run: latest } : { kind: "none" }
  const failedAfter =
    latest !== null &&
    latest.id !== data.id &&
    (latest.status === "failed" || latest.status === "interrupted")
  return {
    kind: "data",
    run: data,
    toCheck: s.to_check ?? 0,
    looksGood: s.looks_good ?? 0,
    /** The run to warn about: a newer attempt that failed or stopped, else the data run when it didn't finish. */
    warn: failedAfter ? latest : data.status === "finished" ? null : data,
  }
}

function isMissingKey(e: unknown): boolean {
  return typeof e === "object" && e !== null && "kind" in e && e.kind === "missing_key"
}

function useRunHandlers(onDone?: (run: RunInfo) => void) {
  const queryClient = useQueryClient()
  const settings = useSettingsRoute()
  return {
    onSuccess: (run: RunInfo) => {
      void queryClient.invalidateQueries({ queryKey: ["get", "/api/papers"] })
      void queryClient.invalidateQueries({ queryKey: ["get", "/api/papers/{paper_id}"] })
      void queryClient.invalidateQueries({ queryKey: ["get", "/api/runs"] })
      onDone?.(run)
    },
    onError: (e: unknown) =>
      isMissingKey(e)
        ? toast.error("Add your Anthropic API key first", {
            action: { label: "Open settings", onClick: () => void settings.open() },
          })
        : toast.error(errorMessage(e)),
  }
}

export function useStartRun(onStarted?: (run: RunInfo) => void) {
  return $api.useMutation("post", "/api/papers/{paper_id}/runs", useRunHandlers(onStarted))
}

export function useResumeRun(onStarted?: (run: RunInfo) => void) {
  return $api.useMutation("post", "/api/runs/{run_id}/resume", useRunHandlers(onStarted))
}

export function useContinueRun(onStarted?: (run: RunInfo) => void) {
  return $api.useMutation("post", "/api/runs/{run_id}/continue", useRunHandlers(onStarted))
}

export function useCarryOn(openDialog: (run: RunInfo) => void) {
  const proceed = useContinueRun()
  return (run: RunInfo) =>
    run.status === "budget_exhausted" && run.counts?.records
      ? proceed.mutate({ params: { path: { run_id: run.id } }, body: { review_notes: true } })
      : openDialog(run)
}

export function useStopRun(onStopped?: (run: RunInfo) => void) {
  return $api.useMutation("post", "/api/runs/{run_id}/stop", useRunHandlers(onStopped))
}
