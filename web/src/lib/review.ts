import { useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { $api, errorMessage, type Schemas } from "@/api/client"
import { itemCount } from "@/lib/format"
import type { Item, QueueItem } from "@/lib/items"
import type { Summary } from "@/lib/paper-state"

export type Edits = Record<string, string>

type ReviewBody = Schemas["Review"]

/** Accept, edit and "Not in the paper" for the items of one extraction. Each call appends to reviews.jsonl; the
 * latest review of an item wins. A review shows at once: the queue and the paper's counts take it before the server
 * answers, and the server's copy replaces them once the last save lands. */
export function useReview(runId: string) {
  const queryClient = useQueryClient()
  const queueKey = $api.queryOptions("get", "/api/runs/{run_id}/queue", {
    params: { path: { run_id: runId } },
  }).queryKey
  const refresh = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["get", "/api/runs/{run_id}/queue"] }),
      queryClient.invalidateQueries({ queryKey: ["get", "/api/papers/{paper_id}"] }),
      queryClient.invalidateQueries({ queryKey: ["get", "/api/papers"] }),
    ])
  // The item leaves the queue's open set, and the paper's count of what is left drops with it.
  const show = (body: ReviewBody) => {
    const before = queryClient
      .getQueryData<QueueItem[]>(queueKey)
      ?.find((i) => i.record_id === body.record_id)
    queryClient.setQueryData<QueueItem[]>(queueKey, (queue) =>
      queue?.map((i) => (i.record_id === body.record_id ? { ...i, review: body } : i)),
    )
    if (!before || before.review) return
    const field = before.status === "verified" ? "looks_good" : "to_check"
    queryClient.setQueriesData<Summary>({ queryKey: ["get", "/api/papers/{paper_id}"] }, (s) =>
      s?.data_run?.id === runId ? { ...s, [field]: Math.max((s[field] ?? 0) - 1, 0) } : s,
    )
  }
  const post = $api.useMutation("post", "/api/reviews", {
    onMutate: async ({ body }) => {
      // A poll that started before the save would land the old queue or count over the shown review.
      await Promise.all([
        queryClient.cancelQueries({ queryKey: queueKey }),
        queryClient.cancelQueries({ queryKey: ["get", "/api/papers/{paper_id}"] }),
      ])
      show(body)
    },
    onError: (e) => toast.error(errorMessage(e)),
    // Refetch only after the last save in flight, so an earlier save's refetch can't undo a later one's review.
    onSettled: () =>
      queryClient.isMutating({ mutationKey: ["post", "/api/reviews"] }) === 1 ? refresh() : undefined,
  })
  const good = $api.useMutation("post", "/api/runs/{run_id}/accept-looks-good", {
    onSuccess: async (accepted) => {
      await refresh()
      toast(`Accepted ${itemCount(accepted.length)}`)
    },
    onError: (e) => toast.error(errorMessage(e)),
  })
  // A failed save has already shown its toast through the mutation's onError, and the refetch takes its review back.
  const save = (bodies: ReviewBody[]) => {
    for (const body of bodies) post.mutate({ body })
  }
  const base = (item: Item) => ({ run_id: runId, record_id: item.id })
  return {
    busy: good.isPending,
    accept: (item: Item) => save([{ ...base(item), action: "accept" }]),
    acceptMany: (items: Item[]) => save(items.map((item) => ({ ...base(item), action: "accept" as const }))),
    edit: (item: Item, edits: Edits) =>
      save(Object.entries(edits).map(([field, value]) => ({ ...base(item), action: "edit", field, value }))),
    reject: (item: Item, note: string) =>
      save([{ ...base(item), action: "reject", note: note.trim() || null }]),
    acceptLooksGood: () => good.mutate({ params: { path: { run_id: runId } } }),
  }
}
