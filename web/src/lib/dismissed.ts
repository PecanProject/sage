import { useSyncExternalStore } from "react"

const KEY = "sage.dismissed-alerts"
const KEEP = 200
const listeners = new Set<() => void>()

function subscribe(onChange: () => void) {
  listeners.add(onChange)
  window.addEventListener("storage", onChange)
  return () => {
    listeners.delete(onChange)
    window.removeEventListener("storage", onChange)
  }
}

function parse(raw: string | null): string[] {
  try {
    const ids: unknown = JSON.parse(raw ?? "[]")
    return Array.isArray(ids) ? ids.filter((i): i is string => typeof i === "string") : []
  } catch {
    return []
  }
}

export function useDismissed() {
  const ids = parse(useSyncExternalStore(subscribe, () => localStorage.getItem(KEY)))
  return {
    has: (runId: string) => ids.includes(runId),
    dismiss: (runId: string) => {
      localStorage.setItem(KEY, JSON.stringify([...ids.filter((i) => i !== runId), runId].slice(-KEEP)))
      for (const onChange of listeners) onChange()
    },
  }
}
