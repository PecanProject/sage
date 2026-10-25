import type * as React from "react"
import { cn } from "@/lib/utils"

function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="skeleton"
      className={cn("animate-pulse rounded-control bg-surface-2", className)}
      {...props}
    />
  )
}

/** One line of the surrounding text style, so text replaces it without moving anything. Give it a width class. */
function TextSkeleton({ className }: { className?: string }) {
  return (
    <span aria-hidden data-slot="skeleton" className="flex h-lh min-w-0 items-center">
      <span className={cn("h-[0.75em] animate-pulse rounded-bar bg-surface-2", className)} />
    </span>
  )
}

/** A PDF page while it loads, at the width it will draw. */
function PageSkeleton({ width }: { width: number }) {
  return <Skeleton className="rounded-page bg-surface" style={{ width, height: width * 1.3 }} />
}

export { PageSkeleton, Skeleton, TextSkeleton }
