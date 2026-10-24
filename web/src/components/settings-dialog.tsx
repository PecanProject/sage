import { Collapsible } from "@base-ui/react/collapsible"
import {
  ArrowCounterClockwiseIcon,
  CaretRightIcon,
  CheckIcon,
  CircleNotchIcon,
  InfoIcon,
  XCircleIcon,
  XIcon,
} from "@phosphor-icons/react"
import { useQueryClient } from "@tanstack/react-query"
import { useTheme } from "next-themes"
import { useState } from "react"
import { toast } from "sonner"
import { $api, errorMessage, type Schemas } from "@/api/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Segmented } from "@/components/ui/segmented"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton, TextSkeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { Tip } from "@/components/ui/tooltip"
import { capital, modelHint, modelName } from "@/lib/format"
import { useSettingsRoute } from "@/lib/settings-route"
import { type FieldSpec, fieldsFromSchema } from "@/lib/settings-schema"
import { cn } from "@/lib/utils"

type Value = string | number | boolean | null
type View = Schemas["SettingsView"]
type Errors = { byField: Record<string, string>; rest: string | null }

const BASIC = ["extraction_model", "max_cost_usd"]
const TITLES: Record<string, string> = {
  extraction_model: "Model",
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

export function SettingsDialog() {
  const route = useSettingsRoute()
  const { data, error } = $api.useQuery("get", "/api/settings")
  return (
    <Dialog open={route.isOpen} onOpenChange={(o) => !o && void route.close()}>
      <DialogContent className="max-w-[560px]" data-testid="settings-dialog">
        <DialogHeader>
          <DialogTitle>Settings</DialogTitle>
        </DialogHeader>
        {data ? (
          <SettingsForm view={data} onDone={() => void route.close()} />
        ) : error ? (
          <DialogBody className="pt-3 pb-inset">
            <p className="text-body text-danger">Couldn't load settings: {errorMessage(error)}</p>
          </DialogBody>
        ) : (
          <SettingsSkeleton />
        )}
      </DialogContent>
    </Dialog>
  )
}

function SettingsSkeleton() {
  const field = (label: string) => (
    <div className="grid gap-1.5">
      <div className="text-label">
        <TextSkeleton className={label} />
      </div>
      <Skeleton className="h-control" />
    </div>
  )
  return (
    <>
      <DialogBody className="space-y-6 pt-3" aria-hidden data-testid="settings-loading">
        {field("w-32")}
        {field("w-12")}
        {field("w-36")}
        <div className="flex items-center justify-between gap-4">
          <div className="text-label">
            <TextSkeleton className="w-24" />
          </div>
          <Skeleton className="h-control w-[200px]" />
        </div>
        <div className="border-t border-line pt-4 text-control">
          <TextSkeleton className="w-20" />
        </div>
      </DialogBody>
      <DialogFooter className="border-t border-line pt-4">
        <Skeleton className="h-control w-[76px]" />
        <Skeleton className="h-control w-16" />
      </DialogFooter>
    </>
  )
}

/** The form's draft lives only while the dialog is open: closing unmounts it, so every open starts clean. */
function SettingsForm({ view, onDone }: { view: View; onDone: () => void }) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Record<string, Value>>({})
  const [errors, setErrors] = useState<Errors>({ byField: {}, rest: null })
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const fields = fieldsFromSchema(view.json_schema)
  // The key the chosen model needs comes first; the other providers' keys stay with the rest, under Advanced.
  const shownFirst = new Set([view.key_field, ...BASIC])
  const save = $api.useMutation("put", "/api/settings", {
    onSuccess: (saved) => {
      queryClient.setQueryData(["get", "/api/settings"], saved)
      toast.success("Saved")
      onDone()
    },
    onError: (e) => {
      const found = fieldErrors(errorMessage(e), new Set(fields.map((f) => f.name)))
      setErrors(found)
      // A failed Save never hides why: Advanced opens on an error in one of its fields, and stays open for the fix.
      if (Object.keys(found.byField).some((name) => !shownFirst.has(name))) setAdvancedOpen(true)
    },
  })
  /** A secret never shows its saved value, which the server sends masked. */
  const shown = (f: FieldSpec): Value =>
    f.name in draft ? (draft[f.name] ?? null) : f.kind.kind === "secret" ? null : asValue(view.values[f.name])
  const change = (f: FieldSpec) => (v: Value | undefined) => {
    setDraft((d) => {
      const next = { ...d }
      if (v === undefined) delete next[f.name]
      else next[f.name] = v
      return next
    })
    // An edit answers the field's error; the next Save checks it again.
    setErrors((e) => ({
      ...e,
      byField: Object.fromEntries(Object.entries(e.byField).filter(([k]) => k !== f.name)),
    }))
  }
  const field = (f: FieldSpec, basic: boolean) => (
    <SettingField
      key={f.name}
      field={f}
      basic={basic}
      value={shown(f)}
      saved={asValue(view.values[f.name])}
      env={view.locked[f.name]}
      error={errors.byField[f.name]}
      onChange={change(f)}
    />
  )
  const byName = new Map(fields.map((f) => [f.name, f]))
  const basic = (name: string) => {
    const f = byName.get(name)
    return f ? field(f, true) : null
  }
  const keyField = byName.get(view.key_field)
  const advanced = fields.filter((f) => !shownFirst.has(f.name))
  return (
    // The server checks every value, so its message shows under the field instead of the browser's bubble.
    <form
      className="contents"
      noValidate
      onSubmit={(e) => {
        e.preventDefault()
        if (Object.keys(draft).length) save.mutate({ body: { values: draft } })
        else onDone()
      }}
    >
      <DialogBody className="space-y-6 pt-3">
        {keyField ? (
          <ApiKey
            field={keyField}
            value={shown(keyField)}
            saved={asValue(view.values[view.key_field])}
            env={view.locked[view.key_field]}
            error={errors.byField[view.key_field]}
            onChange={change(keyField)}
          />
        ) : null}
        {basic("extraction_model")}
        {basic("max_cost_usd")}
        <Appearance />
        <Advanced fields={advanced} open={advancedOpen} onOpenChange={setAdvancedOpen}>
          {(f) => field(f, false)}
        </Advanced>
      </DialogBody>
      <DialogFooter className="border-t border-line pt-4">
        {errors.rest ? <p className="mr-auto text-caption text-danger">{errors.rest}</p> : null}
        <Button variant="ghost" type="button" onClick={onDone} data-testid="cancel-settings">
          Cancel
        </Button>
        <Button type="submit" disabled={save.isPending} data-testid="save-settings">
          Save
        </Button>
      </DialogFooter>
    </form>
  )
}

function Appearance() {
  const { theme, setTheme } = useTheme()
  return (
    <div className="flex items-center justify-between gap-4">
      <span className="text-label">Appearance</span>
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
    </div>
  )
}

function Advanced({
  fields,
  open,
  onOpenChange,
  children,
}: {
  fields: FieldSpec[]
  open: boolean
  onOpenChange: (open: boolean) => void
  children: (f: FieldSpec) => React.ReactNode
}) {
  const groups = new Map<string, FieldSpec[]>()
  for (const f of fields) groups.set(f.group, [...(groups.get(f.group) ?? []), f])
  return (
    <Collapsible.Root className="border-t border-line pt-4" open={open} onOpenChange={onOpenChange}>
      <Collapsible.Trigger
        className="group flex cursor-pointer items-center gap-1.5 rounded-inline text-label text-muted-ink outline-none hover:text-ink focus-visible:text-ink"
        data-testid="advanced-toggle"
      >
        <CaretRightIcon className="size-3.5 transition-transform duration-150 group-data-[panel-open]:rotate-90" />
        Advanced
      </Collapsible.Trigger>
      <Collapsible.Panel className="space-y-8 pt-5">
        {[...groups].map(([group, gs]) => (
          <section key={group} className="space-y-4">
            <h3 className="text-caption font-medium text-muted-ink">{group}</h3>
            {gs.map(children)}
          </section>
        ))}
      </Collapsible.Panel>
    </Collapsible.Root>
  )
}

type ControlProps = {
  id: string
  field: FieldSpec
  value: Value
  saved: Value
  disabled: boolean
  onChange: (v: Value | undefined) => void
}

function SettingField({
  field,
  value,
  saved,
  env,
  error,
  basic,
  onChange,
}: Omit<ControlProps, "id" | "disabled"> & {
  env: string | undefined
  error: string | undefined
  basic: boolean
}) {
  const reset = !env && !basic && value !== null && value !== field.default
  return (
    <div className={cn("grid gap-1.5", !basic && "grid-cols-[minmax(0,1fr)_220px] items-center gap-x-4")}>
      <div className="flex min-w-0 items-center gap-1.5">
        <label htmlFor={`setting-${field.name}`} className="truncate text-label">
          {TITLES[field.name] ?? field.title}
        </label>
        {basic || !field.description ? null : <Hint text={field.description} />}
        {env ? <span className="text-caption text-muted-ink">Set by {env}</span> : null}
        {reset ? <Reset field={field} onReset={() => onChange(null)} /> : null}
      </div>
      <div className="min-w-0">
        <Control
          id={`setting-${field.name}`}
          field={field}
          value={value}
          saved={saved}
          disabled={Boolean(env)}
          onChange={onChange}
        />
        {error ? <p className="mt-1 text-caption text-danger">{error}</p> : null}
      </div>
    </div>
  )
}

function Hint({ text }: { text: string }) {
  return (
    <Tip label={text}>
      <button
        type="button"
        className="flex cursor-help text-muted-ink outline-none hover:text-ink"
        aria-label={text}
      >
        <InfoIcon className="size-4" />
      </button>
    </Tip>
  )
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
    case "secret":
      return <SecretInput {...props} />
    case "enum":
      return <Choice {...props} options={k.options.map((o) => ({ value: o, label: capital(o) }))} />
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
      return k.choices.length ? <ModelChoice {...props} choices={k.choices} /> : <TextInput {...props} />
    default: {
      const never: never = k
      return never
    }
  }
}

function SecretInput({
  id,
  field,
  value,
  saved,
  disabled,
  onChange,
  children,
}: ControlProps & { children?: React.ReactNode }) {
  const [changing, setChanging] = useState(false)
  const queryClient = useQueryClient()
  const remove = $api.useMutation("put", "/api/settings", {
    onSuccess: (view) => queryClient.setQueryData(["get", "/api/settings"], view),
    onError: (e) => toast.error(errorMessage(e)),
  })
  const masked = typeof saved === "string" ? saved : null
  if (disabled || (masked && !changing))
    return (
      <div className="flex h-control items-center gap-4">
        <span
          className="min-w-0 flex-1 truncate text-control tabular-nums"
          data-testid={`secret-${field.name}`}
        >
          {masked ?? <span className="text-muted-ink">Not set</span>}
        </span>
        {disabled ? (
          children
        ) : (
          <>
            <Button
              variant="text"
              onClick={() => setChanging(true)}
              data-testid={`secret-change-${field.name}`}
            >
              Change
            </Button>
            {children}
            <Button
              variant="text"
              className="text-danger"
              disabled={remove.isPending}
              onClick={() => remove.mutate({ body: { values: { [field.name]: null } } })}
              data-testid={`secret-remove-${field.name}`}
            >
              Remove
            </Button>
          </>
        )}
      </div>
    )
  return (
    <div className="flex items-center gap-1">
      <Input
        id={id}
        type="password"
        autoComplete="off"
        autoFocus={masked !== null}
        placeholder="Paste the key"
        value={typeof value === "string" ? value : ""}
        onChange={(e) => onChange(e.target.value.trim() || undefined)}
        data-testid={`secret-input-${field.name}`}
      />
      {masked ? (
        <Tip label="Keep the saved key">
          <Button
            variant="quiet"
            size="icon-sm"
            aria-label="Keep the saved key"
            onClick={() => {
              setChanging(false)
              onChange(undefined)
            }}
            data-testid={`secret-keep-${field.name}`}
          >
            <XIcon />
          </Button>
        </Tip>
      ) : null}
    </div>
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
  const dollars = field.name.endsWith("_usd")
  return (
    <div className="relative">
      {dollars ? (
        <span className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-control text-muted-ink">
          $
        </span>
      ) : null}
      <Input
        id={id}
        type="number"
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        className={cn(dollars && "pl-7")}
        value={value === null ? "" : String(value)}
        onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
      />
    </div>
  )
}

function ModelChoice({ value, choices, ...props }: ControlProps & { choices: string[] }) {
  const all = typeof value === "string" && !choices.includes(value) ? [...choices, value] : choices
  return (
    <Choice
      {...props}
      value={value}
      options={all.map((c) => ({ value: c, label: modelName(c), hint: modelHint(c) }))}
    />
  )
}

function Choice({
  id,
  field,
  value,
  disabled,
  onChange,
  options,
}: ControlProps & { options: { value: string; label: string; hint?: string }[] }) {
  return (
    <Select
      value={typeof value === "string" ? value : ""}
      onValueChange={(v) => typeof v === "string" && onChange(v)}
      items={options.map((o) => ({ value: o.value, label: o.label }))}
      disabled={disabled}
    >
      <SelectTrigger id={id} aria-label={field.title}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {options.map((o) => (
          <SelectItem key={o.value} value={o.value}>
            {o.label}
            {o.hint ? <span className="text-caption text-muted-ink">{o.hint}</span> : null}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function ApiKey({
  field,
  value,
  saved,
  env,
  error,
  onChange,
}: {
  field: FieldSpec
  value: Value
  saved: Value
  env: string | undefined
  error: string | undefined
  onChange: (v: Value | undefined) => void
}) {
  const test = $api.useMutation("post", "/api/settings/test-key")
  /** The key the last test ran on, so a result never outlives a changed or removed key. */
  const [tested, setTested] = useState<Value>(null)
  const answer = test.data ?? (test.error ? { ok: false, message: errorMessage(test.error) } : null)
  const result = tested === saved ? answer : null
  return (
    <div className="grid gap-1.5">
      <div className="flex items-center gap-1.5">
        <label htmlFor={`setting-${field.name}`} className="text-label">
          {field.title}
        </label>
        {env ? <span className="text-caption text-muted-ink">Set by {env}</span> : null}
      </div>
      <SecretInput
        id={`setting-${field.name}`}
        field={field}
        value={value}
        saved={saved}
        disabled={Boolean(env)}
        onChange={onChange}
      >
        {saved === null ? null : (
          <Button
            variant="text"
            disabled={test.isPending}
            onClick={() => {
              setTested(saved)
              test.mutate({})
            }}
            data-testid="test-key"
          >
            {test.isPending ? <CircleNotchIcon className="size-3.5 animate-spin" /> : null}
            Test
          </Button>
        )}
      </SecretInput>
      {result && !test.isPending ? <KeyResult ok={result.ok} message={result.message} /> : null}
      {error ? <p className="text-caption text-danger">{error}</p> : null}
    </div>
  )
}

function KeyResult({ ok, message }: { ok: boolean; message: string }) {
  return (
    <span
      className={cn("flex items-center gap-1.5 text-caption", ok ? "text-good" : "text-danger")}
      data-testid="test-key-result"
      data-ok={ok}
    >
      {ok ? <CheckIcon weight="bold" className="size-4" /> : <XCircleIcon weight="fill" className="size-4" />}
      {message}
    </span>
  )
}
