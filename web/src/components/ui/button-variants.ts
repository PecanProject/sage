import { cva } from "class-variance-authority"

/** The button's looks, for a control that has to look like one without being one, such as a combobox trigger. */
export const buttonVariants = cva(
  "inline-flex shrink-0 cursor-pointer items-center justify-center gap-2 rounded-control text-control font-medium whitespace-nowrap transition-[background-color,color,border-color,box-shadow] duration-150 ease-out outline-none select-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sage disabled:pointer-events-none disabled:opacity-45 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-icon",
  {
    variants: {
      variant: {
        default: "bg-sage text-on-sage hover:bg-sage-hover active:bg-sage-hover",
        outline:
          "border border-line bg-surface text-ink hover:border-line-strong hover:bg-surface-2 aria-expanded:bg-surface-2",
        ghost: "text-ink hover:bg-surface-2 aria-expanded:bg-surface-2",
        quiet:
          "text-muted-ink hover:bg-surface-2 hover:text-ink aria-expanded:bg-surface-2 aria-expanded:text-ink aria-[current=page]:bg-surface-2 aria-[current=page]:text-sage",
        text: "h-auto rounded-inline px-1 text-sage underline-offset-4 hover:underline",
      },
      size: {
        default: "h-control px-4",
        compact: "h-control-sm px-3",
        icon: "size-control",
        "icon-sm": "size-control-sm [&_svg:not([class*='size-'])]:size-4",
        inline: "h-auto p-0",
      },
    },
  },
)
