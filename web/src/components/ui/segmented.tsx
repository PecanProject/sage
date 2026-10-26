import { Toggle } from "@base-ui/react/toggle"
import { ToggleGroup } from "@base-ui/react/toggle-group"
import { cn } from "@/lib/utils"

export function Segmented<T extends string>({
  value,
  onChange,
  options,
  label,
  className,
}: {
  value: T
  onChange: (v: T) => void
  options: { value: T; label: string; testid?: string }[]
  label: string
  className?: string
}) {
  return (
    <ToggleGroup
      value={[value]}
      onValueChange={(v) => {
        const next = v[0]
        if (next !== undefined) onChange(next as T)
      }}
      aria-label={label}
      className={cn(
        "inline-flex h-control items-center gap-0.5 rounded-control bg-surface-2 p-0.75",
        className,
      )}
    >
      {options.map((o) => (
        <Toggle
          key={o.value}
          value={o.value}
          data-testid={o.testid}
          className="h-full cursor-pointer rounded-control px-3.5 text-label text-muted-ink outline-none transition-[background-color,color,box-shadow] duration-150 hover:text-ink focus-visible:outline-2 focus-visible:outline-sage data-pressed:bg-surface-raised data-pressed:text-ink data-pressed:shadow-card"
        >
          {o.label}
        </Toggle>
      ))}
    </ToggleGroup>
  )
}
