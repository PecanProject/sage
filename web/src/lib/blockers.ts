import { $api } from "@/api/client"
import { agentConfigs, defaultUsing, parseUsing, useAgents, useSettingsValues } from "@/lib/agents"
import { useModelTitle } from "@/lib/models"

export type Blocker = {
  /** What has to change on the Manage page first, in a few words, or null when nothing. */
  line: string | null
  /** The server would refuse the run. */
  blocked: boolean
}

/** What stops a run driven by `using` (an agent or a model) before it starts: an agent that isn't ready, a provider
 * that still lacks a key, or a model no longer among yours. An agent not yet asked about blocks nothing; the server
 * checks it again. */
export function useBlocker(using: string | null): Blocker {
  const values = useSettingsValues()
  const { data: agents } = useAgents()
  const { data: providers } = $api.useQuery("get", "/api/providers")
  const { data: models } = $api.useQuery("get", "/api/models")
  const title = useModelTitle()
  const configs = agentConfigs(values)
  const none = { line: null, blocked: false }
  if (using === null || values === undefined) return none
  const chosen = parseUsing(using, configs)
  if (chosen.kind === "agent") {
    const status = agents?.find((a) => a.id === chosen.agent)
    if (!status || status.state === "ready") return none
    const isnt = status.state === "not_installed" ? "isn't installed" : "isn't signed in"
    return { line: `${status.title} ${isnt}`, blocked: true }
  }
  if (models && !models.some((m) => m.id === chosen.model))
    return { line: `${title(chosen.model)} isn't one of your models any more`, blocked: true }
  const provider = providers?.find(
    (p) => p.id === chosen.model.slice(0, chosen.model.indexOf(":")) && p.missing.length,
  )
  if (provider) return { line: `Needs ${provider.title}'s ${provider.missing.join(" and ")}`, blocked: true }
  return none
}

/** Whether anything can extract: what Extract uses by default can start, or the SAGE skill is installed, so an
 * agent in a terminal can. `known` stays false until the answers it needs are in, so nothing flashes while they
 * load; the agents' own answer, the slow one, is needed only when the default is an agent. */
export function useCanExtract(): { known: boolean; can: boolean } {
  const values = useSettingsValues()
  const using = values === undefined ? null : defaultUsing(values)
  const blocker = useBlocker(using)
  const agents = useAgents()
  const providers = $api.useQuery("get", "/api/providers")
  const models = $api.useQuery("get", "/api/models")
  const skill = $api.useQuery("get", "/api/skill")
  const onAgent = using !== null && parseUsing(using, agentConfigs(values)).kind === "agent"
  const known = Boolean(values && providers.data && models.data && skill.data && (!onAgent || agents.data))
  const skilled = skill.data?.some((p) => p.state !== "missing") ?? false
  return { known, can: !blocker.blocked || skilled }
}
