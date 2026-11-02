import { ArrowCounterClockwiseIcon, CaretRightIcon } from "@phosphor-icons/react"
import { useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { useTheme } from "next-themes"
import { type ReactNode, useState } from "react"
import { toast } from "sonner"
import { $api, errorMessage, type Schemas } from "@/api/client"
import { TopBar } from "@/components/top-bar"
import { Button } from "@/components/ui/button"
import { DollarInput, Input } from "@/components/ui/input"
import { Segmented } from "@/components/ui/segmented"
import { Select, SelectContent, SelectItems, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton, TextSkeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { Tip } from "@/components/ui/tooltip"
import { capital } from "@/lib/format"
import { type FieldSpec, fieldsFromSchema, groupsFromSchema } from "@/lib/settings-schema"
import { cn } from "@/lib/utils"

type Value = string | number | boolean | null
type View = Schemas["SettingsView"]
type Errors = { byField: Record<string, string>; rest: string | null }

/** Shown first, under General, since most people change only these. */
const GENERAL = ["max_cost_usd"]
const TITLES: Record<string, string> = {
  max_cost_usd: "Cost limit per paper",
}

function asValue(v: unknown): Value {
  return typeof v === "string" || typeof v === "number" || typeof v === "boolean" ? v : null
}

/** The server names the field first: "max_cost_usd: Input should be…". */
function fieldErrors(message: string, names: ReadonlySet<string>): Errors {
  const byField: Record<string, string> = {}
  const rest: string[] = []
  for (const part of message.split("; ")) {
    const [head, ...tail] = part.split(": ")
    if (head && names.has(head) && tail.length) byField[head] = tail.join(": ")
    else rest.push(part)
  }
  return { byField, rest: rest.length ? rest.join("; ") : null }
}

export function SettingsPage() {
  const { data, error } = $api.useQuery("get", "/api/settings")
  return (
    <div className="min-h-svh" data-testid="settings-page" data-scroll="document">
      <TopBar title="Settings" />
      <main className="mx-auto w-full max-w-[760px] px-gutter pt-10 pb-16">
        <h1 className="mb-12 px-inset text-display">Settings</h1>
        {data ? (
          <SettingsForm view={data} />
        ) : error ? (
          <p className="px-inset text-body text-danger">Couldn't load settings: {errorMessage(error)}</p>
        ) : (
          <SettingsSkeleton />
        )}
      </main>
    </div>
  )
}

function SettingsSkeleton() {
  return (
    <div className="space-y-6 px-inset" aria-hidden data-testid="settings-loading">
      {["w-32", "w-24", "w-36"].map((w) => (
        <div key={w} className="grid grid-cols-[minmax(0,1fr)_240px] items-center gap-x-8">
          <div className="text-label">
            <TextSkeleton className={w} />
          </div>
          <Skeleton className="h-control" />
        </div>
      ))}
    </div>
  )
}

function Section({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <section className="space-y-5 px-inset">
      <div className="space-y-1">
        <h2 className="text-title">{title}</h2>
        {hint ? <p className="text-caption text-muted-ink">{hint}</p> : null}
      </div>
      {children}
    </section>
  )
}

/** The draft is the page's own: leaving the page drops it, and Discard puts the saved values back. */
function SettingsForm({ view }: { view: View }) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Record<string, Value>>({})
  const [errors, setErrors] = useState<Errors>({ byField: {}, rest: null })
  // Agents, providers, models and what Extract uses are the Manage page's.
  const fields = fieldsFromSchema(view.json_schema).filter((f) => f.page === null)
  const groups = groupsFromSchema(view.json_schema)
  const save = $api.useMutation("put", "/api/settings", {
    onSuccess: (saved) => {
      queryClient.setQueryData(["get", "/api/settings"], saved)
      setDraft({})
      toast("Settings saved")
    },
    onError: (e) => setErrors(fieldErrors(errorMessage(e), new Set(fields.map((f) => f.name)))),
  })
  const shown = (f: FieldSpec): Value =>
    f.name in draft ? (draft[f.name] ?? null) : asValue(view.values[f.name])
  const change = (f: FieldSpec) => (v: Value) => {
    setDraft((d) => ({ ...d, [f.name]: v }))
    setErrors((e) => ({
      ...e,
      byField: Object.fromEntries(Object.entries(e.byField).filter(([k]) => k !== f.name)),
    }))
  }
  const field = (f: FieldSpec) => (
    <SettingField
      key={f.name}
      field={f}
      value={shown(f)}
      env={view.locked[f.name]}
      error={errors.byField[f.name]}
      onChange={change(f)}
    />
  )
  const byGroup = Map.groupBy(
    fields.filter((f) => !GENERAL.includes(f.name)),
    (f) => f.group,
  )
  const dirty = Object.keys(draft).length > 0
  const wrong = fields.filter((f) => f.name in errors.byField).map((f) => f.title)
  const problem =
    errors.rest ??
    (wrong.length
      ? `Can't save ${wrong.join(", ")}; the reason is under ${wrong.length === 1 ? "it" : "each"}.`
      : null)
  return (
    // The server checks every value, so its message shows under the field instead of the browser's bubble.
    <form
      className="space-y-12"
      noValidate
      onSubmit={(e) => {
        e.preventDefault()
        if (dirty) save.mutate({ body: { values: draft } })
      }}
    >
      <Section title="General">
        {fields.filter((f) => GENERAL.includes(f.name)).map(field)}
        <Appearance />
        <ManageLink />
      </Section>
      {[...byGroup].map(([group, gs]) => (
        <Section key={group} title={group} hint={groups[group]}>
          {gs.map(field)}
        </Section>
      ))}
      {dirty || errors.rest ? (
        <div
          className="sticky bottom-4 flex items-center gap-3 rounded-card border border-line bg-surface px-inset py-3 shadow-float"
          data-testid="settings-save-bar"
        >
          <p className={cn("mr-auto text-caption", problem ? "text-danger" : "text-muted-ink")}>
            {problem ?? "Unsaved changes"}
          </p>
          <Button
            variant="ghost"
            type="button"
            onClick={() => {
              setDraft({})
              setErrors({ byField: {}, rest: null })
            }}
            data-testid="discard-settings"
          >
            Discard
          </Button>
          <Button type="submit" disabled={save.isPending || !dirty} data-testid="save-settings">
            Save
          </Button>
        </div>
      ) : null}
    </form>
  )
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_240px] items-center gap-x-8">
      <span className="text-label">{label}</span>
      <div className="flex min-w-0 justify-end">{children}</div>
    </div>
  )
}

/** Agents, models and their providers have a page of their own. */
function ManageLink() {
  return (
    <Row label="Manage">
      <Link
        to="/manage"
        className="flex items-center gap-1 text-control text-muted-ink hover:text-ink"
        data-testid="settings-manage-link"
      >
        Agents, models and providers
        <CaretRightIcon className="size-3.5" />
      </Link>
    </Row>
  )
}

function Appearance() {
  const { theme, setTheme } = useTheme()
  return (
    <Row label="Appearance">
      <Segmented
        label="Appearance"
        value={theme === "light" || theme === "dark" ? theme : "system"}
        onChange={setTheme}
        options={[
          { value: "system", label: "System", testid: "theme-system" },
          { value: "light", label: "Light", testid: "theme-light" },
          { value: "dark", label: "Dark", testid: "theme-dark" },
        ]}
      />
    </Row>
  )
}

function SettingField({
  field,
  value,
  env,
  error,
  onChange,
}: Omit<ControlProps, "id" | "disabled"> & {
  env: string | undefined
  error: string | undefined
}) {
  const reset = !env && value !== null && value !== field.default
  return (
    <div
      className="grid grid-cols-[minmax(0,1fr)_240px] items-start gap-x-8"
      data-testid={`setting-row-${field.name}`}
    >
      <div className="min-w-0 space-y-0.5 pt-1.5">
        <div className="flex items-center gap-1.5">
          <label htmlFor={`setting-${field.name}`} className="text-label">
            {TITLES[field.name] ?? field.title}
          </label>
          {env ? <span className="text-caption text-muted-ink">Set by {env}</span> : null}
          {reset ? <Reset field={field} onReset={() => onChange(null)} /> : null}
        </div>
        {field.description ? (
          <p className="text-caption whitespace-pre-line text-muted-ink">{field.description}</p>
        ) : null}
      </div>
      <div className="min-w-0">
        <Control
          id={`setting-${field.name}`}
          field={field}
          value={value}
          disabled={Boolean(env)}
          onChange={onChange}
        />
        {error ? <p className="mt-1 text-caption text-danger">{error}</p> : null}
      </div>
    </div>
  )
}

type ControlProps = {
  id: string
  field: FieldSpec
  value: Value
  disabled: boolean
  onChange: (v: Value) => void
}

function Reset({ field, onReset }: { field: FieldSpec; onReset: () => void }) {
  return (
    <Tip label="Back to default">
      <Button
        variant="quiet"
        size="icon-sm"
        className="ml-auto size-7"
        onClick={onReset}
        aria-label={`Reset ${field.title}`}
        data-testid={`setting-reset-${field.name}`}
      >
        <ArrowCounterClockwiseIcon className="size-3.5" />
      </Button>
    </Tip>
  )
}

function Control(props: ControlProps) {
  const k = props.field.kind
  switch (k.kind) {
    case "enum":
      return <ChoiceInput {...props} options={k.options} />
    case "boolean":
      return (
        <Switch
          id={props.id}
          checked={props.value === true}
          onCheckedChange={props.onChange}
          disabled={props.disabled}
        />
      )
    case "integer":
    case "number":
      return <NumberInput {...props} min={k.min} max={k.max} step={k.kind === "integer" ? 1 : "any"} />
    case "text":
      return <TextInput {...props} />
    default: {
      const never: never = k
      return never
    }
  }
}

/** A setting with a fixed set of values, such as a log level. */
function ChoiceInput({
  id,
  field,
  value,
  disabled,
  onChange,
  options,
}: ControlProps & { options: string[] }) {
  const items = options.map((o) => ({ value: o, label: capital(o) }))
  return (
    <Select
      value={typeof value === "string" ? value : ""}
      onValueChange={(v) => typeof v === "string" && onChange(v)}
      items={items}
      disabled={disabled}
    >
      <SelectTrigger id={id} aria-label={field.title}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItems items={items} />
      </SelectContent>
    </Select>
  )
}

function TextInput({ id, value, disabled, onChange }: ControlProps) {
  return (
    <Input
      id={id}
      disabled={disabled}
      value={value === null ? "" : String(value)}
      onChange={(e) => onChange(e.target.value)}
    />
  )
}

function NumberInput({
  id,
  field,
  value,
  disabled,
  onChange,
  min,
  max,
  step,
}: ControlProps & { min?: number; max?: number; step: number | "any" }) {
  const Field = field.name.endsWith("_usd") ? DollarInput : Input
  return (
    <Field
      id={id}
      type="number"
      min={min}
      max={max}
      step={step}
      disabled={disabled}
      value={value === null ? "" : String(value)}
      onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
    />
  )
}
