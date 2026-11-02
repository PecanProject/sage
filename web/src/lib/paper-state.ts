import { useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { toast } from "sonner"
import { $api, errorMessage, type Schemas } from "@/api/client"
import { isLive, isResumable, type LiveStep, liveStep, type RunInfo, startsOver } from "@/lib/format"

export type Summary = Schemas["PaperSummary"]

/** Where a paper stands, from `PaperSummary` alone, so Home, the Paper screen and the CLI agree. */
export type PaperState =
  | { kind: "live"; run: RunInfo; step: LiveStep }
  | { kind: "none" }
  | { kind: "failed"; run: RunInfo }
  | { kind: "data"; run: RunInfo; toCheck: number; looksGood: number; warn: RunInfo | null }

export function paperState(s: Summary): PaperState {
  const latest = s.latest_run ?? null
  if (latest && isLive(latest)) return { kind: "live", run: latest, step: liveStep(latest, s.live_stage) }
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

function useRunHandlers<T>(onDone?: (result: T) => void) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  return {
    onSuccess: (result: T) => {
      void queryClient.invalidateQueries({ queryKey: ["get", "/api/papers"] })
      void queryClient.invalidateQueries({ queryKey: ["get", "/api/papers/{paper_id}"] })
      void queryClient.invalidateQueries({ queryKey: ["get", "/api/runs"] })
      onDone?.(result)
    },
    onError: (e: unknown) =>
      isMissingKey(e)
        ? toast.error(errorMessage(e).split(". ")[0] ?? errorMessage(e), {
            action: { label: "Set up", onClick: () => void navigate({ to: "/manage" }) },
          })
        : toast.error(errorMessage(e)),
  }
}

/** Starts runs: on the papers given, or with none given, on every paper with no data and nothing live. */
export function useStartRuns(onStarted?: (runs: RunInfo[]) => void) {
  return $api.useMutation("post", "/api/runs", useRunHandlers(onStarted))
}

function useResumeRun(onStarted?: (run: RunInfo) => void) {
  return $api.useMutation("post", "/api/runs/{run_id}/resume", useRunHandlers(onStarted))
}

export function useContinueRun(onStarted?: (run: RunInfo) => void) {
  return $api.useMutation("post", "/api/runs/{run_id}/continue", useRunHandlers(onStarted))
}

/** Carries a run on in one click, as `carryOnLabel` names it: a resume or a continue with what the run used. A run
 * with nothing to build on starts over through `startOver`, and a finished one takes guidance through `guide`. */
export function useCarryOn(on: { startOver: (run: RunInfo) => void; guide: (run: RunInfo) => void }) {
  const resume = useResumeRun()
  const proceed = useContinueRun()
  return (run: RunInfo) => {
    const request = { params: { path: { run_id: run.id } }, body: {} }
    if (startsOver(run)) on.startOver(run)
    else if (isResumable(run)) resume.mutate(request)
    else if (run.status === "finished") on.guide(run)
    else proceed.mutate(request)
  }
}

/** Answers what the agent extracting a paper is waiting on. */
export function useAnswerAgent() {
  return $api.useMutation("post", "/api/papers/{paper_id}/extract/answer", useRunHandlers())
}

/** Stops the runs given, or with none given, every queued and running one. */
export function useStopRuns(onStopped?: (runs: RunInfo[]) => void) {
  return $api.useMutation("post", "/api/runs/stop", useRunHandlers(onStopped))
}
