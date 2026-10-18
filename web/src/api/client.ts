import { QueryClient } from "@tanstack/react-query"
import createFetchClient from "openapi-fetch"
import createClient from "openapi-react-query"
import type { components, paths } from "./schema"

export type Schemas = components["schemas"]

export const fetchClient = createFetchClient<paths>({ baseUrl: "" })
export const $api = createClient(fetchClient)

/** The app's one query cache. Route loaders fill it under the same keys `$api` hooks read. */
export const queryClient = new QueryClient({
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
