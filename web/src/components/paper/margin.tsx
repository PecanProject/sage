import { CheckIcon } from "@phosphor-icons/react"
import { type CSSProperties, type ReactNode, useEffect, useRef, useState } from "react"
import { ItemLine, Line } from "@/components/paper/card-parts"
import { ItemExcerpt, ItemFields, ItemFooter, ItemPanel, ItemTitle } from "@/components/paper/item-panel"
import { TableRows } from "@/components/paper/table-rows"
import { StatusShape } from "@/components/status-mark"
import { Button } from "@/components/ui/button"
import { Fold } from "@/components/ui/fold"
import { Segmented } from "@/components/ui/segmented"
import { Skeleton, TextSkeleton } from "@/components/ui/skeleton"
import { Tip } from "@/components/ui/tooltip"
import { type Status, several } from "@/lib/format"
import { type Box, type Item, needsLook } from "@/lib/items"
import type { KeyHint } from "@/lib/key-hint"
import { GAP, layout } from "@/lib/margin"
import { useSize } from "@/lib/media"
import type { PageTops } from "@/lib/pages"
import { type Reading, readTable } from "@/lib/reading"
import type { Edits } from "@/lib/review"
import { cardTitle, type Unit } from "@/lib/units"
import { cn } from "@/lib/utils"
import { placementOf, type Tab } from "@/lib/workbook"

export const MARGIN_WIDTH = 380
/** A collapsed card's height before it has been measured. */
const COLLAPSED = 32

export type Filter = "check" | "all"

export type ReviewActions = {
  busy: boolean
  accept: (item: Item) => void
  edit: (item: Item, edits: Edits) => void
  /** Rejects with a reason. */
  reject: (items: Item[], note: string) => void
  /** A comment on the items, or on one field of them; it changes no decision. */
  comment: (items: Item[], note: string, field: string | null) => void
  /** Change or remove a comment, by its id, on every item it was left on. */
  editComment: (id: string, note: string) => void
  removeComment: (id: string) => void
  /** The comment open for editing, and opening one on an item it was left on, or closing it. */
  commentOpen: string | null
  openComment: (item: Item, id: string) => void
  closeComment: () => void
  acceptMany: (items: Item[]) => void
  /** Reasons to offer when rejecting, latest first. */
  reasons: string[]
}

/** Everything the margin shows that doesn't come from the PDF's geometry. */
export type MarginProps = {
  items: Item[]
  units: Unit[]
  filter: Filter
  active: Item | null
  boxes: Record<string, Box>
  /** The workbook, whose row for an item is what its card shows. */
  tabs: Tab[]
  actions: ReviewActions
  hint: KeyHint
  /** What to offer once nothing is left to check: the Export menu. */
  done: ReactNode
  /** Ask SAGE, kept at the margin's foot. */
  ask: ReactNode
  onFilter: (f: Filter) => void
  onActivate: (id: string) => void
  onSource: () => void
}

function useHeights() {
  const [heights, setHeights] = useState<Record<string, number>>({})
  const [observer] = useState(
    () =>
      new ResizeObserver((entries) =>
        setHeights((h) => {
          let next = h
          for (const e of entries) {
            const key = (e.target as HTMLElement).dataset.unit
            const px = Math.round(e.borderBoxSize[0]?.blockSize ?? e.contentRect.height)
            if (key && next[key] !== px) next = { ...next, [key]: px }
          }
          return next
        }),
      ),
  )
  useEffect(() => () => observer.disconnect(), [observer])
  const observe = (el: HTMLElement | null) => {
    if (!el) return
    observer.observe(el)
    return () => observer.unobserve(el)
  }
  return { heights, observe }
}

/** The header's height, with its padding and border, once it has been laid out. */
const headerBox = (_el: HTMLElement, e?: ResizeObserverEntry) =>
  e ? Math.round(e.borderBoxSize[0]?.blockSize ?? 0) : 0

/** Where a unit wants its card: level with the top of its source, in px from the top of the page column. */
function want(unit: Unit, tops: PageTops): number {
  if (!unit.at) return 0
  const page = tops.get(unit.at.page)
  return page ? page.top + unit.at.y * page.scale - 6 : 0
}

export function Margin({
  tops,
  minHeight,
  hover,
  onHover,
  ...props
}: MarginProps & {
  tops: PageTops
  /** The page column's height, so the margin is never shorter than the paper. */
  minHeight: number
  hover: string | null
  onHover: (id: string | null) => void
}) {
  const { units, active } = props
  const { heights, observe } = useHeights()
  const headerRef = useRef<HTMLDivElement>(null)
  const headerHeight = useSize(headerRef, headerBox)
  const at = active ? units.findIndex((u) => u.items.includes(active)) : -1
  const height = (u: Unit) => heights[u.key] ?? COLLAPSED
  const positions = layout(
    units.map((u) => want(u, tops)),
    units.map(height),
    at,
    headerHeight + GAP,
  )
  const last = units.length - 1
  const bottom = last >= 0 ? (positions[last] ?? 0) + height(units[last] as Unit) + 48 : 0
  return (
    <aside
      className="relative flex shrink-0 flex-col"
      style={{ width: MARGIN_WIDTH, minHeight: Math.max(minHeight, bottom) }}
      data-testid="margin"
      data-filter={props.filter}
    >
      <div ref={headerRef} className="sticky top-0 z-30 bg-paper-canvas pb-2" data-testid="margin-header">
        <MarginHeader {...props} />
      </div>
      <div className="absolute inset-x-0 top-0">
        {units.map((u, i) => (
          <section
            key={u.key}
            ref={observe}
            data-unit={u.key}
            data-top={positions[i] ?? 0}
            data-open={i === at || undefined}
            className="absolute inset-x-0 transition-transform duration-150 ease-out"
            style={{ transform: `translateY(${positions[i] ?? 0}px)`, zIndex: i === at ? 20 : 10 }}
            aria-label={cardTitle(u)}
            onMouseEnter={() => onHover(u.key)}
            onMouseLeave={() => onHover(null)}
            onFocus={() => onHover(u.key)}
            onBlur={() => onHover(null)}
          >
            <UnitCard unit={u} hovered={hover === u.key} {...props} />
          </section>
        ))}
      </div>
      <span className="flex-1" />
      <div className="sticky bottom-0 z-30 space-y-2 bg-paper-canvas pt-2 pb-3">
        {props.ask}
        {props.hint.show ? (
          <p className="text-caption text-muted-ink" data-testid="key-hint">
            j k move · a accept · e edit · r reject · m comment
          </p>
        ) : null}
      </div>
    </aside>
  )
}

export function MarginSkeleton() {
  return (
    <aside className="shrink-0" style={{ width: MARGIN_WIDTH }} aria-hidden data-testid="margin-loading">
      <div className="pb-2">
        <div className="space-y-3 border-b border-line pb-3">
          <div>
            <div className="h-1 rounded-full bg-line" />
            <div className="pt-1.5 text-caption">
              <TextSkeleton className="w-24 bg-line" />
            </div>
          </div>
          <Skeleton className="h-control-sm w-[136px] bg-line" />
        </div>
      </div>
      <div className="flex flex-col" style={{ gap: GAP }}>
        {["w-56", "w-40", "w-48", "w-32", "w-44"].map((w) => (
          <div key={w} className="flex h-control-sm items-center gap-2.5 px-2 text-value">
            <span className="size-3.5 shrink-0 animate-pulse rounded-full bg-line" />
            <TextSkeleton className={cn(w, "bg-line")} />
          </div>
        ))}
      </div>
    </aside>
  )
}

function MarginHeader({ items, filter, actions, done, onFilter }: MarginProps) {
  const reviewed = items.filter((i) => i.review !== null).length
  const left = items.filter(needsLook).length
  const share = items.length ? reviewed / items.length : 0
  return (
    <div className="space-y-3 border-b border-line pb-3">
      <div>
        <div
          className="h-1 overflow-hidden rounded-full bg-line"
          role="progressbar"
          aria-label="Reviewed"
          aria-valuemin={0}
          aria-valuemax={items.length}
          aria-valuenow={reviewed}
        >
          <div
            className="h-full origin-left bg-sage transition-transform duration-200 ease-out"
            style={{ transform: `scaleX(${share})` }}
          />
        </div>
        <p
          className="pt-1.5 text-caption text-muted-ink tabular-nums"
          data-testid="review-progress"
          data-reviewed={reviewed}
        >
          {reviewed} of {items.length} reviewed
          {left ? ` · ${left} need you` : null}
        </p>
      </div>
      {left === 0 ? (
        <div className="flex items-center justify-between" data-testid="all-checked">
          <span className="flex items-center gap-2 text-value">
            <CheckIcon weight="bold" className="size-4 text-good" aria-hidden />
            All checked
          </span>
          {done}
        </div>
      ) : null}
      <div className="flex items-center justify-between gap-3">
        <Segmented
          label="Show"
          value={filter}
          onChange={onFilter}
          className="h-control-sm"
          options={[
            { value: "check", label: "To check", testid: "filter-to-check" },
            { value: "all", label: "All", testid: "filter-all" },
          ]}
        />
        <AcceptGood items={items} actions={actions} />
      </div>
    </div>
  )
}

/** Accepts every item SAGE checked that has no review yet, in one call. Shown only while there is one. */
export function AcceptGood({ items, actions }: { items: Item[]; actions: ReviewActions }) {
  const good = items.filter((i) => i.review === null && i.status === "verified")
  if (good.length === 0) return null
  return (
    <Tip label={`Accept the ${several(good.length, "item")} SAGE matched to the paper`}>
      <Button
        variant="text"
        className="shrink-0 text-label"
        disabled={actions.busy}
        onClick={() => actions.acceptMany(good)}
        data-testid="accept-looks-good"
      >
        Accept {good.length} SAGE checked
      </Button>
    </Tip>
  )
}

/** An item's review in a card: its title, when the card has none above it, then its facts and buttons. */
function Review({
  item,
  props,
  title,
  own,
  excerpt,
}: {
  item: Item
  props: MarginProps
  title?: ReactNode
  /** The columns to show up front; see `ItemPanel`. */
  own?: string[]
  excerpt?: boolean
}) {
  return (
    <ItemPanel
      item={item}
      placement={placementOf(item, props.tabs)}
      own={own}
      excerpt={excerpt}
      boxes={props.boxes}
      actions={props.actions}
      onShow={props.onSource}
      onKey={props.hint.used}
    >
      {title}
      <div
        className="-mx-3 border-t border-line"
        style={{ "--panel-x": "12px" } as CSSProperties}
        data-testid="item-detail"
        data-item={item.id}
      >
        <ItemFields />
      </div>
      <ItemFooter />
    </ItemPanel>
  )
}

/** A unit's card: open, with the item's review, or closed to one line that the pointer or the PDF's hover lights. */
function Card({
  children,
  unit,
  open = false,
  hovered = false,
}: {
  children: ReactNode
  unit: Unit
  open?: boolean
  hovered?: boolean
}) {
  return (
    <article
      className={
        open
          ? "space-y-2 rounded-card border border-sage bg-surface p-3"
          : cn("rounded-control transition-colors duration-150 hover:bg-surface", hovered && "bg-surface")
      }
      data-testid="card"
      data-kind={unit.kind}
      data-unit={unit.key}
      data-active={open || undefined}
      data-hover={hovered || undefined}
    >
      {children}
    </article>
  )
}

type CardProps = MarginProps & { unit: Unit; hovered: boolean }

function UnitCard(props: CardProps) {
  return props.unit.kind === "item" ? <ItemCard {...props} /> : <ClusterCard {...props} />
}

function ItemCard({ unit, hovered, ...props }: CardProps) {
  const item = unit.items[0] as Item
  if (props.active?.id !== item.id)
    return (
      <Card unit={unit} hovered={hovered}>
        <ItemLine item={item} onClick={() => props.onActivate(item.id)} />
      </Card>
    )
  return (
    <Card unit={unit} open>
      <Review
        item={item}
        props={props}
        excerpt
        title={
          <>
            <ItemTitle />
            <ItemExcerpt />
          </>
        }
      />
    </Card>
  )
}

/** A table's or the unplaced items' card. Open, a table shows how SAGE read it, then the cells left to check, each
 * opening in place; Accept table takes the rest. */
function ClusterCard({ unit, hovered, ...props }: CardProps) {
  const open = props.active && unit.items.includes(props.active) ? props.active : null
  const head = <ClusterLine unit={unit} onOpen={props.onActivate} />
  if (!open)
    return (
      <Card unit={unit} hovered={hovered}>
        {head}
      </Card>
    )
  const table = unit.kind === "table"
  const unreviewed = unit.all.filter((i) => i.review === null)
  const reading = table ? readTable(unit, props.tabs) : null
  const cells = reading ? unit.all.filter((i) => mapped(reading).has(i.id)) : []
  return (
    <Card unit={unit} open>
      <div className="-mx-2 -mt-1 flex items-center gap-2">
        <div className="min-w-0 flex-1">{head}</div>
        {unreviewed.length ? (
          <Tip label="Shift A">
            <Button
              size="compact"
              variant="ghost"
              className="h-7"
              disabled={props.actions.busy}
              onClick={() => props.actions.acceptMany(unreviewed)}
              data-testid={table ? "accept-table" : "accept-cluster"}
            >
              {table ? "Accept table" : "Accept all"}
            </Button>
          </Tip>
        ) : null}
      </div>
      {reading && unit.table && cells.includes(open) ? (
        <TableRows
          table={unit.table}
          reading={reading}
          open={open}
          quiet={props.filter === "check"}
          cell={(item, own) => <Review item={item} props={props} own={own} />}
          onActivate={props.onActivate}
          onAccept={props.actions.acceptMany}
        />
      ) : null}
      <ClusterList unit={unit} skip={mapped(reading)} open={open} props={props} />
    </Card>
  )
}

/** The items a table's rows show, which the list under them leaves out. */
const mapped = (reading: Reading | null) =>
  new Set(reading?.rows.flatMap((r) => r.cells.flatMap((c) => (c ? [c.item.id] : []))) ?? [])

/** A cluster's items, one line each, the open one with its review under it. Under a table's map, these are the
 * table's other records, like the variables its rows name. */
function ClusterList({
  unit,
  skip,
  open,
  props,
}: {
  unit: Unit
  skip: Set<string>
  open: Item
  props: MarginProps
}) {
  const items = unit.items.filter((i) => !skip.has(i.id))
  if (items.length === 0) return null
  const list = (
    <ul className="-mx-2">
      {items.map((item) => (
        <li key={item.id}>
          <ItemLine item={item} onClick={() => props.onActivate(item.id)} />
          {item.id === open.id ? (
            <div className="mx-2 space-y-2 pt-1 pb-2" data-testid="cluster-open">
              <Review item={item} props={props} />
            </div>
          ) : null}
        </li>
      ))}
    </ul>
  )
  if (skip.size === 0) return list
  // Under a table, its other records wait behind a fold unless one needs a look or is open.
  const quiet = !items.some((i) => needsLook(i) || i.id === open.id)
  return (
    <section className="space-y-1 border-t border-line pt-3">
      {quiet ? (
        <Fold label={`Also on this table (${items.length})`} className="pt-1.5">
          {list}
        </Fold>
      ) : (
        <>
          <h4 className="text-caption text-muted-ink">Also on this table</h4>
          {list}
        </>
      )}
    </section>
  )
}

function ClusterLine({ unit, onOpen }: { unit: Unit; onOpen: (id: string) => void }) {
  const left = unit.all.filter(needsLook)
  const status: Status = left.some((i) => i.status === "flagged")
    ? "flagged"
    : left.length
      ? "unresolved"
      : "verified"
  const first = left[0] ?? unit.items[0]
  return (
    <Line
      mark={
        left.length ? (
          <StatusShape status={status} />
        ) : (
          <CheckIcon weight="bold" className="size-3.5 shrink-0 text-muted-ink" aria-label="Reviewed" />
        )
      }
      label={cardTitle(unit)}
      note={left.length ? `${left.length} to check` : null}
      muted={left.length === 0}
      onClick={() => first && onOpen(first.id)}
      data-testid="cluster"
      data-unit={unit.key}
    />
  )
}
