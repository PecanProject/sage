import type { ReactNode } from "react"
import { BrandIcon } from "@/components/brand-icon"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectGroupLabel,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { agentConfigs, type Choice, useAgents, useChoiceOf, useSettingsValues } from "@/lib/agents"
import { price, useModels } from "@/lib/models"

/** An agent's mark beside its name; a model has no mark. */
function ChoiceLabel({ choice }: { choice: Choice }) {
  return (
    <span className="flex min-w-0 items-center gap-2">
      {choice.brand ? <BrandIcon brand={choice.brand} className="text-ink" /> : null}
      <span className="truncate">{choice.title}</span>
    </span>
  )
}

function Option({ value, choice, aside }: { value: string; choice: Choice; aside: ReactNode }) {
  return (
    <SelectItem value={value} data-testid="using-option" data-using={value}>
      <ChoiceLabel choice={choice} />
      <span className="ml-auto text-caption text-muted-ink tabular-nums">{aside}</span>
    </SelectItem>
  )
}

/** What a run is extracted with: one of the agents that are ready, with the model each is set to, or one of your
 * models, priced per million tokens in / out. A value that is neither any more, such as an old run's, still shows, so
 * the field never reads blank. */
export function ExtractWithSelect({
  value,
  onChange,
  testid,
}: {
  value: string
  onChange: (using: string) => void
  testid: string
}) {
  const { data: agents } = useAgents()
  const configs = agentConfigs(useSettingsValues())
  const models = useModels()
  const choice = useChoiceOf()
  const current = choice(value)
  const ready = new Set((agents ?? []).filter((a) => a.state === "ready").map((a) => a.id))
  const agentValues = current.using.kind === "agent" && !ready.has(value) ? [...ready, value] : [...ready]
  const modelIds = models.map((m) => m.id)
  const modelValues =
    current.using.kind === "model" && value && !modelIds.includes(value) ? [...modelIds, value] : modelIds
  const all = [...agentValues, ...modelValues]
  const grouped = agentValues.length > 0
  return (
    <Select
      value={value}
      onValueChange={(v) => typeof v === "string" && onChange(v)}
      items={all.map((v) => ({ value: v, label: choice(v).title }))}
    >
      <SelectTrigger aria-label="Extract with" data-testid={testid}>
        <SelectValue>{(v: string) => <ChoiceLabel choice={choice(v)} />}</SelectValue>
      </SelectTrigger>
      <SelectContent>
        {grouped ? (
          <SelectGroup>
            <SelectGroupLabel>Agents</SelectGroupLabel>
            {agentValues.map((v) => {
              const c = choice(v)
              if (c.using.kind !== "agent") return null
              const model = c.using.model === null ? configs[v]?.model : null
              const aside = ready.has(c.using.agent) ? model : "Not ready"
              return <Option key={v} value={v} choice={c} aside={aside} />
            })}
          </SelectGroup>
        ) : null}
        <SelectGroup>
          {grouped ? <SelectGroupLabel>Models</SelectGroupLabel> : null}
          {modelValues.map((v) => {
            const option = models.find((m) => m.id === v)
            return <Option key={v} value={v} choice={choice(v)} aside={option ? price(option) : "Removed"} />
          })}
        </SelectGroup>
      </SelectContent>
    </Select>
  )
}
