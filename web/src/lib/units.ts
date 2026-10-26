import type { Doc, DocTable } from "@/lib/format"
import { type Box, type Item, needsLook } from "@/lib/items"

/** One card in the Check margin: a single item, the items on one table, or every item the paper can't place. */
export type Unit = {
  key: string
  kind: "item" | "table" | "unlocated"
  items: Item[]
  table: DocTable | null
  /** A table cluster's leading columns that name a row (a key, then the label), pinned while its values scroll. */
  stub: number
  /** Where the card pins: the top of its own highlight, or of the table. Null for "Not located". */
  at: { page: number; y: number; x: number } | null
}

const CLUSTER = 3

const TABLE_OF = /^(p\d+\.t\d+)\.r\d+\.c\d+$/

const tableOf = (anchor: string | null) => (anchor ? (TABLE_OF.exec(anchor)?.[1] ?? null) : null)

/** At most a row key and a label stay pinned, so the values keep room. */
const MAX_STUB = 2

const COLUMN = /\.c(\d+)$/

/** The columns left of the first one holding a value name the row. */
function stubOf(items: Item[]): number {
  const values = items.flatMap((i) => {
    const m = COLUMN.exec(i.primary ?? "")
    return m ? [Number(m[1])] : []
  })
  return Math.min(values.length ? Math.min(...values) : 1, MAX_STUB)
}

const spot = (b: Box) => ({ page: b.page, y: b.bbox[1] ?? 0, x: b.bbox[0] ?? 0 })

const byPlace = (a: Unit, b: Unit) =>
  (a.at?.page ?? -1) - (b.at?.page ?? -1) ||
  (a.at?.y ?? 0) - (b.at?.y ?? 0) ||
  (a.at?.x ?? 0) - (b.at?.x ?? 0)

function cellOrder(table: DocTable, items: Item[]): Item[] {
  const at = new Map<string, number>()
  for (const [r, row] of (table.rows ?? []).entries())
    for (const [c, cell] of row.entries()) at.set(cell.anchor, r * 1000 + c)
  const rank = (i: Item) => at.get(i.primary ?? "") ?? Number.MAX_SAFE_INTEGER
  return items.toSorted((a, b) => rank(a) - rank(b))
}

export function buildUnits(items: Item[], doc: Doc | undefined, boxes: Record<string, Box>): Unit[] {
  const tables = new Map((doc?.tables ?? []).map((t) => [t.anchor, t]))
  const unlocated: Item[] = []
  const byTable = new Map<string, Item[]>()
  const placed: { item: Item; box: Box }[] = []
  for (const item of items) {
    // `primary` is set exactly when the item has a drawable anchor.
    const box = item.primary ? boxes[item.primary] : undefined
    if (!box) {
      unlocated.push(item)
      continue
    }
    placed.push({ item, box })
    const table = tableOf(item.primary)
    if (table && tables.has(table)) byTable.set(table, [...(byTable.get(table) ?? []), item])
  }
  const clustered = new Set<string>()
  const units: Unit[] = []
  for (const [anchor, list] of byTable) {
    const table = tables.get(anchor)
    if (!table || list.length < CLUSTER) continue
    for (const i of list) clustered.add(i.id)
    const [x0 = 0, y0 = 0] = table.region
    units.push({
      key: anchor,
      kind: "table",
      items: cellOrder(table, list),
      table,
      stub: stubOf(list),
      at: { page: table.page, y: y0, x: x0 },
    })
  }
  for (const { item, box } of placed)
    if (!clustered.has(item.id))
      units.push({ key: item.id, kind: "item", items: [item], table: null, stub: 0, at: spot(box) })
  units.sort(byPlace)
  return unlocated.length
    ? [{ key: "unlocated", kind: "unlocated", items: unlocated, table: null, stub: 0, at: null }, ...units]
    : units
}

/** The cards the filter shows. "To check" keeps a card with anything left to check, plus the open one; a table
 * stays whole, since its grid is the paper's table, and other clusters list only what is left. */
export function visibleUnits(units: Unit[], filter: "check" | "all", keep: string | null): Unit[] {
  if (filter === "all") return units
  const shown = (i: Item) => needsLook(i) || i.id === keep
  return units.flatMap((u) => {
    if (!u.items.some(shown)) return []
    return [u.kind === "table" ? u : { ...u, items: u.items.filter(shown) }]
  })
}

export function cardTitle(unit: Unit): string {
  if (unit.kind === "table") return `${unit.table?.label ?? "Table"} · ${unit.items.length} values`
  if (unit.kind === "unlocated") return "Not located"
  return unit.items[0]?.label ?? ""
}

export type Direction = "up" | "down" | "left" | "right"

export function gridMove(unit: Unit, from: string, dir: Direction): string | null {
  const rows = unit.table?.rows ?? []
  const byAnchor = new Map(unit.items.flatMap((i) => (i.primary ? [[i.primary, i.id] as const] : [])))
  const cells: { r: number; c: number; id: string }[] = []
  for (const [r, row] of rows.entries())
    for (const [c, cell] of row.entries()) {
      const id = byAnchor.get(cell.anchor)
      if (id) cells.push({ r, c, id })
    }
  const here = cells.find((x) => x.id === from)
  if (!here) return null
  const ahead = cells.filter((x) => {
    if (dir === "up") return x.r < here.r
    if (dir === "down") return x.r > here.r
    if (dir === "left") return x.r === here.r && x.c < here.c
    return x.r === here.r && x.c > here.c
  })
  const cost = (x: { r: number; c: number }) =>
    dir === "up" || dir === "down"
      ? Math.abs(x.r - here.r) * 1000 + Math.abs(x.c - here.c)
      : Math.abs(x.c - here.c)
  return ahead.toSorted((a, b) => cost(a) - cost(b))[0]?.id ?? null
}
