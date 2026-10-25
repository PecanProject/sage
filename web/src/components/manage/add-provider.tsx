import { CircleNotchIcon } from "@phosphor-icons/react"
import { useState } from "react"
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
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { type Kind, type Provider, useSaveProvider } from "@/lib/providers"

/** A provider id from its name: "Company gateway" becomes company-gateway. */
function slug(name: string): string {
  return name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 32)
}

export function AddProviderDialog({
  open,
  kinds,
  providers,
  onClose,
  onAdded,
}: {
  open: boolean
  kinds: Kind[]
  providers: Provider[]
  onClose: () => void
  /** With the new provider's id, to open its fields. */
  onAdded: (id: string) => void
}) {
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-[520px]" data-testid="add-provider-dialog">
        {open ? (
          <AddProviderForm kinds={kinds} providers={providers} onClose={onClose} onAdded={onAdded} />
        ) : null}
      </DialogContent>
    </Dialog>
  )
}

function AddProviderForm({
  kinds,
  providers,
  onClose,
  onAdded,
}: {
  kinds: Kind[]
  providers: Provider[]
  onClose: () => void
  onAdded: (id: string) => void
}) {
  const [kindId, setKindId] = useState("openai")
  const [name, setName] = useState("")
  const [id, setId] = useState<string | null>(null)
  const [values, setValues] = useState<Record<string, string>>({})
  const save = useSaveProvider((p) => onAdded(p.id))
  const kind = kinds.find((k) => k.kind === kindId)
  const providerId = id ?? slug(name)
  const taken = providers.some((p) => p.id === providerId)
  const value = (field: string) => values[field] ?? kind?.fields.find((f) => f.name === field)?.default ?? ""
  const ready = Boolean(kind) && name.trim() !== "" && /^[a-z0-9][a-z0-9-]{0,31}$/.test(providerId) && !taken
  return (
    <form
      className="contents"
      onSubmit={(e) => {
        e.preventDefault()
        if (!ready || !kind) return
        const filled = Object.fromEntries(
          kind.fields.map((f) => [f.name, value(f.name).trim()]).filter(([, v]) => v !== ""),
        )
        save.mutate({
          params: { path: { provider: providerId } },
          body: { kind: kindId, values: { name: name.trim(), ...filled } },
        })
      }}
    >
      <DialogHeader>
        <DialogTitle>Add a provider</DialogTitle>
      </DialogHeader>
      <DialogBody className="space-y-4">
        <div className="grid gap-1.5">
          <label htmlFor="add-provider-kind" className="text-label">
            Kind
          </label>
          <Select
            value={kindId}
            onValueChange={(v) => typeof v === "string" && setKindId(v)}
            items={kinds.map((k) => ({ value: k.kind, label: k.title }))}
          >
            <SelectTrigger id="add-provider-kind" data-testid="add-provider-kind">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {kinds.map((k) => (
                <SelectItem key={k.kind} value={k.kind}>
                  {k.title}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {kind ? <p className="text-caption text-muted-ink">{kind.help}</p> : null}
        </div>
        <div className="grid grid-cols-[minmax(0,1fr)_10rem] gap-3">
          <div className="grid gap-1.5">
            <label htmlFor="add-provider-name" className="text-label">
              Name
            </label>
            <Input
              id="add-provider-name"
              value={name}
              placeholder="Company gateway"
              onChange={(e) => setName(e.target.value)}
              data-testid="add-provider-name"
            />
          </div>
          <div className="grid gap-1.5">
            <label htmlFor="add-provider-id" className="text-label">
              ID
            </label>
            <Input
              id="add-provider-id"
              value={providerId}
              onChange={(e) => setId(slug(e.target.value))}
              data-testid="add-provider-id"
            />
          </div>
        </div>
        <p className="-mt-2 text-caption text-muted-ink">
          {taken ? (
            <span className="text-danger">There is already a provider {providerId}.</span>
          ) : (
            `Its models go by ${providerId || "id"}:name.`
          )}
        </p>
        {kind?.fields.map((f) => (
          <div key={f.name} className="grid gap-1.5">
            <label htmlFor={`add-provider-${f.name}`} className="text-label">
              {f.title}
            </label>
            <Input
              id={`add-provider-${f.name}`}
              type={f.secret ? "password" : "text"}
              autoComplete="off"
              placeholder={f.placeholder}
              value={value(f.name)}
              onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
              data-testid={`add-provider-${f.name}`}
            />
            {f.help ? <span className="text-caption text-muted-ink">{f.help}</span> : null}
          </div>
        ))}
      </DialogBody>
      <DialogFooter>
        <Button variant="ghost" type="button" onClick={onClose}>
          Cancel
        </Button>
        <Button type="submit" disabled={!ready || save.isPending} data-testid="add-provider-save">
          {save.isPending ? <CircleNotchIcon className="animate-spin" /> : null}
          Add provider
        </Button>
      </DialogFooter>
    </form>
  )
}
