import { Collapsible } from "@base-ui/react/collapsible"
import { CaretRightIcon, CircleNotchIcon, WarningIcon, XIcon } from "@phosphor-icons/react"
import { type ReactNode, useState } from "react"
import { $api } from "@/api/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Textarea } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { capital, endedAlert, isResumable, MODEL_NAMES, modelName, money, type RunInfo } from "@/lib/format"
import { useContinueRun, useResumeRun, useStartRun, useStopRun } from "@/lib/paper-state"
import { fieldsFromSchema } from "@/lib/settings-schema"

function useChoices() {
  const { data } = $api.useQuery("get", "/api/settings")
  const values = data?.values ?? {}
  const fields = data ? fieldsFromSchema(data.json_schema) : []
  const kind = (name: string) => fields.find((f) => f.name === name)?.kind
  const m = kind("extraction_model")
  const e = kind("effort")
  const model = typeof values.extraction_model === "string" ? values.extraction_model : ""
  const choices = m?.kind === "text" ? m.choices : []
  return {
    model,
    effort: typeof values.effort === "string" ? values.effort : "medium",
    models: model && !choices.includes(model) ? [...choices, model] : choices,
    efforts: e?.kind === "enum" ? e.options : [],
    cost: typeof values.max_cost_usd === "number" ? values.max_cost_usd : null,
  }
}

function ModelSelect({
  value,
  models,
  onChange,
  testid,
}: {
  value: string
  models: string[]
  onChange: (m: string) => void
  testid: string
}) {
  return (
    <Select
      value={value}
      onValueChange={(v) => typeof v === "string" && onChange(v)}
      items={models.map((id) => ({ value: id, label: modelName(id) }))}
    >
      <SelectTrigger aria-label="Model" data-testid={testid}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {models.map((id) => (
          <SelectItem key={id} value={id}>
            {modelName(id)}
            <span className="text-caption text-muted-ink">{MODEL_NAMES[id]?.hint}</span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function MoreOptions({ children }: { children: ReactNode }) {
  return (
    <Collapsible.Root>
      <Collapsible.Trigger className="group flex cursor-pointer items-center gap-1.5 rounded-inline text-caption font-medium text-muted-ink outline-none hover:text-ink">
        <CaretRightIcon className="size-3.5 transition-transform duration-150 group-data-[panel-open]:rotate-90" />
        More options
      </Collapsible.Trigger>
      <Collapsible.Panel className="grid grid-cols-[1fr_200px] items-center gap-3 pt-3">
        {children}
      </Collapsible.Panel>
    </Collapsible.Root>
  )
}

function EffortSelect({
  value,
  efforts,
  onChange,
  testid,
}: {
  value: string
  efforts: string[]
  onChange: (e: string) => void
  testid: string
}) {
  return (
    <>
      <label htmlFor={testid} className="text-label">
        Thinking effort
      </label>
      <Select
        value={value}
        onValueChange={(v) => typeof v === "string" && onChange(v)}
        items={efforts.map((e) => ({ value: e, label: capital(e) }))}
      >
        <SelectTrigger id={testid} data-testid={testid}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {efforts.map((e) => (
            <SelectItem key={e} value={e}>
              {capital(e)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
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
  const c = useChoices()
  const [model, setModel] = useState<string | null>(null)
  const [effort, setEffort] = useState<string | null>(null)
  const start = useStartRun(onClose)
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-[440px]" data-testid="extract-dialog">
        <DialogHeader>
          <DialogTitle>Extract data</DialogTitle>
        </DialogHeader>
        <DialogBody className="space-y-4">
          <ModelSelect
            value={model ?? c.model}
            models={c.models}
            onChange={setModel}
            testid="extract-model"
          />
          <MoreOptions>
            <EffortSelect
              value={effort ?? c.effort}
              efforts={c.efforts}
              onChange={setEffort}
              testid="extract-effort"
            />
          </MoreOptions>
        </DialogBody>
        <DialogFooter className="justify-between">
          <span className="text-caption text-muted-ink" data-testid="extract-limit">
            {c.cost !== null ? `Stops at ${money(c.cost)}` : null}
          </span>
          <Button
            disabled={start.isPending}
            onClick={() => start.mutate({ params: { path: { paper_id: paperId } }, body: { model, effort } })}
            data-testid="start-extraction"
          >
            {start.isPending ? <CircleNotchIcon className="animate-spin" /> : null}
            Extract data
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function StartCard({
  paperId,
  failed,
  onRetry,
  onDetails,
  onDismiss,
}: {
  paperId: string
  failed: RunInfo | null
  onRetry: (run: RunInfo) => void
  onDetails: () => void
  onDismiss: (run: RunInfo) => void
}) {
  const c = useChoices()
  const [model, setModel] = useState<string | null>(null)
  const start = useStartRun()
  const alert = failed ? endedAlert(failed) : null
  return (
    <div
      className="pointer-events-auto w-[360px] rounded-card border border-line bg-surface p-4 shadow-float"
      data-testid="extract-card"
      data-state={alert ? "failed" : "new"}
    >
      {failed && alert ? (
        <div className="space-y-4">
          <div className="flex items-start gap-2">
            <div className="min-w-0 flex-1 space-y-1">
              <p className="flex items-center gap-2 text-value text-danger">
                <WarningIcon weight="fill" className="size-icon" aria-hidden /> {alert.title}
              </p>
              <p
                className="text-caption text-muted-ink"
                data-testid="extract-card-reason"
                title={failed.error ?? ""}
              >
                {alert.reason}
              </p>
            </div>
            <Button
              variant="quiet"
              size="icon-sm"
              aria-label="Dismiss"
              onClick={() => onDismiss(failed)}
              data-testid="extract-card-dismiss"
            >
              <XIcon />
            </Button>
          </div>
          <div className="flex gap-2">
            <Button onClick={() => onRetry(failed)} data-testid="extract-card-retry">
              {alert.action}
            </Button>
            <Button variant="ghost" onClick={onDetails} data-testid="extract-card-details">
              Details
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex gap-2">
          <div className="min-w-0 flex-1">
            <ModelSelect
              value={model ?? c.model}
              models={c.models}
              onChange={setModel}
              testid="extract-card-model"
            />
          </div>
          <Button
            disabled={start.isPending}
            onClick={() => start.mutate({ params: { path: { paper_id: paperId } }, body: { model } })}
            data-testid="extract-card-start"
          >
            {start.isPending ? <CircleNotchIcon className="animate-spin" /> : null}
            Extract data
          </Button>
        </div>
      )}
    </div>
  )
}

export function ContinueDialog({ run, onClose }: { run: RunInfo | null; onClose: () => void }) {
  return (
    <Dialog open={run !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-[480px]" data-testid="continue-dialog">
        {run && isResumable(run) ? <ResumeForm key={run.id} run={run} onClose={onClose} /> : null}
        {run && !isResumable(run) ? <ContinueForm key={run.id} run={run} onClose={onClose} /> : null}
      </DialogContent>
    </Dialog>
  )
}

function GuidanceFields({
  text,
  onText,
  notes,
  onNotes,
  placeholder,
  onSubmit,
}: {
  text: string
  onText: (t: string) => void
  notes: boolean
  onNotes: (on: boolean) => void
  placeholder: string
  onSubmit: () => void
}) {
  return (
    <>
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
      <div className="flex items-center gap-3">
        <Switch id="continue-notes" checked={notes} onCheckedChange={onNotes} data-testid="continue-notes" />
        <label htmlFor="continue-notes" className="text-control">
          Include my review notes
        </label>
      </div>
    </>
  )
}

function ResumeForm({ run, onClose }: { run: RunInfo; onClose: () => void }) {
  const [text, setText] = useState("")
  const [notes, setNotes] = useState(true)
  const resume = useResumeRun(onClose)
  const submit = () => {
    if (resume.isPending) return
    resume.mutate({
      params: { path: { run_id: run.id } },
      body: { guidance: text.trim() || null, review_notes: notes },
    })
  }
  return (
    <>
      <DialogHeader>
        <DialogTitle>Resume extraction</DialogTitle>
      </DialogHeader>
      <DialogBody className="space-y-4">
        <p className="text-caption text-muted-ink">It picks up from what it had found.</p>
        <GuidanceFields
          text={text}
          onText={setText}
          notes={notes}
          onNotes={setNotes}
          placeholder="Optional"
          onSubmit={submit}
        />
      </DialogBody>
      <DialogFooter>
        <Button disabled={resume.isPending} onClick={submit} data-testid="continue-start">
          {resume.isPending ? <CircleNotchIcon className="animate-spin" /> : null}
          Resume
        </Button>
      </DialogFooter>
    </>
  )
}

function ContinueForm({ run, onClose }: { run: RunInfo; onClose: () => void }) {
  const c = useChoices()
  const [text, setText] = useState("")
  const [notes, setNotes] = useState(true)
  const [model, setModel] = useState<string | null>(null)
  const [effort, setEffort] = useState<string | null>(null)
  const proceed = useContinueRun(onClose)
  const guidance = text.trim()
  const needsGuidance = run.status !== "budget_exhausted"
  const ready = !proceed.isPending && (guidance !== "" || !needsGuidance)
  const submit = () => {
    if (!ready) return
    proceed.mutate({
      params: { path: { run_id: run.id } },
      body: { guidance: guidance || null, review_notes: notes, model, effort },
    })
  }
  const models = c.models.includes(run.extraction_model) ? c.models : [...c.models, run.extraction_model]
  return (
    <>
      <DialogHeader>
        <DialogTitle>Continue extraction</DialogTitle>
      </DialogHeader>
      <DialogBody className="space-y-4">
        <p className="text-caption text-muted-ink">
          A new extraction starts from this one's data. This one stays as it is.
        </p>
        <GuidanceFields
          text={text}
          onText={setText}
          notes={notes}
          onNotes={setNotes}
          placeholder={needsGuidance ? "Table 3 also gives soil nitrogen at two depths" : "Optional"}
          onSubmit={submit}
        />
        <MoreOptions>
          <span className="text-label">Model</span>
          <ModelSelect
            value={model ?? run.extraction_model}
            models={models}
            onChange={setModel}
            testid="continue-model"
          />
          <EffortSelect
            value={effort ?? run.effort}
            efforts={c.efforts}
            onChange={setEffort}
            testid="continue-effort"
          />
        </MoreOptions>
      </DialogBody>
      <DialogFooter className="justify-between">
        <span className="text-caption text-muted-ink" data-testid="continue-limit">
          {c.cost !== null ? `Stops at ${money(c.cost)}` : null}
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
  const stop = useStopRun(onClose)
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
            onClick={() => run && stop.mutate({ params: { path: { run_id: run.id } } })}
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
