import { CircleNotchIcon } from "@phosphor-icons/react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { errorMessage, type Schemas } from "@/api/client"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectItems,
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
        <SelectItems items={items} testid={`${testid}-option`} />
      </SelectContent>
    </Select>
  )
}

type AgentSelectProps = {
  agent: { id: string; title: string }
  value: string | null
  onChange: (value: string | null) => void
  testid: string
  /** `ghost` in a list row. */
  variant?: TriggerVariant
}

/** The model an agent uses; "Default model" (null) leaves it to the agent. */
export function AgentModelSelect(props: AgentSelectProps) {
  return (
    <OfferSelect
      {...props}
      model={null}
      clearable
      noun="model"
      read={(offer) => ({ offered: offer?.models ?? [], own: offer?.model ?? null })}
      label={(m) => m}
    />
  )
}

/** The effort an agent thinks at on `model` (null for its default model), in its own words. Once known, a model it
 * offers no effort for has no choice. Where `clearable`, "Default effort" (null) leaves it to the agent; elsewhere
 * null shows what the agent would use. */
export function AgentEffortSelect({
  model,
  clearable,
  id,
  ...props
}: AgentSelectProps & { model: string | null; clearable: boolean; id?: string }) {
  return (
    <OfferSelect
      {...props}
      model={model}
      clearable={clearable}
      id={id}
      noun="effort"
      read={(offer) => {
        const offered = offer ? offeredEfforts(offer) : []
        if (offer && offered.length === 0) return null
        return { offered, own: offer?.effort && offer.effort !== "default" ? offer.effort : null }
      }}
      label={capital}
    />
  )
}

/** A select over what an agent offers for `model`. Asking starts the agent for a few seconds, so it asks only when
 * the list opens. A saved value the agent no longer offers stays listed, so the field never reads blank. `read`
 * returns null when the agent offers nothing to choose, which hides the select. */
function OfferSelect({
  agent,
  model,
  value,
  onChange,
  clearable,
  id,
  testid,
  variant,
  noun,
  read,
  label,
}: AgentSelectProps & {
  model: string | null
  clearable: boolean
  id?: string
  noun: "model" | "effort"
  read: (offer: Schemas["AgentOffer"] | undefined) => { offered: string[]; own: string | null } | null
  label: (value: string) => string
}) {
  const queryClient = useQueryClient()
  const options = agentOffer(agent.id, model)
  const offer = useQuery({ ...options, enabled: false })
  const levels = read(offer.data)
  if (levels === null) return null
  const { offered, own } = levels
  const shown = value ?? (clearable ? null : own)
  const listed = shown !== null && !offered.includes(shown) ? [shown, ...offered] : offered
  const items = listed.map((v) => ({ value: v, label: label(v) }))
  const none = `Default ${noun}`
  return (
    <Select
      value={shown}
      onValueChange={(v) => onChange(typeof v === "string" ? v : null)}
      onOpenChange={(open) => {
        if (open) void queryClient.prefetchQuery(options)
      }}
      items={clearable ? [{ value: null, label: none }, ...items] : items}
    >
      <SelectTrigger
        variant={variant}
        id={id}
        aria-label={id ? undefined : `${agent.title}'s ${noun}`}
        data-testid={testid}
        data-value={shown ?? ""}
      >
        <SelectValue placeholder={none} />
      </SelectTrigger>
      <SelectContent>
        {clearable ? (
          <SelectItem value={null} data-testid={`${testid}-option`}>
            {none}
            <span className="ml-auto text-caption text-muted-ink">{own === null ? null : label(own)}</span>
          </SelectItem>
        ) : null}
        <SelectItems items={items} testid={`${testid}-option`} />
        <Asking title={agent.title} asking={offer.isFetching && !offer.data} error={offer.error} />
      </SelectContent>
    </Select>
  )
}

/** Inside an agent's list while it is being asked what it offers, or why asking failed. */
function Asking({ title, asking, error }: { title: string; asking: boolean; error: unknown }) {
  if (error) return <p className="px-3 py-1.5 text-caption text-danger">{errorMessage(error)}</p>
  if (!asking) return null
  return (
    <p className="flex min-h-control items-center gap-2 px-3 text-control text-muted-ink">
      <CircleNotchIcon className="size-3.5 animate-spin" /> Asking {title}…
    </p>
  )
}
