import { useRef, useState } from "react"
import { type AskRun, AskSage, SelectionBar } from "@/components/paper/ask"
import { ItemSheet } from "@/components/paper/item-sheet"
import { AcceptGood, type ReviewActions } from "@/components/paper/margin"
import { WorkbookTable, WorkbookTableSkeleton } from "@/components/paper/workbook-table"
import { Button } from "@/components/ui/button"
import { TextSkeleton } from "@/components/ui/skeleton"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { type Box, type Item, needsLook } from "@/lib/items"
import { useKeys } from "@/lib/keys"
import {
  namesOf,
  placementOf,
  rowsOf,
  showCell,
  TAB_OF,
  type Tab,
  tabLabel,
  type WorkbookRow,
} from "@/lib/workbook"

/** The columns shown by default: the ones a reviewer reads first, less any that no row fills and no field needs a
 * look at. All columns shows the workbook's every column, in its order. */
function columnsFor(tab: Tab, rows: WorkbookRow[], all: boolean): string[] {
  if (all) return tab.columns
  return tab.review.filter((c) => {
    const i = tab.columns.indexOf(c)
    const field = tab.fields[i] ?? null
    return rows.some(
      (r) =>
        showCell(r.cells[i] ?? null) !== null ||
        (field !== null && r.item?.rows.some((f) => f.name === field && f.status !== "verified")),
    )
  })
}

/** Rows picked for a shared decision: a short-lived selection, kept to one tab and gone with a reload. Shift picks
 * every row from the last one picked. */
function usePicks(tab: string | undefined, rows: WorkbookRow[]) {
  const [picked, setPicked] = useState<{ tab: string; ids: string[]; last: string | null }>({
    tab: "",
    ids: [],
    last: null,
  })
  const ids = new Set(tab !== undefined && picked.tab === tab ? picked.ids : [])
  const pick = (row: WorkbookRow, shift: boolean) => {
    if (tab === undefined || row.id === null) return
    const next = new Set(ids)
    if (shift && picked.last !== null) {
      const from = rows.findIndex((r) => r.id === picked.last)
      const to = rows.indexOf(row)
      for (const r of rows.slice(Math.min(from, to), Math.max(from, to) + 1)) if (r.id) next.add(r.id)
    } else if (next.has(row.id)) next.delete(row.id)
    else next.add(row.id)
    setPicked({ tab, ids: [...next], last: row.id })
  }
  return { ids, pick, clear: () => setPicked({ tab: "", ids: [], last: null }) }
}

/** The workbook's tabs that have rows, each with what is left to check on it, or else its row count. */
function WorkbookTabs({
  tabs,
  current,
  items,
  onTab,
}: {
  tabs: Tab[]
  current: string | undefined
  items: Map<string, Item>
  onTab: (tab: string) => void
}) {
  return (
    <Tabs
      className="min-w-0 flex-1"
      value={current ?? ""}
      onValueChange={(v) => {
        if (typeof v === "string") onTab(v)
      }}
    >
      <TabsList className="overflow-x-auto px-gutter">
        {tabs.map((t) => {
          const left = new Set(
            rowsOf(t, items).flatMap((r) => (r.item && needsLook(r.item) ? [r.item.id] : [])),
          )
          return (
            <TabsTrigger key={t.name} value={t.name} data-testid={`data-tab-${t.name}`}>
              {tabLabel(t.name)}
              <span className="font-normal text-muted-ink tabular-nums">
                {left.size ? `${left.size} to check` : t.rows.length}
              </span>
            </TabsTrigger>
          )
        })}
      </TabsList>
    </Tabs>
  )
}

/** Data mode is the workbook: the export's tabs, rows and columns, so what a scientist checks here is what the Excel
 * file holds. A row opens its record in the side sheet; picked rows share one decision, comment or request. */
export function DataMode({
  items,
  tabs,
  boxes,
  actions,
  selected,
  tab,
  allColumns,
  ask,
  onSelect,
  onTab,
  onColumns,
  onShow,
}: {
  items: Item[]
  tabs: Tab[]
  boxes: Record<string, Box>
  actions: ReviewActions
  selected: Item | null
  /** The tab picked last, which stays when the sheet closes. The open item's tab wins over it, and Observations,
   * the rows the export is for, is the one shown first. */
  tab: string | undefined
  allColumns: boolean
  ask: AskRun
  onSelect: (id: string | null) => void
  onTab: (tab: string) => void
  onColumns: (all: boolean) => void
  onShow: (id: string) => void
}) {
  const root = useRef<HTMLDivElement>(null)
  const byId = new Map(items.map((i) => [i.id, i]))
  const shown = tabs.filter((t) => t.rows.length > 0)
  const current =
    shown.find((t) => t.name === (selected ? TAB_OF[selected.type] : undefined)) ??
    shown.find((t) => t.name === tab) ??
    shown.find((t) => t.name === "observations") ??
    shown[0]
  const rows = current ? rowsOf(current, byId) : []
  const columns = current ? columnsFor(current, rows, allColumns) : []
  const picks = usePicks(current?.name, rows)
  const pickedItems = [...picks.ids].flatMap((id) => byId.get(id) ?? [])
  const close = () => {
    if (!selected) return
    onSelect(null)
    root.current?.querySelector<HTMLElement>(`tr[data-item="${CSS.escape(selected.id)}"]`)?.focus()
  }
  useKeys(
    { Escape: () => (picks.ids.size ? picks.clear() : close()) },
    selected !== null || picks.ids.size > 0,
  )
  const open = (item: Item) => onSelect(item.id)
  const ofTab = rows.flatMap((r) => r.item ?? [])
  return (
    <div ref={root} className="flex h-full min-h-0 flex-col" data-testid="data-mode">
      <div className="flex items-center gap-4 border-b border-line pr-inset">
        <WorkbookTabs tabs={shown} current={current?.name} items={byId} onTab={onTab} />
        <Button
          variant="text"
          className="shrink-0 text-label text-muted-ink hover:text-ink"
          onClick={() => onColumns(!allColumns)}
          data-testid="all-columns"
        >
          {allColumns ? "Review columns" : "All columns"}
        </Button>
        <AcceptGood items={ofTab} actions={actions} />
      </div>
      <div className="flex min-h-0 flex-1">
        <div className="relative flex min-w-0 flex-1 flex-col">
          <div className="min-h-0 flex-1 scroll-pt-10 scroll-pb-28 overflow-auto pb-28">
            {current ? (
              <WorkbookTable
                tab={current}
                names={namesOf(tabs)}
                rows={rows}
                columns={columns}
                selected={selected?.id ?? null}
                picked={picks.ids}
                onOpen={open}
                onPick={picks.pick}
              />
            ) : null}
          </div>
          <div className="pointer-events-none absolute inset-x-0 bottom-4 flex justify-center px-gutter [&>*]:pointer-events-auto">
            {pickedItems.length ? (
              <SelectionBar
                items={pickedItems}
                actions={actions}
                askDisabled={ask.disabled}
                onAsk={ask.send}
                onClear={picks.clear}
              />
            ) : (
              <AskSage
                items={items}
                tabs={tabs}
                ask={ask}
                actions={actions}
                onOpen={onSelect}
                className="w-[min(560px,100%)]"
              />
            )}
          </div>
        </div>
        {selected ? (
          <ItemSheet
            item={selected}
            placement={placementOf(selected, tabs)}
            boxes={boxes}
            actions={actions}
            onClose={close}
            onShow={() => onShow(selected.id)}
          />
        ) : null}
      </div>
    </div>
  )
}

export function DataModeSkeleton() {
  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="data-mode-loading">
      <div className="border-b border-line">
        <div className="flex h-11 items-center gap-6 px-gutter text-label">
          {["w-24", "w-20", "w-28"].map((w) => (
            <TextSkeleton key={w} className={w} />
          ))}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-hidden">
        <WorkbookTableSkeleton />
      </div>
    </div>
  )
}
