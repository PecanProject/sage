import type { RunEvent } from "@/api/events"
import {
  anchorLabel,
  capital,
  type Doc,
  isLive,
  itemCount,
  limitTitle,
  modelName,
  type RunInfo,
  TYPE_PLURAL,
} from "@/lib/format"

type Stage = "parse" | "tables" | "agent" | "settle"
type Step = Stage | "done"

const STEPS: { stage: Step; label: string }[] = [
  { stage: "parse", label: "Read paper" },
  { stage: "tables", label: "Tables" },
  { stage: "agent", label: "Extract" },
  { stage: "settle", label: "Check" },
  { stage: "done", label: "Done" },
]

type Tone = "info" | "good" | "warn" | "bad"

export type Line = { kind: "line"; key: string; tone: Tone; text: string }
export type Problem = { key: string; field: string | null; text: string; full: string }
export type Turn = {
  kind: "turn"
  key: string
  asked: string | null
  answered: boolean
  thinking: string
  say: string
  actions: Line[]
  problems: Problem[]
}
type Entry =
  | Line
  | Turn
  | { kind: "problems"; key: string; problems: Problem[] }
  | { kind: "summary"; key: string; text: string }

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
type ToolResult = Extract<RunEvent, { type: "llm.tool_result" }>
type StageFinished = Extract<RunEvent, { type: "stage.finished" }>
type Tally = NonNullable<ToolResult["tally"]>

/** A tool's figures. Logs written before results carried a tally have only the clipped text, read as well as it
 * allows. */
function tallyOf(result: ToolResult | undefined): Tally | null {
  if (!result) return null
  if (result.tally) return result.tally
  const text = result.text
  const out = obj(json(text))
  switch (result.tool) {
    case "write":
      return { tool: "write", rejected: text.match(/"ok":\s*false/g)?.length ?? 0 }
    case "expand_table": {
      const observations = count(text, "observations")
      return observations === null
        ? null
        : { tool: "expand_table", observations, failed: count(text, "failed") ?? 0 }
    }
    case "check":
      if (!Array.isArray(out.blocking)) return null
      return {
        tool: "check",
        blocking: out.blocking.length,
        warnings: Array.isArray(out.warnings) ? out.warnings.length : 0,
      }
    default:
      return null
  }
}

function json(text: string | undefined): unknown {
  if (text === undefined) return null
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

function count(text: string | undefined, key: string): number | null {
  const m = text === undefined ? null : new RegExp(`"${key}":\\s*(\\d+)`).exec(text)
  return m ? Number(m[1]) : null
}

function obj(v: unknown): Record<string, unknown> {
  return v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
}

const COUNTED: Record<string, [string, string]> = {
  TreatmentPair: ["Comparison", "Comparisons"],
  Management: ["Field event", "Field events"],
  Citation: ["Citation", "Citations"],
}

function plural(type: string, n: number): string {
  const [one, many] = COUNTED[type] ?? [type, TYPE_PLURAL[type] ?? `${type}s`]
  return n === 1 ? one : many
}

function counted(types: string[]): string {
  const counts = new Map<string, number>()
  for (const t of types) counts.set(t, (counts.get(t) ?? 0) + 1)
  return [...counts].map(([t, n]) => `${n} ${plural(t, n)}`).join(", ")
}

function describeTool(
  call: ToolCall,
  result: ToolResult | undefined,
  doc: Doc | undefined,
): { tone: Tone; text: string } {
  const args = obj(call.args)
  const tally = tallyOf(result)
  const failed = result !== undefined && !result.ok
  switch (call.tool) {
    case "write": {
      const recs = Array.isArray(args.records) ? args.records.map(obj) : []
      const deletes = recs.filter((r) => r.delete !== undefined).length
      const types = recs.filter((r) => typeof r.type === "string").map((r) => String(r.type))
      const rejected = tally?.tool === "write" ? tally.rejected : 0
      const parts = [
        types.length ? `Wrote ${counted(types)}` : null,
        deletes ? `removed ${deletes}` : null,
      ].filter(Boolean)
      const text = `${parts.join(", ") || "Wrote nothing"}${rejected ? `; ${rejected} rejected` : ""}`
      return { tone: failed || rejected ? "warn" : "good", text }
    }
    case "expand_table": {
      const id = typeof args.table_id === "string" ? args.table_id : "?"
      const name = anchorLabel(id, doc)
      if (typeof args.irrelevant_reason === "string" && args.irrelevant_reason)
        return { tone: "info", text: `Skipped ${name}: ${args.irrelevant_reason}` }
      // A tool's result is logged with the next model request, so the last turn of a run never gets one.
      if (!result) return { tone: "info", text: `Expanding ${name}` }
      if (tally?.tool === "expand_table") {
        const { observations: n, failed: bad } = tally
        if (bad) return { tone: "warn", text: `Expanded ${name}; ${bad} of ${n} rejected` }
        return { tone: "good", text: `Expanded ${name} into ${n} observations` }
      }
      const first = result.text.split("\n")[0] ?? ""
      const why = first.startsWith("{") ? "" : clip(first, 80).replace(/:$/, "")
      return { tone: "warn", text: `Couldn't expand ${name} yet${why ? `: ${why}` : ""}` }
    }
    case "check": {
      if (!result) return { tone: "info", text: "Ran the check" }
      if (tally?.tool !== "check") {
        // An old log whose clipped result says only whether there were any.
        if (!/"blocking":\s*\[\s*\]/.test(result.text)) return { tone: "warn", text: "Check found problems" }
        return {
          tone: "good",
          text: `Check passed${/"warnings":\s*\[\s*"/.test(result.text) ? " with notes" : ""}`,
        }
      }
      const { blocking, warnings } = tally
      if (blocking)
        return { tone: "warn", text: `Check found ${blocking} problem${blocking === 1 ? "" : "s"}` }
      return {
        tone: "good",
        text: `Check passed${warnings ? ` with ${warnings} note${warnings === 1 ? "" : "s"}` : ""}`,
      }
    }
    case "view": {
      const page = typeof args.page === "number" ? args.page : "?"
      return { tone: "info", text: `Looked at page ${page}${args.bbox ? " (zoomed in)" : ""}` }
    }
    case "read": {
      const n = Array.isArray(args.ids) ? args.ids.length : 0
      return { tone: "info", text: `Read back ${n} record${n === 1 ? "" : "s"}` }
    }
    case "finish": {
      const unresolved = Array.isArray(args.unresolved) ? args.unresolved.length : 0
      return {
        tone: "good",
        text: `Finished${unresolved ? `, with ${unresolved} thing${unresolved === 1 ? "" : "s"} the paper doesn't settle` : ""}`,
      }
    }
    default:
      return { tone: failed ? "warn" : "info", text: call.tool }
  }
}

function clip(text: string, n = 160): string {
  const one = text.replace(/\s+/g, " ").trim()
  return one.length <= n ? one : `${one.slice(0, n - 1)}…`
}

function problem(key: string, error: string): Problem {
  const m = /^([\w.-]+): (.*)$/s.exec(error)
  const body = m?.[2] ?? error
  return {
    key,
    field: m?.[1] ?? null,
    text: capital(clip(body.split(/(?<=[.;])\s+(?=[A-Z])/)[0] ?? body, 220)),
    full: error,
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

const SETTLED = /^(\d+) records: (\d+) verified, (\d+) flagged, (\d+) unresolved$/

/** Logs written before stage.finished carried its counts say them only in the detail. */
function settledDetail(detail: string): string {
  const m = SETTLED.exec(detail)
  if (!m) return capital(detail)
  const [, records, verified, flagged, unresolved] = m
  return settled({
    records: Number(records),
    verified: Number(verified),
    flagged: Number(flagged),
    unresolved: Number(unresolved),
  })
}

export function timeline(events: RunEvent[], run: RunInfo, doc: Doc | undefined): Section[] {
  const results = new Map<string, ToolResult>()
  for (const e of events) if (e.type === "llm.tool_result") results.set(e.call_id, e)
  const log = untilError(events)
  const sections = progress(log, run).map(
    (s): Section => ({ ...s, started: null, finished: null, entries: [] }),
  )
  const by = Object.fromEntries(sections.map((s) => [s.stage, s])) as Record<Step, Section>
  let section = by.parse
  let turn: Turn | null = null
  const push = (entry: Entry) => {
    section.entries.push(entry)
    if (entry.kind !== "turn") turn = null
  }
  const problems = (key: string, errors: string[]) => {
    const found = errors.map((err, j) => problem(`${key}.${j}`, err))
    const last = section.entries.at(-1)
    if (turn) turn.problems.push(...found)
    else if (last?.kind === "problems") last.problems.push(...found)
    else push({ kind: "problems", key, problems: found })
  }
  // A resume's marker waits for Extract, the one step whose work carries on across attempts, so it opens the new
  // attempt there. The run's opening lines survive the restarts that replace every other step's lines.
  let resumed: Line[] = []
  const opening = new Set<string>()
  for (const { e, key } of log) {
    switch (e.type) {
      case "run.started":
        if (e.resumed) {
          resumed = [
            { kind: "line", key, tone: "info", text: `Resumed with ${modelName(e.extraction_model)}` },
          ]
          if (e.guidance)
            resumed.push({
              kind: "line",
              key: `${key}.guidance`,
              tone: "info",
              text: `Guidance: ${e.guidance}`,
            })
          break
        }
        if (e.continued_from)
          push({ kind: "line", key, tone: "info", text: "Continued from an earlier extraction" })
        if (e.guidance)
          push({ kind: "line", key: `${key}.guidance`, tone: "info", text: `Guidance: ${e.guidance}` })
        for (const entry of section.entries) opening.add(entry.key)
        break
      case "stage.started":
        // A stage the next one cut short ends where that one starts. A restart clears this stage's time and every
        // later one's, so each step shows its latest attempt.
        if (section.started && !section.finished) section.finished = e.ts ?? null
        for (const { stage } of STEPS.slice(STEPS.findIndex((s) => s.stage === e.stage))) {
          by[stage].started = null
          by[stage].finished = null
          if (stage !== "agent") by[stage].entries = by[stage].entries.filter((x) => opening.has(x.key))
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
            text: e.counts
              ? settled(e.counts)
              : e.stage === "settle"
                ? settledDetail(e.detail)
                : capital(e.detail),
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
          problems: [],
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
      case "llm.tool_call": {
        const line: Line = { kind: "line", key, ...describeTool(e, results.get(e.call_id), doc) }
        if (turn) {
          turn.answered = true
          turn.actions.push(line)
        } else push(line)
        break
      }
      case "check.failed":
        problems(key, e.record_id && !e.errors.length ? [`${e.record_id}: failed a check`] : e.errors)
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

function untilError(events: RunEvent[]): Logged[] {
  let errored = false
  return events.flatMap((e, i) => {
    if (e.type === "run.started") errored = false
    else if (errored) return []
    if (e.type === "run.error") errored = true
    return [{ e, key: `${i}` }]
  })
}

function progress(log: Logged[], run: RunInfo): { stage: Step; label: string; state: StepState }[] {
  const done = new Set<Step>()
  const cut = new Set<Step>()
  let current: Stage | null = null
  for (const { e } of log) {
    if (e.type === "stage.started") {
      if (current && !done.has(current)) cut.add(current)
      const from = STEPS.findIndex((s) => s.stage === e.stage)
      for (const { stage } of STEPS.slice(from)) {
        cut.delete(stage)
        done.delete(stage)
      }
      current = e.stage
    } else if (e.type === "stage.finished") done.add(e.stage)
    else if (e.type === "run.finished") done.add("done")
  }
  const errored = log.at(-1)?.e.type === "run.error"
  const live = isLive(run)
  const running = live ? (current ?? "parse") : null
  const salvaged = cut.size > 0 && current !== null && done.has(current)
  const stoppedAt = live || done.has("done") || salvaged ? null : (current ?? "parse")
  const stopped: StepState = errored || run.status === "failed" ? "failed" : "stopped"
  const limited = run.status === "budget_exhausted" && done.has("done")
  return STEPS.map((s) => ({
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
