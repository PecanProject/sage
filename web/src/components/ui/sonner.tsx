import { CheckIcon, CircleNotchIcon, InfoIcon, WarningIcon, XCircleIcon } from "@phosphor-icons/react"
import { useTheme } from "next-themes"
import type * as React from "react"
import { Toaster as Sonner, type ToasterProps } from "sonner"

const Toaster = (props: ToasterProps) => {
  const { resolvedTheme = "light" } = useTheme()
  return (
    <Sonner
      theme={resolvedTheme as ToasterProps["theme"]}
      closeButton
      icons={{
        success: <CheckIcon weight="bold" className="size-4 text-good" />,
        info: <InfoIcon className="size-4 text-muted-ink" />,
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
          toast: "!shadow-float !text-control !gap-3 !px-4 !py-3",
          description: "!text-muted-ink !text-caption",
          actionButton: "!bg-transparent !text-ink !font-medium !text-control",
        },
      }}
      {...props}
    />
  )
}

export { Toaster }
