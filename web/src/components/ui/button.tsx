import { Button as ButtonPrimitive } from "@base-ui/react/button"
import { CircleNotchIcon } from "@phosphor-icons/react"
import type { VariantProps } from "class-variance-authority"
import { buttonVariants } from "@/components/ui/button-variants"
import { cn } from "@/lib/utils"

function Button({
  className,
  variant = "default",
  size = "default",
  pending = false,
  children,
  ...props
}: ButtonPrimitive.Props &
  VariantProps<typeof buttonVariants> & {
    /** Draws a spinner before the label while the button's action runs. It doesn't disable the button. */
    pending?: boolean
  }) {
  return (
    <ButtonPrimitive
      data-slot="button"
      className={cn(buttonVariants({ variant, size: variant === "text" ? "inline" : size }), className)}
      {...props}
    >
      {pending ? <CircleNotchIcon className="animate-spin" /> : null}
      {children}
    </ButtonPrimitive>
  )
}

export { Button }
