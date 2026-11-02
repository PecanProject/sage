import { Popover as PopoverPrimitive } from "@base-ui/react/popover"
import { popup } from "@/components/ui/dropdown-menu"
import { cn } from "@/lib/utils"

const Popover = PopoverPrimitive.Root
const PopoverTrigger = PopoverPrimitive.Trigger

function PopoverContent({ className, ...props }: PopoverPrimitive.Popup.Props) {
  return (
    <PopoverPrimitive.Portal>
      <PopoverPrimitive.Positioner className="z-50 outline-none" align="start" side="top" sideOffset={6}>
        <PopoverPrimitive.Popup className={cn(popup, "p-inset", className)} {...props} />
      </PopoverPrimitive.Positioner>
    </PopoverPrimitive.Portal>
  )
}

export { Popover, PopoverContent, PopoverTrigger }
