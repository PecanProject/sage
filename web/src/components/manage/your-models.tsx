import { CloudIcon, DotsThreeIcon } from "@phosphor-icons/react"
import { useNavigate } from "@tanstack/react-router"
import type { Schemas } from "@/api/client"
import { BrandIcon } from "@/components/brand-icon"
import { ModelEffortSelect } from "@/components/effort-select"
import { AddModel } from "@/components/manage/add-model"
import { DefaultMark, Row, Section } from "@/components/manage/section"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { defaultUsing, useSaveSettings } from "@/lib/agents"
import { type ModelOption, price, useSetModelEffort } from "@/lib/models"
import { type Provider, providerBrand, useRemoveModel } from "@/lib/providers"
import { cn } from "@/lib/utils"

export function YourModels({
  view,
  models,
  providers,
}: {
  view: Schemas["SettingsView"]
  models: ModelOption[]
  providers: Provider[]
}) {
  const save = useSaveSettings()
  const remove = useRemoveModel()
  const setEffort = useSetModelEffort()
  const using = defaultUsing(view.values)
  return (
    <Section
      title="Your models"
      hint="SAGE's own agent runs on these. Prices are per million tokens, in / out."
    >
      <ul className="flex flex-col" data-testid="your-models">
        {models.map((m) => (
          <ModelRow
            key={m.id}
            model={m}
            provider={providers.find((p) => p.id === m.provider)}
            isDefault={using === m.id}
            onDefault={() => save.mutate({ body: { values: { extract_with: m.id } } })}
            onEffort={(effort) => setEffort.mutate({ body: { id: m.id, effort } })}
            onRemove={() => remove.mutate({ body: { ids: [m.id] } })}
          />
        ))}
      </ul>
      <div className="px-inset">
        <AddModel providers={providers} yours={models.map((m) => m.id)} />
      </div>
    </Section>
  )
}

function ModelRow({
  model,
  provider,
  isDefault,
  onDefault,
  onEffort,
  onRemove,
}: {
  model: ModelOption
  provider: Provider | undefined
  /** What Extract uses unless a run picks. */
  isDefault: boolean
  onDefault: () => void
  onEffort: (effort: string) => void
  onRemove: () => void
}) {
  const navigate = useNavigate()
  const unready = Boolean(provider?.missing.length)
  const cost = price(model)
  return (
    <Row
      lead={<DefaultMark on={isDefault} testid="model-roles" />}
      icon={<BrandIcon brand={providerBrand(provider)} fallback={CloudIcon} className="size-icon text-ink" />}
      title={<span className="truncate text-value">{model.title}</span>}
      detail={
        <>
          <span className={cn(unready && "text-danger")}>
            {provider?.title ?? model.provider}
            {unready ? " · needs setup" : ""}
          </span>
          {cost ? (
            <span className="tabular-nums" data-testid="model-price">
              {` · ${cost}`}
              {model.your_price ? " yours" : ""}
            </span>
          ) : null}
        </>
      }
      actions={
        <>
          <div className="w-36 shrink-0">
            <ModelEffortSelect
              model={model}
              value={model.effort}
              onChange={onEffort}
              variant="ghost"
              testid="model-effort"
            />
          </div>
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <Button
                  variant="quiet"
                  size="icon-sm"
                  className="-mr-2"
                  aria-label="More"
                  data-testid="model-menu"
                />
              }
            >
              <DotsThreeIcon weight="bold" />
            </DropdownMenuTrigger>
            <DropdownMenuContent>
              <DropdownMenuItem disabled={isDefault} onClick={onDefault} data-testid="model-use">
                Use by default
              </DropdownMenuItem>
              <DropdownMenuItem
                onClick={() =>
                  void navigate({ to: "/manage", search: { price: model.id }, resetScroll: false })
                }
                data-testid="model-set-price"
              >
                Set price…
              </DropdownMenuItem>
              <DropdownMenuItem onClick={onRemove}>Remove</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </>
      }
      data-testid="model-row"
      data-model={model.id}
    />
  )
}
