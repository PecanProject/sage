import { Collapsible } from "@base-ui/react/collapsible"
import { CaretRightIcon } from "@phosphor-icons/react"
import type { ReactNode } from "react"
import { cn } from "@/lib/utils"

/** A disclosure: a muted caption with a caret that turns as its panel opens. */
export function Fold({
  label,
  defaultOpen,
  className,
  testid,
  children,
}: {
  label: string
  defaultOpen?: boolean
  /** The panel's layout. */
  className?: string
  testid?: string
  children: ReactNode
}) {
  return (
    <Collapsible.Root defaultOpen={defaultOpen}>
      <Collapsible.Trigger className="group flex cursor-pointer items-center gap-1.5 rounded-inline text-caption font-medium text-muted-ink outline-none hover:text-ink">
        <CaretRightIcon className="size-3.5 transition-transform duration-150 group-data-[panel-open]:rotate-90" />
        {label}
      </Collapsible.Trigger>
      <Collapsible.Panel className={cn("pt-3", className)} data-testid={testid}>
        {children}
      </Collapsible.Panel>
    </Collapsible.Root>
  )
}
