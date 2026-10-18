import { Select as SelectPrimitive } from "@base-ui/react/select"
import { CaretUpDownIcon, CheckIcon } from "@phosphor-icons/react"
import { cn } from "@/lib/utils"

const Select = SelectPrimitive.Root

function SelectValue({ className, ...props }: SelectPrimitive.Value.Props) {
  return <SelectPrimitive.Value className={cn("flex-1 truncate text-left", className)} {...props} />
}

function SelectTrigger({ className, children, ...props }: SelectPrimitive.Trigger.Props) {
  return (
    <SelectPrimitive.Trigger
      className={cn(
        "flex h-control w-full cursor-pointer items-center gap-2 rounded-control border border-line bg-surface pr-2.5 pl-3 text-control text-ink transition-[border-color] duration-150 outline-none select-none hover:border-line-strong focus-visible:border-sage focus-visible:outline-none data-disabled:cursor-not-allowed data-disabled:bg-surface-2 data-disabled:text-muted-ink data-placeholder:text-muted-ink",
        className,
      )}
      {...props}
    >
      {children}
      <SelectPrimitive.Icon render={<CaretUpDownIcon className="size-4 shrink-0 text-muted-ink" />} />
    </SelectPrimitive.Trigger>
  )
}

function SelectContent({ className, children, ...props }: SelectPrimitive.Popup.Props) {
  return (
    <SelectPrimitive.Portal>
      <SelectPrimitive.Positioner sideOffset={6} alignItemWithTrigger={false} className="z-60">
        <SelectPrimitive.Popup
          className={cn(
            "max-h-(--available-height) min-w-(--anchor-width) origin-(--transform-origin) overflow-y-auto rounded-card border border-line bg-surface p-1.5 text-ink shadow-float transition-opacity duration-150 outline-none data-ending-style:opacity-0 data-starting-style:opacity-0",
            className,
          )}
          {...props}
        >
          <SelectPrimitive.List>{children}</SelectPrimitive.List>
        </SelectPrimitive.Popup>
      </SelectPrimitive.Positioner>
    </SelectPrimitive.Portal>
  )
}

function SelectItem({ className, children, ...props }: SelectPrimitive.Item.Props) {
  return (
    <SelectPrimitive.Item
      className={cn(
        "relative flex min-h-control cursor-pointer items-center gap-2 rounded-control py-1.5 pr-9 pl-3 text-control outline-none select-none data-highlighted:bg-surface-2 data-disabled:opacity-45",
        className,
      )}
      {...props}
    >
      <SelectPrimitive.ItemText className="flex flex-1 items-baseline gap-2">
        {children}
      </SelectPrimitive.ItemText>
      <SelectPrimitive.ItemIndicator className="absolute right-3 flex">
        <CheckIcon weight="bold" className="size-4 text-ink" />
      </SelectPrimitive.ItemIndicator>
    </SelectPrimitive.Item>
  )
}

export { Select, SelectContent, SelectItem, SelectTrigger, SelectValue }
