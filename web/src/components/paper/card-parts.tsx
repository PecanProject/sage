import { CheckIcon, PencilSimpleIcon, XIcon } from "@phosphor-icons/react"
import { type CSSProperties, type ReactNode, useEffect, useRef } from "react"
import { StatusMark, ToCheck } from "@/components/status-mark"
import { Button } from "@/components/ui/button"
import { REVIEW_LABEL, STATUS_LABEL } from "@/lib/format"
import { type Item, needsLook } from "@/lib/items"
import type { Unit } from "@/lib/units"
import { cn } from "@/lib/utils"

const REVIEW_ICON = { accept: CheckIcon, edit: PencilSimpleIcon, reject: XIcon }

export function ReviewIcon({ action, className }: { action: keyof typeof REVIEW_ICON; className?: string }) {
  const Icon = REVIEW_ICON[action]
  return <Icon weight="bold" className={cn("size-3.5 shrink-0", className)} aria-hidden />
}

export function ReviewMark({ item, className }: { item: Item; className?: string }) {
  const action = item.review?.action
  if (!action) return <StatusMark status={item.status} className={className} />
  return (
    <span
      role="img"
      aria-label={REVIEW_LABEL[action]}
      className={cn("inline-flex size-4 shrink-0 items-center justify-center text-muted-ink", className)}
    >
      <ReviewIcon action={action} />
    </span>
  )
}

/** What a reviewed item shows in place of the review buttons, until Change brings them back. */
export function Reviewed({
  review,
  onChange,
}: {
  review: NonNullable<Item["review"]>
  onChange: () => void
}) {
  return (
    <p className="flex min-w-0 items-center gap-1.5 text-caption text-muted-ink" data-testid="reviewed">
      <span className="min-w-0 truncate">
        {REVIEW_LABEL[review.action]}
        {review.note ? `: ${review.note}` : null}
      </span>
      <span aria-hidden>·</span>
      <Button variant="text" className="text-caption" onClick={onChange} data-testid="review-change">
        Change
      </Button>
    </p>
  )
}

export function Line({
  mark,
  label,
  note,
  muted = false,
  onClick,
  ...data
}: {
  mark: ReactNode
  label: string
  note?: string | null
  muted?: boolean
  onClick: () => void
} & Record<`data-${string}`, string | undefined>) {
  return (
    <button
      type="button"
      onClick={onClick}
      {...data}
      className="flex h-control-sm w-full min-w-0 cursor-pointer items-center gap-2.5 rounded-control px-2 text-left outline-none focus-visible:outline-2 focus-visible:outline-sage"
    >
      {mark}
      <span className={cn("min-w-0 flex-1 truncate text-value", muted ? "text-muted-ink" : "text-ink")}>
        {label}
      </span>
      {note ? (
        <span className="max-w-[45%] shrink-0 truncate text-caption text-muted-ink" data-testid="row-problem">
          {note}
        </span>
      ) : null}
    </button>
  )
}

export function ItemLine({ item, onClick }: { item: Item; onClick: () => void }) {
  return (
    <Line
      mark={<ReviewMark item={item} />}
      label={item.label}
      note={item.review ? null : item.brief}
      muted={item.review !== null}
      onClick={onClick}
      data-testid="review-item"
      data-item={item.id}
      data-status={item.status}
      data-reviewed={item.review?.action ?? undefined}
    />
  )
}

function CellValue({ item, text }: { item: Item; text: string }) {
  const action = item.review?.action
  if (needsLook(item)) return <ToCheck className="truncate px-0.5">{text}</ToCheck>
  return (
    <>
      {action ? <ReviewIcon action={action} className="size-3" /> : null}
      <span className="truncate">{text}</span>
    </>
  )
}

/** A pinned row key's width; the label after it gets more. Both are fixed so each pinned column knows where the
 * one before it ends. */
const KEY_WIDTH = 40
const LABEL_WIDTH = 120

function pinOf(col: number, stub: number): CSSProperties | undefined {
  if (col >= stub) return undefined
  const width = col === stub - 1 ? LABEL_WIDTH : KEY_WIDTH
  return { left: col * KEY_WIDTH, width, minWidth: width, maxWidth: width }
}

const pinClass = (col: number, stub: number) =>
  col < stub && cn("sticky z-10 bg-surface", col === stub - 1 && "border-r border-line")

export function Grid({
  unit,
  active,
  onActivate,
}: {
  unit: Unit
  active: string
  onActivate: (id: string) => void
}) {
  const rows = unit.table?.rows ?? []
  const header = unit.table?.header_rows ?? 1
  const byAnchor = new Map(unit.items.flatMap((i) => (i.primary ? [[i.primary, i] as const] : [])))
  const pinned = unit.stub ? (unit.stub - 1) * KEY_WIDTH + LABEL_WIDTH : 0
  const box = useRef<HTMLDivElement>(null)
  // A wide table scrolls sideways inside the card; keep the picked cell in view and clear of the pinned columns.
  // Only this box scrolls, never the pane, which follows the PDF on its own.
  useEffect(() => {
    const el = box.current
    const cell = el?.querySelector(`[data-item="${CSS.escape(active)}"]`)
    if (!el || !cell || cell.closest("[data-pinned]")) return
    const at = cell.getBoundingClientRect()
    const view = el.getBoundingClientRect()
    const left = view.left + pinned
    if (at.right > view.right) el.scrollLeft += at.right - view.right + 8
    else if (at.left < left) el.scrollLeft -= left - at.left + 8
  }, [active, pinned])
  return (
    <div ref={box} className="-mx-1 scroll-shadow-x" data-testid="table-grid-scroll">
      <table className="border-separate border-spacing-0 text-caption" data-testid="table-grid">
        <tbody>
          {rows.map((row, r) => (
            <tr key={row[0]?.anchor ?? r}>
              {row.map((cell, c) => {
                const item = byAnchor.get(cell.anchor)
                const raw = cell.text.trim()
                const text = raw || "–"
                const pin = pinOf(c, unit.stub)
                const pinning = pinClass(c, unit.stub)
                if (r < header)
                  return (
                    <th
                      key={cell.anchor}
                      scope="col"
                      style={pin}
                      data-pinned={pin ? true : undefined}
                      className={cn(
                        "max-w-[88px] truncate border-b border-line px-1.5 py-1 text-left font-medium text-muted-ink",
                        pinning,
                      )}
                      title={raw}
                    >
                      {raw}
                    </th>
                  )
                if (!item)
                  return (
                    <td
                      key={cell.anchor}
                      style={pin}
                      data-pinned={pin ? true : undefined}
                      className={cn("max-w-[88px] truncate px-1.5 py-0.5 text-muted-ink", pinning)}
                      title={raw}
                    >
                      {text}
                    </td>
                  )
                const on = item.id === active
                return (
                  <td
                    key={cell.anchor}
                    style={pin}
                    data-pinned={pin ? true : undefined}
                    className={cn("p-0.5", pinning)}
                  >
                    <button
                      type="button"
                      onClick={() => onActivate(item.id)}
                      data-testid="grid-cell"
                      data-item={item.id}
                      data-status={item.status}
                      data-reviewed={item.review?.action ?? undefined}
                      data-active={on || undefined}
                      aria-label={`${item.label}: ${text}, ${item.review ? REVIEW_LABEL[item.review.action] : STATUS_LABEL[item.status]}`}
                      title={pin ? text : undefined}
                      className={cn(
                        "flex h-7 max-w-full cursor-pointer items-center gap-1 rounded-inline px-1 whitespace-nowrap tabular-nums outline-none hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-sage",
                        on && "bg-surface-2 outline-[1.5px] outline-sage",
                        item.review ? "text-muted-ink" : "text-ink",
                      )}
                    >
                      <CellValue item={item} text={text} />
                    </button>
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
