import {
  lazy,
  type ReactNode,
  Suspense,
  useEffect,
  useEffectEvent,
  useLayoutEffect,
  useRef,
  useState,
} from "react"
import { MARGIN_WIDTH, Margin, type MarginProps, MarginSkeleton } from "@/components/paper/margin"
import type { Region } from "@/components/pdf-viewer"
import { Button } from "@/components/ui/button"
import { PageSkeleton } from "@/components/ui/skeleton"
import { Tip } from "@/components/ui/tooltip"
import { highlights } from "@/lib/evidence"
import type { Box, Item } from "@/lib/items"
import { useSize } from "@/lib/media"
import { PAGE_GAP, type PageSize, pageTops } from "@/lib/pages"
import { loadPdfViewer } from "@/lib/pdf-chunk"
import { usePdfZoom } from "@/lib/pdf-zoom"
import type { Unit } from "@/lib/units"

// pdf.js is most of the bundle; the bar, the status line and the margin paint before it arrives. The paper route
// starts the download as it opens (or on hover), alongside the API calls instead of after them.
const PdfPages = lazy(loadPdfViewer)

const GUTTER = 32
const FIT_MAX = 1040
const INSET = 16

/** The pane's width: its client width at first, then its content box's, in whole pixels. */
const paneWidth = (el: HTMLElement, e?: ResizeObserverEntry) =>
  e ? Math.floor(e.contentRect.width) : el.clientWidth

function centre(el: HTMLElement, span: [number, number], highlight: [number, number]): number {
  const header = el.querySelector<HTMLElement>('[data-testid="margin-header"]')?.offsetHeight ?? 0
  const footer = el.querySelector<HTMLElement>('[data-testid="key-hint"]')?.offsetHeight ?? 0
  const from = header + INSET
  const to = el.clientHeight - footer - INSET
  const [s0, s1] = span
  if (s1 - s0 <= to - from) return (s0 + s1) / 2 - (from + to) / 2
  const [m0, m1] = highlight
  if (m1 - m0 > to - from) return m0 - from
  return Math.max(s0 - from, m1 - to)
}

function across(el: HTMLElement, highlight: [number, number], cell: [number, number] | null): number {
  const margin = el.querySelector<HTMLElement>('[data-testid="margin-slot"]')?.offsetWidth ?? 0
  const room = el.clientWidth - margin
  const fits = ([m0, m1]: [number, number]) => m1 - m0 <= room - 2 * INSET
  const [m0, m1] = fits(highlight) || !cell ? highlight : cell
  if (m0 >= el.scrollLeft + INSET && m1 <= el.scrollLeft + room - INSET) return el.scrollLeft
  return fits([m0, m1]) ? (m0 + m1) / 2 - room / 2 : m0 - INSET
}

function useFollow(
  scroller: React.RefObject<HTMLElement | null>,
  column: React.RefObject<HTMLElement | null>,
  pages: PageSize[] | undefined,
  width: number,
  follow: { source: Region | null; target: Region | null; top: boolean },
  key: string,
) {
  const run = useEffectEvent(() => {
    const el = scroller.current
    const col = column.current
    if (!el || !col) return
    if (follow.top) {
      el.scrollTo({ top: 0, behavior: "smooth" })
      return
    }
    const { source, target } = follow
    const tops = pageTops(pages ?? [], width)
    const at = source ? tops.get(source.page) : undefined
    if (!at || !source) return
    const y = (b: Region, i: 1 | 3) => (tops.get(b.page)?.top ?? 0) + (b.bbox[i] ?? 0) * at.scale
    const tall = y(source, 3) - y(source, 1) > el.clientHeight * 0.6
    const focus = tall && target ? target : source
    const mark: [number, number] = [y(focus, 1), y(focus, 3)]
    const card = el.querySelector<HTMLElement>("section[data-open]")
    const cardTop = Number(card?.dataset.top ?? 0)
    const span: [number, number] = card
      ? [Math.min(mark[0], cardTop), Math.max(mark[1], cardTop + card.offsetHeight)]
      : mark
    const next = Math.max(0, Math.round(centre(el, span, mark)))
    const left = col.getBoundingClientRect().left - el.getBoundingClientRect().left + el.scrollLeft
    const x = (b: Region): [number, number] => [
      left + (b.bbox[0] ?? 0) * at.scale,
      left + (b.bbox[2] ?? 0) * at.scale,
    ]
    const side = Math.max(0, Math.round(across(el, x(focus), target ? x(target) : null)))
    if (Math.abs(next - el.scrollTop) < 8 && Math.abs(side - el.scrollLeft) < 8) return
    el.scrollTo({ top: next, left: side, behavior: "smooth" })
  })
  // biome-ignore lint/correctness/useExhaustiveDependencies: `key` says when to follow; the rest is read fresh
  useLayoutEffect(() => {
    if (pages) run()
  }, [key, pages])
}

function useMiddlePage(
  scroller: React.RefObject<HTMLElement | null>,
  pages: PageSize[] | undefined,
  width: number,
): number {
  const [page, setPage] = useState(1)
  const update = useEffectEvent(() => {
    const el = scroller.current
    if (!el || !pages?.length) return
    const middle = el.scrollTop + el.clientHeight / 2
    let at = pages[0]?.number ?? 1
    for (const [n, t] of pageTops(pages, width)) if (t.top <= middle) at = n
    setPage(at)
  })
  useEffect(() => {
    const el = scroller.current
    if (!el) return
    const on = () => update()
    el.addEventListener("scroll", on, { passive: true })
    return () => el.removeEventListener("scroll", on)
  }, [scroller])
  return page
}

function follow(active: Item, units: Unit[], boxes: Record<string, Box>) {
  const unit = units.find((u) => u.items.includes(active))
  const box = active.primary ? boxes[active.primary] : undefined
  const table = unit?.kind === "table" ? unit.table : null
  const target = box ? { page: box.page, bbox: box.bbox } : null
  const region = table ? { page: table.page, bbox: table.region } : null
  return {
    target,
    region,
    source: region ?? target,
    top: unit?.kind === "unlocated",
    focus: new Set(unit && unit.kind !== "item" ? unit.items.map((i) => i.id) : [active.id]),
  }
}

/** Hovering a card brightens its highlights, and hovering a highlight marks its card. */
function useLinkedHover(units: Unit[]) {
  const [unit, set] = useState<string | null>(null)
  const unitOf = new Map(units.flatMap((u) => u.items.map((i) => [i.id, u.key] as const)))
  return {
    unit,
    set,
    items: new Set(units.find((u) => u.key === unit)?.items.map((i) => i.id) ?? []),
    fromHighlight: (id: string | null) => set(id ? (unitOf.get(id) ?? null) : null),
  }
}

const NOTHING = { target: null, region: null, source: null, top: false, focus: null }

/** Every highlight: the active item's (or cluster's) and the hovered card's bright, the rest faded. */
function marks(
  items: Item[],
  boxes: Record<string, Box>,
  active: Item | null,
  focus: ReadonlySet<string> | null,
  hovered: ReadonlySet<string>,
) {
  const bright = focus ? new Set([...focus, ...hovered]) : null
  return highlights(items, boxes, focus ? (active?.id ?? null) : null, bright)
}

function FitButton({ zoom, onFit }: { zoom: number; onFit: () => void }) {
  if (zoom === 1) return null
  return (
    <Tip label={`${Math.round(zoom * 100)}% · 0`}>
      <Button
        variant="outline"
        size="compact"
        className="absolute top-2 right-4 z-40 h-7 shadow-card"
        onClick={onFit}
        data-testid="fit-page"
      >
        Fit page
      </Button>
    </Tip>
  )
}

function MarginSlot({
  loading,
  margin,
  pages,
  width,
  link,
}: {
  loading: boolean
  margin: MarginProps | null
  pages: PageSize[] | undefined
  width: number
  link: ReturnType<typeof useLinkedHover>
}) {
  if (loading) return <MarginSkeleton />
  if (!margin) return null
  const column = (pages ?? []).reduce((h, p) => h + p.height * (width / p.width) + PAGE_GAP, 0)
  return (
    <Margin
      {...margin}
      tops={pageTops(pages ?? [], width)}
      minHeight={column}
      hover={link.unit}
      onHover={link.set}
    />
  )
}

/** The paper with, in Check mode, the Docs-style margin beside it in the same scroll container, so cards move with
 * the pages. Read mode is the same pages with every highlight on and a tooltip on each. */
export function PaperCanvas({
  mode,
  loading,
  url,
  pages,
  items,
  boxes,
  units,
  active,
  followKey,
  overlay,
  margin,
  zoom,
  onZoom,
  onHighlight,
}: {
  mode: "check" | "read"
  loading: boolean
  url: string
  pages: PageSize[] | undefined
  items: Item[]
  boxes: Record<string, Box>
  /** Every card, for following the active item; the margin draws the ones the filter shows. */
  units: Unit[]
  active: Item | null
  /** Changes whenever the PDF should bring the active item into view again. */
  followKey: string
  overlay?: ReactNode
  margin: MarginProps | null
  zoom: number
  onZoom: (zoom: number) => void
  onHighlight: (id: string) => void
}) {
  const scroller = useRef<HTMLDivElement>(null)
  const column = useRef<HTMLDivElement>(null)
  const pane = useSize(scroller, paneWidth)
  const check = mode === "check"
  const withMargin = check && (loading || margin !== null)
  const on = check && active ? follow(active, units, boxes) : NOTHING
  const room = Math.max(320, pane - 2 * GUTTER - (withMargin ? MARGIN_WIDTH + GUTTER : 0))
  const fit = Math.min(FIT_MAX, room)
  const width = Math.round(fit * zoom)
  const zooming = usePdfZoom({ scroller, column, pages, width, zoom, onZoom })
  const [loaded, setLoaded] = useState(false)
  useFollow(scroller, column, loaded ? pages : undefined, width, on, followKey)
  const middle = useMiddlePage(scroller, pages, width)
  const link = useLinkedHover(units)
  const label = new Map(items.map((i) => [i.id, i.label]))

  return (
    <div className="relative flex h-full min-w-0 flex-col bg-paper-canvas">
      <FitButton zoom={zoom} onFit={zooming.fitPage} />
      <div
        ref={scroller}
        className="mt-2 min-h-0 flex-1 overflow-auto"
        data-testid="pdf-pane"
        data-zoom={zoom}
        data-mode={mode}
      >
        <div className="mx-auto flex w-fit pb-16 pl-gutter">
          <div ref={column} style={{ width }} className="shrink-0" data-testid="pdf-column">
            {pane > 0 ? (
              <Suspense fallback={<PageSkeleton width={width} />}>
                <PdfPages
                  url={url}
                  pages={pages ?? []}
                  width={width}
                  highlights={marks(items, boxes, active, on.focus, link.items)}
                  region={on.region}
                  middle={middle}
                  hovered={link.items}
                  onHighlight={onHighlight}
                  onHover={check ? link.fromHighlight : undefined}
                  onLoad={() => setLoaded(true)}
                  tip={check ? undefined : (id) => label.get(id) ?? null}
                />
              </Suspense>
            ) : (
              <PageSkeleton width={fit} />
            )}
          </div>
          {withMargin ? (
            <div className="sticky right-0 z-20 shrink-0 bg-paper-canvas px-gutter" data-testid="margin-slot">
              <MarginSlot loading={loading} margin={margin} pages={pages} width={width} link={link} />
            </div>
          ) : (
            <span className="w-gutter shrink-0" />
          )}
        </div>
      </div>
      <div className="pointer-events-none absolute inset-x-0 top-2 z-40 flex justify-center px-gutter">
        {overlay}
      </div>
    </div>
  )
}
