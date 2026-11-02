import { ArrowDownIcon, ArrowUpIcon } from "@phosphor-icons/react"
import {
  createColumnHelper,
  createSortedRowModel,
  rowSortingFeature,
  sortFn_alphanumeric,
  tableFeatures,
  useTable,
} from "@tanstack/react-table"
import { useEffect, useRef } from "react"
import { ReviewIcon } from "@/components/paper/card-parts"
import { ToCheck } from "@/components/status-mark"
import { Checkbox } from "@/components/ui/checkbox"
import { TextSkeleton } from "@/components/ui/skeleton"
import { Tip } from "@/components/ui/tooltip"
import { REVIEW_LABEL, STATUS_LABEL, type Status } from "@/lib/format"
import { type Item, needsLook } from "@/lib/items"
import { useKeys } from "@/lib/keys"
import { cn } from "@/lib/utils"
import { columnTitle, namedCell, type RecordNames, type Tab, type WorkbookRow } from "@/lib/workbook"

const features = tableFeatures({
  rowSortingFeature,
  sortedRowModel: createSortedRowModel(),
  sortFns: { alphanumeric: sortFn_alphanumeric },
})
const helper = createColumnHelper<typeof features, WorkbookRow>()
const TD = "h-11 border-b border-line px-3 align-middle whitespace-nowrap first:pl-gutter"
const TH = "h-10 border-b border-line px-3 text-left align-middle first:w-16 first:pl-gutter"

/** The status the cell's field has on its record; a column that reads no one field has none to show. */
function cellStatus(row: WorkbookRow, field: string | null): Status {
  if (!row.item || field === null) return "verified"
  return row.item.rows.find((r) => r.name === field)?.status ?? "verified"
}

function Cell({
  row,
  value,
  field,
  names,
}: {
  row: WorkbookRow
  value: Tab["rows"][number][number]
  field: string | null
  names: RecordNames
}) {
  const text = namedCell(value, field, names)
  const status = cellStatus(row, field)
  const look = status !== "verified" && row.item !== undefined && needsLook(row.item)
  if (text === null)
    return (
      <span data-testid="data-cell" data-status={status}>
        {look ? (
          <span aria-hidden>
            <ToCheck>–</ToCheck>
          </span>
        ) : (
          <span className="text-muted-ink" aria-hidden>
            –
          </span>
        )}
        <span className="sr-only">{look ? `Not given, ${STATUS_LABEL[status]}` : "Not given"}</span>
      </span>
    )
  return (
    <span
      className="flex max-w-[280px] items-center tabular-nums"
      data-testid="data-cell"
      data-status={status}
      title={text}
    >
      {look ? (
        <ToCheck className="min-w-0 truncate">
          {text}
          <span className="sr-only">, {STATUS_LABEL[status]}</span>
        </ToCheck>
      ) : (
        <span className="truncate">{text}</span>
      )}
    </span>
  )
}

/** The pick box, which shows the row's decision while it isn't picked or hovered. */
function Pick({
  row,
  picked,
  onPick,
}: {
  row: WorkbookRow
  picked: boolean
  onPick: (shift: boolean) => void
}) {
  const r = row.item?.review
  const said = row.item?.comments.length ?? 0
  const comments = `${said} comment${said === 1 ? "" : "s"}`
  return (
    <span className="flex items-center gap-2">
      <Checkbox
        checked={picked}
        aria-label="Select"
        disabled={!row.item}
        onClick={(e) => e.stopPropagation()}
        onCheckedChange={(_, d) => onPick(d.event instanceof MouseEvent && d.event.shiftKey)}
        className={cn(
          !picked &&
            "opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100 group-data-picking:opacity-100",
        )}
        data-testid="row-pick"
      />
      {r ? (
        <Tip label={REVIEW_LABEL[r.action]}>
          <span
            role="img"
            aria-label={REVIEW_LABEL[r.action]}
            className="text-muted-ink"
            data-testid="row-review"
          >
            <ReviewIcon action={r.action} />
          </span>
        </Tip>
      ) : null}
      {said ? (
        <Tip label={comments}>
          <span role="img" aria-label={comments} className="text-muted-ink" data-testid="row-comments">
            <ReviewIcon action="comment" />
          </span>
        </Tip>
      ) : null}
    </span>
  )
}

export function WorkbookTable({
  tab,
  names,
  rows,
  columns,
  selected,
  picked,
  onOpen,
  onPick,
}: {
  tab: Tab
  names: RecordNames
  rows: WorkbookRow[]
  /** The columns shown, by the workbook's names. */
  columns: string[]
  selected: string | null
  /** The records picked for a decision or a comment together. */
  picked: ReadonlySet<string>
  onOpen: (item: Item) => void
  /** Pick or unpick a row; with shift, every row from the last one picked. */
  onPick: (row: WorkbookRow, shift: boolean) => void
}) {
  const body = useRef<HTMLTableSectionElement>(null)
  const defs = helper.columns([
    helper.display({ id: "pick", header: "" }),
    ...columns.map((c) => {
      const i = tab.columns.indexOf(c)
      return helper.accessor((r) => namedCell(r.cells[i] ?? null, tab.fields[i] ?? null, names) ?? "", {
        id: c,
        header: columnTitle(c),
        sortFn: "alphanumeric",
      })
    }),
  ])
  const table = useTable({ features, columns: defs, data: rows, enableSortingRemoval: true })
  const shown = table.getRowModel().rows
  const cursor = shown.find((r) => r.original.id === selected) ?? shown[0]
  const focusedRow = () => {
    const key = document.activeElement?.closest("tr[data-row]")?.getAttribute("data-row")
    return shown.find((r) => r.original.key === key)
  }
  const move = (step: 1 | -1, e: KeyboardEvent) => {
    const from = focusedRow() ?? shown.find((r) => r.original.id === selected)
    const at = from ? shown.indexOf(from) : -1
    const next = shown[at === -1 ? 0 : Math.min(Math.max(at + step, 0), shown.length - 1)]
    if (!next) return
    e.preventDefault()
    if (selected && next.original.item) onOpen(next.original.item)
    body.current?.querySelector<HTMLElement>(`tr[data-row="${next.original.key}"]`)?.focus()
  }
  useKeys({
    j: (e) => move(1, e),
    k: (e) => move(-1, e),
    x: (e) => {
      const row = focusedRow()?.original ?? cursor?.original
      if (!row?.item) return
      e.preventDefault()
      onPick(row, false)
    },
  })
  // However an item gets picked (a click, j/k, a review moving on, the URL, or Check mode handing it over), bring its
  // row into view, and take the keyboard focus with it if a row had it, so the focus mark follows the selection.
  useEffect(() => {
    if (!selected) return
    const row = body.current?.querySelector<HTMLElement>(`tr[data-item="${CSS.escape(selected)}"]`)
    if (!row) return
    row.scrollIntoView({ block: "nearest" })
    if (document.activeElement?.closest("tr[data-row]") && body.current?.contains(document.activeElement))
      row.focus({ preventScroll: true })
  }, [selected])
  const picking = picked.size > 0
  return (
    <table
      aria-label={tab.name}
      className="w-full border-separate border-spacing-0 text-control"
      data-testid="item-table"
      data-tab={tab.name}
    >
      <thead className="sticky top-0 z-10 bg-canvas">
        {table.getHeaderGroups().map((g) => (
          <tr key={g.id}>
            {g.headers.map((h) => {
              const sorted = h.column.getIsSorted()
              return (
                <th key={h.id} className={TH}>
                  {h.isPlaceholder || h.column.id === "pick" ? null : (
                    <button
                      type="button"
                      aria-label={`Sort by ${columnTitle(h.column.id)}`}
                      title={h.column.id}
                      onClick={h.column.getToggleSortingHandler()}
                      className="inline-flex cursor-pointer items-center gap-1 text-caption font-medium whitespace-nowrap text-muted-ink outline-none hover:text-ink"
                    >
                      <table.FlexRender header={h} />
                      {sorted === "asc" ? (
                        <ArrowUpIcon className="size-3" />
                      ) : sorted === "desc" ? (
                        <ArrowDownIcon className="size-3" />
                      ) : null}
                    </button>
                  )}
                </th>
              )
            })}
          </tr>
        ))}
      </thead>
      <tbody ref={body}>
        {shown.map((r) => {
          const row = r.original
          const isSel = row.id !== null && selected === row.id
          const isPicked = row.id !== null && picked.has(row.id)
          return (
            <tr
              key={row.key}
              tabIndex={r === cursor ? 0 : -1}
              aria-current={isSel || undefined}
              aria-selected={isPicked}
              // Shift picks a range of rows, not a range of text.
              onMouseDown={(e) => {
                if (e.shiftKey) e.preventDefault()
              }}
              onClick={(e) => {
                if (!row.item) return
                if (picking || e.metaKey || e.ctrlKey || e.shiftKey) onPick(row, e.shiftKey)
                else onOpen(row.item)
              }}
              onKeyDown={(e) => {
                if (e.target !== e.currentTarget || (e.key !== "Enter" && e.key !== " ") || !row.item) return
                e.preventDefault()
                onOpen(row.item)
              }}
              className="group cursor-pointer outline-none data-reviewed:text-muted-ink focus-visible:[&>td]:bg-surface-2/60 focus-visible:[&>td:first-child]:shadow-cursor [&>td]:transition-colors [&>td]:duration-150 hover:[&>td]:bg-surface-2/60 data-picked:[&>td]:bg-good-tint data-selected:[&>td]:bg-surface-2"
              data-testid="data-row"
              data-row={row.key}
              data-item={row.id ?? undefined}
              data-selected={isSel || undefined}
              data-picked={isPicked || undefined}
              data-picking={picking || undefined}
              data-reviewed={row.item?.review?.action ?? undefined}
            >
              <td className={TD}>
                <Pick row={row} picked={isPicked} onPick={(shift) => onPick(row, shift)} />
              </td>
              {columns.map((c) => {
                const i = tab.columns.indexOf(c)
                return (
                  <td key={c} className={TD}>
                    <Cell
                      row={row}
                      value={row.cells[i] ?? null}
                      field={tab.fields[i] ?? null}
                      names={names}
                    />
                  </td>
                )
              })}
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

const SKELETON_COLUMNS = ["w-20", "w-28", "w-16", "w-24"]
const SKELETON_ROWS = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]

export function WorkbookTableSkeleton() {
  return (
    <table
      aria-hidden
      className="w-full border-separate border-spacing-0 text-control"
      data-testid="item-table-loading"
    >
      <thead>
        <tr>
          <th className={TH} />
          {SKELETON_COLUMNS.map((w) => (
            <th key={w} className={`${TH} text-caption`}>
              <TextSkeleton className="w-14" />
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {SKELETON_ROWS.map((r) => (
          <tr key={r}>
            <td className={TD} />
            {SKELETON_COLUMNS.map((w) => (
              <td key={w} className={TD}>
                <TextSkeleton className={w} />
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  )
}
