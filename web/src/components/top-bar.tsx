import { GearSixIcon } from "@phosphor-icons/react"
import { Link } from "@tanstack/react-router"
import type { ReactNode } from "react"
import { $api } from "@/api/client"
import { Wordmark } from "@/components/logo"
import { Button } from "@/components/ui/button"
import { Tip } from "@/components/ui/tooltip"
import { useSettingsRoute } from "@/lib/settings-route"

export function TopBar({
  title,
  crumb,
  center,
  children,
}: {
  /** The page's name in the browser tab, as "<title> · sage". */
  title?: string
  crumb?: ReactNode
  center?: ReactNode
  children?: ReactNode
}) {
  const settings = useSettingsRoute()
  const { data } = $api.useQuery("get", "/api/settings")
  const missingKey = data ? data.values[data.key_field] === null : false
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
        <Tip label="Settings">
          <Button
            variant="quiet"
            size="icon"
            onClick={() => void settings.open()}
            aria-label="Settings"
            data-testid="settings-button"
          >
            <GearSixIcon />
          </Button>
        </Tip>
      </div>
      {missingKey ? (
        <div
          className="flex h-9 items-center justify-center gap-3 border-y border-line bg-surface-2 text-caption text-ink"
          data-testid="key-strip"
        >
          Add your API key to start extracting.
          <Button variant="text" onClick={() => void settings.open()} className="text-caption">
            Open settings
          </Button>
        </div>
      ) : (
        <div className="h-px bg-line" />
      )}
    </header>
  )
}
