import type { Schemas } from "@/api/client"

export type Status = Schemas["FieldStatus"]["status"]
/** What the screens need of the parsed paper (the API returns the full Document): its tables and their cells. */
export type DocTable = {
  anchor: string
  page: number
  label: string
  caption: string
  region: number[]
  header_rows: number
  rows?: { anchor: string; text: string }[][]
}
export type Doc = { tables?: DocTable[] | null }
export type SettledRecord = Schemas["SettledRecord"]
export type RunInfo = Schemas["RunInfo"]

type Stage = NonNullable<Schemas["PaperSummary"]["live_stage"]>
export type LiveStep = { word: string; share: number }

/** A live run's stage as Home and the status line word it, and how far along the progress bar it puts the run. */
const LIVE_STEP: Record<Stage, LiveStep> = {
  parse: { word: "Reading the paper", share: 14 },
  agent: { word: "Extracting", share: 62 },
  settle: { word: "Checking", share: 90 },
}

/** The word for an agent waiting on the person, in a tooltip and on Home. */
export const NEEDS_OK = "Needs your OK"

/** A queued run waits for one of the `parallel_runs` places; a running one that hasn't begun a stage is starting. An
 * agent's run, SAGE's or a terminal's, has no stages: it extracts until done. */
export function liveStep(run: RunInfo, stage: Stage | null | undefined): LiveStep {
  if (run.status === "queued") return { word: "Queued", share: 0 }
  if (stage) return LIVE_STEP[stage]
  return run.driver === "sage" ? { word: "Starting", share: 4 } : LIVE_STEP.agent
}

export function isLive(run: RunInfo): boolean {
  return run.status === "queued" || run.status === "running"
}

/** A run SAGE drove that failed or was stopped picks up where it was. One that ran out of budget is continued instead,
 * since a resume shares the budget it used up, and so is an agent's, which starts a new session on the same folder. */
export function isResumable(run: RunInfo): boolean {
  return run.driver === "sage" && (run.status === "failed" || run.status === "interrupted")
}

/** A run that ended with nothing found and can't be resumed has nothing to build on, so carrying on is a new
 * extraction. An agent's run that failed or stopped carries on even with nothing kept, since its folder holds its
 * work. */
export function startsOver(run: RunInfo): boolean {
  if (run.driver !== "sage" && (run.status === "failed" || run.status === "interrupted")) return false
  return !isResumable(run) && !run.counts?.records
}

export function carryOnLabel(run: RunInfo, withGuidance: string): string {
  if (isResumable(run)) return "Resume…"
  if (startsOver(run)) return "Extract again…"
  return run.status === "budget_exhausted" ? "Continue" : withGuidance
}

export const STATUS_LABEL: Record<Status, string> = {
  verified: "Looks good",
  flagged: "Check this",
  unresolved: "Missing",
}

export const BASIS_LABEL: Record<string, string> = {
  stated: "Quoted",
  derived: "Calculated",
  inferred: "Inferred",
}

const CAUSE_SHORT: Record<string, string> = {
  absent: "Not stated in the paper",
  ambiguous: "Unclear what it applies to",
  conflict: "The paper contradicts itself",
  grounding_failure: "Quote not found in the paper",
  validation_failure: "Failed a check",
  schema_limitation: "Can't be stored as given",
  blocked_prerequisite: "Depends on a missing item",
  figure_only: "Only shown in a figure",
  repeated_error: "Kept failing a check",
  budget_exhausted: "Run stopped at its limit",
}

const BASIS_SHORT: Record<string, string> = {
  inferred: "Guessed, not quoted",
  derived: "Calculated, not quoted",
}

const BASIS_BRIEF: Record<string, string> = { inferred: "guessed", derived: "calculated" }

/** Checker reasons start with the basis: "inferred: from the table layout". */
function reasonBasis(reason: string | null): string {
  return (reason ?? "").split(":")[0]?.trim() ?? ""
}

export function problemBrief(field: string, cause: string | null, reason: string | null): string {
  const name = fieldTitle(field)
  if (cause === "absent") return `No ${name.toLowerCase()}`
  if (cause) return CAUSE_SHORT[cause] ?? "Needs a look"
  const verb = BASIS_BRIEF[reasonBasis(reason)]
  return verb ? `${name} ${verb}` : "Needs a look"
}

export function problemWords(cause: string | null, reason: string | null): string {
  if (cause) return CAUSE_SHORT[cause] ?? "Needs a look"
  return BASIS_SHORT[reasonBasis(reason)] ?? "Needs a look"
}

const RUN_STATUS_LABEL: Record<Exclude<RunInfo["status"], "budget_exhausted">, string> = {
  queued: "Queued",
  running: "Extracting",
  finished: "Finished",
  failed: "Failed",
  interrupted: "Interrupted",
}

export function runStatusLabel(run: RunInfo): string {
  return run.status === "budget_exhausted" ? limitTitle(run) : RUN_STATUS_LABEL[run.status]
}

export const REVIEW_LABEL: Record<"accept" | "edit" | "reject", string> = {
  accept: "Accepted",
  edit: "Edited",
  reject: "Not in the paper",
}

export const TYPE_PLURAL: Record<string, string> = {
  Citation: "Citation",
  Study: "Studies",
  Site: "Sites",
  Species: "Species",
  Crop: "Crops",
  Treatment: "Treatments",
  Variable: "Variables",
  Method: "Methods",
  Management: "Management",
  TreatmentPair: "Comparisons",
  Observation: "Observations",
  Coverage: "Coverage",
}

export const TYPE_ORDER = Object.keys(TYPE_PLURAL)

/** Only the first clause. The details drawer shows the whole error. */
function shortError(error: string | null | undefined): string {
  if (!error) return "Something went wrong"
  return capital(error.split(/;|\n/)[0]?.trim() ?? error)
}

export function itemCount(n: number): string {
  return `${n} ${n === 1 ? "item" : "items"}`
}

type EndedAlert = { title: string; reason: string; action: string }

/** How a run that ended short reads in a line about the paper. */
export function lastAttempt(run: RunInfo): string {
  if (run.status === "budget_exhausted") return limitTitle(run)
  return run.status === "interrupted" ? "Last attempt stopped" : "Last attempt failed"
}

/** A run that ended with nothing to show, in a word or two, for Home's row. */
export function endedWord(run: RunInfo): string {
  switch (run.status) {
    case "interrupted":
      return "Stopped"
    case "budget_exhausted":
      return "Hit its limit"
    case "finished":
      return "Nothing found"
    default:
      return "Failed"
  }
}

/** Which limit a run that ran out of budget hit. */
export function limitTitle(run: RunInfo): string {
  if (run.limit === "time") return "Stopped at the time limit"
  return run.limit === "turns" ? "Stopped at the turn limit" : "Stopped at the cost limit"
}

/** The alert for a run that ended short; its action is the one the details drawer offers. */
export function endedAlert(run: RunInfo): EndedAlert | null {
  const found = run.counts?.records ? `${itemCount(run.counts.records)} so far` : "Nothing found yet"
  const action = carryOnLabel(run, "Continue")
  switch (run.status) {
    case "failed":
      return { title: "Extraction failed", reason: shortError(run.error), action }
    case "interrupted":
      return { title: "Extraction stopped", reason: run.error ? shortError(run.error) : found, action }
    case "budget_exhausted":
      return { title: limitTitle(run), reason: found, action }
    default:
      return null
  }
}

export function stamp(iso: string | null | undefined): string {
  if (!iso) return ""
  return new Date(iso).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  })
}

export function capital(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1)
}

export function money(usd: number): string {
  if (usd === 0) return "$0"
  return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`
}

export function duration(
  from: string | null | undefined,
  to: string | null | undefined,
  now: number,
): string {
  if (!from) return ""
  const ms = (to ? new Date(to).getTime() : now) - new Date(from).getTime()
  const s = Math.max(0, Math.round(ms / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`
}

export function when(iso: string | null | undefined): string {
  if (!iso) return ""
  const d = new Date(iso)
  // Calendar days, so last night at 11 PM isn't shown as a time today. Rounding absorbs a DST hour.
  const midnight = (t: Date) => new Date(t.getFullYear(), t.getMonth(), t.getDate()).getTime()
  const days = Math.round((midnight(new Date()) - midnight(d)) / 86_400_000)
  if (days <= 0) return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
  if (days < 7) return d.toLocaleDateString([], { weekday: "short", hour: "numeric", minute: "2-digit" })
  return d.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" })
}

const CELL = /^p(\d+)\.t(\d+)(?:\.r(\d+)(?:\.c(\d+))?)?$/
const BLOCK = /^p(\d+)\.b\d+$/

export function anchorLabel(anchor: string, doc: Doc | undefined): string {
  const cell = CELL.exec(anchor)
  if (cell) {
    const table = doc?.tables?.find((t) => t.anchor === `p${cell[1]}.t${cell[2]}`)
    const name = table?.label ?? `Table on page ${cell[1]}`
    if (cell[3] === undefined) return name
    const row = Number(cell[3])
    const header = table?.header_rows ?? 1
    const col = cell[4] === undefined ? null : Number(cell[4])
    const heads =
      col === null ? [] : (table?.rows ?? []).slice(0, header).map((r) => (r[col]?.text ?? "").trim())
    const head = heads.filter(Boolean).at(-1) ?? ""
    const column = head ? `, ${head.length > 24 ? `${head.slice(0, 23)}…` : head}` : ""
    if (row < header) return `${name}, header${column}`
    return `${name}, row ${row - header + 1}${column}`
  }
  const block = BLOCK.exec(anchor)
  if (block) return `Page ${block[1]}`
  return anchor
}

type FieldLike = {
  value: unknown
  basis: string
  evidence?: { anchor: string; quote: string }[]
  note?: string | null
}

export function isField(v: unknown): v is FieldLike {
  return typeof v === "object" && v !== null && "basis" in v && "value" in v
}

/** Papers write the degree sign with look-alikes (˚, º), which the unit parser reads as °. */
const degrees = (text: string) => text.replace(/[\u00ba\u02da]/g, "°")

export function showValue(value: unknown): string {
  if (value === null || value === undefined) return "–"
  if (typeof value === "string") return value
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  if (Array.isArray(value)) return value.length ? value.map(showValue).join(", ") : "none"
  if (typeof value === "object") {
    const o = value as Record<string, unknown>
    if (typeof o.reported_text === "string") {
      const units = typeof o.reported_units === "string" ? o.reported_units : ""
      const written = degrees(o.reported_text).includes(degrees(units))
      return units && !written ? `${o.reported_text} ${units}` : o.reported_text
    }
    if (typeof o.statistic_name === "string") return `${o.statistic_name} ${showValue(o.statistic_value)}`
    return JSON.stringify(value)
  }
  return String(value)
}

export function fieldTitle(name: string): string {
  const special: Record<string, string> = {
    persistent_identifier: "DOI",
    reported_effect_scope: "What the value is",
    aggregated_over_factors: "Averaged over",
    temporal_info: "When",
    is_raw_replicate_level: "Single replicate",
    statistical_encoding: "Statistic",
    variable_name: "Variable",
    study_id: "Study",
    control_status: "Control",
    scientific_name: "Scientific name",
    species_epithet: "Species",
    state_or_region: "State or region",
    nearest_city: "Nearest city",
    soil_context: "Soil",
    event_type: "Event",
    treatment_ids: "Applies to",
    crop_id: "Crop",
    cover_crop: "Cover crop",
    n: "Replicates (n)",
    comparison_factor: "Compares",
    comparison_label: "Comparison",
  }
  return special[name] ?? name.replaceAll("_", " ").replace(/^\w/, (c) => c.toUpperCase())
}

const SCOPE_TEXT: Record<string, string> = {
  treatment_mean: "Mean for one treatment",
  aggregated_mean: "Mean across other factors",
}

export function friendlyValue(name: string, value: unknown): string {
  if (name === "reported_effect_scope" && typeof value === "string") return SCOPE_TEXT[value] ?? value
  if (typeof value === "boolean") return value ? "Yes" : "No"
  return showValue(value)
}
