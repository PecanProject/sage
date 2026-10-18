import { useState } from "react"

const KEY = "sage.key-hint.uses"
const UNTIL = 10

export type KeyHint = { show: boolean; used: () => void }

/** The panel's one-line key hint, shown until the reader has used the keys ten times. The count lives in
 * localStorage, so it stays hidden afterwards. */
export function useKeyHint(): KeyHint {
  const [uses, setUses] = useState(() => Number(localStorage.getItem(KEY)) || 0)
  const used = () => {
    if (uses >= UNTIL) return
    setUses(uses + 1)
    localStorage.setItem(KEY, String(uses + 1))
  }
  return { show: uses < UNTIL, used }
}
