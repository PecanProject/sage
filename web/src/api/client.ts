import { MutationCache, QueryClient } from "@tanstack/react-query"
import createFetchClient from "openapi-fetch"
import createClient from "openapi-react-query"
import { toast } from "sonner"
import type { components, paths } from "./schema"

export type Schemas = components["schemas"]

export const fetchClient = createFetchClient<paths>({ baseUrl: "" })
export const $api = createClient(fetchClient)

/** The request options, and so the query key part, for a route on one paper or one run. */
export const paperPath = (paperId: string) => ({ params: { path: { paper_id: paperId } } })
export const runPath = (runId: string) => ({ params: { path: { run_id: runId } } })

/** The app's one query cache. Route loaders fill it under the same keys `$api` hooks read. A failed action says why
 * in a toast, unless its mutation handles the error itself. */
export const queryClient = new QueryClient({
  mutationCache: new MutationCache({
    onError: (error, _variables, _context, mutation) => {
      if (!mutation.options.onError) toast.error(errorMessage(error))
    },
  }),
  defaultOptions: {
    queries: { refetchOnWindowFocus: false, retry: (failures, error) => failures < 1 && !notFound(error) },
  },
})

/** The server's answer for an id that doesn't exist. Asking again won't change it. */
function notFound(error: unknown): boolean {
  return Boolean(error && typeof error === "object" && "kind" in error && error.kind === "not_found")
}

/** fetch rejects with a TypeError, worded per browser, when the server can't be reached at all. */
function unreachable(error: unknown): boolean {
  return error instanceof TypeError && /failed to fetch|load failed|networkerror/i.test(error.message)
}

export function errorMessage(error: unknown): string {
  if (unreachable(error)) return "Can't reach SAGE. Is `sage ui` still running?"
  if (error && typeof error === "object" && "message" in error && typeof error.message === "string") {
    return error.message
  }
  if (error && typeof error === "object" && "detail" in error) {
    return JSON.stringify(error.detail)
  }
  return String(error)
}
