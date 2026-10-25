import { anchorLabel, type Doc, fieldTitle, friendlyValue, type Status, showValue } from "@/lib/format"
import type { Item } from "@/lib/items"

export type Column = {
  key: string
  title: string
  value: (item: Item) => { text: string | null; status: Status }
}

function fieldCell(item: Item, name: string): { text: string | null; status: Status } {
  const row = item.rows.find((r) => r.name === name)
  // A field the record doesn't have is as missing as one left empty.
  if (!row || row.value === null) return { text: null, status: row?.status ?? "unresolved" }
  return { text: friendlyValue(name, row.value), status: row.status }
}

export function fieldColumns(items: Item[]): Column[] {
  const names = new Set<string>()
  for (const i of items) for (const r of i.rows) names.add(r.name)
  return [...names].map((name) => ({
    key: name,
    title: fieldTitle(name),
    value: (item) => fieldCell(item, name),
  }))
}

export function observationColumns(all: Item[], doc: Doc | undefined): Column[] {
  const treatments = new Map<string, string>()
  for (const i of all)
    if (i.type === "Treatment")
      treatments.set(i.id, showValue(i.rows.find((r) => r.name === "name")?.value ?? i.id))
  return [
    {
      key: "variable",
      title: "Variable",
      value: (i) => fieldCell(i, "variable_name"),
    },
    {
      key: "treatment",
      title: "Treatment",
      value: (i) => {
        const tid = (i.record.record as Record<string, unknown>).treatment_id
        return { text: typeof tid === "string" ? (treatments.get(tid) ?? tid) : null, status: "verified" }
      },
    },
    { key: "value", title: "Value", value: (i) => fieldCell(i, "value") },
    {
      key: "statistic",
      title: "Statistic",
      value: (i) => fieldCell(i, "statistical_encoding"),
    },
    { key: "when", title: "When", value: (i) => fieldCell(i, "temporal_info") },
    {
      key: "source",
      title: "Source",
      value: (i) => ({ text: i.primary ? anchorLabel(i.primary, doc) : null, status: "verified" }),
    },
  ]
}
