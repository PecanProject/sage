import { useSyncExternalStore } from "react"

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
