import { CircleNotchIcon } from "@phosphor-icons/react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { errorMessage, type Schemas } from "@/api/client"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  type TriggerVariant,
} from "@/components/ui/select"
import { agentOffer, offeredEfforts } from "@/lib/agents"
import { capital } from "@/lib/format"
import type { ModelOption } from "@/lib/models"

/** The effort SAGE's own agent has a model think at, from the levels that model has. `value` shows while the model
 * has it, else the model's own effort. A model that doesn't think has no choice. */
export function ModelEffortSelect({
  model,
  value,
  onChange,
  id,
  testid,
  variant,
}: {
  model: ModelOption
  value: string | null
  onChange: (effort: string) => void
  /** For a visible label; without one the select names itself after the model. */
  id?: string
  testid: string
  /** `ghost` in a list row. */
  variant?: TriggerVariant
}) {
  if (model.efforts.length === 0) return null
  const shown = model.efforts.find((e) => e === value) ?? model.effort
  const items = model.efforts.map((e) => ({ value: e, label: capital(e) }))
  return (
    <Select value={shown} onValueChange={(v) => typeof v === "string" && onChange(v)} items={items}>
      <SelectTrigger
        variant={variant}
        id={id}
        aria-label={id ? undefined : `${model.title}'s effort`}
        data-testid={testid}
        data-value={shown}
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {items.map((e) => (
          <SelectItem key={e.value} value={e.value} data-testid={`${testid}-option`}>
            {e.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

/** The effort an agent thinks at on `model` (null for its default model), in its own words. What it offers depends
 * on the model and is asked only when the list opens; once known, a model it offers no effort for has no choice.
 * Where `clearable`, "Default effort" (null) leaves it to the agent; elsewhere null shows what the agent would use. */
export function AgentEffortSelect({
  agent,
  model,
  value,
  onChange,
  clearable,
  id,
  testid,
  variant,
}: {
  agent: { id: string; title: string }
  model: string | null
  value: string | null
  onChange: (effort: string | null) => void
  clearable: boolean
  id?: string
  testid: string
  /** `ghost` in a list row. */
  variant?: TriggerVariant
}) {
  const queryClient = useQueryClient()
  const options = agentOffer(agent.id, model)
  const offer = useQuery({ ...options, enabled: false })
  const { none, current, shown, items } = agentLevels(offer.data, value, clearable)
  if (none) return null
  return (
    <Select
      value={shown}
      onValueChange={(v) => onChange(typeof v === "string" ? v : null)}
      onOpenChange={(open) => {
        if (open) void queryClient.prefetchQuery(options)
      }}
      items={clearable ? [{ value: null, label: "Default effort" }, ...items] : items}
    >
      <SelectTrigger
        variant={variant}
        id={id}
        aria-label={id ? undefined : `${agent.title}'s effort`}
        data-testid={testid}
        data-value={shown ?? ""}
      >
        <SelectValue placeholder="Default effort" />
      </SelectTrigger>
      <SelectContent>
        {clearable ? (
          <SelectItem value={null} data-testid={`${testid}-option`}>
            Default effort
            <span className="ml-auto text-caption text-muted-ink">{current}</span>
          </SelectItem>
        ) : null}
        {items.map((e) => (
          <SelectItem key={e.value} value={e.value} data-testid={`${testid}-option`}>
            {e.label}
          </SelectItem>
        ))}
        <Asking title={agent.title} asking={offer.isFetching && !offer.data} error={offer.error} />
      </SelectContent>
    </Select>
  )
}

/** What an agent's effort select shows: nothing once the agent offers no levels (`none`), the level it uses on its
 * own, the value, and the levels with a saved one it no longer offers kept, so the field never reads blank. */
function agentLevels(offer: Schemas["AgentOffer"] | undefined, value: string | null, clearable: boolean) {
  const offered = offer ? offeredEfforts(offer) : []
  const own = offer?.effort && offer.effort !== "default" ? offer.effort : null
  const shown = value ?? (clearable ? null : own)
  const levels = shown !== null && !offered.includes(shown) ? [shown, ...offered] : offered
  return {
    none: offer !== undefined && offered.length === 0,
    current: own ? capital(own) : null,
    shown,
    items: levels.map((e) => ({ value: e, label: capital(e) })),
  }
}

/** Inside an agent's list while it is being asked what it offers, or why asking failed. */
export function Asking({ title, asking, error }: { title: string; asking: boolean; error: unknown }) {
  if (error) return <p className="px-3 py-1.5 text-caption text-danger">{errorMessage(error)}</p>
  if (!asking) return null
  return (
    <p className="flex min-h-control items-center gap-2 px-3 text-control text-muted-ink">
      <CircleNotchIcon className="size-3.5 animate-spin" /> Asking {title}…
    </p>
  )
}
