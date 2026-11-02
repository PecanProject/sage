import { Combobox as ComboboxPrimitive } from "@base-ui/react/combobox"
import { CheckIcon, MagnifyingGlassIcon } from "@phosphor-icons/react"
import { buttonVariants } from "@/components/ui/button-variants"
import { groupLabelClass, itemClass, popupClass } from "@/components/ui/select"
import { cn } from "@/lib/utils"

/** A list with a search box at the top of its popup, for a list too long to scan. */
const Combobox = ComboboxPrimitive.Root

/** Looks like a quiet button with no caret, for an action at the end of a list. */
function ComboboxTrigger({ className, ...props }: ComboboxPrimitive.Trigger.Props) {
  return (
    <ComboboxPrimitive.Trigger
      className={cn(buttonVariants({ variant: "quiet", size: "compact" }), className)}
      {...props}
    />
  )
}

/** The popup scrolls its list and keeps the search box and the footer in view. */
function ComboboxContent({
  className,
  children,
  placeholder,
  empty,
  footer,
  ...props
}: Omit<ComboboxPrimitive.Popup.Props, "children"> & {
  children: ComboboxPrimitive.List.Props["children"]
  placeholder: string
  empty: React.ReactNode
  footer?: React.ReactNode
}) {
  return (
    <ComboboxPrimitive.Portal>
      <ComboboxPrimitive.Positioner sideOffset={6} align="start" className="z-60">
        <ComboboxPrimitive.Popup
          className={cn(
            popupClass,
            "flex max-h-[min(440px,var(--available-height))] w-(--anchor-width) min-w-80 flex-col overflow-hidden p-0",
            className,
          )}
          {...props}
        >
          <div className="flex items-center gap-2 border-b border-line px-3">
            <MagnifyingGlassIcon className="size-4 shrink-0 text-muted-ink" />
            <ComboboxPrimitive.Input
              placeholder={placeholder}
              className="h-control min-w-0 flex-1 bg-transparent text-control text-ink outline-none placeholder:text-muted-ink"
            />
          </div>
          <ComboboxPrimitive.Empty className="px-3 py-4 text-caption text-muted-ink empty:hidden">
            {empty}
          </ComboboxPrimitive.Empty>
          <ComboboxPrimitive.List className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-1.5 empty:hidden">
            {children}
          </ComboboxPrimitive.List>
          {footer ? (
            <div className="border-t border-line px-3 py-2 text-caption text-muted-ink">{footer}</div>
          ) : null}
        </ComboboxPrimitive.Popup>
      </ComboboxPrimitive.Positioner>
    </ComboboxPrimitive.Portal>
  )
}

const ComboboxGroup = ComboboxPrimitive.Group
const ComboboxCollection = ComboboxPrimitive.Collection

function ComboboxGroupLabel({ className, ...props }: ComboboxPrimitive.GroupLabel.Props) {
  return <ComboboxPrimitive.GroupLabel className={cn(groupLabelClass, className)} {...props} />
}

function ComboboxItem({ className, children, ...props }: ComboboxPrimitive.Item.Props) {
  return (
    <ComboboxPrimitive.Item className={cn(itemClass, className)} {...props}>
      {children}
      <ComboboxPrimitive.ItemIndicator className="absolute right-3 flex">
        <CheckIcon weight="bold" className="size-4 text-ink" />
      </ComboboxPrimitive.ItemIndicator>
    </ComboboxPrimitive.Item>
  )
}

export {
  Combobox,
  ComboboxCollection,
  ComboboxContent,
  ComboboxGroup,
  ComboboxGroupLabel,
  ComboboxItem,
  ComboboxTrigger,
}
