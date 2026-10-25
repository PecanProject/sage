import { Collapsible } from "@base-ui/react/collapsible"
import {
  ArrowDownIcon,
  CaretRightIcon,
  CheckIcon,
  CircleNotchIcon,
  HandPalmIcon,
  PauseIcon,
  WarningIcon,
  XIcon,
} from "@phosphor-icons/react"
import { type ReactNode, useEffect, useState } from "react"
import { StickToBottom, useStickToBottomContext } from "use-stick-to-bottom"
import { type RunEvent, useRunEvents } from "@/api/events"
import { BrandIcon } from "@/components/brand-icon"
import { CopyButton } from "@/components/copy"
import { RunUsage } from "@/components/paper/run-usage"
import { Button } from "@/components/ui/button"
import { Dialog, DialogTitle, SheetContent } from "@/components/ui/dialog"
import { Segmented } from "@/components/ui/segmented"
import { SidePanel } from "@/components/ui/side-panel"
import { Tip } from "@/components/ui/tooltip"
import { TERMINAL, useDriverOf } from "@/lib/agents"
import {
  type Check,
  type Line,
  type Problem,
  type Section,
  type StepState,
  type Turn,
  timeline,
} from "@/lib/feed"
import {
  capital,
  carryOnLabel,
  duration,
  isLive,
  money,
  NEEDS_OK,
  type RunInfo,
  runStatusLabel,
  stamp,
} from "@/lib/format"
import { useModelTitle } from "@/lib/models"
import { useAnswerAgent } from "@/lib/paper-state"
import { cn } from "@/lib/utils"

export function DetailsDrawer({
  run,
  open,
  onClose,
  onContinue,
  onStop,
}: {
  run: RunInfo | null
  open: boolean
  onClose: () => void
  onContinue: (run: RunInfo) => void
  onStop: () => void
}) {
  return (
    <Dialog open={open && run !== null} onOpenChange={(o) => !o && onClose()}>
      <SheetContent data-testid="details-drawer">
        {run ? (
          <RunDetails
            key={run.id}
            run={run}
            title={<DialogTitle>This run</DialogTitle>}
            titleClass="pr-8"
            onContinue={onContinue}
            onStop={onStop}
          />
        ) : null}
      </SheetContent>
    </Dialog>
  )
}

export function RunDock({ run, onStop }: { run: RunInfo; onStop: () => void }) {
  return (
    <SidePanel data-testid="run-dock">
      <RunDetails key={run.id} run={run} title={<h2 className="text-title">This run</h2>} onStop={onStop} />
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
  title,
  titleClass,
  onContinue,
  onStop,
}: {
  run: RunInfo
  title: ReactNode
  /** Room the title row leaves at its right, for the drawer's close button. */
  titleClass?: string
  onContinue?: (run: RunInfo) => void
  /** Asks before it stops the run; shown while the run is live. */
  onStop: () => void
}) {
  const modelName = useModelTitle()
  const [tab, setTab] = useState<Tab>("activity")
  const events = useRunEvents(run.id, `${run.resumes ?? 0}:${run.started_at ?? ""}`)
  const live = isLive(run)
  const sections = timeline(events, run, modelName)
  const ended = live ? null : (events.at(-1)?.ts ?? null)
  const failedInFeed = sections.some((s) => s.entries.some((e) => e.kind === "line" && e.tone === "bad"))
  return (
    <>
      <header className="border-b border-line p-inset">
        <div className={cn("flex items-start justify-between gap-4", titleClass)}>
          <div className="min-w-0">
            {title}
            <p className="mt-1 text-caption text-muted-ink">
              {runStatusLabel(run)} · {stamp(run.created_at)}
            </p>
          </div>
          {live ? (
            <Button variant="outline" size="compact" onClick={onStop} data-testid="run-stop">
              Stop
            </Button>
          ) : null}
        </div>
        <RunStats run={run} live={live} ended={ended} events={events} />
        {live && run.waiting ? <AgentAsks run={run} events={events} /> : null}
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

function RunStats({
  run,
  live,
  ended,
  events,
}: {
  run: RunInfo
  live: boolean
  ended: string | null
  events: RunEvent[]
}) {
  const driver = useDriverOf()(run)
  const cost = run.usage?.cost_usd ?? 0
  const checks = events.filter((e) => e.type === "check.ran").length
  const sage = run.driver === "sage"
  return (
    <dl className="mt-6 grid grid-cols-2 gap-x-6 gap-y-5">
      <Stat label={sage ? "Model" : "Agent"} testid="run-model">
        <span className="flex min-w-0 items-center gap-2">
          {driver?.brand ? <BrandIcon brand={driver.brand} /> : null}
          <span className="truncate">{driver?.title ?? TERMINAL}</span>
          {run.effort ? <span className="text-muted-ink">· {run.effort}</span> : null}
        </span>
      </Stat>
      <Stat label="Time" testid="run-time">
        <Elapsed from={run.started_at ?? run.created_at ?? null} to={run.finished_at ?? ended} live={live} />
      </Stat>
      {sage ? (
        <>
          <Stat label="Cost" testid="run-cost">
            {money(cost)} <span className="text-muted-ink">of {money(run.max_cost_usd)}</span>
          </Stat>
          <Stat label="Turns" testid="run-turns">
            {run.turns} <span className="text-muted-ink">of {run.max_turns}</span>
          </Stat>
        </>
      ) : (
        <>
          <Stat label="Checks" testid="run-checks">
            {checks}
          </Stat>
          {/* An agent reports its cost only when its provider bills by the token; a terminal reports nothing. */}
          {cost > 0 ? (
            <Stat label="Cost" testid="run-cost">
              {money(cost)}
            </Stat>
          ) : null}
        </>
      )}
    </dl>
  )
}

/** What the agent driving the run waits for you to allow, with the command or files it names. */
function AgentAsks({ run, events }: { run: RunInfo; events: RunEvent[] }) {
  const answer = useAnswerAgent()
  const asked = events.findLast((e) => e.type === "agent.asked")
  const title = asked?.title ?? run.waiting
  const command = asked ? asked.command : run.waiting
  const reply = (allow: boolean) =>
    answer.mutate({ params: { path: { paper_id: run.paper_id } }, body: { allow } })
  return (
    <div className="mt-5 space-y-3 rounded-card border border-line bg-surface-2 p-4" data-testid="agent-asks">
      <p className="flex items-start gap-2 text-value">
        <Tip label={NEEDS_OK}>
          <span role="img" aria-label={NEEDS_OK} className="flex h-5 shrink-0 items-center">
            <HandPalmIcon weight="fill" className="size-4 text-check" />
          </span>
        </Tip>
        <span className="min-w-0 break-words">{title}</span>
      </p>
      {command && command !== title ? (
        <pre className="rounded-inline border border-line bg-surface px-3 py-2 font-mono text-caption break-all whitespace-pre-wrap text-ink">
          {command}
        </pre>
      ) : null}
      {asked?.paths?.length ? (
        <p className="font-mono text-caption break-all text-muted-ink">{asked.paths.join("\n")}</p>
      ) : null}
      <div className="flex justify-end gap-2">
        <Button
          variant="outline"
          size="compact"
          disabled={answer.isPending}
          onClick={() => reply(false)}
          data-testid="agent-deny"
        >
          Deny
        </Button>
        <Button
          size="compact"
          disabled={answer.isPending}
          onClick={() => reply(true)}
          data-testid="agent-allow"
        >
          Allow
        </Button>
      </div>
    </div>
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
        <CopyButton text={text} failed="Couldn't copy the log" testid="copy-log" />
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
              case "check":
                return <CheckView key={e.key} check={e} />
              case "turn":
                return (
                  <TurnView key={e.key} turn={e} waiting={live && s.state === "active" && e === latest} />
                )
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
      <span className="min-w-0 break-words">
        {line.text}
        {line.code ? (
          <code className="mt-1 block font-mono text-caption break-all text-muted-ink">{line.code}</code>
        ) : null}
      </span>
    </div>
  )
}

/** One check of the folder, the line every driver shares, with its first findings folded under it. */
function CheckView({ check: c }: { check: Check }) {
  return (
    <div className="space-y-2" data-testid="feed-check" data-tone={c.tone}>
      <div className="grid grid-cols-[14px_1fr_auto] gap-2.5 text-control text-ink">
        <span className="flex h-5 items-center justify-center">{TONE_ICON[c.tone]}</span>
        <span className="min-w-0 break-words">{c.text}</span>
        {c.snapshot !== null ? (
          <span className="text-caption leading-5 text-muted-ink tabular-nums">Snapshot {c.snapshot}</span>
        ) : null}
      </div>
      {c.problems.length ? (
        <Problems
          problems={c.problems}
          label={c.problems.length < c.total ? `First ${c.problems.length}` : undefined}
        />
      ) : null}
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
      {turn.actions.map((a) =>
        a.kind === "check" ? <CheckView key={a.key} check={a} /> : <FeedLine key={a.key} line={a} />,
      )}
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

function Problems({ problems, label }: { problems: Problem[]; label?: string }) {
  const n = problems.length
  return (
    <Fold
      trigger={
        <span className="grid grid-cols-[14px_auto] items-center gap-2.5 text-caption text-muted-ink">
          <span className="flex justify-center">
            <WarningIcon weight="fill" className="size-3.5 text-check" />
          </span>
          {label ?? `${n} ${n === 1 ? "problem" : "problems"}`}
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
