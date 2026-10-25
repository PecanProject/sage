import { CircleNotchIcon, PlusIcon } from "@phosphor-icons/react"
import { useQueries } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { useState } from "react"
import { toast } from "sonner"
import { errorMessage, fetchClient, type Schemas } from "@/api/client"
import {
  Combobox,
  ComboboxCollection,
  ComboboxContent,
  ComboboxGroup,
  ComboboxGroupLabel,
  ComboboxItem,
  ComboboxTrigger,
} from "@/components/ui/combobox"
import { price } from "@/lib/models"
import { available, type Provider, useAddModel } from "@/lib/providers"

type Candidate = Schemas["Candidate"]
type Item = { id: string; option: Candidate | null }
type Group = { provider: string; title: string; items: Item[] }

/** Every word has to match, and hyphens and dots count as spaces, so "gemini 3" finds google:gemini-3.5-flash. */
function matches(text: string, query: string): boolean {
  const plain = text.toLowerCase().replace(/[-_.]/g, " ")
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((w) => plain.includes(w.replace(/[-_.]/g, " ")))
}

/** Searches the models each ready provider offers, by name or id. A model SAGE has no price for asks for yours
 * first; one that can't call tools shows why it can't be added. A full `provider:name` can be added by name too. */
export function AddModel({ providers, yours }: { providers: Provider[]; yours: string[] }) {
  const navigate = useNavigate()
  const [query, setQuery] = useState("")
  const add = useAddModel()
  const ready = providers.filter((p) => p.missing.length === 0)
  const lists = useQueries({ queries: ready.map((p) => available(p.id)) })
  const taken = new Set(yours)
  const groups: Group[] = ready.flatMap((p, i) => {
    const items = (lists[i]?.data?.models ?? [])
      .filter((m) => !taken.has(m.id) && matches(`${m.title} ${m.id}`, query))
      .map((m) => ({ id: m.id, option: m }))
    return items.length ? [{ provider: p.id, title: p.title, items }] : []
  })
  const typed = query.trim()
  const [named, name] = typed.split(/:(.*)/)
  if (
    name &&
    providers.some((p) => p.id === named) &&
    !taken.has(typed) &&
    !groups.some((g) => g.items.some((i) => i.id === typed))
  )
    groups.push({ provider: "", title: "By name", items: [{ id: typed, option: null }] })
  const loading = lists.some((q) => q.isLoading)
  const pick = async (item: Item) => {
    const found = item.option ?? (await describe(item.id))
    if (found === null) return
    if (!found.tools) toast.error(`${found.title} can't call tools, and SAGE extracts only through them`)
    else if (found.input_usd === null) void navigate({ to: "/manage", search: { price: found.id } })
    else add.mutate({ body: { id: found.id, title: found.title } })
  }
  return (
    <Combobox<Item>
      items={groups}
      filter={null}
      autoHighlight
      value={null}
      onValueChange={(item) => {
        if (item) void pick(item)
        setQuery("")
      }}
      inputValue={query}
      onInputValueChange={setQuery}
      onOpenChange={(open) => !open && setQuery("")}
      isItemEqualToValue={(a, b) => a.id === b.id}
      itemToStringLabel={(item) => item.option?.title ?? item.id}
      disabled={add.isPending}
    >
      <ComboboxTrigger className="-ml-3" aria-label="Add a model" data-testid="add-model">
        {add.isPending ? (
          <CircleNotchIcon className="size-4 animate-spin" />
        ) : (
          <PlusIcon className="size-4" />
        )}
        Add a model
      </ComboboxTrigger>
      <ComboboxContent
        placeholder="Search, or type provider:name"
        empty={
          loading
            ? "Asking your providers…"
            : !ready.length
              ? "Set up a provider below to see its models."
              : lists.some((q) => q.data?.ok)
                ? "None of your providers' models matches that."
                : "None of your providers answered. Providers below says why."
        }
        footer="Prices per million tokens, in / out"
        data-testid="add-model-list"
      >
        {(group: Group) => (
          <ComboboxGroup key={group.provider} items={group.items}>
            <ComboboxGroupLabel>{group.title}</ComboboxGroupLabel>
            <ComboboxCollection>
              {(item: Item) => (
                <ComboboxItem
                  key={item.id}
                  value={item}
                  disabled={item.option !== null && !item.option.tools}
                  data-testid="add-model-option"
                  data-model={item.id}
                >
                  <span className="min-w-0 flex-1 truncate">{item.option?.title ?? `Add ${item.id}`}</span>
                  <span className="shrink-0 text-caption text-muted-ink tabular-nums">
                    {item.option === null
                      ? null
                      : !item.option.tools
                        ? "Can't call tools"
                        : (price(item.option) ?? "Needs your price")}
                  </span>
                </ComboboxItem>
              )}
            </ComboboxCollection>
          </ComboboxGroup>
        )}
      </ComboboxContent>
    </Combobox>
  )
}

async function describe(id: string): Promise<Candidate | null> {
  const { data, error } = await fetchClient.GET("/api/models/describe", { params: { query: { id } } })
  if (error) toast.error(errorMessage(error))
  return data ?? null
}
