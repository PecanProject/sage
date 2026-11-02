import { Tooltip as TooltipPrimitive } from "@base-ui/react/tooltip"
import type * as React from "react"

function TooltipProvider(props: TooltipPrimitive.Provider.Props) {
  return <TooltipPrimitive.Provider delay={350} closeDelay={0} {...props} />
}

function Tip({ label, children }: { label: React.ReactNode; children: React.ReactElement }) {
  return (
    <TooltipPrimitive.Root>
      <TooltipPrimitive.Trigger render={children} />
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Positioner side="top" sideOffset={6} align="center" className="z-60">
          <TooltipPrimitive.Popup className="max-w-72 origin-(--transform-origin) rounded-control bg-ink px-2.5 py-1.5 text-caption text-canvas transition-opacity duration-100 data-ending-style:opacity-0 data-starting-style:opacity-0">
            {label}
          </TooltipPrimitive.Popup>
        </TooltipPrimitive.Positioner>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  )
}

export { Tip, TooltipProvider }
