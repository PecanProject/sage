import type * as React from "react"
import { cn } from "@/lib/utils"

function SidePanel({ className, ...props }: React.ComponentProps<"aside">) {
  return (
    <aside
      data-slot="side-panel"
      className={cn("flex w-[440px] shrink-0 flex-col border-l border-line bg-surface", className)}
      {...props}
    />
  )
}

export { SidePanel }
