import { CircleDashedIcon, CircleIcon } from "@phosphor-icons/react"
import { useQueryClient } from "@tanstack/react-query"
import type { ReactNode } from "react"
import { errorMessage, type Schemas } from "@/api/client"
import { BrandIcon } from "@/components/brand-icon"
import { InlineCommand } from "@/components/copy"
import { AgentEffortSelect, AgentModelSelect } from "@/components/effort-select"
import { DefaultMark, Row, Section } from "@/components/manage/section"

import { DropdownMenuItem, MoreMenu } from "@/components/ui/dropdown-menu"
import { TextSkeleton } from "@/components/ui/skeleton"
import { Tip } from "@/components/ui/tooltip"
import {
  type Agent,
  agentConfigs,
  agentOffer,
  defaultUsing,
  offeredEfforts,
  useAgents,
  useSaveSettings,
  useSetAgent,
} from "@/lib/agents"

/** The words for an agent's state, for its shape's tooltip and aria-label. */
const AGENT_STATE: Record<Exclude<Agent["state"], "ready">, string> = {
  not_signed_in: "Not signed in",
  not_installed: "Not installed",
}

const SHAPE: Record<Exclude<Agent["state"], "ready">, ReactNode> = {
  not_signed_in: <CircleIcon weight="bold" className="size-3.5 text-check" />,
  not_installed: <CircleDashedIcon weight="bold" className="size-3.5 text-muted-ink" />,
}

/** Why an agent can't run, as a shape; the word is in the tooltip. A ready agent shows nothing. */
function AgentShape({ state }: { state: Agent["state"] }) {
  if (state === "ready") return null
  return (
    <Tip label={AGENT_STATE[state]}>
      <span
        role="img"
        aria-label={AGENT_STATE[state]}
        className="inline-flex size-4 shrink-0 items-center justify-center"
        data-testid="agent-state"
        data-state={state}
      >
        {SHAPE[state]}
      </span>
    </Tip>
  )
}

/** How to install an agent or sign in to it, with the commands in mono. The settings write commands in backticks;
 * a line with none is a command itself, which gets a copy button. */
function Fix({ text }: { text: string }) {
  const parts = text.split("`")
  if (parts.length === 1) return <InlineCommand text={text} testid="agent-fix" />
  return (
    <>
      {parts.map((part, i) =>
        i % 2 ? (
          // biome-ignore lint/suspicious/noArrayIndexKey: the parts of one fixed string, in order
          <code key={i} className="font-mono text-ink">
            {part}
          </code>
        ) : (
          part
        ),
      )}
    </>
  )
}

export function Agents({ view }: { view: Schemas["SettingsView"] }) {
  const agents = useAgents()
  const save = useSaveSettings()
  const set = useSetAgent()
  const queryClient = useQueryClient()
  const using = defaultUsing(view.values)
  const configs = agentConfigs(view.values)
  const setAgent = (id: string, body: { model?: string; effort?: string }, onSaved?: () => Promise<void>) =>
    set.mutate({ params: { path: { agent_id: id } }, body }, { onSuccess: onSaved })
  // The levels depend on the model, so a new model's are asked at once, and an effort it lacks goes back to default.
  const setModel = (a: Agent, model: string | null) =>
    setAgent(a.id, { model: model ?? "" }, async () => {
      const options = agentOffer(a.id, model)
      await queryClient.prefetchQuery(options)
      const offer = queryClient.getQueryData<Schemas["AgentOffer"]>(options.queryKey)
      if (offer && a.effort && !offeredEfforts(offer).includes(a.effort)) setAgent(a.id, { effort: "" })
    })
  return (
    <Section title="Agents" hint="Coding agents SAGE starts on a paper, with your own sign-in.">
      {agents.error ? (
        <p className="px-inset text-caption text-danger">
          Couldn't ask the agents: {errorMessage(agents.error)}
        </p>
      ) : (
        <ul className="flex flex-col" data-testid="agents">
          {agents.data
            ? agents.data.map((a) => (
                <AgentRow
                  key={a.id}
                  agent={a}
                  isDefault={using === a.id || using.startsWith(`${a.id}:`)}
                  onModel={(m) => setModel(a, m)}
                  onEffort={(e) => setAgent(a.id, { effort: e ?? "" })}
                  onDefault={() => save.mutate({ body: { values: { extract_with: a.id } } })}
                />
              ))
            : Object.keys(configs).map((id) => (
                <AgentSkeleton key={id} id={id} title={configs[id]?.title ?? id} />
              ))}
        </ul>
      )}
    </Section>
  )
}

function AgentSkeleton({ id, title }: { id: string; title: string }) {
  return (
    <Row
      icon={<BrandIcon brand={id} className="size-icon text-muted-ink opacity-50" />}
      title={<span className="truncate text-value text-muted-ink">{title}</span>}
      actions={
        <span className="w-44 text-caption">
          <TextSkeleton className="w-32" />
        </span>
      }
    />
  )
}

function AgentRow({
  agent: a,
  isDefault,
  onModel,
  onEffort,
  onDefault,
}: {
  agent: Agent
  isDefault: boolean
  onModel: (model: string | null) => void
  onEffort: (effort: string | null) => void
  onDefault: () => void
}) {
  const ready = a.state === "ready"
  return (
    <Row
      lead={<DefaultMark on={isDefault} testid="agent-default" />}
      icon={
        <BrandIcon
          brand={a.id}
          className={ready ? "size-icon text-ink" : "size-icon text-muted-ink opacity-50"}
        />
      }
      title={
        <>
          <span className="truncate text-value">{a.title}</span>
          <AgentShape state={a.state} />
        </>
      }
      detail={
        !ready && a.fix ? (
          <span title={a.detail ?? undefined}>
            <Fix text={a.fix} />
          </span>
        ) : null
      }
      actions={
        <>
          {ready ? (
            <>
              <div className="w-44 shrink-0">
                <AgentModelSelect
                  agent={a}
                  value={a.model ?? null}
                  onChange={onModel}
                  variant="ghost"
                  testid="agent-model"
                />
              </div>
              <div className="w-36 shrink-0">
                <AgentEffortSelect
                  agent={a}
                  model={a.model ?? null}
                  value={a.effort ?? null}
                  onChange={onEffort}
                  clearable
                  variant="ghost"
                  testid="agent-effort"
                />
              </div>
            </>
          ) : null}
          <MoreMenu testid="agent-menu">
            <DropdownMenuItem disabled={isDefault || !ready} onClick={onDefault} data-testid="agent-use">
              Use by default
            </DropdownMenuItem>
          </MoreMenu>
        </>
      }
      data-testid="agent-row"
      data-agent={a.id}
      data-state={a.state}
    />
  )
}
