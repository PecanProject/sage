import { Collapsible } from "@base-ui/react/collapsible"
import { CaretRightIcon } from "@phosphor-icons/react"
import type { ReactNode } from "react"
import type { RunEvent } from "@/api/events"
import { Tip } from "@/components/ui/tooltip"
import { modelName, money, type RunInfo, runStatusLabel, stamp } from "@/lib/format"
import {
  compact,
  dollars,
  type InputSplit,
  MISS_REASON,
  percent,
  split,
  sum,
  type TurnUsage,
  turnUsage,
  type Usage,
  used,
  whole,
} from "@/lib/usage"
import { cn } from "@/lib/utils"

export function RunUsage({ run, events }: { run: RunInfo; events: RunEvent[] }) {
  const total = sum(used(run.usage), used(run.table_usage))
  const all = split(total)
  const turns = turnUsage(events)
  return (
    <div className="space-y-10" data-testid="run-usage">
      <Section title="Budget">
        <Meter label="Cost" value={total.cost_usd} max={run.max_cost_usd} testid="meter-cost">
          {money(total.cost_usd)} <span className="text-muted-ink">of {money(run.max_cost_usd)}</span>
        </Meter>
        <Meter label="Turns" value={run.turns} max={run.max_turns} testid="meter-turns">
          {run.turns} <span className="text-muted-ink">of {run.max_turns}</span>
        </Meter>
      </Section>

      <Section title="Tokens">
        <dl className="grid grid-cols-2 gap-x-6 gap-y-5" data-testid="run-tokens">
          <Tile label="Input" value={compact(total.input_tokens)} />
          <Tile label="Output" value={compact(total.output_tokens)} />
          <Tile label="Read from the cache" value={percent(all.read, all.input)} />
          <Tile label="Requests" value={whole(total.requests)} />
        </dl>
        <InputBar split={all} />
        <Legend split={all} />
        {turns.length ? (
          <>
            <TurnChart turns={turns} />
            <TurnTable turns={turns} />
          </>
        ) : null}
      </Section>

      <Section title="By pass">
        <PassTable run={run} />
      </Section>

      <Section title="Run">
        <RunFacts run={run} />
      </Section>
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-5">
      <h3 className="text-caption font-medium text-muted-ink">{title}</h3>
      {children}
    </section>
  )
}

function Meter({
  label,
  value,
  max,
  testid,
  children,
}: {
  label: string
  value: number
  max: number
  testid: string
  children: ReactNode
}) {
  const share = Math.min(1, value / max)
  return (
    <div data-testid={testid}>
      <div className="flex items-baseline justify-between text-control">
        <span className="text-muted-ink">{label}</span>
        <span className="text-ink tabular-nums">{children}</span>
      </div>
      <meter className="sr-only" aria-label={label} min={0} max={max} value={value} />
      <div className="mt-2 h-1.5 rounded-full bg-meter-track" aria-hidden>
        <div
          className={cn(
            "h-full rounded-full transition-[width] duration-500 ease-out",
            share >= 1 ? "bg-danger" : share >= 0.8 ? "bg-check" : "bg-sage",
          )}
          style={{ width: `${share * 100}%` }}
        />
      </div>
    </div>
  )
}

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-caption text-muted-ink">{label}</dt>
      <dd className="mt-1 text-title text-ink">{value}</dd>
    </div>
  )
}

const PARTS = [
  { key: "read", label: "Cache read", fill: "bg-input-read" },
  { key: "write", label: "Cache write", fill: "bg-input-write" },
  { key: "fresh", label: "Uncached", fill: "bg-input-fresh" },
] as const

function InputBar({ split: s }: { split: InputSplit }) {
  if (!s.input) return null
  return (
    <div className="flex h-2.5 gap-0.5" aria-hidden>
      {PARTS.filter((p) => s[p.key] > 0).map((p) => (
        <div
          key={p.key}
          className={cn("min-w-1 first:rounded-l-full last:rounded-r-full", p.fill)}
          style={{ flexGrow: s[p.key] }}
        />
      ))}
    </div>
  )
}

function Legend({ split: s }: { split: InputSplit }) {
  return (
    <ul className="space-y-2">
      {PARTS.map((p) => (
        <li key={p.key} className="flex items-center gap-2.5 text-control">
          <span className={cn("size-2.5 shrink-0 rounded-mark", p.fill)} />
          <span className="flex-1 text-ink">{p.label}</span>
          <span className="text-ink tabular-nums">{whole(s[p.key])}</span>
          <span className="w-10 text-right text-muted-ink tabular-nums">{percent(s[p.key], s.input)}</span>
        </li>
      ))}
    </ul>
  )
}

function TurnChart({ turns }: { turns: TurnUsage[] }) {
  const top = Math.max(...turns.map((t) => t.usage.input_tokens), 1)
  return (
    <figure className="space-y-2 pt-2" data-testid="turn-chart">
      <figcaption className="flex justify-between text-caption text-muted-ink">
        <span>Input per turn</span>
        <span className="tabular-nums">{compact(top)}</span>
      </figcaption>
      <div className="flex h-28 items-end gap-0.5 border-b border-line">
        {turns.map((t) => (
          <TurnColumn key={t.key} turn={t} top={top} />
        ))}
      </div>
      <div className="flex justify-between text-caption text-muted-ink tabular-nums">
        <span>1</span>
        <span>{turns.length}</span>
      </div>
    </figure>
  )
}

function TurnColumn({ turn: t, top }: { turn: TurnUsage; top: number }) {
  const s = split(t.usage)
  return (
    <Tip label={<TurnTip turn={t} />}>
      <button
        type="button"
        aria-label={`Turn ${t.turn}: ${whole(t.usage.input_tokens)} input tokens`}
        className="group relative flex h-full max-w-6 min-w-0 flex-1 cursor-default flex-col justify-end outline-none focus-visible:outline-2 focus-visible:outline-sage"
      >
        <span
          className="flex flex-col-reverse gap-0.5 overflow-hidden rounded-t-bar group-hover:opacity-80"
          style={{ height: `${(s.input / top) * 100}%` }}
        >
          {PARTS.filter((p) => s[p.key] > 0).map((p) => (
            <span key={p.key} className={cn("min-h-px", p.fill)} style={{ flexGrow: s[p.key] }} />
          ))}
        </span>
        {t.miss ? (
          <span className="absolute -bottom-2.5 left-1/2 size-1 -translate-x-1/2 rounded-full bg-check" />
        ) : null}
      </button>
    </Tip>
  )
}

function TurnTip({ turn: t }: { turn: TurnUsage }) {
  const s = split(t.usage)
  return (
    <span className="block space-y-0.5 tabular-nums">
      <span className="block font-medium">
        Turn {t.turn} · {dollars(t.usage.cost_usd)}
      </span>
      <span className="block">
        {compact(s.read)} read · {compact(s.write)} written · {compact(s.fresh)} uncached
      </span>
      <span className="block">{compact(t.usage.output_tokens)} output</span>
      {t.miss ? <span className="block">Cache missed: {MISS_REASON[t.miss.reason]}</span> : null}
    </span>
  )
}

function TurnTable({ turns }: { turns: TurnUsage[] }) {
  return (
    <Collapsible.Root>
      <Collapsible.Trigger className="group flex cursor-pointer items-center gap-2 rounded-inline text-caption text-muted-ink outline-none hover:text-ink focus-visible:outline-2 focus-visible:outline-sage">
        Every turn
        <CaretRightIcon className="size-3 transition-transform duration-150 group-data-[panel-open]:rotate-90" />
      </Collapsible.Trigger>
      <Collapsible.Panel>
        <table className="mt-3 w-full text-caption tabular-nums" data-testid="turn-table">
          <thead className="text-muted-ink">
            <tr className="border-b border-line">
              <th className="py-2 text-left font-normal">Turn</th>
              <th className="py-2 text-right font-normal">Input</th>
              <th className="py-2 text-right font-normal">Read</th>
              <th className="py-2 text-right font-normal">Output</th>
              <th className="py-2 text-right font-normal">Cost</th>
            </tr>
          </thead>
          <tbody>
            {turns.map((t) => (
              <tr
                key={t.key}
                className="border-b border-line/60"
                title={t.miss ? `Cache missed: ${MISS_REASON[t.miss.reason]}` : undefined}
              >
                <td className="py-2 text-left">
                  {t.turn}
                  {t.miss ? (
                    <span className="ml-1.5 inline-block size-1 rounded-full bg-check align-middle" />
                  ) : null}
                </td>
                <td className="py-2 text-right">{whole(t.usage.input_tokens)}</td>
                <td className="py-2 text-right">
                  {percent(t.usage.cache_read_tokens, t.usage.input_tokens)}
                </td>
                <td className="py-2 text-right">{whole(t.usage.output_tokens)}</td>
                <td className="py-2 text-right">{dollars(t.usage.cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Collapsible.Panel>
    </Collapsible.Root>
  )
}

const PASS_ROWS: { label: string; value: (u: Usage) => string }[] = [
  { label: "Requests", value: (u) => whole(u.requests) },
  { label: "Input", value: (u) => whole(u.input_tokens) },
  { label: "Cache read", value: (u) => whole(u.cache_read_tokens) },
  { label: "Cache write", value: (u) => whole(u.cache_write_tokens) },
  { label: "Output", value: (u) => whole(u.output_tokens) },
  { label: "Cost", value: (u) => dollars(u.cost_usd) },
]

function PassTable({ run }: { run: RunInfo }) {
  return (
    <table className="w-full text-control tabular-nums" data-testid="pass-table">
      <thead>
        <tr className="border-b border-line text-caption text-muted-ink">
          <th className="pb-2 text-left font-normal" />
          <th className="pb-2 text-right font-normal">Extraction</th>
          <th className="pb-2 text-right font-normal">Tables</th>
        </tr>
      </thead>
      <tbody>
        <tr className="border-b border-line/60">
          <td className="py-2 text-muted-ink">Model</td>
          <td className="py-2 text-right">{modelName(run.extraction_model)}</td>
          <td className="py-2 text-right">{modelName(run.table_model)}</td>
        </tr>
        {PASS_ROWS.map((r) => (
          <tr key={r.label} className="border-b border-line/60 last:border-0">
            <td className="py-2 text-muted-ink">{r.label}</td>
            <td className="py-2 text-right">{r.value(used(run.usage))}</td>
            <td className="py-2 text-right">{r.value(used(run.table_usage))}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function RunFacts({ run }: { run: RunInfo }) {
  const c = run.counts
  return (
    <dl className="grid grid-cols-[120px_1fr] gap-x-4 gap-y-3 text-control" data-testid="run-facts">
      <Fact label="Run">
        <Mono>{run.id}</Mono>
      </Fact>
      <Fact label="Paper">
        <Mono>{run.paper_id}</Mono>
      </Fact>
      <Fact label="Status">{runStatusLabel(run)}</Fact>
      <Fact label="Effort">{run.effort}</Fact>
      <Fact label="Extraction model">
        <Mono>{run.extraction_model}</Mono>
      </Fact>
      <Fact label="Table model">
        <Mono>{run.table_model}</Mono>
      </Fact>
      <Fact label="Created">{stamp(run.created_at)}</Fact>
      <Fact label="Started">{stamp(run.started_at) || "–"}</Fact>
      <Fact label="Finished">{stamp(run.finished_at) || "–"}</Fact>
      <Fact label="Resumes">{whole(run.resumes)}</Fact>
      <Fact label="Items">
        {c
          ? `${whole(c.records)} ${c.records === 1 ? "item" : "items"}: ${c.verified} look good, ${c.flagged} to check, ${c.unresolved} missing`
          : "–"}
      </Fact>
      {run.parent_id ? (
        <Fact label="Continues">
          <Mono>{run.parent_id}</Mono>
        </Fact>
      ) : null}
      {run.guidance ? (
        <Fact label="Guidance">
          <span className="whitespace-pre-wrap">{run.guidance}</span>
        </Fact>
      ) : null}
    </dl>
  )
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-muted-ink">{label}</dt>
      <dd className="min-w-0 break-words text-ink">{children}</dd>
    </>
  )
}

function Mono({ children }: { children: ReactNode }) {
  return <span className="font-mono text-caption">{children}</span>
}
