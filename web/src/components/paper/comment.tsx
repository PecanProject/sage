import { PencilSimpleIcon, TrashIcon } from "@phosphor-icons/react"
import { type ReactNode, useState } from "react"
import type { ReviewActions } from "@/components/paper/margin"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"

export type CommentActions = Pick<
  ReviewActions,
  "busy" | "editComment" | "removeComment" | "commentOpen" | "openComment" | "closeComment"
>

/** A comment's words with Edit and Remove on hover, or its text in an input while `editing`. Either reaches every
 * item the comment was left on. Edit calls `onEdit`, which opens the comment on an item it was left on. One not yet
 * saved has no id, and nothing to change. */
export function EditableComment({
  id,
  note,
  editing,
  onEdit,
  actions,
  className,
  children,
}: {
  id: string | undefined
  note: string
  editing: boolean
  onEdit: (id: string) => void
  actions: CommentActions
  className?: string
  children: ReactNode
}) {
  const [draft, setDraft] = useState<string | null>(null)
  const close = () => {
    setDraft(null)
    actions.closeComment()
  }
  if (editing && id) {
    const text = draft ?? note
    return (
      <form
        className="flex items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (!text.trim()) return
          actions.editComment(id, text)
          close()
        }}
        onKeyDown={(e) => {
          if (e.key !== "Escape") return
          e.stopPropagation()
          close()
        }}
      >
        <Input
          autoFocus
          value={text}
          onChange={(e) => setDraft(e.target.value)}
          aria-label="Comment"
          data-testid="comment-edit-input"
        />
        <Button
          size="compact"
          type="submit"
          disabled={actions.busy || !text.trim()}
          data-testid="comment-edit-save"
        >
          Save
        </Button>
        <Button size="compact" variant="ghost" type="button" onClick={close}>
          Cancel
        </Button>
      </form>
    )
  }
  return (
    <div className={cn("group flex items-start gap-2", className)}>
      {children}
      {id ? (
        <span className="ml-auto flex shrink-0 opacity-0 transition-opacity group-focus-within:opacity-100 group-hover:opacity-100">
          <Button
            variant="quiet"
            size="icon-sm"
            className="size-5"
            aria-label="Edit comment"
            onClick={() => onEdit(id)}
            data-testid="comment-edit"
          >
            <PencilSimpleIcon className="size-3" />
          </Button>
          <Button
            variant="quiet"
            size="icon-sm"
            className="size-5"
            aria-label="Remove comment"
            onClick={() => actions.removeComment(id)}
            data-testid="comment-remove"
          >
            <TrashIcon className="size-3" />
          </Button>
        </span>
      ) : null}
    </div>
  )
}
