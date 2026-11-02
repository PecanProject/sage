import type { DocTable } from "@/lib/format"

/** A column's most specific header: the lowest header row with text in it, so "Fallow mean ± SE" rather than the
 * "Cover crop treatment" spanning it. */
export function columnHead(table: DocTable, c: number): string {
  const heads = (table.rows ?? []).slice(0, table.header_rows).map((row) => row[c]?.text.trim() ?? "")
  return heads.filter(Boolean).at(-1) ?? `Column ${c + 1}`
}
