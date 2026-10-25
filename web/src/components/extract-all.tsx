import { CircleNotchIcon } from "@phosphor-icons/react"
import { useNavigate, useSearch } from "@tanstack/react-router"
import { ChoiceFields } from "@/components/paper/extract"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { isLive } from "@/lib/format"
import { paperState, type Summary, useStartRuns, useStopRuns } from "@/lib/paper-state"
import { limitLine, useRunChoice } from "@/lib/run-choice"
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
      <ExtractAllDialog open={search.extract === 1 && pending > 1} papers={pending} onClose={close} />
      <StopAllDialog open={search.stop === 1 && live.length > 0} count={live.length} onClose={close} />
    </>
  )
}

function ExtractAllDialog({ open, papers, onClose }: { open: boolean; papers: number; onClose: () => void }) {
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-[440px]" data-testid="extract-all-dialog">
        {open ? <ExtractAllForm papers={papers} onClose={onClose} /> : null}
      </DialogContent>
    </Dialog>
  )
}

function ExtractAllForm({ papers, onClose }: { papers: number; onClose: () => void }) {
  const c = useRunChoice(null)
  const start = useStartRuns(onClose)
  const label = `Extract ${papers} papers`
  return (
    <>
      <DialogHeader>
        <DialogTitle>{label}</DialogTitle>
      </DialogHeader>
      <DialogBody className="space-y-4">
        <ChoiceFields choice={c} testid="extract-all-model" />
      </DialogBody>
      <DialogFooter className="justify-between">
        <span className="text-caption text-muted-ink" data-testid="extract-all-limit">
          {limitLine(c, papers)}
        </span>
        <Button
          disabled={start.isPending || c.blocker.blocked}
          onClick={() => start.mutate({ body: { using: c.using, effort: c.effortAsked } })}
          data-testid="extract-all-start"
        >
          {start.isPending ? <CircleNotchIcon className="animate-spin" /> : null}
          {label}
        </Button>
      </DialogFooter>
    </>
  )
}

function StopAllDialog({ open, count, onClose }: { open: boolean; count: number; onClose: () => void }) {
  const stop = useStopRuns(onClose)
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-[400px]" data-testid="stop-all-dialog">
        <DialogHeader>
          <DialogTitle>Stop {count} extractions?</DialogTitle>
        </DialogHeader>
        <DialogBody>
          <p className="text-control text-muted-ink">
            Each keeps what it has found so far, and you can resume it later. Queued ones won't start.
          </p>
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Keep going
          </Button>
          <Button
            disabled={stop.isPending}
            onClick={() => stop.mutate({ body: {} })}
            data-testid="stop-all-confirm"
          >
            {stop.isPending ? <CircleNotchIcon className="animate-spin" /> : null}
            Stop all
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
