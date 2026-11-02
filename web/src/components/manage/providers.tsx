import { CircleNotchIcon, CloudIcon, PlusIcon, XCircleIcon } from "@phosphor-icons/react"
import { useQuery } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { useState } from "react"
import { errorMessage } from "@/api/client"
import { BrandIcon } from "@/components/brand-icon"
import { Row, Section } from "@/components/manage/section"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import { Tip } from "@/components/ui/tooltip"
import { useSaveSettings, useSettingsValues } from "@/lib/agents"
import {
  available,
  type Kind,
  type KindField,
  type Provider,
  providerBrand,
  useRemoveProvider,
  useSaveProvider,
} from "@/lib/providers"

/** What the Manage page offers to edit beyond a kind's own fields: an added provider's name. */
const NAME: KindField = {
  name: "name",
  title: "Name",
  help: "What this page calls it.",
  secret: false,
  placeholder: "",
  default: null,
  required: false,
  instead: [],
}

export function Providers({
  providers,
  kinds,
  open,
}: {
  providers: Provider[]
  kinds: Kind[]
  /** The provider whose fields are open, from the URL. */
  open: string | undefined
}) {
  const navigate = useNavigate()
  // Opening one provider closes the other, which moves every row below it. Scroll by that much, so the row that was
  // clicked stays where it was on screen.
  const show = (id: string | undefined, anchor: HTMLElement) => {
    const before = anchor.getBoundingClientRect().top
    void navigate({
      to: "/manage",
      search: id ? { provider: id } : {},
      replace: true,
      resetScroll: false,
    }).then(() =>
      requestAnimationFrame(() => window.scrollBy(0, anchor.getBoundingClientRect().top - before)),
    )
  }
  return (
    <Section id="providers" title="Providers" hint="Where your models come from, with your own credentials.">
      <ul className="flex flex-col">
        {providers.map((p) => {
          const kind = kinds.find((k) => k.kind === p.kind)
          return kind ? (
            <ProviderRow
              key={p.id}
              provider={p}
              kind={kind}
              open={open === p.id}
              onToggle={(anchor) => show(open === p.id ? undefined : p.id, anchor)}
            />
          ) : null
        })}
      </ul>
      <div className="px-inset">
        <Button
          variant="quiet"
          size="compact"
          className="-ml-3"
          onClick={() => void navigate({ to: "/manage", search: { add: "provider" }, resetScroll: false })}
          data-testid="add-provider"
        >
          <PlusIcon className="size-4" /> Add a provider
        </Button>
      </div>
    </Section>
  )
}

function host(url: string | null | undefined): string | null {
  if (!url) return null
  try {
    return new URL(url).host
  } catch {
    return url
  }
}

/** A provider's saved key, masked as the server sends it, or the variable that sets it. */
function keySummary(provider: Provider, kind: Kind): string | null {
  for (const f of kind.fields.filter((f) => f.secret)) {
    const env = provider.locked[f.name]
    if (env) return `Set by ${env}`
    const saved = provider.values[f.name]
    if (saved) return saved
  }
  return null
}

/** One quiet row: the provider's mark, name, where it is and its key, whether it works, and Set up or Change, which
 * opens its fields under the row. */
function ProviderRow({
  provider,
  kind,
  open,
  onToggle,
}: {
  provider: Provider
  kind: Kind
  open: boolean
  onToggle: (anchor: HTMLElement) => void
}) {
  const where = host(provider.values.base_url)
  const subtitle = provider.builtin
    ? where
    : [kind.title, where ?? provider.values.region].filter(Boolean).join(" · ")
  const detail = [subtitle, keySummary(provider, kind)].filter(Boolean).join(" · ")
  const ready = provider.missing.length === 0
  return (
    <Row
      icon={<BrandIcon brand={providerBrand(provider)} fallback={CloudIcon} className="size-icon text-ink" />}
      title={<span className="truncate text-value">{provider.title}</span>}
      // Open, the fields below show the key and endpoint themselves. The line keeps its room, so the name doesn't move.
      detail={
        detail ? (
          <span className={open ? "invisible tabular-nums" : "tabular-nums"} aria-hidden={open}>
            {detail}
          </span>
        ) : null
      }
      actions={
        <>
          {/* "Set up" already says it lacks something; its fields say what. */}
          {ready ? <ProviderCheck provider={provider} /> : null}
          <Button
            variant="quiet"
            size="compact"
            className="-mr-3 min-w-18 aria-expanded:bg-transparent aria-expanded:hover:bg-surface-2"
            aria-expanded={open}
            onClick={(e) => onToggle(e.currentTarget)}
            data-testid="provider-setup"
          >
            {open ? "Done" : ready ? "Change" : "Set up"}
          </Button>
        </>
      }
      className={
        open
          ? "rounded-card bg-surface shadow-card ring-1 ring-line [&>div:first-child]:hover:bg-transparent"
          : undefined
      }
      data-testid="provider"
      data-provider={provider.id}
    >
      {open ? <ProviderFields provider={provider} kind={kind} /> : null}
    </Row>
  )
}

/** A provider's fields, under its row while it is open, lined up with its name: what it needs first, then the
 * optional ones. */
function ProviderFields({ provider, kind }: { provider: Provider; kind: Kind }) {
  const fields = [...kind.fields, ...(provider.builtin ? [] : [NAME])]
  return (
    <div className="flex gap-4 px-inset pt-1 pb-6" data-testid="provider-fields">
      <span className="size-icon shrink-0" />
      <div className="min-w-0 flex-1 space-y-3">
        {[...fields.filter((f) => f.required), ...fields.filter((f) => !f.required)].map((f) => (
          <ProviderField key={f.name} provider={provider} field={f} />
        ))}
        {provider.builtin && provider.kind === "anthropic" ? <CacheDiagnostics /> : null}
        {provider.builtin ? null : <RemoveProvider provider={provider} />}
      </div>
    </div>
  )
}

/** Anthropic's own API alone can say where a turn's prompt first differed from the last, so its switch sits here. */
function CacheDiagnostics() {
  const values = useSettingsValues()
  const save = useSaveSettings()
  return (
    <div className="grid grid-cols-[8rem_minmax(0,1fr)] items-start gap-x-4">
      <label htmlFor="cache-diagnostics" className="pt-0.5 text-label">
        Cache diagnostics
      </label>
      <div className="min-w-0 space-y-1">
        <Switch
          id="cache-diagnostics"
          checked={values?.cache_diagnostics === true}
          disabled={values === undefined || save.isPending}
          onCheckedChange={(on) => save.mutate({ body: { values: { cache_diagnostics: on } } })}
          data-testid="cache-diagnostics"
        />
        <p className="text-caption text-muted-ink">
          Log where each turn of SAGE's own agent missed the prompt cache. Free, but refused for HIPAA-enabled
          organizations.
        </p>
      </div>
    </div>
  )
}

/** Under a provider you added: Remove, which the server refuses while your models use it. */
function RemoveProvider({ provider }: { provider: Provider }) {
  const remove = useRemoveProvider()
  return (
    <Button
      variant="text"
      className="text-caption text-danger"
      disabled={remove.isPending}
      onClick={() => remove.mutate({ params: { path: { provider: provider.id } } })}
      data-testid="provider-remove"
    >
      Remove {provider.title}
    </Button>
  )
}

function ProviderCheck({ provider }: { provider: Provider }) {
  const q = useQuery(available(provider.id))
  if (q.isFetching)
    return (
      <span className="flex items-center gap-1.5 text-caption text-muted-ink" data-testid="key-check">
        <CircleNotchIcon className="size-3.5 animate-spin" /> Checking
      </span>
    )
  const ok = q.data?.ok ?? false
  const message = q.data ? q.data.message : q.error ? errorMessage(q.error) : ""
  const usable = q.data?.models.filter((m) => m.tools).length ?? 0
  // A provider that works shows nothing, like a ready agent; only a failure takes room on the row.
  if (ok) {
    return (
      <span className="sr-only" data-testid="key-check" data-ok>
        Works · {q.data?.models.length ?? 0} models, {usable} can extract
      </span>
    )
  }
  return (
    <span className="flex min-w-0 items-center gap-2" data-testid="key-check" data-ok={false}>
      <span className="flex min-w-0 items-center gap-1.5 text-caption text-danger">
        <XCircleIcon weight="fill" className="size-3.5 shrink-0" />
        <span className="max-w-80 truncate" title={message}>
          {message}
        </span>
      </span>
      <Button
        variant="text"
        className="text-caption"
        onClick={() => void q.refetch()}
        data-testid="key-recheck"
      >
        Check again
      </Button>
    </span>
  )
}

/** Under a field's name: "Optional", or the field that would do instead of it. A field it simply needs says nothing. */
function FieldNeed({ field }: { field: KindField }) {
  const text = !field.required ? "Optional" : field.instead.length ? `Or ${field.instead.join(" or ")}` : null
  return text ? (
    <p className="text-caption text-muted-ink" data-testid="field-need">
      {text}
    </p>
  ) : null
}

/** The field's name. Its help is in a tooltip, so the open form is only names and inputs. */
function FieldLabel({ provider, field }: { provider: Provider; field: KindField }) {
  const label = (
    <label htmlFor={`provider-${provider.id}-${field.name}`} className="text-label">
      {field.title}
    </label>
  )
  return field.help ? <Tip label={field.help}>{label}</Tip> : label
}

function ProviderField({ provider, field }: { provider: Provider; field: KindField }) {
  const env = provider.locked[field.name]
  const saved =
    field.name === "name" ? (provider.builtin ? null : provider.title) : (provider.values[field.name] ?? null)
  return (
    <div className="grid grid-cols-[8rem_minmax(0,1fr)] items-start gap-x-4">
      <div className="pt-1.5">
        <FieldLabel provider={provider} field={field} />
        <FieldNeed field={field} />
      </div>
      <div className="min-w-0 space-y-1">
        {env ? (
          <p
            className="flex h-control items-center text-control text-muted-ink"
            data-testid={`field-${field.name}`}
          >
            Set by {env}
          </p>
        ) : field.secret ? (
          <SecretField provider={provider} field={field} saved={saved} />
        ) : (
          <TextField provider={provider} field={field} saved={saved ?? ""} />
        )}
      </div>
    </div>
  )
}

/** A key never shows its saved value, which the server sends masked. */
function SecretField({
  provider,
  field,
  saved,
}: {
  provider: Provider
  field: KindField
  saved: string | null
}) {
  const save = useSaveProvider()
  const [draft, setDraft] = useState<string | null>(null)
  const put = (value: string) =>
    save.mutate(
      { params: { path: { provider: provider.id } }, body: { values: { [field.name]: value } } },
      { onSuccess: () => setDraft(null) },
    )
  if (saved && draft === null)
    return (
      <div className="flex h-control items-center gap-4">
        <span
          className="min-w-0 flex-1 truncate text-control tabular-nums"
          data-testid={`secret-${field.name}`}
        >
          {saved}
        </span>
        <Button variant="text" onClick={() => setDraft("")} data-testid={`secret-change-${field.name}`}>
          Change
        </Button>
        <Button
          variant="text"
          className="text-danger"
          disabled={save.isPending}
          onClick={() => put("")}
          data-testid={`secret-remove-${field.name}`}
        >
          Remove
        </Button>
      </div>
    )
  const typed = draft?.trim() ?? ""
  return (
    <form
      className="flex items-center gap-2"
      onSubmit={(e) => {
        e.preventDefault()
        if (typed) put(typed)
      }}
    >
      <Input
        id={`provider-${provider.id}-${field.name}`}
        type="password"
        autoComplete="off"
        autoFocus={saved !== null}
        placeholder={`Paste your ${provider.title} ${field.title}`}
        value={draft ?? ""}
        onChange={(e) => setDraft(e.target.value)}
        data-testid={`secret-input-${field.name}`}
      />
      {saved ? (
        <Button
          variant="ghost"
          type="button"
          onClick={() => setDraft(null)}
          data-testid={`secret-keep-${field.name}`}
        >
          Cancel
        </Button>
      ) : null}
      <Button type="submit" disabled={!typed || save.isPending} data-testid={`secret-save-${field.name}`}>
        Save
      </Button>
    </form>
  )
}

function TextField({ provider, field, saved }: { provider: Provider; field: KindField; saved: string }) {
  const save = useSaveProvider()
  const [draft, setDraft] = useState<string | null>(null)
  const changed = draft !== null && draft.trim() !== saved
  return (
    <form
      className="flex items-center gap-2"
      onSubmit={(e) => {
        e.preventDefault()
        if (changed)
          save.mutate(
            { params: { path: { provider: provider.id } }, body: { values: { [field.name]: draft.trim() } } },
            { onSuccess: () => setDraft(null) },
          )
      }}
    >
      <Input
        id={`provider-${provider.id}-${field.name}`}
        placeholder={field.placeholder || field.default || ""}
        value={draft ?? saved}
        onChange={(e) => setDraft(e.target.value)}
        data-testid={`field-${field.name}`}
      />
      {changed ? (
        <Button type="submit" disabled={save.isPending} data-testid={`field-save-${field.name}`}>
          Save
        </Button>
      ) : null}
    </form>
  )
}
