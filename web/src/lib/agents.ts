import { useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { $api, errorMessage, type Schemas } from "@/api/client"
import type { RunInfo } from "@/lib/format"
import { useModelTitle } from "@/lib/models"
import { obj } from "@/lib/utils"

export type Agent = Schemas["AgentStatus"]

/** An agent as settings hold it. Only the fields the screens read. */
type AgentConfig = {
  title: string
  model: string | null
  effort: string | null
  time_limit_minutes: number | null
}

/** Each agent SAGE can start and whether it can run. The server asks each agent's own CLI, which takes a moment, so
 * the answer is kept for a minute. */
export function useAgents() {
  return $api.useQuery("get", "/api/agents", {}, { staleTime: 60_000 })
}

/** What an agent offers: its models and modes, and the efforts of `model`, else of its own default model. Asking
 * starts the agent for a few seconds and spends no tokens, so screens ask only when a list opens, and keep the answer
 * for the session. */
export function agentOffer(agent: string, model: string | null) {
  return $api.queryOptions(
    "get",
    "/api/agents/{agent_id}/offer",
    { params: { path: { agent_id: agent }, query: model ? { model } : undefined } },
    { staleTime: Number.POSITIVE_INFINITY, retry: false },
  )
}

/** The levels an agent offers for a model, in its own words, without its "default", which is what no effort means. */
export function offeredEfforts(offer: Schemas["AgentOffer"]): string[] {
  return offer.efforts.filter((e) => e !== "default")
}

/** Saves an agent's model or effort, and shows the answer at once. */
export function useSetAgent() {
  const queryClient = useQueryClient()
  return $api.useMutation("put", "/api/agents/{agent_id}", {
    onSuccess: (agent) => {
      queryClient.setQueriesData<Agent[]>({ queryKey: ["get", "/api/agents"] }, (list) =>
        list?.map((a) => (a.id === agent.id ? agent : a)),
      )
      void queryClient.invalidateQueries({ queryKey: ["get", "/api/settings"] })
    },
    onError: (e) => toast.error(errorMessage(e)),
  })
}

/** The agents in settings, by id, known as soon as settings are, before `/api/agents` has asked each one. */
export function agentConfigs(values: Record<string, unknown> | undefined): Record<string, AgentConfig> {
  return Object.fromEntries(
    Object.entries(obj(values?.agents)).map(([id, raw]) => {
      const c = obj(raw)
      return [
        id,
        {
          title: typeof c.title === "string" ? c.title : id,
          model: typeof c.model === "string" ? c.model : null,
          effort: typeof c.effort === "string" ? c.effort : null,
          time_limit_minutes: typeof c.time_limit_minutes === "number" ? c.time_limit_minutes : null,
        },
      ]
    }),
  )
}

export function useSettingsValues(): Record<string, unknown> | undefined {
  return $api.useQuery("get", "/api/settings").data?.values
}

/** Saves settings and shows the answer at once. A change to an agent can change whether it is ready. */
export function useSaveSettings() {
  const queryClient = useQueryClient()
  return $api.useMutation("put", "/api/settings", {
    onSuccess: (view) => {
      queryClient.setQueryData(["get", "/api/settings"], view)
      void queryClient.invalidateQueries({ queryKey: ["get", "/api/agents"] })
    },
    onError: (e) => toast.error(errorMessage(e)),
  })
}

/** What "Extract with" names: an agent SAGE starts (`claude`, or `claude:sonnet` for one of its models) or a model
 * SAGE's own agent runs on. Agents are matched first, as the server does. */
type Using = { kind: "agent"; agent: string; model: string | null } | { kind: "model"; model: string }

export function parseUsing(using: string, agents: Record<string, AgentConfig>): Using {
  const at = using.indexOf(":")
  const head = at < 0 ? using : using.slice(0, at)
  if (head in agents)
    return { kind: "agent", agent: head, model: at < 0 ? null : using.slice(at + 1) || null }
  return { kind: "model", model: using }
}

/** What Extract uses unless a run picks: the `extract_with` setting, else the first of your models. */
export function defaultUsing(values: Record<string, unknown> | undefined): string {
  const chosen = values?.extract_with
  if (typeof chosen === "string" && chosen) return chosen
  const [first] = Array.isArray(values?.models) ? (values.models as { id?: unknown }[]) : []
  return typeof first?.id === "string" ? first.id : ""
}

/** What a continued run is driven by unless the person picks: its own agent or model. A terminal's run has neither. */
export function runUsing(run: RunInfo): string | null {
  if (run.driver === "agent" && run.agent) return run.agent + (run.agent_model ? `:${run.agent_model}` : "")
  if (run.driver === "sage") return run.extraction_model ?? null
  return null
}

export type Choice = { using: Using; title: string; brand: string | null }

function agentChoice(agent: string, model: string | null, agents: Record<string, AgentConfig>): Choice {
  const title = agents[agent]?.title ?? agent
  return {
    using: { kind: "agent", agent, model },
    title: model ? `${title} · ${model}` : title,
    brand: agent,
  }
}

/** How a choice reads: an agent's title with the model it names, or a model's name. */
export function useChoiceOf(): (using: string) => Choice {
  const agents = agentConfigs(useSettingsValues())
  const modelTitle = useModelTitle()
  return (id) => {
    const using = parseUsing(id, agents)
    if (using.kind === "agent") return agentChoice(using.agent, using.model, agents)
    return { using, title: modelTitle(using.model), brand: null }
  }
}

/** Who drove a run, for the run screens: an agent and its model, or the model SAGE's agent ran on. Null for a run
 * driven from a terminal. */
export function useDriverOf(): (run: RunInfo) => Choice | null {
  const agents = agentConfigs(useSettingsValues())
  const modelTitle = useModelTitle()
  return (run) => {
    if (run.driver === "here") return null
    if (run.driver === "agent") return agentChoice(run.agent ?? "agent", run.agent_model ?? null, agents)
    const model = run.extraction_model ?? ""
    return { using: { kind: "model", model }, title: modelTitle(model), brand: null }
  }
}

export const TERMINAL = "From a terminal"

export type SkillPlace = Schemas["SkillPlace"]

export function useInstallSkill(onDone?: (places: SkillPlace[]) => void) {
  const queryClient = useQueryClient()
  return $api.useMutation("post", "/api/skill/install", {
    onSuccess: (places) => {
      queryClient.setQueryData(["get", "/api/skill"], places)
      onDone?.(places)
    },
    onError: (e) => toast.error(errorMessage(e)),
  })
}
