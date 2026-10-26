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
import { TextSkeleton } from "@/components/ui/skeleton"
import { Tip } from "@/components/ui/tooltip"
import type { Column } from "@/lib/columns"
import { REVIEW_LABEL, STATUS_LABEL } from "@/lib/format"
import type { Item } from "@/lib/items"
import { useKeys } from "@/lib/keys"

const features = tableFeatures({
  rowSortingFeature,
  sortedRowModel: createSortedRowModel(),
  sortFns: { alphanumeric: sortFn_alphanumeric },
})
const helper = createColumnHelper<typeof features, Item>()
const TD = "h-11 border-b border-line px-3 align-middle whitespace-nowrap first:pl-gutter"
const TH = "h-10 border-b border-line px-3 text-left align-middle first:w-10 first:pl-gutter"
/** `max-w-0` lets value columns shrink below their text, so the table fits its pane and truncates instead of
 * scrolling sideways. */
const VALUE_TD = `${TD} max-w-0`

function RowMark({ item }: { item: Item }) {
  const r = item.review
  if (!r) return null
  return (
    <Tip label={REVIEW_LABEL[r.action]}>
      <span
        role="img"
        aria-label={REVIEW_LABEL[r.action]}
        className="flex size-4 items-center justify-center text-muted-ink"
        data-testid="row-review"
      >
        <ReviewIcon action={r.action} />
      </span>
    </Tip>
  )
}

function ValueCell({ item, col }: { item: Item; col: Column }) {
  const v = col.value(item)
  if (v.text === null)
    return (
      <span data-testid="data-cell" data-status={v.status}>
        <span className="text-muted-ink" aria-hidden>
          –
        </span>
        <span className="sr-only">Not given</span>
      </span>
    )
  return (
    <span
      className="flex max-w-[320px] items-center gap-1.5 tabular-nums"
      data-testid="data-cell"
      data-status={v.status}
      title={v.text}
    >
      {v.status === "verified" || item.review ? (
        <span className="truncate">{v.text}</span>
      ) : (
        <ToCheck className="min-w-0 truncate">
          {v.text}
          <span className="sr-only">, {STATUS_LABEL[v.status]}</span>
        </ToCheck>
      )}
    </span>
  )
}

const defsFor = (columns: Column[]) =>
  helper.columns([
    helper.display({ id: "mark", header: "" }),
    ...columns.map((col) =>
      helper.accessor((i) => col.value(i).text ?? "", {
        id: col.key,
        header: col.title,
        sortFn: "alphanumeric",
      }),
    ),
  ])

export function ItemTable({
  label,
  items,
  columns,
  selected,
  onOpen,
}: {
  label: string
  items: Item[]
  columns: Column[]
  selected: string | null
  onOpen: (item: Item) => void
}) {
  const body = useRef<HTMLTableSectionElement>(null)
  const table = useTable({ features, columns: defsFor(columns), data: items, enableSortingRemoval: true })
  const rows = table.getRowModel().rows
  const cursor = rows.find((r) => r.original.id === selected) ?? rows[0]
  const move = (step: 1 | -1, e: KeyboardEvent) => {
    const focused = document.activeElement?.closest("tr[data-item]")?.getAttribute("data-item")
    const at = rows.findIndex((r) => r.original.id === (focused ?? selected))
    const next = rows[at === -1 ? 0 : Math.min(Math.max(at + step, 0), rows.length - 1)]
    if (!next) return
    e.preventDefault()
    if (selected) onOpen(next.original)
    body.current?.querySelector<HTMLElement>(`tr[data-item="${CSS.escape(next.original.id)}"]`)?.focus()
  }
  useKeys({ j: (e) => move(1, e), k: (e) => move(-1, e) })
  // However an item gets picked (a click, j/k, a review moving on, the URL, or Check mode handing it over), bring its
  // row into view, and take the keyboard focus with it if a row had it, so the focus mark follows the selection.
  useEffect(() => {
    if (!selected) return
    const row = body.current?.querySelector<HTMLElement>(`tr[data-item="${CSS.escape(selected)}"]`)
    if (!row) return
    row.scrollIntoView({ block: "nearest" })
    if (document.activeElement?.closest("tr[data-item]") && body.current?.contains(document.activeElement))
      row.focus({ preventScroll: true })
  }, [selected])
  return (
    <table
      aria-label={label}
      className="w-full border-separate border-spacing-0 text-control"
      data-testid="item-table"
    >
      <thead className="sticky top-0 z-10 bg-canvas">
        {table.getHeaderGroups().map((g) => (
          <tr key={g.id}>
            {g.headers.map((h) => {
              const sorted = h.column.getIsSorted()
              return (
                <th key={h.id} className={TH}>
                  {h.isPlaceholder || h.column.id === "mark" ? null : (
                    <button
                      type="button"
                      aria-label={`Sort by ${String(h.column.columnDef.header ?? h.column.id)}`}
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
        {rows.map((row) => {
          const isSel = selected === row.original.id
          return (
            <tr
              key={row.id}
              tabIndex={row === cursor ? 0 : -1}
              aria-current={isSel || undefined}
              onClick={() => onOpen(row.original)}
              onKeyDown={(e) => {
                if (e.target !== e.currentTarget || (e.key !== "Enter" && e.key !== " ")) return
                e.preventDefault()
                onOpen(row.original)
              }}
              className="cursor-pointer outline-none data-reviewed:text-muted-ink focus-visible:[&>td]:bg-surface-2/60 focus-visible:[&>td:first-child]:shadow-cursor [&>td]:transition-colors [&>td]:duration-150 hover:[&>td]:bg-surface-2/60 data-selected:[&>td]:bg-surface-2"
              data-testid="data-row"
              data-item={row.original.id}
              data-selected={isSel || undefined}
              data-reviewed={row.original.review?.action ?? undefined}
            >
              <td className={TD}>
                <RowMark item={row.original} />
              </td>
              {columns.map((col) => (
                <td key={col.key} className={VALUE_TD}>
                  <ValueCell item={row.original} col={col} />
                </td>
              ))}
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

const SKELETON_COLUMNS = ["w-20", "w-28", "w-16", "w-24"]
const SKELETON_ROWS = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]

export function ItemTableSkeleton() {
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
