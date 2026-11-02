import { columnHead } from "@/lib/cells"
import { type Item, needsLook } from "@/lib/items"
import type { Unit } from "@/lib/units"
import { type Fact, placementOf, type Tab } from "@/lib/workbook"

/** One workbook column's value across a group of cells, amber when any cell's field behind it needs a look. */
export type Said = { column: string; title: string; text: string; look: boolean }

/** One cell of a row: its item, where it sits, and the columns it fills on its own ("value 20 · stat 1"). */
export type MapCell = { item: Item; c: number; own: Said[] }

/** A value column of the paper's table: its header text, and what SAGE read it as. */
export type MapColumn = { key: string; c: number; head: string; said: Said[] }

/** A row of the paper's table: its header text, what SAGE read it as, and its cells under the value columns. */
export type MapRow = { key: string; r: number; head: string; said: Said[]; cells: (MapCell | null)[] }

/** How SAGE read a table: what holds for every value, what each column and each row stands for, which columns each
 * cell fills on its own, and every cell where it sits. A table's cells share most of what they say, so checking
 * this checks them all. */
export type Reading = { every: Said[]; columns: MapColumn[]; rows: MapRow[]; cells: string[] }

const CELL = /\.r(\d+)\.c(\d+)$/

type Placed = { item: Item; r: number; c: number; facts: Map<string, Fact> }

function placed(items: Item[], tabs: Tab[]): Placed[] {
  return items.flatMap((item) => {
    const m = CELL.exec(item.primary ?? "")
    if (!m) return []
    const facts = new Map(placementOf(item, tabs).facts.map((f) => [f.key, f]))
    return [{ item, r: Number(m[1]), c: Number(m[2]), facts }]
  })
}

const textOf = (p: Placed, column: string) => p.facts.get(column)?.text ?? "–"

/** Every group agrees on the column, so it says one thing per group. */
const steady = (groups: Placed[][], column: string) =>
  groups.every((g) => new Set(g.map((p) => textOf(p, column))).size === 1)

function groupBy(cells: Placed[], by: (p: Placed) => number): [number, Placed[]][] {
  const out = new Map<number, Placed[]>()
  for (const p of cells) out.set(by(p), [...(out.get(by(p)) ?? []), p])
  return [...out].sort(([a], [b]) => a - b)
}

const said = (group: Placed[], column: string): Said => ({
  column,
  title: (group[0] as Placed).facts.get(column)?.label ?? column,
  text: textOf(group[0] as Placed, column),
  look: group.some((p) => {
    const f = p.facts.get(column)
    return f !== undefined && f.status !== "verified" && needsLook(p.item)
  }),
})

const clean = (parts: string[]) =>
  parts
    .map((t) => t.trim())
    .filter(Boolean)
    .join(" · ")

export function readTable(unit: Unit, tabs: Tab[]): Reading | null {
  const table = unit.table
  if (!table) return null
  // The table's values are its most common kind of record; others on it, like the variables a row names, are
  // reviewed one by one under the map.
  const count = new Map<string, number>()
  for (const i of unit.all) count.set(i.type, (count.get(i.type) ?? 0) + 1)
  const main = [...count].toSorted((a, b) => b[1] - a[1])[0]?.[0]
  const cells = placed(
    unit.all.filter((i) => i.type === main),
    tabs,
  )
  if (cells.length === 0) return null
  const header = table.header_rows
  const rows = table.rows ?? []
  const byColumn = groupBy(cells, (p) => p.c)
  const byRow = groupBy(cells, (p) => p.r)
  const columnGroups = byColumn.map(([, g]) => g)
  const rowGroups = byRow.map(([, g]) => g)
  /** Whether a workbook column says one thing for the whole table, one per column or row of it, or one per cell. */
  const scope = (column: string) => {
    if (steady([cells], column)) return "every"
    if (columnGroups.length > 1 && steady(columnGroups, column)) return "column"
    if (rowGroups.length > 1 && steady(rowGroups, column)) return "row"
    return "cell"
  }
  const names = [...new Set(cells.flatMap((p) => [...p.facts.keys()]))]
  const of = (where: string) => names.filter((c) => scope(c) === where)
  const own = of("cell")
  const first = Math.min(...cells.map((p) => p.c))
  const rowHead = (r: number) =>
    clean((rows[r] ?? []).slice(0, first).map((cell) => cell.text)) || `Row ${r - header + 1}`
  const columns = byColumn.map(([c, g]) => ({
    key: `c${c}`,
    c,
    head: columnHead(table, c),
    said: of("column").map((k) => said(g, k)),
  }))
  return {
    every: of("every")
      .map((c) => said(cells, c))
      .filter((s) => s.text !== "–" || s.look),
    columns,
    rows: byRow.map(([r, g]) => ({
      key: `r${r}`,
      r,
      head: rowHead(r),
      said: of("row").map((k) => said(g, k)),
      cells: columns.map(({ c }) => {
        const p = g.find((x) => x.c === c)
        return p ? { item: p.item, c, own: own.map((k) => said([p], k)) } : null
      }),
    })),
    cells: own,
  }
}
