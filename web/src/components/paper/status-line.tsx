import { CircleNotchIcon, WarningIcon, XIcon } from "@phosphor-icons/react"
import type { ReactNode } from "react"
import { Button } from "@/components/ui/button"
import { useDismissed } from "@/lib/dismissed"
import { endedAlert, type LiveStep, lastAttempt, type RunInfo, stamp } from "@/lib/format"
import type { PaperState } from "@/lib/paper-state"

function TextButton({
  children,
  onClick,
  testid,
}: {
  children: ReactNode
  onClick: () => void
  testid: string
}) {
  return (
    <Button variant="text" className="text-caption" onClick={onClick} data-testid={testid}>
      {children}
    </Button>
  )
}

function Details({ onDetails }: { onDetails: (() => void) | null }) {
  if (onDetails === null) return null
  return (
    <TextButton onClick={onDetails} testid="status-details">
      Details
    </TextButton>
  )
}

function Live({
  step,
  onDetails,
  onStop,
}: {
  step: LiveStep
  onDetails: (() => void) | null
  onStop: () => void
}) {
  return (
    <>
      <div className="absolute inset-x-0 top-0 h-0.5 overflow-hidden bg-line" aria-hidden>
        <div
          className="h-full origin-left bg-sage transition-transform duration-700 ease-out"
          style={{ transform: `scaleX(${step.share / 100})` }}
        />
      </div>
      <span className="flex items-center gap-2" data-testid="status-live">
        <CircleNotchIcon className="size-4 animate-spin text-muted-ink" aria-hidden />
        <span className="text-ink">{step.word}…</span>
      </span>
      <Details onDetails={onDetails} />
      <TextButton onClick={onStop} testid="status-stop">
        Stop
      </TextButton>
    </>
  )
}

function alertRun(state: PaperState | null, older: RunInfo | null): RunInfo | null {
  if (older || state?.kind !== "data") return null
  return state.warn
}

function RunAlert({
  run,
  onAction,
  onDetails,
  onDismiss,
}: {
  run: RunInfo
  onAction: (run: RunInfo) => void
  onDetails: (() => void) | null
  onDismiss: () => void
}) {
  const alert = endedAlert(run)
  if (alert === null) return null
  return (
    <>
      <span className="flex items-center gap-3" data-testid="last-failed" data-status={run.status}>
        <span className="flex items-center gap-2">
          <WarningIcon weight="fill" className="size-4 text-check" aria-hidden />
          {lastAttempt(run)}
        </span>
        <TextButton onClick={() => onAction(run)} testid="status-retry">
          {alert.action}
        </TextButton>
      </span>
      <Details onDetails={onDetails} />
      <Button
        variant="quiet"
        size="icon-sm"
        className="ml-auto"
        aria-label="Dismiss"
        onClick={onDismiss}
        data-testid="status-dismiss"
      >
        <XIcon />
      </Button>
    </>
  )
}

export function StatusLine({
  state,
  older,
  standing,
  onDetails,
  onRetry,
  onStop,
  onLatest,
}: {
  state: PaperState | null
  older: RunInfo | null
  /** The extraction that stands for the paper, which "Back" returns to. */
  standing: RunInfo | null
  /** Null while the run is docked beside the paper, which already shows its details. */
  onDetails: (() => void) | null
  onRetry: (run: RunInfo) => void
  onStop: () => void
  onLatest: () => void
}) {
  const live = state?.kind === "live" ? state.step : null
  const dismissed = useDismissed()
  const found = alertRun(state, older)
  const alerted = found && !dismissed.has(found.id) ? found : null
  if (!older && live === null && !alerted) return null
  return (
    <div
      className="relative flex h-10 shrink-0 items-center gap-4 border-b border-line bg-surface-2 px-inset text-caption text-muted-ink tabular-nums"
      data-testid="status-line"
      data-state={state?.kind ?? "loading"}
    >
      {older ? (
        <span className="flex items-center gap-3" data-testid="viewing-older">
          <span className="text-ink">{stamp(older.created_at)}</span>
          <TextButton onClick={onLatest} testid="back-to-latest">
            {standing && (standing.created_at ?? "") < (older.created_at ?? "")
              ? `Back to ${stamp(standing.created_at)}`
              : "Back to latest"}
          </TextButton>
        </span>
      ) : null}
      {live !== null ? <Live step={live} onDetails={onDetails} onStop={onStop} /> : null}
      {alerted ? (
        <RunAlert
          run={alerted}
          onAction={onRetry}
          onDetails={onDetails}
          onDismiss={() => dismissed.dismiss(alerted.id)}
        />
      ) : null}
    </div>
  )
}
