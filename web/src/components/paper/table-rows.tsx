import { CaretLeftIcon, CaretRightIcon } from "@phosphor-icons/react"
import type { ReactNode } from "react"
import { ReviewMark } from "@/components/paper/card-parts"
import { ToCheck } from "@/components/status-mark"
import { Button } from "@/components/ui/button"
import { columnHead } from "@/lib/cells"
import type { DocTable } from "@/lib/format"
import { type Item, needsLook } from "@/lib/items"
import type { MapCell, MapColumn, MapRow, Reading, Said } from "@/lib/reading"
import { cn } from "@/lib/utils"

function Pairs({ said, className }: { said: Said[]; className?: string }) {
  if (said.length === 0) return null
  return (
    <p className={cn("flex flex-wrap gap-x-2.5 text-caption", className)}>
      {said.map((s) => (
        <span key={s.column} className="min-w-0" title={s.column}>
          <span className="text-muted-ink">{s.title} </span>
          {s.look ? <ToCheck>{s.text}</ToCheck> : <span className="text-ink tabular-nums">{s.text}</span>}
        </span>
      ))}
    </p>
  )
}

/** One cell of the row: the paper's column header and the cell as printed, beside what SAGE put in the export for
 * it. Clicking it opens the cell's own record under it, which then says those values itself. */
function CellLine({
  cell,
  column,
  table,
  r,
  open,
  children,
  onActivate,
}: {
  cell: MapCell
  column: MapColumn
  table: DocTable
  r: number
  open: boolean
  children: ReactNode
  onActivate: (id: string) => void
}) {
  const printed = table.rows?.[r]?.[cell.c]?.text.trim() || "–"
  return (
    <li
      className={cn("rounded-control", open && "bg-surface-2")}
      data-testid="row-cell"
      data-item={cell.item.id}
      data-active={open || undefined}
    >
      <button
        type="button"
        onClick={() => onActivate(cell.item.id)}
        aria-expanded={open}
        className="grid w-full cursor-pointer grid-cols-[16px_minmax(0,1fr)_auto] items-start gap-x-2.5 rounded-control px-2 py-2 text-left outline-none hover:bg-surface-2 focus-visible:shadow-cursor"
      >
        <span className="pt-0.5">
          <ReviewMark item={cell.item} />
        </span>
        <span className="min-w-0 space-y-0.5">
          <span className="block truncate text-caption text-muted-ink" title={columnHead(table, cell.c)}>
            {columnHead(table, cell.c)}
          </span>
          {open ? null : <Pairs said={[...column.said, ...cell.own]} />}
        </span>
        <span
          className={cn(
            "rounded-mark border px-1.5 text-control tabular-nums",
            open ? "border-ink bg-surface text-ink" : "border-transparent text-muted-ink",
          )}
        >
          {printed}
        </span>
      </button>
      {open ? (
        <div className="space-y-2 px-2 pb-3" data-testid="cluster-open">
          {children}
        </div>
      ) : null}
    </li>
  )
}

/** A table, reviewed a row at a time. What every value shares is said once at the top; the row's header and what
 * SAGE read it as come next; then each cell, the paper's text beside what goes in the export, the open one with its
 * own record under it. ‹ › move between rows (only those with something left, under To check). */
export function TableRows({
  table,
  reading,
  open,
  quiet,
  cell,
  onActivate,
  onAccept,
}: {
  table: DocTable
  reading: Reading
  open: Item
  quiet: boolean
  /** The open cell's record, given the columns it fills on its own. */
  cell: (item: Item, own: string[]) => ReactNode
  onActivate: (id: string) => void
  onAccept: (items: Item[]) => void
}) {
  const { every, columns, rows } = reading
  const has = (r: MapRow, id: string) => r.cells.some((c) => c?.item.id === id)
  const shown = quiet
    ? rows.filter((r) => has(r, open.id) || r.cells.some((c) => c && needsLook(c.item)))
    : rows
  const at = shown.findIndex((r) => has(r, open.id))
  const row = shown[at]
  if (!row) return null
  const go = (to: MapRow | undefined) => {
    const cells = to?.cells.flatMap((c) => (c ? [c.item] : [])) ?? []
    const first = cells.find(needsLook) ?? cells[0]
    if (first) onActivate(first.id)
  }
  const unreviewed = row.cells.flatMap((c) => (c && c.item.review === null ? [c.item] : []))
  // A cell's own record leads with what differs from cell to cell: its column's columns, then its own.
  const own = [...(columns[0]?.said.map((s) => s.column) ?? []), ...reading.cells]
  return (
    <div className="space-y-3" data-testid="table-rows">
      <Pairs said={every} className="text-muted-ink" />
      <section className="space-y-1 border-t border-line pt-3" data-testid="table-row" data-row={row.r}>
        <div className="flex items-center gap-1">
          <p className="min-w-0 flex-1 truncate text-control text-ink" title={row.head}>
            {row.head}
          </p>
          <span className="shrink-0 text-caption text-muted-ink tabular-nums" data-testid="row-position">
            {at + 1} / {shown.length}
          </span>
          <Button
            variant="quiet"
            size="icon-sm"
            className="size-6"
            aria-label="Previous row"
            disabled={at === 0}
            onClick={() => go(shown[at - 1])}
            data-testid="row-prev"
          >
            <CaretLeftIcon className="size-3.5" />
          </Button>
          <Button
            variant="quiet"
            size="icon-sm"
            className="size-6"
            aria-label="Next row"
            disabled={at === shown.length - 1}
            onClick={() => go(shown[at + 1])}
            data-testid="row-next"
          >
            <CaretRightIcon className="size-3.5" />
          </Button>
        </div>
        <div className="flex items-start gap-3">
          <Pairs said={row.said} className="min-w-0 flex-1" />
          {unreviewed.length > 1 ? (
            <Button
              variant="text"
              className="shrink-0 text-caption"
              onClick={() => onAccept(unreviewed)}
              data-testid="accept-row"
            >
              Accept row
            </Button>
          ) : null}
        </div>
        <ul className="-mx-2 pt-1">
          {row.cells.map((c, i) =>
            c ? (
              <CellLine
                key={c.item.id}
                cell={c}
                column={columns[i] as MapColumn}
                table={table}
                r={row.r}
                open={c.item.id === open.id}
                onActivate={onActivate}
              >
                {cell(c.item, own)}
              </CellLine>
            ) : null,
          )}
        </ul>
      </section>
    </div>
  )
}
