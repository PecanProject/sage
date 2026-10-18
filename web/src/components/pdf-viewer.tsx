import { type ReactNode, Suspense, useState } from "react"
import { Document, Page, pdfjs } from "react-pdf"
import { Skeleton } from "@/components/ui/skeleton"
import { Tip } from "@/components/ui/tooltip"
import type { Highlight, Tone } from "@/lib/evidence"
import { PAGE_GAP, type PageSize } from "@/lib/pages"
import { cn } from "@/lib/utils"

pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
).toString()

/** Where pdf.js fetches what a PDF doesn't carry itself. vite.config.ts copies these from pdfjs-dist. */
const PDF_OPTIONS = {
  wasmUrl: "/pdfjs/wasm/",
  standardFontDataUrl: "/pdfjs/standard_fonts/",
  cMapUrl: "/pdfjs/cmaps/",
  iccUrl: "/pdfjs/iccs/",
}

// The page is always white paper, in dark mode too, so the tints multiply onto it.
const TONE: Record<Tone, string> = {
  check: "bg-hl-check/70 mix-blend-multiply",
  good: "bg-hl-good/70 mix-blend-multiply",
  missing: "outline-[1.5px] outline-dashed outline-hl-missing",
  rejected: "outline-1 outline-dashed outline-hl-missing/70",
}

const ACTIVE: Record<Tone, string> = {
  check: "outline-2 outline-offset-1 outline-hl-check-line animate-flash",
  good: "outline-2 outline-offset-1 outline-hl-good-line animate-flash",
  missing: "outline-2 outline-offset-1 outline-dashed outline-hl-ink/60",
  rejected: "outline-2 outline-offset-1 outline-dashed outline-hl-ink/60",
}

const QUOTED_BLOCK: Record<Tone, string> = {
  check:
    "bg-hl-check/25 mix-blend-multiply outline-1 outline-offset-1 outline-hl-check-line/60 animate-flash",
  good: "bg-hl-good/30 mix-blend-multiply outline-1 outline-offset-1 outline-hl-good-line/60 animate-flash",
  missing: ACTIVE.missing,
  rejected: ACTIVE.rejected,
}

const QUOTED_LINE: Record<Tone, string> = {
  check: "bg-hl-check",
  good: "bg-hl-good",
  missing: "bg-hl-missing/40",
  rejected: "bg-hl-missing/40",
}

const MAX_PIXELS = 12_000_000
const PAGES_EACH_SIDE = 2
/** Points a highlight reaches past its block on each side, and past each quoted line. */
const PAD_X = 1.5
const PAD_Y = 1
const LINE_PAD = 1

/** A box on a page, in PDF points. */
export type Region = { page: number; bbox: number[] }

export default function PdfPages({
  url,
  pages,
  width,
  highlights,
  region,
  middle,
  hovered,
  onHighlight,
  onHover,
  onLoad,
  tip,
}: {
  url: string
  pages: PageSize[]
  width: number
  highlights: Highlight[]
  region: Region | null
  /** The page in the middle of the pane. Only it and PAGES_EACH_SIDE pages on each side draw. */
  middle: number
  hovered: ReadonlySet<string>
  onHighlight: (itemId: string) => void
  onHover?: (itemId: string | null) => void
  /** The PDF has loaded and the pages have their full height. */
  onLoad: () => void
  tip?: (itemId: string) => ReactNode
}) {
  const [failed, setFailed] = useState<string | null>(null)
  const byPage = new Map<number, Highlight[]>()
  for (const h of highlights) byPage.set(h.page, [...(byPage.get(h.page) ?? []), h])
  const [file] = useState(() => ({ url }))
  const placeholder = <Skeleton className="rounded-page bg-surface" style={{ width, height: width * 1.3 }} />

  if (failed) return <p className="py-16 text-center text-muted-ink">Couldn't show the PDF: {failed}</p>
  return (
    <Document
      file={file}
      options={PDF_OPTIONS}
      onLoadSuccess={onLoad}
      onLoadError={(e) => setFailed(e.message)}
      loading={placeholder}
      className="flex flex-col"
      data-testid="pdf-viewer"
    >
      {pages.length === 0 ? placeholder : null}
      {pages.map((p) => {
        const scale = width / p.width
        const height = p.height * scale
        const draw = Math.abs(p.number - middle) <= PAGES_EACH_SIDE
        const ratio = Math.min(window.devicePixelRatio || 1, Math.sqrt(MAX_PIXELS / (width * height)))
        return (
          <div
            key={p.number}
            data-page={p.number}
            className="relative overflow-hidden rounded-page bg-page shadow-page"
            style={{ width, height, marginBottom: PAGE_GAP }}
          >
            {draw ? (
              // A page that mounts later (scrolling moves which pages draw) may suspend. Its own boundary keeps that
              // from blanking the whole column, which would also throw away the scroll position.
              <Suspense fallback={null}>
                <Page
                  pageNumber={p.number}
                  width={width}
                  devicePixelRatio={ratio}
                  renderTextLayer={false}
                  renderAnnotationLayer={false}
                  loading={<div className="size-full bg-page" />}
                />
              </Suspense>
            ) : null}
            {region?.page === p.number ? <RegionOutline bbox={region.bbox} scale={scale} /> : null}
            {(byPage.get(p.number) ?? []).map((h) => {
              const mark = (
                <Mark
                  key={h.active ? `${h.key}:active` : h.key}
                  h={h}
                  scale={scale}
                  hovered={hovered.has(h.itemId)}
                  onHighlight={onHighlight}
                  onHover={onHover}
                />
              )
              return tip && h.primary ? (
                <Tip key={h.key} label={tip(h.itemId)}>
                  {mark}
                </Tip>
              ) : (
                mark
              )
            })}
          </div>
        )
      })}
    </Document>
  )
}

function markClass(h: Highlight, hovered: boolean): string {
  const own = h.lines.length > 0 ? QUOTED_BLOCK[h.tone] : cn(TONE[h.tone], h.active && ACTIVE[h.tone])
  return cn(
    "absolute cursor-pointer rounded-highlight transition-[outline-color,background-color,opacity] duration-150",
    h.primary ? own : "outline-1 outline-dashed outline-hl-ink/60",
    h.dim && !hovered && "opacity-25 hover:opacity-70",
    hovered && !h.active && "ring-2 ring-hl-ink/40",
  )
}

function Mark({
  h,
  scale,
  hovered,
  onHighlight,
  onHover,
  ...props
}: {
  h: Highlight
  scale: number
  hovered: boolean
  onHighlight: (itemId: string) => void
  onHover?: (itemId: string | null) => void
}) {
  const [x0 = 0, y0 = 0, x1 = 0, y1 = 0] = h.bbox
  return (
    <button
      type="button"
      // Read mode's tooltip passes its own pointer handlers through `props`, and those win.
      onMouseEnter={onHover ? () => onHover(h.itemId) : undefined}
      onMouseLeave={onHover ? () => onHover(null) : undefined}
      {...props}
      onClick={() => onHighlight(h.itemId)}
      aria-label={`Show ${h.itemId}`}
      data-testid="highlight"
      data-item={h.itemId}
      data-tone={h.tone}
      data-active={h.active || undefined}
      data-dim={h.dim || undefined}
      data-hover={hovered || undefined}
      data-precise={h.lines.length > 0 || undefined}
      className={markClass(h, hovered)}
      style={{
        left: (x0 - PAD_X) * scale,
        top: (y0 - PAD_Y) * scale,
        width: (x1 - x0 + 2 * PAD_X) * scale,
        height: (y1 - y0 + 2 * PAD_Y) * scale,
      }}
    >
      {h.lines.map((line) => (
        <QuoteLine key={line.join(",")} line={line} from={h.bbox} tone={h.tone} scale={scale} />
      ))}
    </button>
  )
}

function QuoteLine({
  line,
  from,
  tone,
  scale,
}: {
  line: number[]
  from: number[]
  tone: Tone
  scale: number
}) {
  const [x0 = 0, y0 = 0, x1 = 0, y1 = 0] = line
  const [bx = 0, by = 0] = from
  return (
    <span
      aria-hidden
      data-testid="quote-line"
      className={cn("pointer-events-none absolute rounded-highlight mix-blend-multiply", QUOTED_LINE[tone])}
      style={{
        left: (x0 - LINE_PAD - (bx - PAD_X)) * scale,
        top: (y0 - LINE_PAD - (by - PAD_Y)) * scale,
        width: (x1 - x0 + 2 * LINE_PAD) * scale,
        height: (y1 - y0 + 2 * LINE_PAD) * scale,
      }}
    />
  )
}

function RegionOutline({ bbox, scale }: { bbox: number[]; scale: number }) {
  const [x0 = 0, y0 = 0, x1 = 0, y1 = 0] = bbox
  return (
    <div
      aria-hidden
      data-testid="table-outline"
      className="pointer-events-none absolute rounded-mark outline-[1.5px] outline-hl-ink/70"
      style={{
        left: (x0 - 4) * scale,
        top: (y0 - 4) * scale,
        width: (x1 - x0 + 8) * scale,
        height: (y1 - y0 + 8) * scale,
      }}
    />
  )
}
