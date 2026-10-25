import { useState } from "react"
import { $api } from "@/api/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Fold } from "@/components/ui/fold"
import { DollarInput } from "@/components/ui/input"
import { useSettingsValues } from "@/lib/agents"
import { useAddModel } from "@/lib/providers"

type Prices = { input: string; output: string; read: string; write: string }

const text = (v: unknown) => (typeof v === "number" ? String(v) : "")

/** The prices the dialog starts from: SAGE's for the model, and your cache prices as config.toml holds them. */
function useSavedPrices(
  id: string,
  found: { input_usd: number | null; output_usd: number | null } | undefined,
) {
  const values = useSettingsValues()
  const yours = (Array.isArray(values?.models) ? values.models : []).find(
    (m): m is Record<string, unknown> => typeof m === "object" && m !== null && "id" in m && m.id === id,
  )
  return {
    input: text(found?.input_usd),
    output: text(found?.output_usd),
    read: text(yours?.cache_read_usd),
    write: text(yours?.cache_write_usd),
  }
}

const valid = (v: string) => v !== "" && Number(v) >= 0

/** The prices to save, or null while one is missing or below 0. Cache prices are both or neither; left out, SAGE
 * uses the model's own cache discount, else the input price. */
function priceBody(p: Prices) {
  const cached = p.read !== "" || p.write !== ""
  if (!valid(p.input) || !valid(p.output) || (cached && !(valid(p.read) && valid(p.write)))) return null
  return {
    input_usd: Number(p.input),
    output_usd: Number(p.output),
    cache_read_usd: cached ? Number(p.read) : null,
    cache_write_usd: cached ? Number(p.write) : null,
  }
}

function PriceField({
  label,
  value,
  onChange,
  testid,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  testid: string
}) {
  return (
    <div className="grid gap-1.5">
      <label htmlFor={testid} className="text-label">
        {label}
      </label>
      <DollarInput
        id={testid}
        min={0}
        step="any"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        data-testid={testid}
      />
    </div>
  )
}

/** Your price for a model, which adds it when it isn't yours yet. SAGE stops a run at its cost limit, so a model
 * genai-prices can't price, such as one on a company gateway or a local server, needs one. */
export function PriceDialog({ modelId, onClose }: { modelId: string | null; onClose: () => void }) {
  return (
    <Dialog open={modelId !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-[440px]" data-testid="price-dialog">
        {modelId !== null ? <PriceForm key={modelId} modelId={modelId} onClose={onClose} /> : null}
      </DialogContent>
    </Dialog>
  )
}

function PriceForm({ modelId, onClose }: { modelId: string; onClose: () => void }) {
  const { data } = $api.useQuery("get", "/api/models/describe", { params: { query: { id: modelId } } })
  const [draft, setDraft] = useState<Partial<Prices>>({})
  const shown: Prices = { ...useSavedPrices(modelId, data), ...draft }
  const body = priceBody(shown)
  const add = useAddModel(onClose)
  const field = (key: keyof Prices, label: string, testid: string) => (
    <PriceField
      label={label}
      value={shown[key]}
      onChange={(v) => setDraft((d) => ({ ...d, [key]: v }))}
      testid={testid}
    />
  )
  return (
    <form
      className="contents"
      onSubmit={(e) => {
        e.preventDefault()
        if (body) add.mutate({ body: { id: modelId, title: data?.title, ...body } })
      }}
    >
      <DialogHeader>
        <DialogTitle>{data?.title ?? modelId}</DialogTitle>
      </DialogHeader>
      <DialogBody className="space-y-4">
        <p className="text-caption text-muted-ink">
          Per million tokens. SAGE stops a run at its cost limit, so it needs a price for every model. For a
          model that costs nothing, such as a local one, enter 0.
        </p>
        <div className="grid grid-cols-2 gap-3">
          {field("input", "Input", "price-input")}
          {field("output", "Output", "price-output")}
        </div>
        <Fold
          label="Cache prices"
          defaultOpen={shown.read !== "" || shown.write !== ""}
          className="grid grid-cols-2 gap-3"
          testid="price-cache"
        >
          {field("read", "Cache read", "price-cache-read")}
          {field("write", "Cache write", "price-cache-write")}
        </Fold>
      </DialogBody>
      <DialogFooter>
        <Button variant="ghost" type="button" onClick={onClose}>
          Cancel
        </Button>
        <Button
          type="submit"
          disabled={!body || add.isPending}
          pending={add.isPending}
          data-testid="price-save"
        >
          {data?.yours ? "Save" : "Add model"}
        </Button>
      </DialogFooter>
    </form>
  )
}
