import { CheckIcon } from "@phosphor-icons/react"
import type { ReactNode } from "react"
import { Tip } from "@/components/ui/tooltip"
import { STATUS_LABEL, type Status } from "@/lib/format"
import { cn } from "@/lib/utils"

/** The word goes in the aria-label and StatusMark's tooltip, never on screen. */
export function StatusShape({ status, className }: { status: Status; className?: string }) {
  return (
    <span
      role="img"
      aria-label={STATUS_LABEL[status]}
      data-status={status}
      className={cn("inline-flex size-4 shrink-0 items-center justify-center", className)}
    >
      {status === "verified" ? <CheckIcon weight="bold" className="size-3.5 text-good" /> : null}
    </span>
  )
}

export function StatusMark({ status, className }: { status: Status; className?: string }) {
  if (status !== "verified") return <StatusShape status={status} className={className} />
  return (
    <Tip label={STATUS_LABEL[status]}>
      <StatusShape status={status} className={className} />
    </Tip>
  )
}

export function ToCheck({ children, className }: { children: ReactNode; className?: string }) {
  return <mark className={cn("rounded-mark bg-check-tint px-1 text-ink", className)}>{children}</mark>
}
