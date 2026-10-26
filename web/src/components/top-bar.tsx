import { CpuIcon, GearSixIcon } from "@phosphor-icons/react"
import { Link, useLocation, useNavigate } from "@tanstack/react-router"
import type { ReactNode } from "react"
import { Wordmark } from "@/components/logo"
import { Button } from "@/components/ui/button"
import { buttonVariants } from "@/components/ui/button-variants"
import { Tip } from "@/components/ui/tooltip"
import { useCanExtract } from "@/lib/blockers"

export function TopBar({
  title,
  crumb,
  center,
  setup = true,
  children,
}: {
  /** The page's name in the browser tab, as "<title> · sage". */
  title?: string
  crumb?: ReactNode
  center?: ReactNode
  /** The "No way to extract yet" strip, which a screen that already says so turns off. */
  setup?: boolean
  children?: ReactNode
}) {
  const navigate = useNavigate()
  const onManage = useLocation().pathname === "/manage"
  const extract = useCanExtract()
  return (
    <header className="sticky top-0 z-30 bg-canvas">
      <title>{title ? `${title} · sage` : "sage"}</title>
      <div className="flex h-14 items-center gap-4 px-inset">
        {/* One baseline for the wordmark and the crumb, whose text sizes differ. */}
        <div className="flex min-w-0 items-baseline gap-4">
          <Link
            to="/"
            className="flex shrink-0 items-baseline rounded-control py-1"
            data-testid="home-link"
            aria-label="sage, home"
          >
            <Wordmark />
          </Link>
          {crumb ? (
            <div className="flex min-w-0 items-center gap-2 text-control text-muted-ink">{crumb}</div>
          ) : null}
        </div>
        <div className="flex min-w-0 flex-1 justify-center">{center}</div>
        <div className="flex shrink-0 items-center gap-2">{children}</div>
        <Tip label="Manage">
          <Link
            to="/manage"
            className={buttonVariants({ variant: "quiet", size: "icon" })}
            aria-label="Manage"
            data-testid="manage-button"
          >
            <CpuIcon />
          </Link>
        </Tip>
        <Tip label="Settings">
          <Link
            to="/settings"
            className={buttonVariants({ variant: "quiet", size: "icon" })}
            aria-label="Settings"
            data-testid="settings-button"
          >
            <GearSixIcon />
          </Link>
        </Tip>
      </div>
      {setup && extract.known && !extract.can && !onManage ? (
        <div
          className="flex h-9 items-center justify-center gap-3 border-y border-line bg-surface-2 text-caption text-ink"
          data-testid="setup-strip"
        >
          No way to extract yet
          <Button variant="text" onClick={() => void navigate({ to: "/manage" })} className="text-caption">
            Set up
          </Button>
        </div>
      ) : (
        <div className="h-px bg-line" />
      )}
    </header>
  )
}
