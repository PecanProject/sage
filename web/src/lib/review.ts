import { useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { $api, type Schemas } from "@/api/client"
import { itemCount } from "@/lib/format"
import type { Item, QueueItem } from "@/lib/items"
import type { Summary } from "@/lib/paper-state"

export type Edits = Record<string, string>

type ReviewBody = Schemas["Review"]
type Batch = Schemas["ReviewBatch"]

/** What a shared decision or comment says once it is saved: "Accepted 12 records". */
const DONE: Record<Batch["action"], string> = {
  accept: "Accepted",
  edit: "Edited",
  reject: "Rejected",
  comment: "Commented on",
}

/** Reasons offered when nothing has been rejected yet. */
const FIRST_REASONS = ["Not in the paper", "Wrong value", "Wrong treatment or crop"]

/** The reasons to offer for a rejection: the ones used in this extraction, latest first, then the usual ones. */
export function reasonsOf(items: { review?: ReviewBody | null }[]): string[] {
  const used = items
    .flatMap((i) => (i.review?.action === "reject" && i.review.note ? [i.review] : []))
    .sort((a, b) => String(b.ts).localeCompare(String(a.ts)))
    .map((r) => r.note as string)
  return [...new Set([...used, ...FIRST_REASONS])].slice(0, 4)
}

/** Accept, edit, reject and comment for the items of one extraction. Each call appends to reviews.jsonl; the latest
 * decision on an item wins, and comments add up. A review shows at once: the queue and the paper's counts take it
 * before the server answers, and the server's copy replaces them once the last save lands. A comment can be changed
 * or removed later, on every item it was left on. */
export function useReview(runId: string, paperId: string) {
  const queryClient = useQueryClient()
  const queueKey = $api.queryOptions("get", "/api/runs/{run_id}/queue", {
    params: { path: { run_id: runId } },
  }).queryKey
  const refresh = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["get", "/api/runs/{run_id}/queue"] }),
      queryClient.invalidateQueries({ queryKey: ["get", "/api/runs/{run_id}/workbook"] }),
      queryClient.invalidateQueries({ queryKey: ["get", "/api/papers/{paper_id}"] }),
      queryClient.invalidateQueries({ queryKey: ["get", "/api/papers"] }),
    ])
  // A decision takes the item out of the queue's open set, and the paper's count of what is left drops with it. A
  // comment only joins the item's comments.
  const show = (body: ReviewBody) => {
    const before = queryClient
      .getQueryData<QueueItem[]>(queueKey)
      ?.find((i) => i.record_id === body.record_id)
    queryClient.setQueryData<QueueItem[]>(queueKey, (queue) =>
      queue?.map((i) =>
        i.record_id !== body.record_id
          ? i
          : body.action === "comment"
            ? { ...i, comments: [...(i.comments ?? []), body] }
            : { ...i, review: body },
      ),
    )
    if (!before || before.review || body.action === "comment") return
    const field = before.status === "verified" ? "looks_good" : "to_check"
    queryClient.setQueriesData<Summary>({ queryKey: ["get", "/api/papers/{paper_id}"] }, (s) =>
      s?.data_run?.id === runId ? { ...s, [field]: Math.max((s[field] ?? 0) - 1, 0) } : s,
    )
  }
  const hold = () =>
    // A poll that started before the save would land the old queue or count over the shown review.
    Promise.all([
      queryClient.cancelQueries({ queryKey: queueKey }),
      queryClient.cancelQueries({ queryKey: ["get", "/api/papers/{paper_id}"] }),
    ])
  // Refetch only after the last save in flight, so an earlier save's refetch can't undo a later one's review.
  const settle = () =>
    queryClient.isMutating({ mutationKey: ["post", "/api/reviews"] }) +
      queryClient.isMutating({ mutationKey: ["post", "/api/reviews/batch"] }) ===
    1
      ? refresh()
      : undefined
  const post = $api.useMutation("post", "/api/reviews", {
    onMutate: async ({ body }) => {
      await hold()
      show(body)
    },
    onSettled: settle,
  })
  const many = $api.useMutation("post", "/api/reviews/batch", {
    onMutate: async ({ body }) => {
      await hold()
      for (const record_id of body.record_ids)
        show({ run_id: body.run_id, record_id, action: body.action, field: body.field, note: body.note })
    },
    onSuccess: (saved) => toast(`${DONE[saved[0]?.action ?? "accept"]} ${itemCount(saved.length)}`),
    onSettled: settle,
  })
  const changeComment = $api.useMutation("patch", "/api/papers/{paper_id}/comments/{comment_id}", {
    onSettled: refresh,
  })
  const removeComment = $api.useMutation("delete", "/api/papers/{paper_id}/comments/{comment_id}", {
    onSuccess: () => toast("Comment removed"),
    onSettled: refresh,
  })
  const commentPath = (id: string) => ({ path: { paper_id: paperId, comment_id: id } })
  // A failed save has already shown its toast, and the refetch takes its review back.
  const batch = (
    items: Item[],
    action: Batch["action"],
    note: string | null = null,
    field: string | null = null,
  ) => {
    if (items.length === 0) return
    if (items.length === 1 && items[0])
      post.mutate({ body: { run_id: runId, record_id: items[0].id, action, field, note } })
    else many.mutate({ body: { run_id: runId, record_ids: items.map((i) => i.id), action, field, note } })
  }
  return {
    busy: many.isPending,
    accept: (item: Item) => batch([item], "accept"),
    acceptMany: (items: Item[]) => batch(items, "accept"),
    edit: (item: Item, edits: Edits) => {
      for (const [field, value] of Object.entries(edits))
        post.mutate({ body: { run_id: runId, record_id: item.id, action: "edit", field, value } })
    },
    reject: (items: Item[], note: string) => batch(items, "reject", note.trim() || null),
    comment: (items: Item[], note: string, field: string | null) =>
      batch(items, "comment", note.trim(), field),
    editComment: (id: string, note: string) =>
      changeComment.mutate({ params: commentPath(id), body: { note: note.trim() } }),
    removeComment: (id: string) => removeComment.mutate({ params: commentPath(id) }),
  }
}
