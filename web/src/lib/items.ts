import type { Schemas } from "@/api/client"
import {
  friendlyValue,
  isField,
  problemBrief,
  problemWords,
  type SettledRecord,
  type Status,
} from "@/lib/format"

type Review = Schemas["Review"]
export type QueueItem = Schemas["QueueItem"]
export type RecordsView = { settled: Schemas["Settled"]; boxes: Record<string, Box> }
/** `AnchorBox` as the records query returns it, tuples widened to arrays. */
export type Box = { page: number; bbox: number[]; text: string; quotes?: Record<string, number[][]> }

export type FieldRow = {
  name: string
  value: unknown
  basis: string | null
  quotes: { anchor: string; quote: string }[]
  status: Status
  cause: string | null
  reason: string | null
}

export type Item = {
  id: string
  type: string
  status: Status
  label: string
  review: Review | null
  rows: FieldRow[]
  /** The first problem, in a few words. */
  problem: string | null
  /** The same problem short enough for a review row: "Year guessed". */
  brief: string | null
  /** The anchor the card lines up with and the highlight that stands for the item, if the PDF can draw one. */
  primary: string | null
  /** Every drawable anchor the item's evidence cites. */
  anchors: string[]
  record: SettledRecord
}

const HIDDEN = new Set(["id", "type"])
/** `site_id`, `treatment_ids`, `treatment_id_1`. */
const LINK = /_ids?(_\d+)?$/

/** A record's fields, in schema order. Links to other records are left out; their names are in the label. */
function fieldRows(rec: SettledRecord): FieldRow[] {
  const statuses = new Map((rec.fields ?? []).map((f) => [f.field, f]))
  const out: FieldRow[] = []
  const plain = (name: string, value: unknown): FieldRow => ({
    name,
    value,
    basis: null,
    quotes: [],
    status: "verified",
    cause: null,
    reason: null,
  })
  for (const [name, v] of Object.entries(rec.record)) {
    if (HIDDEN.has(name) || LINK.test(name)) continue
    const s = statuses.get(name)
    const status = s?.status ?? "verified"
    if (isField(v)) {
      out.push({
        name,
        value: v.value,
        basis: v.basis,
        quotes: v.evidence ?? [],
        status,
        cause: s?.cause ?? null,
        reason: status === "verified" ? null : (s?.reason ?? null),
      })
    } else if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") {
      // Plain values carry no evidence: a pair's notes, every Coverage field.
      out.push(plain(name, v))
    } else if (v !== null && typeof v === "object" && !Array.isArray(v) && Object.keys(v).length) {
      const text = Object.entries(v as Record<string, unknown>)
        .map(([k, x]) => `${k}: ${String(x)}`)
        .join("; ")
      out.push(plain(name, text))
    }
  }
  return out
}

function problemOf(rec: SettledRecord, rows: FieldRow[]): { problem: string; brief: string } | null {
  const open = rows.filter((r) => r.status !== "verified")
  const row = open.find((r) => r.cause !== "absent") ?? (rec.status === "unresolved" ? open[0] : undefined)
  if (row)
    return {
      problem: problemWords(row.cause, row.reason),
      brief: problemBrief(row.name, row.cause, row.reason),
    }
  return rec.issues?.length ? { problem: "Failed a check", brief: "Failed a check" } : null
}

export function editText(row: FieldRow): string {
  return row.value === null ? "" : friendlyValue(row.name, row.value)
}

export function firstProblemField(item: Item): string | null {
  return item.rows.find((r) => r.status !== "verified" && r.cause !== "absent")?.name ?? null
}

export function buildItems(view: RecordsView, queue: QueueItem[]): Item[] {
  const byId = new Map(view.settled.records.map((r) => [r.record.id, r]))
  const out: Item[] = []
  for (const q of queue) {
    const rec = byId.get(q.record_id)
    if (!rec) continue
    const rows = fieldRows(rec)
    const anchors = [...new Set(rows.flatMap((r) => r.quotes.map((e) => e.anchor)))].filter(
      (a) => view.boxes[a],
    )
    const valueRow = rows.find((r) => r.name === "value")
    const primary = valueRow?.quotes.map((e) => e.anchor).find((a) => view.boxes[a]) ?? anchors[0] ?? null
    const problem = q.status === "verified" ? null : problemOf(rec, rows)
    out.push({
      id: q.record_id,
      type: q.record_type,
      status: q.status,
      label: q.label,
      review: q.review ?? null,
      rows,
      problem: problem?.problem ?? null,
      brief: problem?.brief ?? null,
      primary,
      anchors,
      record: rec,
    })
  }
  return out
}

export const needsLook = (i: Item) => i.review === null && i.status !== "verified"

export function nextOpen(
  items: Item[],
  from: string | null,
  done: ReadonlySet<string> = new Set(),
): Item | null {
  const at = items.findIndex((i) => i.id === from)
  const after = [...items.slice(at + 1), ...items.slice(0, Math.max(at, 0))]
  const open = (i: Item) => i.review === null && !done.has(i.id)
  return after.find((i) => open(i) && i.status !== "verified") ?? after.find(open) ?? null
}
