import { useQueryClient } from "@tanstack/react-query"
import { $api, type Schemas } from "@/api/client"

export type ModelOption = Schemas["ModelOption"]

/** Your models, the ones extraction may use, from the Manage page. */
export function useModels(): ModelOption[] {
  const { data } = $api.useQuery("get", "/api/models")
  return data ?? []
}

/** A model's name: the server's for one of yours, else its id without the provider, such as an old run's. */
export function useModelTitle(): (id: string) => string {
  const models = useModels()
  return (id) => models.find((m) => m.id === id)?.title ?? id.slice(id.indexOf(":") + 1)
}

/** Saves the effort SAGE's own agent has one of your models think at, and shows the answer at once. */
export function useSetModelEffort() {
  const queryClient = useQueryClient()
  return $api.useMutation("post", "/api/models/effort", {
    onSuccess: (models) => queryClient.setQueryData(["get", "/api/models"], models),
  })
}

/** Dollars per million tokens, to the cent. */
function perMillion(usd: number): string {
  return `$${Number.isInteger(usd) ? usd : usd.toFixed(2)}`
}

/** A price per million tokens, in / out, or null when SAGE has none. */
export function price(m: { input_usd: number | null; output_usd: number | null }): string | null {
  if (m.input_usd === null || m.output_usd === null) return null
  return `${perMillion(m.input_usd)} / ${perMillion(m.output_usd)}`
}
