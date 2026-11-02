import { type RefObject, useEffectEvent, useLayoutEffect, useState, useSyncExternalStore } from "react"

/** The app needs this much width. Below it, a notice takes the place of every screen. Matches Tailwind's `md`. */
export const fitsApp = "(min-width: 48rem)"
/** Room for a 440px column beside the paper. Matches Tailwind's `lg`. */
export const wide = "(min-width: 64rem)"

export function useMedia(query: string): boolean {
  return useSyncExternalStore(
    (change) => {
      const list = matchMedia(query)
      list.addEventListener("change", change)
      return () => list.removeEventListener("change", change)
    },
    () => matchMedia(query).matches,
  )
}

/** A size of the element in `ref`, kept as it resizes. `read` takes it from the element before the first paint, then
 * from each resize's entry. */
export function useSize(
  ref: RefObject<HTMLElement | null>,
  read: (el: HTMLElement, entry?: ResizeObserverEntry) => number,
): number {
  const [size, setSize] = useState(0)
  const measure = useEffectEvent(read)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    setSize(measure(el))
    const observer = new ResizeObserver(([e]) => setSize(measure(el, e)))
    observer.observe(el)
    return () => observer.disconnect()
  }, [ref])
  return size
}
