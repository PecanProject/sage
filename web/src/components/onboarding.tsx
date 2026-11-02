import { CaretRightIcon, CircleNotchIcon, CloudIcon, TerminalWindowIcon, XIcon } from "@phosphor-icons/react"
import { useNavigate } from "@tanstack/react-router"
import type { ReactNode } from "react"
import { BrandIcon } from "@/components/brand-icon"
import { Logo } from "@/components/logo"
import { AskAgent } from "@/components/manage/terminal"
import { Button } from "@/components/ui/button"
import { TextSkeleton } from "@/components/ui/skeleton"
import { useAgents, useInstallSkill, useSaveSettings } from "@/lib/agents"

function Choice({
  icon,
  title,
  busy = false,
  onClick,
  testid,
}: {
  icon: ReactNode
  title: string
  busy?: boolean
  onClick: () => void
  testid: string
}) {
  return (
    <li>
      <button
        type="button"
        disabled={busy}
        onClick={onClick}
        className="flex h-14 w-full cursor-pointer items-center gap-4 px-inset text-left outline-none hover:bg-surface-2 focus-visible:bg-surface-2 disabled:cursor-default"
        data-testid={testid}
      >
        <span className="flex size-icon shrink-0 items-center justify-center text-ink">{icon}</span>
        <span className="flex-1 text-value">{title}</span>
        {busy ? (
          <CircleNotchIcon className="size-4 animate-spin text-muted-ink" />
        ) : (
          <CaretRightIcon className="size-4 text-muted-ink" />
        )}
      </button>
    </li>
  )
}

/** The first screen while nothing can extract and the library is empty: the agents already signed in on this
 * computer first, then an API key, then an agent in the person's own terminal. Picking one makes something ready,
 * so the screen goes by itself. */
export function Onboarding({ onTerminal }: { onTerminal: () => void }) {
  const navigate = useNavigate()
  const agents = useAgents()
  const save = useSaveSettings()
  const install = useInstallSkill(onTerminal)
  const ready = agents.data?.filter((a) => a.state === "ready") ?? []
  return (
    <section className="mx-auto max-w-[480px] space-y-8 pt-12" data-testid="onboarding">
      <div className="flex flex-col items-center gap-4">
        <Logo size={48} />
        <h2 className="text-title">How should SAGE extract?</h2>
      </div>
      <ul className="divide-y divide-line overflow-hidden rounded-card border border-line bg-surface">
        {agents.data ? null : (
          <li className="flex h-14 items-center gap-4 px-inset text-value">
            <span className="size-icon" />
            <TextSkeleton className="w-32" />
          </li>
        )}
        {ready.map((a) => (
          <Choice
            key={a.id}
            icon={<BrandIcon brand={a.id} className="size-icon" />}
            title={a.title}
            busy={save.isPending && save.variables?.body.values.extract_with === a.id}
            onClick={() => save.mutate({ body: { values: { extract_with: a.id } } })}
            testid="onboarding-agent"
          />
        ))}
        <Choice
          icon={<CloudIcon className="size-icon" />}
          title="A model provider"
          onClick={() => void navigate({ to: "/manage", hash: "providers" })}
          testid="onboarding-provider"
        />
        <Choice
          icon={<TerminalWindowIcon className="size-icon" />}
          title="From your own terminal"
          busy={install.isPending}
          onClick={() => install.mutate({})}
          testid="onboarding-terminal"
        />
      </ul>
    </section>
  )
}

/** Once the skill is installed from the first screen: what to say to the agent, until the person closes it. */
export function TerminalHint({ onClose }: { onClose: () => void }) {
  return (
    <div
      className="relative mb-6 rounded-card border border-line bg-surface py-5"
      data-testid="terminal-hint"
    >
      <Button
        variant="quiet"
        size="icon-sm"
        className="absolute top-3 right-3"
        aria-label="Close"
        onClick={onClose}
        data-testid="terminal-hint-close"
      >
        <XIcon />
      </Button>
      <AskAgent />
    </div>
  )
}
