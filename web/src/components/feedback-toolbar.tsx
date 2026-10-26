import { useLocation } from "@tanstack/react-router"
import { lazy, Suspense } from "react"

// Dev only: Agentation's toolbar sends UI annotations to agentation-mcp, which hands them to the coding agent. Vite
// proxies /agentation to it, so the toolbar works on the tailnet too. The production build drops the import.
const Agentation = import.meta.env.DEV
  ? lazy(() => import("agentation").then((m) => ({ default: m.Agentation })))
  : undefined
const AgentReplies = import.meta.env.DEV
  ? lazy(() => import("@/components/agent-replies").then((m) => ({ default: m.AgentReplies })))
  : undefined

export function FeedbackToolbar() {
  // Agentation files each note under window.location.pathname, read only when it renders, and it doesn't hear the
  // router's pushState. Subscribing to the location re-renders it on navigation; the compiler would memoize that away.
  "use no memo"
  useLocation()
  if (!Agentation || !AgentReplies) return null
  return (
    <Suspense>
      <Agentation endpoint="/agentation" appName="SAGE" />
      <AgentReplies />
    </Suspense>
  )
}
