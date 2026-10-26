import { useRef } from "react"
import { ItemTable, ItemTableSkeleton } from "@/components/item-table"
import { ItemSheet } from "@/components/paper/item-sheet"
import { AcceptGood, type ReviewActions } from "@/components/paper/margin"
import { TextSkeleton } from "@/components/ui/skeleton"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { fieldColumns, observationColumns } from "@/lib/columns"
import { type Doc, TYPE_ORDER, TYPE_PLURAL } from "@/lib/format"
import type { Box, Item } from "@/lib/items"
import { useKeys } from "@/lib/keys"

export function DataMode({
  items,
  doc,
  boxes,
  actions,
  selected,
  tab,
  onSelect,
  onTab,
  onShow,
}: {
  items: Item[]
  doc: Doc | undefined
  boxes: Record<string, Box>
  actions: ReviewActions
  selected: Item | null
  /** The tab picked last, which stays when the sheet closes. The open item's type wins over it. */
  tab: string | undefined
  onSelect: (id: string | null) => void
  onTab: (type: string) => void
  onShow: (id: string) => void
}) {
  const root = useRef<HTMLDivElement>(null)
  const byType = Map.groupBy(items, (i) => i.type)
  const types = TYPE_ORDER.filter((t) => byType.has(t))
  const current = selected?.type ?? (tab && byType.has(tab) ? tab : types[0]) ?? ""
  const ofType = byType.get(current) ?? []
  const columns = current === "Observation" ? observationColumns(items, doc) : fieldColumns(ofType)
  const close = () => {
    if (!selected) return
    onSelect(null)
    root.current?.querySelector<HTMLElement>(`tr[data-item="${CSS.escape(selected.id)}"]`)?.focus()
  }
  useKeys({ Escape: close }, selected !== null)
  return (
    <div ref={root} className="flex h-full min-h-0 flex-col" data-testid="data-mode">
      <div className="flex items-center gap-6 border-b border-line pr-inset">
        <Tabs
          className="min-w-0 flex-1"
          value={current}
          onValueChange={(v) => {
            if (typeof v === "string") onTab(v)
          }}
        >
          <TabsList className="overflow-x-auto px-gutter">
            {types.map((t) => (
              <TabsTrigger key={t} value={t} data-testid={`data-tab-${t}`}>
                {TYPE_PLURAL[t] ?? t}
                <span className="font-normal text-muted-ink">{byType.get(t)?.length}</span>
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        <AcceptGood items={items} actions={actions} />
      </div>
      <div className="flex min-h-0 flex-1">
        <div className="relative min-w-0 flex-1 scroll-pt-10 overflow-y-auto pb-16">
          <ItemTable
            label={TYPE_PLURAL[current] ?? current}
            items={ofType}
            columns={columns}
            selected={selected?.id ?? null}
            onOpen={(item) => onSelect(item.id)}
          />
        </div>
        {selected ? (
          <ItemSheet
            item={selected}
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
        <ItemTableSkeleton />
      </div>
    </div>
  )
}
