import { Input as InputPrimitive } from "@base-ui/react/input"
import type * as React from "react"
import { cn } from "@/lib/utils"

const field =
  "w-full min-w-0 rounded-control border border-line bg-surface px-3 text-control text-ink transition-[border-color,box-shadow] duration-150 outline-none placeholder:text-muted-ink hover:border-line-strong focus-visible:border-sage focus-visible:shadow-focus focus-visible:outline-none disabled:cursor-not-allowed disabled:bg-surface-2 disabled:text-muted-ink aria-invalid:border-danger"

function Input({ className, ...props }: React.ComponentProps<"input">) {
  return <InputPrimitive data-slot="input" className={cn(field, "h-control", className)} {...props} />
}

function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea data-slot="textarea" className={cn(field, "min-h-24 resize-y py-2", className)} {...props} />
  )
}

export { Input, Textarea }
