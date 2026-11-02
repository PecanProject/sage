export type PageSize = { number: number; width: number; height: number }

export const PAGE_GAP = 24

export type PageTops = Map<number, { top: number; scale: number }>

/** Each page's top, in px from the top of the column, at this width. */
export function pageTops(pages: PageSize[], width: number): PageTops {
  const out: PageTops = new Map()
  let y = 0
  for (const p of pages) {
    const scale = width / p.width
    out.set(p.number, { top: y, scale })
    y += p.height * scale + PAGE_GAP
  }
  return out
}
