export const GAP = 8

/** Where each card sits, as in the Google Docs comment margin. Cards come in document order with the top they
 * want (level with their highlight) and their height. The active card sits exactly at its highlight; cards above it
 * move up and cards below it move down until nothing overlaps. Nothing goes above `floor`, the bottom of the sticky
 * header and the "Not located" card, so if the cards above the active one run out of room the whole run moves down. */
export function layout(wants: number[], heights: number[], active: number, floor: number): number[] {
  const n = wants.length
  const tops = wants.map((w) => Math.max(w, floor))
  const h = (i: number) => heights[i] ?? 0
  for (let i = active - 1; i >= 0; i--) tops[i] = Math.min(tops[i] ?? 0, (tops[i + 1] ?? 0) - h(i) - GAP)
  let next = floor
  for (let i = 0; i < n; i++) {
    const top = Math.max(tops[i] ?? 0, next)
    tops[i] = top
    next = top + h(i) + GAP
  }
  return tops
}
