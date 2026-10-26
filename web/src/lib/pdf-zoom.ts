import { useEffect, useEffectEvent, useLayoutEffect, useRef } from "react"
import { useKeys } from "@/lib/keys"
import { type PageSize, pageTops } from "@/lib/pages"

const ZOOMS = [0.5, 0.67, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5]

const WHEEL_STEP = 40

/** A zoom read from the URL: one of the steps, or undefined for Fit page (1), which the URL leaves out. */
export function zoomFrom(v: unknown): number | undefined {
  const z = Number(v)
  return ZOOMS.includes(z) && z !== 1 ? z : undefined
}

function step(zoom: number, by: 1 | -1): number {
  const at = ZOOMS.indexOf(zoom)
  return ZOOMS[Math.min(Math.max(at + by, 0), ZOOMS.length - 1)] ?? 1
}

type Anchor = { page: number; pageY: number; columnShare: number; paneX: number; paneY: number }

/** Ctrl or ⌘ with the wheel (or a pinch), `+`, `-`, and `0` for Fit page. The paper point under the pointer, or at
 * the pane's centre for a key, stays put on screen while the pages resize. */
export function usePdfZoom({
  scroller,
  column,
  pages,
  width,
  zoom,
  onZoom,
}: {
  scroller: React.RefObject<HTMLElement | null>
  column: React.RefObject<HTMLElement | null>
  pages: PageSize[] | undefined
  width: number
  zoom: number
  onZoom: (zoom: number) => void
}) {
  const anchor = useRef<Anchor | null>(null)
  const wheeled = useRef(0)

  const hold = (clientX?: number, clientY?: number) => {
    const el = scroller.current
    const col = column.current
    if (!el || !col || !pages?.length) return
    const box = el.getBoundingClientRect()
    const paneX = (clientX ?? box.left + el.clientWidth / 2) - box.left
    const paneY = (clientY ?? box.top + el.clientHeight / 2) - box.top
    const y = el.scrollTop + paneY
    let page = pages[0]?.number ?? 1
    const tops = pageTops(pages, width)
    for (const [n, t] of tops) if (t.top <= y) page = n
    const at = tops.get(page)
    if (!at) return
    const x = box.left + paneX - col.getBoundingClientRect().left
    anchor.current = { page, pageY: (y - at.top) / at.scale, columnShare: x / width, paneX, paneY }
  }

  const set = (next: number, clientX?: number, clientY?: number) => {
    if (next === zoom) return
    hold(clientX, clientY)
    onZoom(next)
  }

  const restore = useEffectEvent((width: number) => {
    const a = anchor.current
    const el = scroller.current
    const col = column.current
    anchor.current = null
    if (!a || !el || !col || !pages) return
    const at = pageTops(pages, width).get(a.page)
    if (!at) return
    const left = col.getBoundingClientRect().left - el.getBoundingClientRect().left + el.scrollLeft
    el.scrollTo({
      top: at.top + a.pageY * at.scale - a.paneY,
      left: left + a.columnShare * width - a.paneX,
      behavior: "instant",
    })
  })
  useLayoutEffect(() => restore(width), [width])

  const wheel = useEffectEvent((e: WheelEvent) => {
    if (!e.ctrlKey && !e.metaKey) return
    // The browser would zoom the whole app instead.
    e.preventDefault()
    wheeled.current += e.deltaMode === WheelEvent.DOM_DELTA_PIXEL ? e.deltaY : e.deltaY * WHEEL_STEP
    if (Math.abs(wheeled.current) < WHEEL_STEP) return
    const by = wheeled.current < 0 ? 1 : -1
    wheeled.current = 0
    set(step(zoom, by), e.clientX, e.clientY)
  })
  useEffect(() => {
    const el = scroller.current
    if (!el) return
    const on = (e: WheelEvent) => wheel(e)
    el.addEventListener("wheel", on, { passive: false })
    return () => el.removeEventListener("wheel", on)
  }, [scroller])

  const key = (next: () => number) => (e: KeyboardEvent) => {
    e.preventDefault()
    set(next())
  }
  useKeys({
    Equal: key(() => step(zoom, 1)),
    "Shift+Equal": key(() => step(zoom, 1)),
    Minus: key(() => step(zoom, -1)),
    Digit0: key(() => 1),
  })

  return { fitPage: () => set(1) }
}
