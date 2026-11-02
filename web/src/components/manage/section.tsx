import { ArrowRightIcon } from "@phosphor-icons/react"
import type { ComponentProps, ReactNode } from "react"

export function Section({
  id,
  title,
  hint,
  children,
}: {
  /** The anchor a link can jump to. */
  id?: string
  title: string
  hint: string
  children: ReactNode
}) {
  return (
    <section id={id} className="scroll-mt-20 space-y-3">
      <div className="space-y-1 px-inset">
        <h2 className="text-title">{title}</h2>
        <p className="text-caption text-muted-ink">{hint}</p>
      </div>
      {children}
    </section>
  )
}

/** One row of the page's lists, the same in every section: an icon, a name with an optional caption line under it,
 * then the row's values and its ⋯ at the right (`actions`). `lead` sits in the margin before the row, so it never
 * moves the row's content. `children` go under the line, such as a provider's fields while they are open. */
export function Row({
  lead,
  icon,
  title,
  detail,
  actions,
  children,
  ...props
}: {
  lead?: ReactNode
  icon: ReactNode
  /** The name, with any mark that sits beside it. */
  title: ReactNode
  detail?: ReactNode
  actions?: ReactNode
  children?: ReactNode
} & Omit<ComponentProps<"li">, "title">) {
  return (
    <li {...props}>
      <div className="relative flex h-14 items-center gap-4 rounded-control px-inset hover:bg-surface-2">
        {lead ? <span className="absolute right-full mr-2 flex items-center">{lead}</span> : null}
        <span className="flex size-icon shrink-0 items-center justify-center">{icon}</span>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">{title}</div>
          {detail ? <div className="truncate text-caption text-muted-ink">{detail}</div> : null}
        </div>
        {actions}
      </div>
      {children}
    </li>
  )
}

/** "Default" and an arrow in the margin before the row Extract uses unless a run picks: an agent or one of your
 * models. */
export function DefaultMark({ on, testid }: { on: boolean; testid: string }) {
  if (!on) return null
  return (
    <span
      className="flex items-center gap-1 text-caption text-muted-ink"
      title="Extract uses this unless a run picks another"
      data-testid={testid}
      data-default="true"
    >
      Default
      <ArrowRightIcon className="size-3.5 text-ink" />
    </span>
  )
}
