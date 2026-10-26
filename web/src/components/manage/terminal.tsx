import { InfoIcon, TerminalWindowIcon } from "@phosphor-icons/react"
import { $api, errorMessage } from "@/api/client"
import { CopyLine, TerminalCommand } from "@/components/copy"
import { Row, Section } from "@/components/manage/section"
import { Button } from "@/components/ui/button"
import { Tip } from "@/components/ui/tooltip"
import { type SkillPlace, useInstallSkill } from "@/lib/agents"

/** The skill's state across the folders agents read: current only when every one is. */
function skillState(places: SkillPlace[]): SkillPlace["state"] {
  if (places.every((p) => p.state === "current")) return "current"
  return places.every((p) => p.state === "missing") ? "missing" : "outdated"
}

/** Where agents read the skill, home shortened to ~. */
function Folders({ places }: { places: SkillPlace[] }) {
  return (
    <span className="flex flex-col font-mono">
      {places.map((p) => (
        <span key={p.folder}>{p.folder.replace(/^\/(Users|home)\/[^/]+/, "~")}</span>
      ))}
    </span>
  )
}

/** Install or Update while the skill isn't current, else a quiet "Installed", with its folders in the tooltip. */
function SkillAction({ places }: { places: SkillPlace[] }) {
  const install = useInstallSkill()
  const state = skillState(places)
  if (state === "current")
    return (
      <Tip label={<Folders places={places} />}>
        <span
          className="inline-flex shrink-0 cursor-default items-center gap-1 text-caption text-muted-ink"
          data-testid="skill-state"
        >
          Installed
          <InfoIcon className="size-3.5" />
        </span>
      </Tip>
    )
  return (
    <Button
      variant="outline"
      size="compact"
      disabled={install.isPending}
      pending={install.isPending}
      onClick={() => install.mutate({})}
      data-testid="skill-install"
    >
      {state === "missing" ? "Install" : "Update"}
    </Button>
  )
}

/** What to tell an agent in a terminal, and the commands it runs. */
export function AskAgent() {
  return (
    <div className="space-y-3 px-inset">
      <div className="space-y-1.5">
        <span className="text-label">Say to your agent</span>
        <CopyLine text="Extract <paper> with SAGE" testid="ask-line" />
      </div>
      <pre className="font-mono text-caption leading-5 text-muted-ink" data-testid="extract-commands">
        {`sage extract start <paper>\nsage extract check <paper>\nsage extract done <paper> -m "summary"`}
      </pre>
    </div>
  )
}

/** The SAGE skill, first on the page: the command that installs it, ready to copy, and a button that does. */
export function Terminal() {
  const skill = $api.useQuery("get", "/api/skill")
  return (
    <Section
      title="SAGE skill"
      hint="Install it, then ask Claude Code, Codex or any coding agent to extract a paper with SAGE."
    >
      <ul>
        <Row
          icon={<TerminalWindowIcon className="size-icon text-muted-ink" />}
          title={<TerminalCommand text="sage skill install" testid="skill-command" />}
          actions={skill.data ? <SkillAction places={skill.data} /> : null}
          // Nothing on the row opens or picks, so it has no hover band.
          className="[&>div:first-child]:hover:bg-transparent"
          data-testid="skill-row"
          data-state={skill.data ? skillState(skill.data) : undefined}
        />
      </ul>
      {skill.error ? <p className="px-inset text-caption text-danger">{errorMessage(skill.error)}</p> : null}
    </Section>
  )
}
