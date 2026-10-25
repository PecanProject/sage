import { CircleNotchIcon, WarningIcon, XCircleIcon } from "@phosphor-icons/react"
import { useTheme } from "next-themes"
import type * as React from "react"
import { Toaster as Sonner, type ToasterProps } from "sonner"

const Toaster = (props: ToasterProps) => {
  const { resolvedTheme = "light" } = useTheme()
  return (
    <Sonner
      theme={resolvedTheme as ToasterProps["theme"]}
      // Toasts close themselves and wait while hovered, so there is no floating ✕. Only a warning or an error has
      // an icon: a plain note needs none.
      icons={{
        warning: <WarningIcon weight="fill" className="size-4 text-check" />,
        error: <XCircleIcon weight="fill" className="size-4 text-danger" />,
        loading: <CircleNotchIcon className="size-4 animate-spin text-muted-ink" />,
      }}
      style={
        {
          "--normal-bg": "var(--surface)",
          "--normal-text": "var(--ink)",
          "--normal-border": "var(--line)",
          "--border-radius": "var(--radius-card)",
          "--font-family": "var(--font-sans)",
        } as React.CSSProperties
      }
      toastOptions={{
        classNames: {
          toast: "!shadow-float !text-control !gap-2.5 !px-4 !py-2.5",
          description: "!text-muted-ink !text-caption",
          actionButton: "!bg-transparent !text-ink !font-medium !text-control",
        },
      }}
      {...props}
    />
  )
}

export { Toaster }
