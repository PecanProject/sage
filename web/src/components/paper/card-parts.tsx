import { ChatCircleIcon, CheckCircleIcon, PencilSimpleIcon, XIcon } from "@phosphor-icons/react"
import type { ReactNode } from "react"
import { StatusMark } from "@/components/status-mark"
import { Button } from "@/components/ui/button"
import { REVIEW_LABEL } from "@/lib/format"
import type { Item } from "@/lib/items"
import { cn } from "@/lib/utils"

const REVIEW_ICON = {
  accept: CheckCircleIcon,
  edit: PencilSimpleIcon,
  reject: XIcon,
  comment: ChatCircleIcon,
}

/** A scientist's decision. An acceptance is the one filled mark, in sage, so it never reads as SAGE's own check. */
export function ReviewIcon({ action, className }: { action: keyof typeof REVIEW_ICON; className?: string }) {
  const Icon = REVIEW_ICON[action]
  if (action === "accept")
    return <Icon weight="fill" className={cn("size-4 shrink-0 text-sage", className)} aria-hidden />
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
