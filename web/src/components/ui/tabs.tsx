import { Tabs as TabsPrimitive } from "@base-ui/react/tabs"
import { cn } from "@/lib/utils"

const Tabs = TabsPrimitive.Root

function TabsList({ className, children, ...props }: TabsPrimitive.List.Props) {
  return (
    <TabsPrimitive.List className={cn("relative flex items-center gap-6", className)} {...props}>
      {children}
      <TabsPrimitive.Indicator className="absolute bottom-0 left-0 h-0.5 w-(--active-tab-width) translate-x-(--active-tab-left) rounded-full bg-ink transition-transform duration-200 ease-out" />
    </TabsPrimitive.List>
  )
}

function TabsTrigger({ className, ...props }: TabsPrimitive.Tab.Props) {
  return (
    <TabsPrimitive.Tab
      className={cn(
        "flex h-11 cursor-pointer items-center gap-1.5 text-label whitespace-nowrap text-muted-ink outline-none transition-colors hover:text-ink focus-visible:text-ink data-active:text-ink",
        className,
      )}
      {...props}
    />
  )
}

export { Tabs, TabsList, TabsTrigger }
