import { useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { $api, errorMessage, type Schemas } from "@/api/client"

export type Provider = Schemas["ProviderInfo"]
export type Kind = Schemas["KindInfo"]
export type KindField = Schemas["KindFieldInfo"]

/** The mark a provider shows: its kind's, except a provider you added that speaks OpenAI's API, such as a gateway,
 * which isn't OpenAI. */
export function providerBrand(provider: Provider | undefined): string {
  return provider && (provider.builtin || provider.kind !== "openai") ? provider.kind : ""
}

/** What a provider lets its credentials use. Asking costs nothing but a round trip, so it is asked again only after a
 * few minutes, or when the provider changes. */
export function available(provider: string) {
  return $api.queryOptions(
    "get",
    "/api/providers/{provider}/models",
    { params: { path: { provider } } },
    { staleTime: 5 * 60_000, retry: false },
  )
}

/** Refreshes what a change to providers or models can alter: both lists, which the runs' blockers read, and, for a
 * change to one provider, what that provider offers. The others keep their answers, so their checks don't run again. */
function useRefresh() {
  const queryClient = useQueryClient()
  return (provider?: string) => {
    for (const path of ["/api/providers", "/api/models"])
      void queryClient.invalidateQueries({ queryKey: ["get", path] })
    if (provider !== undefined) void queryClient.invalidateQueries({ queryKey: available(provider).queryKey })
  }
}

export function useSaveProvider(onSaved?: (p: Provider) => void) {
  const refresh = useRefresh()
  return $api.useMutation("put", "/api/providers/{provider}", {
    onSuccess: (p) => {
      refresh(p.id)
      onSaved?.(p)
    },
    onError: (e) => toast.error(errorMessage(e)),
  })
}

export function useRemoveProvider() {
  const refresh = useRefresh()
  return $api.useMutation("delete", "/api/providers/{provider}", {
    onSuccess: (_, { params }) => refresh(params.path.provider),
    onError: (e) => toast.error(errorMessage(e)),
  })
}

export function useAddModel(onAdded?: () => void) {
  const refresh = useRefresh()
  return $api.useMutation("post", "/api/models", {
    onSuccess: () => {
      refresh()
      onAdded?.()
    },
    onError: (e) => toast.error(errorMessage(e)),
  })
}

export function useRemoveModel() {
  const refresh = useRefresh()
  return $api.useMutation("post", "/api/models/remove", {
    onSuccess: () => refresh(),
    onError: (e) => toast.error(errorMessage(e)),
  })
}
