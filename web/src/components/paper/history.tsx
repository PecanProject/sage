import { CheckIcon } from "@phosphor-icons/react"
import {
  DropdownMenuItem,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
} from "@/components/ui/dropdown-menu"
import { TERMINAL, useDriverOf } from "@/lib/agents"
import { itemCount, type RunInfo, runStatusLabel, stamp } from "@/lib/format"

/** History, as a submenu of the Runs menu: the paper's extractions, newest first. */
export function HistoryMenu({
  runs,
  shown,
  onPick,
}: {
  runs: RunInfo[]
  shown: string | null
  onPick: (run: RunInfo) => void
}) {
  const driver = useDriverOf()
  return (
    <DropdownMenuSub>
      <DropdownMenuSubTrigger disabled={!runs.length} data-testid="menu-history">
        History
      </DropdownMenuSubTrigger>
      <DropdownMenuSubContent className="max-h-[360px] w-[360px] overflow-y-auto" data-testid="history">
        {runs.map((r) => {
          const n = r.counts?.records ?? 0
          return (
            <DropdownMenuItem
              key={r.id}
              onClick={() => onPick(r)}
              className="grid h-auto grid-cols-[16px_1fr_auto] gap-x-3 py-2"
              data-testid="history-row"
              data-run={r.id}
              data-current={r.id === shown || undefined}
            >
              <span className="flex">
                {r.id === shown ? <CheckIcon weight="bold" className="size-4" /> : null}
              </span>
              <span className="min-w-0">
                <span className="block text-ink">{stamp(r.created_at)}</span>
                <span className="block truncate text-caption text-muted-ink">
                  {driver(r)?.title ?? TERMINAL} · {runStatusLabel(r)}
                </span>
                {r.guidance ? (
                  <span className="block truncate text-caption text-muted-ink" title={r.guidance}>
                    {r.guidance}
                  </span>
                ) : null}
              </span>
              <span className="text-caption text-muted-ink">{n ? itemCount(n) : "No data"}</span>
            </DropdownMenuItem>
          )
        })}
      </DropdownMenuSubContent>
    </DropdownMenuSub>
  )
}
