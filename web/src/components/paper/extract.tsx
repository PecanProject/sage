import { CircleNotchIcon, WarningIcon, XIcon } from "@phosphor-icons/react"
import { useQuery } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { type ReactNode, useState } from "react"
import { AgentEffortSelect, ModelEffortSelect } from "@/components/effort-select"
import { ExtractWithSelect } from "@/components/extract-with"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Fold } from "@/components/ui/fold"
import { Textarea } from "@/components/ui/input"
import { agentConfigs, agentOffer, offeredEfforts, runUsing, useSettingsValues } from "@/lib/agents"
import { type Blocker, useBlocker } from "@/lib/blockers"
import { endedAlert, isResumable, type RunInfo } from "@/lib/format"
import { useModels } from "@/lib/models"
import { useContinueRun, useResumeRun, useStartRuns, useStopRuns } from "@/lib/paper-state"
import { limitLine, type RunChoice, useRunChoice } from "@/lib/run-choice"

/** What the Manage page has to fix before the run can start: an agent that isn't ready, a missing key, or a model
 * no longer among yours. */
function SetupNeeded({ blocker }: { blocker: Blocker }) {
  const navigate = useNavigate()
  if (blocker.line === null) return null
  return (
    <p className="flex flex-wrap items-center gap-x-2 text-caption text-muted-ink" data-testid="setup-needed">
      {blocker.line}
      <Button variant="text" className="text-caption" onClick={() => void navigate({ to: "/manage" })}>
        Set up
      </Button>
    </p>
  )
}

/** The picker and, under More options, the effort, when what it picked has levels to choose from. An agent's are
 * asked when the list opens, so until then the fold shows, since only the agent knows whether its model has any. */
export function ChoiceFields({ choice: c, testid }: { choice: RunChoice; testid: string }) {
  const id = `${testid}-effort`
  const configs = agentConfigs(useSettingsValues())
  const chosen = c.chosen
  const models = useModels()
  const model = chosen.kind === "model" ? models.find((m) => m.id === chosen.model) : undefined
  const agent = chosen.kind === "agent" ? chosen.agent : ""
  const agentModel = chosen.kind === "agent" ? (chosen.model ?? configs[agent]?.model ?? null) : null
  const offer = useQuery({ ...agentOffer(agent, agentModel), enabled: false })
  const none =
    chosen.kind === "model" ? !model?.efforts.length : offer.data && offeredEfforts(offer.data).length === 0
  return (
    <>
      <ExtractWithSelect value={c.using} onChange={c.pick} testid={testid} />
      <SetupNeeded blocker={c.blocker} />
      {none ? null : (
        <Fold label="More options" className="grid grid-cols-[1fr_200px] items-center gap-3">
          <label htmlFor={id} className="text-label">
            Thinking effort
          </label>
          {model ? (
            <ModelEffortSelect model={model} value={c.effort} onChange={c.setEffort} id={id} testid={id} />
          ) : (
            <AgentEffortSelect
              agent={{ id: agent, title: configs[agent]?.title ?? agent }}
              model={agentModel}
              value={c.effort}
              onChange={(e) => e !== null && c.setEffort(e)}
              clearable={false}
              id={id}
              testid={id}
            />
          )}
        </Fold>
      )}
    </>
  )
}

export function ExtractDialog({
  open,
  paperId,
  onClose,
}: {
  open: boolean
  paperId: string
  onClose: () => void
}) {
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-[440px]" data-testid="extract-dialog">
        {open ? <ExtractForm paperId={paperId} onClose={onClose} /> : null}
      </DialogContent>
    </Dialog>
  )
}

function ExtractForm({ paperId, onClose }: { paperId: string; onClose: () => void }) {
  const c = useRunChoice(null)
  const start = useStartRuns(onClose)
  return (
    <>
      <DialogHeader>
        <DialogTitle>Extract data</DialogTitle>
      </DialogHeader>
      <DialogBody className="space-y-4">
        <ChoiceFields choice={c} testid="extract-model" />
      </DialogBody>
      <DialogFooter className="justify-between">
        <span className="text-caption text-muted-ink" data-testid="extract-limit">
          {limitLine(c)}
        </span>
        <Button
          disabled={start.isPending || c.blocker.blocked}
          onClick={() =>
            start.mutate({ body: { paper_ids: [paperId], using: c.using, effort: c.effortAsked } })
          }
          data-testid="start-extraction"
        >
          {start.isPending ? <CircleNotchIcon className="animate-spin" /> : null}
          Extract data
        </Button>
      </DialogFooter>
    </>
  )
}

function Card({ state, children }: { state: "failed" | "new"; children: ReactNode }) {
  return (
    <div
      className="pointer-events-auto w-[360px] rounded-card border border-line bg-surface p-4 shadow-float"
      data-testid="extract-card"
      data-state={state}
    >
      {children}
    </div>
  )
}

export function EndedCard({
  run,
  onRetry,
  onDetails,
  onDismiss,
}: {
  run: RunInfo
  onRetry: (run: RunInfo) => void
  onDetails: () => void
  onDismiss: (run: RunInfo) => void
}) {
  const alert = endedAlert(run)
  const navigate = useNavigate()
  if (alert === null) return null
  return (
    <Card state="failed">
      <div className="space-y-4">
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1 space-y-1">
            <p className="flex items-center gap-2 text-value text-danger">
              <WarningIcon weight="fill" className="size-icon" aria-hidden /> {alert.title}
            </p>
            <p
              className="text-caption text-muted-ink"
              data-testid="extract-card-reason"
              title={run.error ?? ""}
            >
              {alert.reason}
            </p>
          </div>
          <Button
            variant="quiet"
            size="icon-sm"
            aria-label="Dismiss"
            onClick={() => onDismiss(run)}
            data-testid="extract-card-dismiss"
          >
            <XIcon />
          </Button>
        </div>
        <div className="flex gap-2">
          {/* A refused key or model fails the same way again until the Manage page changes, so that comes first. */}
          {run.refused ? (
            <Button onClick={() => void navigate({ to: "/manage" })} data-testid="extract-card-setup">
              Fix setup
            </Button>
          ) : null}
          <Button
            variant={run.refused ? "ghost" : "default"}
            onClick={() => onRetry(run)}
            data-testid="extract-card-retry"
          >
            {alert.action}
          </Button>
          <Button variant="ghost" onClick={onDetails} data-testid="extract-card-details">
            Details
          </Button>
        </div>
      </div>
    </Card>
  )
}

export function NewCard({ paperId }: { paperId: string }) {
  return (
    <Card state="new">
      <StartForm paperId={paperId} />
    </Card>
  )
}

function StartForm({ paperId }: { paperId: string }) {
  const c = useRunChoice(null)
  const start = useStartRuns()
  return (
    <div className="space-y-3">
      <div className="flex gap-2">
        <div className="min-w-0 flex-1">
          <ExtractWithSelect value={c.using} onChange={c.pick} testid="extract-card-model" />
        </div>
        <Button
          disabled={start.isPending || c.blocker.blocked}
          onClick={() =>
            start.mutate({ body: { paper_ids: [paperId], using: c.using, effort: c.effortAsked } })
          }
          data-testid="extract-card-start"
        >
          {start.isPending ? <CircleNotchIcon className="animate-spin" /> : null}
          Extract data
        </Button>
      </div>
      <SetupNeeded blocker={c.blocker} />
    </div>
  )
}

export function ContinueDialog({ run, onClose }: { run: RunInfo | null; onClose: () => void }) {
  return (
    <Dialog open={run !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-[480px]" data-testid="continue-dialog">
        {run === null ? null : isResumable(run) ? (
          <ResumeForm key={run.id} run={run} onClose={onClose} />
        ) : (
          <ContinueForm key={run.id} run={run} onClose={onClose} />
        )}
      </DialogContent>
    </Dialog>
  )
}

function GuidanceField({
  text,
  onText,
  placeholder,
  onSubmit,
}: {
  text: string
  onText: (t: string) => void
  placeholder: string
  onSubmit: () => void
}) {
  return (
    <div className="space-y-2">
      <label htmlFor="continue-guidance" className="text-label">
        What should it add or fix?
      </label>
      <Textarea
        id="continue-guidance"
        value={text}
        onChange={(e) => onText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) onSubmit()
        }}
        placeholder={placeholder}
        data-testid="continue-guidance"
      />
    </div>
  )
}

function ResumeForm({ run, onClose }: { run: RunInfo; onClose: () => void }) {
  const [text, setText] = useState("")
  const resume = useResumeRun(onClose)
  const blocker = useBlocker(run.extraction_model ?? null)
  const submit = () => {
    if (resume.isPending || blocker.blocked) return
    resume.mutate({ params: { path: { run_id: run.id } }, body: { guidance: text.trim() || null } })
  }
  return (
    <>
      <DialogHeader>
        <DialogTitle>Resume extraction</DialogTitle>
      </DialogHeader>
      <DialogBody className="space-y-4">
        <p className="text-caption text-muted-ink">It picks up from what it had found.</p>
        <GuidanceField text={text} onText={setText} placeholder="Optional" onSubmit={submit} />
        <SetupNeeded blocker={blocker} />
      </DialogBody>
      <DialogFooter>
        <Button disabled={resume.isPending || blocker.blocked} onClick={submit} data-testid="continue-start">
          {resume.isPending ? <CircleNotchIcon className="animate-spin" /> : null}
          Resume
        </Button>
      </DialogFooter>
    </>
  )
}

/** A new run on the folder as this one left it, with its own budget. It defaults to this run's own agent or model;
 * a terminal's run, which SAGE can't start, defaults to what Extract uses. The scientist's reviews reach it through
 * check. */
function ContinueForm({ run, onClose }: { run: RunInfo; onClose: () => void }) {
  const c = useRunChoice(runUsing(run), run.effort)
  const [text, setText] = useState("")
  const proceed = useContinueRun(onClose)
  const guidance = text.trim()
  const needsGuidance = run.status === "finished"
  const ready = !proceed.isPending && !c.blocker.blocked && (guidance !== "" || !needsGuidance)
  const submit = () => {
    if (!ready) return
    proceed.mutate({
      params: { path: { run_id: run.id } },
      body: { guidance: guidance || null, using: c.using, effort: c.effortAsked },
    })
  }
  return (
    <>
      <DialogHeader>
        <DialogTitle>Continue extraction</DialogTitle>
      </DialogHeader>
      <DialogBody className="space-y-4">
        <p className="text-caption text-muted-ink">
          A new extraction starts from this one's data. This one stays as it is.
        </p>
        <GuidanceField
          text={text}
          onText={setText}
          placeholder={needsGuidance ? "Table 3 also gives soil nitrogen at two depths" : "Optional"}
          onSubmit={submit}
        />
        <ChoiceFields choice={c} testid="continue-model" />
      </DialogBody>
      <DialogFooter className="justify-between">
        <span className="text-caption text-muted-ink" data-testid="continue-limit">
          {limitLine(c)}
        </span>
        <Button disabled={!ready} onClick={submit} data-testid="continue-start">
          {proceed.isPending ? <CircleNotchIcon className="animate-spin" /> : null}
          Continue
        </Button>
      </DialogFooter>
    </>
  )
}

export function StopDialog({ run, onClose }: { run: RunInfo | null; onClose: () => void }) {
  const stop = useStopRuns(onClose)
  return (
    <Dialog open={run !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-[400px]" data-testid="stop-dialog">
        <DialogHeader>
          <DialogTitle>Stop this extraction?</DialogTitle>
        </DialogHeader>
        <DialogBody>
          <p className="text-control text-muted-ink">
            It keeps what it has found so far, and you can resume it later.
          </p>
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Keep going
          </Button>
          <Button
            disabled={stop.isPending}
            onClick={() => run && stop.mutate({ body: { run_ids: [run.id] } })}
            data-testid="stop-confirm"
          >
            {stop.isPending ? <CircleNotchIcon className="animate-spin" /> : null}
            Stop
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
