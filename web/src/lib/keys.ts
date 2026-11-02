import { useEffect, useEffectEvent } from "react"
import { type KeybindingsMap, tinykeys } from "tinykeys"

const CONTROL =
  "button,a[href],summary,[role=button],[role=link],[role=tab],[role=checkbox],[role=switch],[role=radio]"

/** Keys typed into a field, Enter or Space on a focused control, and keys pressed while a dialog or menu has focus
 * belong to that element. */
function owned(e: KeyboardEvent): boolean {
  const t = e.target
  if (!(t instanceof HTMLElement)) return false
  if (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName)) return true
  if ((e.key === "Enter" || e.key === " ") && t.closest(CONTROL) !== null) return true
  return t.closest("[role=dialog],[role=menu],[role=listbox],[role=alertdialog]") !== null
}

/** Window keyboard shortcuts for as long as the component is mounted. The handlers always see the latest render. */
export function useKeys(map: KeybindingsMap, enabled = true) {
  const press = useEffectEvent((key: string, e: KeyboardEvent) => {
    if (!owned(e)) map[key]?.(e)
  })
  const keys = Object.keys(map).join("|")
  useEffect(() => {
    if (!enabled) return
    return tinykeys(
      window,
      Object.fromEntries(keys.split("|").map((k) => [k, (e: KeyboardEvent) => press(k, e)])),
    )
  }, [keys, enabled])
}
