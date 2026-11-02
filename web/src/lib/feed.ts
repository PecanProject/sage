import type { RunEvent } from "@/api/events"
import { capital, isLive, itemCount, limitTitle, type RunInfo, several } from "@/lib/format"
import { obj } from "@/lib/utils"

type Stage = Extract<RunEvent, { type: "stage.started" }>["stage"]
type Step = Stage | "done"

const STEPS: { stage: Step; label: string }[] = [
  { stage: "parse", label: "Read paper" },
  { stage: "agent", label: "Extract" },
  { stage: "settle", label: "Check" },
  { stage: "done", label: "Done" },
]

/** The steps every run of a driver shows. SAGE's own loop adds Check when it settles what the folder compiles to. An
 * agent's run, SAGE's or a terminal's, is one Extract step: it checks as it goes. */
const BASE: Record<RunInfo["driver"], Step[]> = {
  sage: ["parse", "agent", "done"],
  agent: ["agent", "done"],
  here: ["agent", "done"],
}

type Tone = "info" | "good" | "warn" | "bad"

/** `code` is a command or path the line names, shown as it is. */
export type Line = { kind: "line"; key: string; tone: Tone; text: string; code?: string }
export type Problem = { key: string; field: string | null; text: string; full: string }
/** One check of the folder, which every driver runs: what blocks, its notes, and the first findings. */
export type Check = {
  kind: "check"
  key: string
  tone: Tone
  text: string
  snapshot: number | null
  /** How many findings there were; `problems` holds only the first few. */
  total: number
  problems: Problem[]
}
export type Turn = {
  kind: "turn"
  key: string
  asked: string | null
  answered: boolean
  thinking: string
  say: string
  actions: (Line | Check)[]
}
type Entry = Line | Check | Turn | { kind: "summary"; key: string; text: string }

export type StepState = "todo" | "active" | "done" | "failed" | "stopped"

export type Section = {
  stage: Step
  label: string
  state: StepState
  started: string | null
  finished: string | null
  entries: Entry[]
}

type ToolCall = Extract<RunEvent, { type: "llm.tool_call" }>
type CheckRan = Extract<RunEvent, { type: "check.ran" }>
type ToolResult = Extract<RunEvent, { type: "llm.tool_result" }>
type StageFinished = Extract<RunEvent, { type: "stage.finished" }>

function describeTool(call: ToolCall, result: ToolResult | undefined): { tone: Tone; text: string } {
  const args = obj(call.args)
  const failed = result !== undefined && !result.ok
  switch (call.tool) {
    case "done":
      return { tone: failed ? "warn" : "good", text: failed ? "Asked to finish, not yet" : "Finished" }
    default: {
      const file = FILE_TOOL[call.tool]
      const text = file ? file(args) : call.tool
      return { tone: failed ? "warn" : WRITES.has(call.tool) ? "good" : "info", text }
    }
  }
}

const WRITES = new Set(["write_file", "edit_file", "create_directory"])

const str = (v: unknown) => (typeof v === "string" ? v : "")

/** A folder the agent named, with the paper's own folder in words. */
const where = (v: unknown) => (["", ".", "./"].includes(str(v)) ? "the folder" : str(v))

/** SAGE's agent works on the paper's folder with file tools; each reads as what it did to which file. */
const FILE_TOOL: Record<string, (args: Record<string, unknown>) => string> = {
  write_file: (a) => `Wrote ${str(a.path)}`,
  edit_file: (a) => `Edited ${str(a.path)}`,
  read_file: (a) => `Read ${str(a.path)}`,
  list_directory: (a) => `Listed ${where(a.path)}`,
  file_info: (a) => `Looked up ${str(a.path)}`,
  view_image: (a) => `Looked at ${str(a.path)}`,
  find_files: (a) => `Looked for ${str(a.pattern)}`,
  search_files: (a) => `Searched ${where(a.path)} for ${str(a.pattern)}`,
  create_directory: (a) => `Made ${str(a.path)}`,
}

function clip(text: string, n = 160): string {
  const one = text.replace(/\s+/g, " ").trim()
  return one.length <= n ? one : `${one.slice(0, n - 1)}…`
}

/** `match` holds the field the message names, then the message. */
function problemOf(key: string, full: string, match: RegExpExecArray | null): Problem {
  const body = match?.[2] ?? full
  return {
    key,
    field: match?.[1] ?? null,
    text: capital(clip(body.split(/(?<=[.;])\s+(?=[A-Z])/)[0] ?? body, 220)),
    full,
  }
}

function problem(key: string, error: string): Problem {
  return problemOf(key, error, /^([\w.-]+): (.*)$/s.exec(error))
}

/** A finding as check prints it: `file:line  target: message`, with `warning` first for a note. */
const FINDING = /^(?:warning\s+)?\S+\s+([^\s:]+): (.*)$/s

function finding(key: string, line: string): Problem {
  const m = FINDING.exec(line)
  return m ? problemOf(key, line, m) : problem(key, line)
}

/** What the scientist asked a resumed or continued run to add or fix. */
function guidanceLine(key: string, guidance: string): Line {
  return { kind: "line", key: `${key}.guidance`, tone: "info", text: `Guidance: ${guidance}` }
}

function checkEntry(key: string, e: CheckRan): Check {
  const notes = e.warnings ? several(e.warnings, "note") : null
  const head = e.blocking
    ? `Check found ${several(e.blocking, "problem")}${notes ? `, ${notes}` : ""}`
    : `Check passed${notes ? ` with ${notes}` : ""}`
  const marked = e.marked?.length ? ` · ${e.marked.length} marked missing` : ""
  const lines = e.lines ?? []
  return {
    kind: "check",
    key,
    tone: e.blocking ? "warn" : "good",
    text: `${head} · ${itemCount(e.records)}${marked}`,
    snapshot: e.changed ? (e.snapshot ?? null) : null,
    total: e.blocking + e.warnings,
    problems: lines.map((line, j) => finding(`${key}.${j}`, line)),
  }
}

type Counts = NonNullable<StageFinished["counts"]>

function settled(c: Counts): string {
  const parts = [
    c.verified ? `${c.verified} ${c.verified === 1 ? "looks" : "look"} good` : null,
    c.flagged ? `${c.flagged} to check` : null,
    c.unresolved ? `${c.unresolved} missing` : null,
  ].filter(Boolean)
  return `${itemCount(c.records)}${parts.length ? `: ${parts.join(", ")}` : ""}`
}

export function timeline(events: RunEvent[], run: RunInfo, modelName: (id: string) => string): Section[] {
  const results = new Map<string, ToolResult>()
  for (const e of events) if (e.type === "llm.tool_result") results.set(e.call_id, e)
  const log = withStages(untilError(events), run)
  const steps = stepsOf(log, run)
  const sections = progress(log, run, steps).map(
    (s): Section => ({ ...s, started: null, finished: null, entries: [] }),
  )
  const by = Object.fromEntries(sections.map((s) => [s.stage, s])) as Record<Step, Section>
  const place = new Map(sections.map((s, i) => [s.stage, i]))
  let section = sections[0] ?? by.done
  let turn: Turn | null = null
  const push = (entry: Entry) => {
    section.entries.push(entry)
    if (entry.kind !== "turn") turn = null
  }
  /** What the agent did, inside the turn it did it in when there is one. */
  const act = (entry: Line | Check) => {
    if (turn) {
      turn.answered = true
      turn.actions.push(entry)
    } else push(entry)
  }
  // A resume's marker waits for Extract, the one step whose work carries on across attempts, so it opens the new
  // attempt there. The run's opening lines survive the restarts that replace every other step's lines.
  let resumed: Line[] = []
  const opening = new Set<string>()
  for (const { e, key } of log) {
    switch (e.type) {
      case "run.started":
        if (e.resumed) {
          const model = e.extraction_model ? ` with ${modelName(e.extraction_model)}` : ""
          resumed = [{ kind: "line", key, tone: "info", text: `Resumed${model}` }]
          if (e.guidance) resumed.push(guidanceLine(key, e.guidance))
          break
        }
        if (e.continued_from)
          push({ kind: "line", key, tone: "info", text: "Continued from an earlier extraction" })
        if (e.guidance) push(guidanceLine(key, e.guidance))
        for (const entry of section.entries) opening.add(entry.key)
        break
      case "stage.started":
        // A stage the next one cut short ends where that one starts. A restart clears this stage's time and every
        // later one's, so each step shows its latest attempt.
        if (section.started && !section.finished) section.finished = e.ts ?? null
        for (const later of sections.slice(place.get(e.stage) ?? 0)) {
          later.started = null
          later.finished = null
          if (later.stage !== "agent") later.entries = later.entries.filter((x) => opening.has(x.key))
        }
        section = by[e.stage]
        section.started = e.ts ?? null
        turn = null
        if (e.stage === "agent") {
          for (const line of resumed) push(line)
          resumed = []
        }
        break
      case "stage.finished":
        by[e.stage].finished = e.ts ?? null
        if (e.detail && e.stage !== "agent")
          push({
            kind: "line",
            key,
            tone: "info",
            text: e.counts ? settled(e.counts) : capital(e.detail),
          })
        break
      case "llm.request": {
        const asked: Turn = {
          kind: "turn",
          key,
          asked: e.ts ?? null,
          answered: false,
          thinking: "",
          say: "",
          actions: [],
        }
        push(asked)
        turn = asked
        break
      }
      case "llm.thinking":
      case "llm.text":
        if (turn && e.text.trim()) {
          turn.answered = true
          if (e.type === "llm.thinking") turn.thinking += `${turn.thinking ? "\n\n" : ""}${e.text.trim()}`
          else turn.say += `${turn.say ? " " : ""}${e.text.trim()}`
        }
        break
      case "llm.tool_call":
        // check.ran says each check itself, so the check tool's own line would say it twice.
        if (e.tool === "check") {
          if (turn) turn.answered = true
          break
        }
        act({ kind: "line", key, ...describeTool(e, results.get(e.call_id)) })
        break
      case "check.ran":
        act(checkEntry(key, e))
        break
      case "agent.asked":
        act({
          kind: "line",
          key,
          tone: "warn",
          text: `Asked: ${e.title}`,
          code: e.command && e.command !== e.title ? e.command : undefined,
        })
        break
      case "agent.answered":
        // SAGE's own refusals come with a note that says what was refused.
        if (e.by === "you")
          act({
            kind: "line",
            key,
            tone: e.allowed ? "good" : "bad",
            text: e.allowed ? "You allowed it" : "You denied it",
          })
        break
      case "note":
        push({
          kind: "line",
          key,
          tone: e.level === "warning" ? "warn" : "info",
          text: capital(clip(e.text, 400)),
        })
        break
      case "run.finished":
        section = by.done
        section.finished = e.ts ?? null
        if (e.status === "budget_exhausted")
          push({ kind: "line", key: `${key}.limit`, tone: "warn", text: limitTitle(run) })
        if (e.summary) push({ kind: "summary", key, text: e.summary })
        break
      case "run.error":
        push({ kind: "line", key, tone: "bad", text: capital(e.message) })
        break
      case "llm.tool_result":
      case "record.settled":
      case "usage":
        break
      default:
        e satisfies never
    }
  }
  for (const line of resumed) push(line)
  const last = log.at(-1)?.e.ts ?? null
  for (const s of sections)
    if (s.started && !s.finished && (s.state === "failed" || s.state === "stopped")) s.finished = last
  return sections
}

type Logged = { e: RunEvent; key: string }

/** An agent's run, SAGE's or a terminal's, logs no stages: it extracts from its start until it ends. Its log gets the
 * Extract step's start and end where those are, so every driver reads the same way. */
function withStages(log: Logged[], run: RunInfo): Logged[] {
  if (run.driver === "sage") return log
  return log.flatMap((l): Logged[] => {
    const { e, key } = l
    if (e.type === "run.started" && !e.resumed)
      return [l, { e: { type: "stage.started", stage: "agent", ts: e.ts }, key: `${key}.stage` }]
    if (e.type === "run.finished" && (e.status === "finished" || e.status === "budget_exhausted"))
      return [{ e: { type: "stage.finished", stage: "agent", ts: e.ts }, key: `${key}.stage` }, l]
    return [l]
  })
}

function stepsOf(log: Logged[], run: RunInfo): { stage: Step; label: string }[] {
  const base = BASE[run.driver]
  const logged = new Set<Step>(log.flatMap(({ e }) => (e.type === "stage.started" ? [e.stage] : [])))
  return STEPS.filter((s) => base.includes(s.stage) || logged.has(s.stage))
}

function untilError(events: RunEvent[]): Logged[] {
  let errored = false
  return events.flatMap((e, i) => {
    if (e.type === "run.started") errored = false
    else if (errored) return []
    if (e.type === "run.error") errored = true
    return [{ e, key: `${i}` }]
  })
}

function progress(
  log: Logged[],
  run: RunInfo,
  steps: { stage: Step; label: string }[],
): { stage: Step; label: string; state: StepState }[] {
  const first: Stage = steps[0]?.stage === "agent" ? "agent" : "parse"
  const place = new Map(steps.map((s, i) => [s.stage, i]))
  const done = new Set<Step>()
  const cut = new Set<Step>()
  let current: Stage | null = null
  for (const { e } of log) {
    if (e.type === "stage.started") {
      if (current && !done.has(current)) cut.add(current)
      for (const { stage } of steps.slice(place.get(e.stage) ?? 0)) {
        cut.delete(stage)
        done.delete(stage)
      }
      current = e.stage
    } else if (e.type === "stage.finished") done.add(e.stage)
    // An agent's run that failed or was stopped ends on the step it was on, as SAGE's does with its error.
    else if (
      e.type === "run.finished" &&
      (run.driver === "sage" || !["failed", "interrupted"].includes(e.status))
    )
      done.add("done")
  }
  const errored = log.at(-1)?.e.type === "run.error"
  const live = isLive(run)
  const running = live ? (current ?? first) : null
  const salvaged = cut.size > 0 && current !== null && done.has(current)
  const stoppedAt = live || done.has("done") || salvaged ? null : (current ?? first)
  const stopped: StepState = errored || run.status === "failed" ? "failed" : "stopped"
  const limited = run.status === "budget_exhausted" && done.has("done")
  return steps.map((s) => ({
    ...s,
    state: cut.has(s.stage)
      ? "stopped"
      : s.stage === stoppedAt || (s.stage === "done" && limited)
        ? stopped
        : done.has(s.stage)
          ? "done"
          : s.stage === running
            ? "active"
            : "todo",
  }))
}
