import { useState } from "react"
import { agentConfigs, defaultUsing, useChoiceOf, useSettingsValues } from "@/lib/agents"
import { useBlocker } from "@/lib/blockers"
import { money } from "@/lib/format"

/** What a run will be extracted with, from what the person picked, else `fallback`: a model or an agent, and the
 * effort it thinks at. Each start button sends exactly what its dialog shows. */
export function useRunChoice(fallback: string | null, fallbackEffort?: string | null) {
  const values = useSettingsValues()
  const choiceOf = useChoiceOf()
  const [picked, setPicked] = useState<string | null>(null)
  const [effort, setEffort] = useState<string | null>(null)
  const using = picked ?? fallback ?? defaultUsing(values)
  const chosen = choiceOf(using).using
  const blocker = useBlocker(using)
  return {
    using,
    /** Another choice has its own levels, so a picked effort goes with the one it was picked for. */
    pick: (next: string) => {
      setPicked(next)
      setEffort(null)
    },
    chosen,
    /** The effort to show until one is picked: `fallbackEffort` for the fallback, else the agent's own. A model's own
     * comes with the model. */
    effort:
      effort ??
      (picked === null ? fallbackEffort : null) ??
      (chosen.kind === "agent" ? (agentConfigs(values)[chosen.agent]?.effort ?? null) : null),
    setEffort,
    /** The effort to send, once the person picks one; else the server uses the same default this shows. */
    effortAsked: effort,
    blocker,
    cost: typeof values?.max_cost_usd === "number" ? values.max_cost_usd : null,
    minutes:
      chosen.kind === "agent" ? (agentConfigs(values)[chosen.agent]?.time_limit_minutes ?? null) : null,
  }
}
export type RunChoice = ReturnType<typeof useRunChoice>

/** Where a run stops: a model's cost limit, or an agent's time limit. */
export function limitLine(c: RunChoice, papers = 1): string | null {
  if (c.chosen.kind === "agent") return c.minutes === null ? null : `Stops after ${c.minutes} minutes`
  if (c.cost === null) return null
  return papers > 1
    ? `Stops at ${money(c.cost)} a paper, ${money(c.cost * papers)} in all`
    : `Stops at ${money(c.cost)}`
}
