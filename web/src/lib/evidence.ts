import type { Box, Item } from "@/lib/items"

export type Tone = "check" | "good" | "missing" | "rejected"

export type Highlight = {
  key: string
  itemId: string
  anchor: string
  page: number
  bbox: number[]
  tone: Tone
  /** The item's own highlight, or one of the extra places the active item cites. */
  primary: boolean
  active: boolean
  /** Outside the item or group being checked: drawn faintly so the active ones stand out. */
  dim: boolean
  /** The active item's quoted lines in this block. Empty draws the whole box. */
  lines: number[][]
}

function quoteLines(item: Item, anchor: string, box: Box): number[][] {
  const quotes = new Set(
    item.rows.flatMap((r) => r.quotes.filter((q) => q.anchor === anchor).map((q) => q.quote)),
  )
  const lines = [...quotes].flatMap((q) => box.quotes?.[q] ?? [])
  const merged: number[][] = []
  for (const [x0 = 0, y0 = 0, x1 = 0, y1 = 0] of lines.toSorted(
    (a, b) => (a[1] ?? 0) - (b[1] ?? 0) || (a[0] ?? 0) - (b[0] ?? 0),
  )) {
    const last = merged.at(-1)
    if (last && Math.abs((last[1] ?? 0) - y0) < 1 && x0 <= (last[2] ?? 0))
      last[2] = Math.max(last[2] ?? 0, x1)
    else merged.push([x0, y0, x1, y1])
  }
  return merged
}

function tone(item: Item): Tone {
  if (item.review?.action === "reject") return "rejected"
  if (item.review) return "good"
  return item.status === "verified" ? "good" : item.status === "flagged" ? "check" : "missing"
}

export function highlights(
  items: Item[],
  boxes: Record<string, Box>,
  active: string | null,
  focus: ReadonlySet<string> | null = null,
): Highlight[] {
  const out: Highlight[] = []
  for (const item of items) {
    const isActive = item.id === active
    const anchors = isActive ? item.anchors : item.primary ? [item.primary] : []
    for (const a of anchors) {
      const b = boxes[a]
      if (!b) continue
      out.push({
        key: `${item.id}:${a}`,
        itemId: item.id,
        anchor: a,
        page: b.page,
        bbox: b.bbox,
        tone: tone(item),
        primary: a === item.primary,
        active: isActive,
        dim: focus !== null && !focus.has(item.id),
        lines: isActive ? quoteLines(item, a, b) : [],
      })
    }
  }
  // Items that cite the same place share one box, so faded boxes don't stack into a bright one. The active item
  // wins the box, then an item in focus, then the first.
  const rank = (h: Highlight) => (h.active ? 2 : 0) + (h.dim ? 0 : 1)
  const one = new Map<string, Highlight>()
  for (const h of out) {
    const seen = one.get(h.anchor)
    if (!seen || rank(h) > rank(seen)) one.set(h.anchor, h)
  }
  // The active item draws last, so it sits on top of anything it overlaps.
  return [...one.values()].sort((x, y) => Number(x.active) - Number(y.active))
}
