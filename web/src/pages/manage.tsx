import { useNavigate, useSearch } from "@tanstack/react-router"
import { $api, errorMessage } from "@/api/client"
import { AddProviderDialog } from "@/components/manage/add-provider"
import { Agents } from "@/components/manage/agents"
import { PriceDialog } from "@/components/manage/price-dialog"
import { Providers } from "@/components/manage/providers"
import { Terminal } from "@/components/manage/terminal"
import { YourModels } from "@/components/manage/your-models"
import { TopBar } from "@/components/top-bar"
import { TextSkeleton } from "@/components/ui/skeleton"
import type { ManageSearch } from "@/router"

/** How SAGE extracts: the skill for a terminal's agent, the agents it starts, and your models and their providers. */
export function ManagePage() {
  const search = useSearch({ strict: false }) as ManageSearch
  const navigate = useNavigate()
  const close = () => void navigate({ to: "/manage", search: {}, resetScroll: false })
  const settings = $api.useQuery("get", "/api/settings")
  const providers = $api.useQuery("get", "/api/providers")
  const kinds = $api.useQuery("get", "/api/provider-kinds", {}, { staleTime: Number.POSITIVE_INFINITY })
  const models = $api.useQuery("get", "/api/models")
  const failed = settings.error ?? providers.error ?? kinds.error ?? models.error
  return (
    <div className="min-h-svh" data-scroll="document" data-testid="manage-page">
      <TopBar title="Manage" />
      <main className="mx-auto w-full max-w-[760px] space-y-12 px-gutter pt-10 pb-16">
        <h1 className="px-inset text-display">
          Manage <span className="text-muted-ink">skills, agents & models</span>
        </h1>
        {failed ? (
          <p className="px-inset text-body text-danger">Couldn't load this page: {errorMessage(failed)}</p>
        ) : settings.data && providers.data && kinds.data && models.data ? (
          <>
            <Terminal />
            <Agents view={settings.data} />
            <YourModels view={settings.data} models={models.data} providers={providers.data} />
            <Providers providers={providers.data} kinds={kinds.data} open={search.provider} />
            <AddProviderDialog
              open={search.add === "provider"}
              kinds={kinds.data}
              providers={providers.data}
              onClose={close}
              onAdded={(id) => void navigate({ to: "/manage", search: { provider: id } })}
            />
            <PriceDialog modelId={search.price ?? null} onClose={close} />
          </>
        ) : (
          <ManageSkeleton />
        )}
      </main>
    </div>
  )
}

function ManageSkeleton() {
  return (
    <ul className="flex flex-col" data-testid="manage-loading">
      {["w-44", "w-52", "w-40"].map((w) => (
        <li key={w} className="flex h-14 items-center gap-6 px-inset">
          <div className="flex-1 text-value">
            <TextSkeleton className={w} />
          </div>
          <div className="text-caption">
            <TextSkeleton className="w-20" />
          </div>
        </li>
      ))}
    </ul>
  )
}
