import { CheckIcon } from "@phosphor-icons/react"
import { type CSSProperties, type ReactNode, useEffect, useRef, useState } from "react"
import { Grid, ItemLine, Line, ReviewMark } from "@/components/paper/card-parts"
import { ItemFields, ItemFooter, ItemPanel } from "@/components/paper/item-panel"
import { StatusShape } from "@/components/status-mark"
import { Button } from "@/components/ui/button"
import { Segmented } from "@/components/ui/segmented"
import { Skeleton, TextSkeleton } from "@/components/ui/skeleton"
import { Tip } from "@/components/ui/tooltip"
import type { Status } from "@/lib/format"
import { type Box, type Item, needsLook } from "@/lib/items"
import type { KeyHint } from "@/lib/key-hint"
import { GAP, layout } from "@/lib/margin"
import { useSize } from "@/lib/media"
import type { PageTops } from "@/lib/pages"
import type { Edits } from "@/lib/review"
import { cardTitle, type Unit } from "@/lib/units"
import { cn } from "@/lib/utils"

export const MARGIN_WIDTH = 380
/** A collapsed card's height before it has been measured. */
const COLLAPSED = 32

export type Filter = "check" | "all"

export type ReviewActions = {
  busy: boolean
  accept: (item: Item) => void
  edit: (item: Item, edits: Edits) => void
  reject: (item: Item, note: string) => void
  acceptMany: (items: Item[]) => void
  acceptLooksGood: () => void
}

/** Everything the margin shows that doesn't come from the PDF's geometry. */
export type MarginProps = {
  items: Item[]
  units: Unit[]
  filter: Filter
  active: Item | null
  boxes: Record<string, Box>
  actions: ReviewActions
  hint: KeyHint
  /** What to offer once nothing is left to check: the Export menu. */
  done: ReactNode
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
      {props.hint.show ? (
        <p
          className="sticky bottom-0 z-30 bg-paper-canvas py-2.5 text-caption text-muted-ink"
          data-testid="key-hint"
        >
          j k move · a accept · e edit · r not in paper
        </p>
      ) : null}
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

/** Accepts every "looks good" item with no review yet, in one call. Shown only while there is one. */
export function AcceptGood({ items, actions }: { items: Item[]; actions: ReviewActions }) {
  const good = items.filter((i) => i.review === null && i.status === "verified").length
  if (good === 0) return null
  return (
    <Button
      variant="text"
      className="shrink-0 text-label"
      disabled={actions.busy}
      onClick={actions.acceptLooksGood}
      data-testid="accept-looks-good"
    >
      Accept {good} good
    </Button>
  )
}

function Review({ item, props }: { item: Item; props: MarginProps }) {
  return (
    <ItemPanel
      item={item}
      boxes={props.boxes}
      actions={props.actions}
      onShow={props.onSource}
      onKey={props.hint.used}
    >
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

function UnitCard({ unit, hovered, ...props }: MarginProps & { unit: Unit; hovered: boolean }) {
  const open = props.active && unit.items.includes(props.active) ? props.active : null
  if (unit.kind === "item") {
    const item = unit.items[0] as Item
    if (!open)
      return (
        <Card unit={unit} hovered={hovered}>
          <ItemLine item={item} onClick={() => props.onActivate(item.id)} />
        </Card>
      )
    return (
      <Card unit={unit} open>
        <div className="-mx-2 -mt-1">
          <ItemLine item={item} onClick={() => props.onActivate(item.id)} />
        </div>
        <Review item={item} props={props} />
      </Card>
    )
  }
  const head = <ClusterLine unit={unit} onOpen={props.onActivate} />
  if (!open)
    return (
      <Card unit={unit} hovered={hovered}>
        {head}
      </Card>
    )
  const unreviewed = unit.items.filter((i) => i.review === null)
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
              data-testid={unit.kind === "table" ? "accept-table" : "accept-cluster"}
            >
              {unit.kind === "table" ? "Accept table" : "Accept all"}
            </Button>
          </Tip>
        ) : null}
      </div>
      {unit.kind === "table" ? (
        <>
          <Grid unit={unit} active={open.id} onActivate={props.onActivate} />
          <div className="space-y-2 pt-1">
            <p className="flex items-center gap-2.5">
              <ReviewMark item={open} />
              <span className="min-w-0 flex-1 truncate text-value">{open.label}</span>
            </p>
            <Review item={open} props={props} />
          </div>
        </>
      ) : (
        <ul className="-mx-2">
          {unit.items.map((item) => (
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
      )}
    </Card>
  )
}

function ClusterLine({ unit, onOpen }: { unit: Unit; onOpen: (id: string) => void }) {
  const left = unit.items.filter(needsLook)
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
