import { HandPalmIcon, WarningIcon, XIcon } from "@phosphor-icons/react"
import type { ReactNode } from "react"
import { Button } from "@/components/ui/button"
import { useDismissed } from "@/lib/dismissed"
import { endedAlert, lastAttempt, NEEDS_OK, type RunInfo, stamp } from "@/lib/format"
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

/** A live run's progress, a thin line along the top edge of the paper. */
function Progress({ share }: { share: number }) {
  return (
    <div
      className="relative h-0.5 shrink-0 overflow-hidden bg-line"
      aria-hidden
      data-testid="status-progress"
    >
      <div
        className="h-full origin-left bg-sage transition-transform duration-700 ease-out"
        style={{ transform: `scaleX(${share / 100})` }}
      />
    </div>
  )
}

/** An agent waiting for the person's OK, which the run's details ask. Shown only while those details are closed. */
function Waiting({ onDetails }: { onDetails: () => void }) {
  return (
    <>
      <span className="flex items-center gap-2" data-testid="status-live" data-waiting>
        <HandPalmIcon weight="fill" className="size-4 text-check" aria-hidden />
        <span className="text-ink">{NEEDS_OK}</span>
      </span>
      <Details onDetails={onDetails} />
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

/** An older extraction is on screen, with the way back to the one that stands for the paper. */
function ViewingOlder({
  older,
  standing,
  onLatest,
}: {
  older: RunInfo
  standing: RunInfo | null
  onLatest: () => void
}) {
  const back =
    standing && (standing.created_at ?? "") < (older.created_at ?? "")
      ? `Back to ${stamp(standing.created_at)}`
      : "Back to latest"
  return (
    <span className="flex items-center gap-3" data-testid="viewing-older">
      <span className="text-ink">{stamp(older.created_at)}</span>
      <TextButton onClick={onLatest} testid="back-to-latest">
        {back}
      </TextButton>
    </span>
  )
}

export function StatusLine({
  state,
  older,
  standing,
  onDetails,
  onRetry,
  onLatest,
}: {
  state: PaperState | null
  older: RunInfo | null
  /** The extraction that stands for the paper, which "Back" returns to. */
  standing: RunInfo | null
  /** Null while the run is docked beside the paper, which already shows its details. */
  onDetails: (() => void) | null
  onRetry: (run: RunInfo) => void
  onLatest: () => void
}) {
  const live = state?.kind === "live" ? state : null
  const dismissed = useDismissed()
  const found = alertRun(state, older)
  const alerted = found && !dismissed.has(found.id) ? found : null
  // A live run shows only its progress; Stop and its details are in the run's panel at the right.
  const waiting = live?.run.waiting && onDetails ? onDetails : null
  const progress = live ? <Progress share={live.step.share} /> : null
  if (!older && !waiting && !alerted) return progress
  return (
    <>
      {progress}
      <div
        className="relative flex h-10 shrink-0 items-center gap-4 border-b border-line bg-surface-2 px-inset text-caption text-muted-ink tabular-nums"
        data-testid="status-line"
        data-state={state?.kind ?? "loading"}
      >
        {older ? <ViewingOlder older={older} standing={standing} onLatest={onLatest} /> : null}
        {waiting ? <Waiting onDetails={waiting} /> : null}
        {alerted ? (
          <RunAlert
            run={alerted}
            onAction={onRetry}
            onDetails={onDetails}
            onDismiss={() => dismissed.dismiss(alerted.id)}
          />
        ) : null}
      </div>
    </>
  )
}
