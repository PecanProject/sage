import { CircleNotchIcon } from "@phosphor-icons/react"
import { useNavigate, useSearch } from "@tanstack/react-router"
import { ExtractDialog, StopDialog } from "@/components/paper/extract"
import { Button } from "@/components/ui/button"
import { isLive } from "@/lib/format"
import { paperState, type Summary } from "@/lib/paper-state"
import type { HomeSearch } from "@/router"

/** Home's controls for many papers at once: extract every paper with no data, and stop every live extraction. The
 * server runs `parallel_runs` at a time and queues the rest. */
export function ExtractAll({ papers }: { papers: Summary[] }) {
  const search = useSearch({ strict: false }) as HomeSearch
  const navigate = useNavigate()
  const setSearch = (patch: HomeSearch) =>
    void navigate({ to: ".", search: (s: object) => ({ ...s, ...patch }) })
  const close = () => setSearch({ extract: undefined, stop: undefined })
  const live = papers.flatMap((s) => (s.latest_run && isLive(s.latest_run) ? [s.latest_run] : []))
  const queued = live.filter((r) => r.status === "queued").length
  const pending = papers.filter((s) => {
    const kind = paperState(s).kind
    return kind === "none" || kind === "failed"
  }).length
  return (
    <>
      {live.length ? (
        <span className="flex items-center gap-2 text-caption text-muted-ink" data-testid="live-runs">
          <CircleNotchIcon className="size-4 animate-spin" />
          {[
            live.length - queued ? `${live.length - queued} extracting` : null,
            queued ? `${queued} queued` : null,
          ]
            .filter(Boolean)
            .join(" · ")}
          {live.length > 1 ? (
            <Button
              variant="text"
              className="text-caption"
              onClick={() => setSearch({ stop: 1 })}
              data-testid="stop-all"
            >
              Stop all
            </Button>
          ) : null}
        </span>
      ) : null}
      {pending > 1 ? (
        <Button variant="outline" onClick={() => setSearch({ extract: 1 })} data-testid="extract-all">
          Extract all
        </Button>
      ) : null}
      <ExtractDialog
        open={search.extract === 1 && pending > 1}
        title={`Extract ${pending} papers`}
        papers={pending}
        testid="extract-all"
        startTestid="extract-all-start"
        onClose={close}
      />
      <StopDialog
        body={search.stop === 1 && live.length > 0 ? {} : null}
        title={`Stop ${live.length} extractions?`}
        confirm="Stop all"
        testid="stop-all"
        onClose={close}
      >
        Each keeps what it has found so far, and you can resume it later. Queued ones won't start.
      </StopDialog>
    </>
  )
}
