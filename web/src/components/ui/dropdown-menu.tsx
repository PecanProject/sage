import { Menu as MenuPrimitive } from "@base-ui/react/menu"
import { CaretRightIcon } from "@phosphor-icons/react"
import type * as React from "react"
import { cn } from "@/lib/utils"

const DropdownMenu = MenuPrimitive.Root
const DropdownMenuTrigger = MenuPrimitive.Trigger

const popup =
  "min-w-48 origin-(--transform-origin) rounded-card border border-line bg-surface p-1.5 text-ink shadow-float transition-[opacity,scale] duration-150 ease-out outline-none data-ending-style:opacity-0 data-starting-style:scale-98 data-starting-style:opacity-0"

function DropdownMenuContent({ className, ...props }: MenuPrimitive.Popup.Props) {
  return (
    <MenuPrimitive.Portal>
      <MenuPrimitive.Positioner className="z-50 outline-none" align="end" side="bottom" sideOffset={6}>
        <MenuPrimitive.Popup className={cn(popup, className)} {...props} />
      </MenuPrimitive.Positioner>
    </MenuPrimitive.Portal>
  )
}

const DropdownMenuSub = MenuPrimitive.SubmenuRoot

/** A submenu opens beside its item, with its first row level with the item: the offset is the popup's padding. */
function DropdownMenuSubContent({ className, ...props }: MenuPrimitive.Popup.Props) {
  return (
    <MenuPrimitive.Portal>
      <MenuPrimitive.Positioner
        className="z-50 outline-none"
        side="inline-end"
        align="start"
        sideOffset={8}
        alignOffset={-7}
      >
        <MenuPrimitive.Popup className={cn(popup, className)} {...props} />
      </MenuPrimitive.Positioner>
    </MenuPrimitive.Portal>
  )
}

const item =
  "flex h-control cursor-pointer items-center gap-3 rounded-control px-3 text-control outline-none select-none data-highlighted:bg-surface-2 data-disabled:pointer-events-none data-disabled:opacity-45 [&_svg:not([class*='size-'])]:size-icon [&_svg]:shrink-0 [&_svg]:text-muted-ink"

function DropdownMenuItem({ className, ...props }: MenuPrimitive.Item.Props) {
  return <MenuPrimitive.Item className={cn(item, className)} {...props} />
}

function DropdownMenuSubTrigger({ className, children, ...props }: MenuPrimitive.SubmenuTrigger.Props) {
  return (
    <MenuPrimitive.SubmenuTrigger className={cn(item, "data-popup-open:bg-surface-2", className)} {...props}>
      {children}
      <CaretRightIcon className="ml-auto size-3.5" />
    </MenuPrimitive.SubmenuTrigger>
  )
}

function DropdownMenuLinkItem({
  className,
  disabled,
  ...props
}: React.ComponentProps<"a"> & { disabled?: boolean }) {
  return <MenuPrimitive.Item disabled={disabled} render={<a className={cn(item, className)} {...props} />} />
}

function DropdownMenuSeparator({ className, ...props }: MenuPrimitive.Separator.Props) {
  return <MenuPrimitive.Separator className={cn("mx-2 my-1.5 h-px bg-line", className)} {...props} />
}

export {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLinkItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
}
