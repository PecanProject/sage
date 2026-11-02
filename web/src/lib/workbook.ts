import { $api, runPath, type Schemas } from "@/api/client"
import { fieldTitle, type Status } from "@/lib/format"
import { type Item, LINK } from "@/lib/items"

export type Tab = Schemas["Tab"]
type Cell = Tab["rows"][number][number]

/** The workbook tab each record type's rows are on. A Study has none: its design and period are in the README. */
export const TAB_OF: Record<string, string> = {
  Citation: "citations",
  Site: "sites",
  Treatment: "treatments",
  Method: "methods",
  Observation: "observations",
  Management: "managements",
  Variable: "variables",
  Crop: "crops",
  Species: "crops",
  TreatmentPair: "treatment_pairs",
  Coverage: "coverage",
}

const TAB_LABEL: Record<string, string> = { treatment_pairs: "Treatment pairs" }

export function tabLabel(name: string): string {
  return TAB_LABEL[name] ?? name.charAt(0).toUpperCase() + name.slice(1)
}

export type Workbook = { tabs: Tab[]; loading: boolean }

/** The run's workbook, as the export writes it, rejected records kept. */
export function useWorkbook(runId: string | null): Workbook {
  const q = $api.useQuery("get", "/api/runs/{run_id}/workbook", runPath(runId ?? ""), {
    enabled: runId !== null,
  })
  return { tabs: (q.data ?? []).filter((t) => t.name !== "README"), loading: runId !== null && q.isLoading }
}

/** One workbook row and the record it shows. A Management applied to several treatments is several rows of one
 * record, which open and select together. */
export type WorkbookRow = {
  key: string
  id: string | null
  item: Item | undefined
  cells: Tab["rows"][number]
}

export function rowsOf(tab: Tab, items: Map<string, Item>): WorkbookRow[] {
  return tab.rows.map((cells, i) => {
    const id = tab.ids[i] ?? null
    return { key: String(i), id, item: id === null ? undefined : items.get(id), cells }
  })
}

const COLUMN_TITLE: Record<string, string> = {
  treatment_id: "Treatment",
  "treatments.name": "Treatments",
  "sites.name": "Site",
  sitename: "Site",
  reported_units: "Units",
  statname: "Statistic",
  stat: "Statistic value",
  n: "Replicates (n)",
  min_date: "From",
  max_date: "To",
  reported_time: "When",
  min_depth: "Depth from",
  max_depth: "Depth to",
  reported_depth: "Depth",
  observation_level: "Per-replicate value",
  mgmttype: "Event",
  crop_name: "Crop",
  level: "Amount",
  factor_levels: "Factor levels",
  name: "ID",
  "method name": "ID",
  label: "Name",
  site_name: "Name",
  citation_id: "Citation",
  pair_id: "ID",
  treatment_id_1: "Treatment 1",
  treatment_id_2: "Treatment 2",
  doi: "DOI",
}

export function columnTitle(column: string): string {
  return COLUMN_TITLE[column] ?? fieldTitle(column)
}

export type RecordNames = Map<string, string>

const KEY_COLUMN = 0
const NAME_COLUMNS = ["label", "site_name"]

export function namesOf(tabs: Tab[]): RecordNames {
  const out: RecordNames = new Map()
  for (const tab of tabs) {
    const at = NAME_COLUMNS.map((c) => tab.columns.indexOf(c)).find((i) => i > KEY_COLUMN)
    if (at === undefined) continue
    for (const row of tab.rows) {
      const key = showCell(row[KEY_COLUMN] as Cell)
      const name = showCell(row[at] as Cell)
      if (key && name) out.set(key, name)
    }
  }
  return out
}

export function namedCell(v: Cell, field: string | null, names: RecordNames): string | null {
  const text = showCell(v)
  if (text === null || !field || !LINK.test(field)) return text
  return text
    .split(/\s*,\s*/)
    .map((id) => names.get(id) ?? id)
    .join(", ")
}

/** One line of what a record says: one workbook column, by its own name, as the export writes it. */
export type Fact = {
  key: string
  /** The workbook column. */
  label: string
  text: string
  /** The record field it reads, for its status, its evidence and its editor. */
  field: string | null
  status: Status
}

export function showCell(v: Cell): string | null {
  if (v === null || v === undefined || v === "") return null
  if (typeof v === "number") return String(Number.parseFloat(v.toPrecision(12)))
  return String(v)
}

/** What a record is, as a reviewer names it. */
const TYPE_NAME: Record<string, string> = {
  Management: "Management event",
  TreatmentPair: "Treatment pair",
}

export const typeName = (type: string) => TYPE_NAME[type] ?? type

/** Where an item goes in the export: its tab, its row's facts in the order a reviewer reads them, and the column
 * each of its fields fills. A Study has no tab: it is the README. */
export type Placement = { tab: string | null; facts: Fact[]; titles: Record<string, string> }

/** The facts of an item's first row on its tab. Empty columns are left out, unless their field still needs a look. */
function facts(item: Item, tab: Tab, names: RecordNames): Fact[] {
  const row = tab.rows[tab.ids.indexOf(item.id)]
  if (!row) return []
  const status = new Map(item.rows.map((r) => [r.name, r.status]))
  return tab.review.flatMap((c) => {
    const i = tab.columns.indexOf(c)
    const field = tab.fields[i] ?? null
    const s: Status = (field && status.get(field)) || "verified"
    const text = namedCell(row[i] as Cell, field, names)
    if (text === null && s === "verified") return []
    return [{ key: c, label: columnTitle(c), text: text ?? "–", field, status: s }]
  })
}

export function placementOf(item: Item, tabs: Tab[]): Placement {
  const name = TAB_OF[item.type] ?? null
  const tab = tabs.find((t) => t.name === name)
  if (!tab) return { tab: name, facts: [], titles: {} }
  const titles: Record<string, string> = {}
  const inOneColumn = (f: string) => tab.fields.filter((x) => x === f).length === 1
  for (const [i, f] of tab.fields.entries()) {
    const c = tab.columns[i]
    if (f !== null && c !== undefined && inOneColumn(f)) titles[f] = columnTitle(c)
  }
  return { tab: name, facts: facts(item, tab, namesOf(tabs)), titles }
}
