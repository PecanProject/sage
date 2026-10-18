import { Collapsible } from "@base-ui/react/collapsible"
import {
  ArrowDownIcon,
  CaretRightIcon,
  CheckIcon,
  CircleNotchIcon,
  CopyIcon,
  PauseIcon,
  WarningIcon,
  XIcon,
} from "@phosphor-icons/react"
import { type ReactNode, useEffect, useState } from "react"
import { toast } from "sonner"
import { StickToBottom, useStickToBottomContext } from "use-stick-to-bottom"
import { type RunEvent, useRunEvents } from "@/api/events"
import { RunUsage } from "@/components/paper/run-usage"
import { Button } from "@/components/ui/button"
import { Dialog, DialogTitle, SheetContent } from "@/components/ui/dialog"
import { Segmented } from "@/components/ui/segmented"
import { SidePanel } from "@/components/ui/side-panel"
import { type Line, type Problem, type Section, type StepState, type Turn, timeline } from "@/lib/feed"
import {
  capital,
  carryOnLabel,
  type Doc,
  duration,
  isLive,
  modelName,
  money,
  type RunInfo,
  runStatusLabel,
  stamp,
  totalCost,
} from "@/lib/format"
import { cn } from "@/lib/utils"

export function DetailsDrawer({
  run,
  open,
  doc,
  onClose,
  onContinue,
}: {
  run: RunInfo | null
  open: boolean
  doc: Doc | undefined
  onClose: () => void
  onContinue: (run: RunInfo) => void
}) {
  return (
    <Dialog open={open && run !== null} onOpenChange={(o) => !o && onClose()}>
      <SheetContent data-testid="details-drawer">
        {run ? (
          <RunDetails
            key={run.id}
            run={run}
            doc={doc}
            title={<DialogTitle>This run</DialogTitle>}
            onContinue={onContinue}
          />
        ) : null}
      </SheetContent>
    </Dialog>
  )
}

export function RunDock({ run, doc }: { run: RunInfo; doc: Doc | undefined }) {
  return (
    <SidePanel data-testid="run-dock">
      <RunDetails key={run.id} run={run} doc={doc} title={<h2 className="text-title">This run</h2>} />
    </SidePanel>
  )
}

function useNow(on: boolean): number {
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    if (!on) return
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [on])
  return now
}

function Elapsed({ from, to, live }: { from: string | null; to: string | null; live: boolean }) {
  const now = useNow(live && !to)
  return <>{to || live ? duration(from, to, now) : ""}</>
}

const STEP_ICON: Record<StepState, ReactNode> = {
  todo: <span className="size-1.5 rounded-full bg-line" />,
  active: <CircleNotchIcon className="size-4 animate-spin text-muted-ink" />,
  done: <CheckIcon weight="bold" className="size-4 text-good" />,
  failed: <XIcon weight="bold" className="size-4 text-danger" />,
  stopped: <PauseIcon weight="fill" className="size-4 text-check" />,
}

const TONE_ICON: Record<Line["tone"], ReactNode> = {
  info: <span className="size-1 rounded-full bg-muted-ink" />,
  good: <CheckIcon weight="bold" className="size-3.5 text-good" />,
  warn: <WarningIcon weight="fill" className="size-3.5 text-check" />,
  bad: <XIcon weight="bold" className="size-3.5 text-danger" />,
}

type Tab = "activity" | "usage" | "log"

const TABS: { value: Tab; label: string; testid: string }[] = [
  { value: "activity", label: "Activity", testid: "tab-activity" },
  { value: "usage", label: "Usage", testid: "tab-usage" },
  { value: "log", label: "Log", testid: "tab-log" },
]

function RunDetails({
  run,
  doc,
  title,
  onContinue,
}: {
  run: RunInfo
  doc: Doc | undefined
  title: ReactNode
  onContinue?: (run: RunInfo) => void
}) {
  const [tab, setTab] = useState<Tab>("activity")
  const events = useRunEvents(run.id, `${run.resumes ?? 0}:${run.started_at ?? ""}`)
  const live = isLive(run)
  const sections = timeline(events, run, doc)
  const ended = live ? null : (events.at(-1)?.ts ?? null)
  const failedInFeed = sections.some((s) => s.entries.some((e) => e.kind === "line" && e.tone === "bad"))
  return (
    <>
      <header className="border-b border-line p-inset">
        <div className="pr-8">
          {title}
          <p className="mt-1 text-caption text-muted-ink">
            {runStatusLabel(run)} · {stamp(run.created_at)}
          </p>
        </div>
        <RunStats run={run} live={live} ended={ended} />
        {run.error && !failedInFeed ? (
          <p className="mt-5 line-clamp-3 text-control text-danger" title={run.error} data-testid="run-error">
            {capital(run.error)}
          </p>
        ) : null}
        <Segmented label="View" value={tab} onChange={setTab} options={TABS} className="mt-6" />
      </header>
      {tab === "activity" ? (
        <Activity sections={sections} live={live} />
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto px-inset pt-inset pb-10">
          {tab === "usage" ? <RunUsage run={run} events={events} /> : <RunLog events={events} />}
        </div>
      )}
      {onContinue && !live ? (
        <footer className="flex h-14 shrink-0 items-center justify-end border-t border-line px-inset">
          <Button variant="outline" size="compact" onClick={() => onContinue(run)} data-testid="resume-run">
            {carryOnLabel(run, "Continue…")}
          </Button>
        </footer>
      ) : null}
    </>
  )
}

function RunStats({ run, live, ended }: { run: RunInfo; live: boolean; ended: string | null }) {
  return (
    <dl className="mt-6 grid grid-cols-2 gap-x-6 gap-y-5">
      <Stat label="Model" testid="run-model">
        {modelName(run.extraction_model)} <span className="text-muted-ink">· {run.effort}</span>
      </Stat>
      <Stat label="Time" testid="run-time">
        <Elapsed from={run.started_at ?? run.created_at ?? null} to={run.finished_at ?? ended} live={live} />
      </Stat>
      <Stat label="Cost" testid="run-cost">
        {money(totalCost(run))} <span className="text-muted-ink">of {money(run.max_cost_usd)}</span>
      </Stat>
      <Stat label="Turns" testid="run-turns">
        {run.turns} <span className="text-muted-ink">of {run.max_turns}</span>
      </Stat>
    </dl>
  )
}

function Activity({ sections, live }: { sections: Section[]; live: boolean }) {
  return (
    <StickToBottom className="relative min-h-0 flex-1" initial={live ? "instant" : false} resize="smooth">
      <StickToBottom.Content
        scrollClassName="overflow-y-auto [scrollbar-gutter:auto]!"
        className="px-inset pt-4 pb-8"
      >
        <ol className="space-y-1" data-testid="feed">
          {sections.map((s) => {
            const open = opensByDefault(s)
            return <Stage key={`${s.stage}:${s.state}:${open}`} section={s} open={open} live={live} />
          })}
        </ol>
      </StickToBottom.Content>
      {live ? <JumpToLatest /> : null}
    </StickToBottom>
  )
}

function RunLog({ events }: { events: RunEvent[] }) {
  const text = events.map((e) => JSON.stringify(e)).join("\n")
  return (
    <>
      <div className="sticky -top-inset z-10 -mx-inset -mt-inset mb-4 flex h-12 items-center justify-between border-b border-line bg-surface px-inset">
        <span className="text-caption text-muted-ink tabular-nums">
          {events.length} {events.length === 1 ? "event" : "events"}
        </span>
        <CopyButton text={text} />
      </div>
      <pre
        className="text-caption leading-5 break-all whitespace-pre-wrap text-muted-ink"
        data-testid="raw-events"
      >
        {text}
      </pre>
    </>
  )
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  const copy = () =>
    navigator.clipboard.writeText(text).then(
      () => {
        setCopied(true)
        setTimeout(() => setCopied(false), 1500)
      },
      () => toast.error("Couldn't copy the log"),
    )
  return (
    <Button
      variant="quiet"
      size="compact"
      disabled={!text}
      onClick={() => void copy()}
      data-testid="copy-log"
    >
      {copied ? <CheckIcon className="size-4 text-good" /> : <CopyIcon className="size-4" />}
      {copied ? "Copied" : "Copy"}
    </Button>
  )
}

function Stat({ label, testid, children }: { label: string; testid: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-caption text-muted-ink">{label}</dt>
      <dd className="mt-1 truncate text-body text-ink tabular-nums" data-testid={testid}>
        {children}
      </dd>
    </div>
  )
}

function JumpToLatest() {
  const { isAtBottom, scrollToBottom } = useStickToBottomContext()
  if (isAtBottom) return null
  return (
    <Button
      variant="outline"
      size="compact"
      className="absolute bottom-5 left-1/2 -translate-x-1/2 rounded-full shadow-float"
      onClick={() => void scrollToBottom()}
      data-testid="jump-to-latest"
    >
      <ArrowDownIcon className="size-3.5" /> Latest
    </Button>
  )
}

function Fold({
  trigger,
  defaultOpen = false,
  disabled = false,
  children,
}: {
  trigger: ReactNode
  defaultOpen?: boolean
  disabled?: boolean
  children: ReactNode
}) {
  const { stopScroll } = useStickToBottomContext()
  return (
    <Collapsible.Root defaultOpen={defaultOpen} disabled={disabled} onOpenChange={() => stopScroll()}>
      <Collapsible.Trigger className="group flex w-full cursor-pointer items-center gap-2 rounded-inline text-left outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sage disabled:cursor-default">
        {trigger}
        <CaretRightIcon
          className={cn(
            "size-3 shrink-0 text-muted-ink transition-transform duration-150 group-data-[panel-open]:rotate-90",
            disabled && "invisible",
          )}
        />
      </Collapsible.Trigger>
      <Collapsible.Panel>{children}</Collapsible.Panel>
    </Collapsible.Root>
  )
}

function opensByDefault(s: Section): boolean {
  if (!s.entries.length) return false
  return s.state === "active" || s.state === "failed" || s.state === "stopped" || s.stage === "done"
}

function Stage({ section: s, open, live }: { section: Section; open: boolean; live: boolean }) {
  const latest = s.entries.findLast((e) => e.kind === "turn")
  return (
    <li data-testid="step" data-state={s.state}>
      <Fold
        defaultOpen={open}
        disabled={!s.entries.length}
        trigger={
          <span className="flex h-10 min-w-0 flex-1 items-center gap-3">
            <span className="flex size-4 shrink-0 items-center justify-center">{STEP_ICON[s.state]}</span>
            <span
              className={cn(
                "flex-1 text-value",
                s.state === "todo" && "text-muted-ink",
                s.state === "failed" && "text-danger",
              )}
            >
              {s.label}
            </span>
            <span className="text-caption text-muted-ink tabular-nums">
              <Elapsed from={s.started} to={s.finished} live={live && s.state === "active"} />
            </span>
          </span>
        }
      >
        <div className="mb-3 ml-2 space-y-5 border-l border-line pt-2 pb-2 pl-6">
          {s.entries.map((e) => {
            switch (e.kind) {
              case "line":
                return <FeedLine key={e.key} line={e} />
              case "turn":
                return (
                  <TurnView key={e.key} turn={e} waiting={live && s.state === "active" && e === latest} />
                )
              case "problems":
                return <Problems key={e.key} problems={e.problems} />
              case "summary":
                return (
                  <p key={e.key} className="text-control leading-6 text-ink" data-testid="run-summary">
                    {e.text}
                  </p>
                )
              default:
                return e satisfies never
            }
          })}
        </div>
      </Fold>
    </li>
  )
}

function FeedLine({ line }: { line: Line }) {
  return (
    <div
      className={cn(
        "grid grid-cols-[14px_1fr] gap-2.5 text-control",
        line.tone === "bad" ? "text-danger" : "text-ink",
      )}
      data-testid="feed-line"
      data-tone={line.tone}
    >
      <span className="flex h-5 items-center justify-center">{TONE_ICON[line.tone]}</span>
      <span className="min-w-0 break-words">{line.text}</span>
    </div>
  )
}

function TurnView({ turn, waiting }: { turn: Turn; waiting: boolean }) {
  if (!turn.answered && !waiting) return null
  return (
    <div className="space-y-2" data-testid="turn">
      {turn.thinking ? (
        <Fold trigger={<span className="text-caption text-muted-ink">Thinking</span>}>
          <p className="mt-2 mb-1 border-l-2 border-line pl-3 text-caption leading-5 whitespace-pre-wrap text-muted-ink">
            {turn.thinking}
          </p>
        </Fold>
      ) : null}
      {turn.say ? <p className="text-control text-ink">{turn.say.replaceAll("`", "")}</p> : null}
      {turn.actions.map((a) => (
        <FeedLine key={a.key} line={a} />
      ))}
      {turn.problems.length ? <Problems problems={turn.problems} /> : null}
      {!turn.answered ? (
        <p className="flex items-center gap-2.5 text-control text-muted-ink" data-testid="pending-turn">
          <CircleNotchIcon className="size-3.5 animate-spin" /> Thinking
          <span className="text-caption tabular-nums">
            <Elapsed from={turn.asked} to={null} live />
          </span>
        </p>
      ) : null}
    </div>
  )
}

function Problems({ problems }: { problems: Problem[] }) {
  const n = problems.length
  return (
    <Fold
      trigger={
        <span className="grid grid-cols-[14px_auto] items-center gap-2.5 text-caption text-muted-ink">
          <span className="flex justify-center">
            <WarningIcon weight="fill" className="size-3.5 text-check" />
          </span>
          {n} {n === 1 ? "problem" : "problems"}
        </span>
      }
    >
      <ul className="mt-2 ml-6 space-y-3" data-testid="problems">
        {problems.map((p) => (
          <li key={p.key} className="text-caption leading-5 text-ink" title={p.full}>
            {p.field ? <span className="block font-mono text-caption text-muted-ink">{p.field}</span> : null}
            {p.text}
          </li>
        ))}
      </ul>
    </Fold>
  )
}
