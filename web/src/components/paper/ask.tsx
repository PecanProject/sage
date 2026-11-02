import { ArrowUpIcon, CaretDownIcon, ChatCircleIcon, XIcon } from "@phosphor-icons/react"
import { useRef, useState } from "react"
import { BrandIcon } from "@/components/brand-icon"
import { type CommentActions, EditableComment } from "@/components/paper/comment"
import { ChoiceFields } from "@/components/paper/extract"
import type { ReviewActions } from "@/components/paper/margin"
import { Button } from "@/components/ui/button"
import { Input, Textarea } from "@/components/ui/input"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Tip } from "@/components/ui/tooltip"
import { useChoiceOf } from "@/lib/agents"
import { capital, fieldTitle } from "@/lib/format"
import type { Item } from "@/lib/items"
import { limitLine, type RunChoice } from "@/lib/run-choice"
import { cn } from "@/lib/utils"
import { placementOf, type Tab } from "@/lib/workbook"

/** A request to SAGE: what to add or fix, and the records the agent starts from. */
type Ask = { text: string; items: Item[] }

/** What the page gives Ask SAGE: why it can't take a request now, what a request runs with, whether to take the
 * cursor, and sending one, which starts a new pass on the run in view. */
export type AskRun = {
  disabled: string | null
  choice: RunChoice
  pending: boolean
  focus: boolean
  onFocused: () => void
  send: (ask: Ask) => void
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`

/** One comment, and every record it was left on: a comment on several rows at once is one line per row, sharing an
 * id. */
type Said = {
  key: string
  id: string | undefined
  note: string
  field: string | null
  ts: string
  items: Item[]
}

function said(items: Item[]): Said[] {
  const out = new Map<string, Said>()
  for (const item of items)
    for (const c of item.comments) {
      const key = c.id ?? `${c.field ?? ""}\u0000${c.note ?? ""}`
      const at = out.get(key)
      if (at) at.items.push(item)
      else
        out.set(key, {
          key,
          id: c.id,
          note: c.note ?? "",
          field: c.field ?? null,
          ts: String(c.ts),
          items: [item],
        })
    }
  return [...out.values()].toSorted((a, b) => b.ts.localeCompare(a.ts))
}

/** Every comment in the extraction, newest first, each with the records it is on; a record opens where it is
 * reviewed. */
function CommentList({
  comments,
  tabs,
  actions,
  onOpen,
  onEdit,
}: {
  comments: Said[]
  tabs: Tab[]
  actions: CommentActions
  onOpen: (id: string) => void
  /** Open a comment for editing on the first record it is on. */
  onEdit: (item: Item, id: string) => void
}) {
  return (
    <ul className="max-h-72 space-y-3 overflow-y-auto px-1 pt-1 pb-2" data-testid="ask-comments">
      {comments.map((c) => {
        const first = c.items[0]
        const column =
          c.field && first ? (placementOf(first, tabs).titles[c.field] ?? fieldTitle(c.field)) : null
        return (
          <li key={c.key} className="space-y-1" data-testid="ask-comment">
            <EditableComment
              id={c.id}
              note={c.note}
              editing={false}
              onEdit={(id) => first && onEdit(first, id)}
              actions={actions}
            >
              <p className="min-w-0 flex-1 text-control leading-5 text-ink">
                {column ? <span className="text-muted-ink">{column} · </span> : null}
                {c.note}
              </p>
            </EditableComment>
            <ul>
              {c.items.map((item) => (
                <li key={item.id}>
                  <button
                    type="button"
                    onClick={() => onOpen(item.id)}
                    className="block w-full cursor-pointer truncate rounded-inline text-left text-caption text-muted-ink outline-none hover:text-ink focus-visible:outline-2 focus-visible:outline-sage"
                    title={item.label}
                  >
                    {item.label}
                  </button>
                </li>
              ))}
            </ul>
          </li>
        )
      })}
    </ul>
  )
}

function focusInput(el: HTMLTextAreaElement | null) {
  el?.focus()
}

/** What a request runs with, as a quiet button beside Send: the agent's mark and name. It opens the "Extract with"
 * and effort selects, over where the run stops. */
function RunSettings({ choice: c }: { choice: RunChoice }) {
  const choice = useChoiceOf()(c.using)
  const effort = c.effort ? `${capital(c.effort)} effort` : "Default effort"
  const limit = limitLine(c)
  return (
    <Popover>
      <PopoverTrigger
        render={
          <Button
            variant="quiet"
            size="compact"
            className="max-w-40 shrink-0 gap-1.5 px-2 text-caption text-muted-ink"
            aria-label={`Runs with ${choice.title}, ${effort}`}
            data-testid="ask-settings"
          />
        }
      >
        {choice.brand ? <BrandIcon brand={choice.brand} className="size-3.5 shrink-0" /> : null}
        <span className="truncate">{choice.title}</span>
        <CaretDownIcon className="size-3 shrink-0 rotate-180" />
      </PopoverTrigger>
      <PopoverContent className="w-[360px] space-y-4">
        <ChoiceFields choice={c} testid="ask-model" />
        {limit ? <p className="text-caption text-muted-ink">{limit}</p> : null}
      </PopoverContent>
    </Popover>
  )
}

/** A request to SAGE in a sentence, which starts a new pass on the paper. The comments the scientist has left go with
 * it, and the agent starts from the records they are on; the count beside the input shows them all. */
export function AskSage({
  items,
  tabs,
  ask,
  actions,
  onOpen,
  className,
}: {
  /** Every item in the extraction; the ones with comments are what the request carries. */
  items: Item[]
  tabs: Tab[]
  ask: AskRun
  actions: CommentActions
  /** Open an item a comment is on. */
  onOpen: (id: string) => void
  className?: string
}) {
  const [text, setText] = useState("")
  const [showing, setShowing] = useState(false)
  const commented = items.filter((i) => i.comments.length > 0)
  const comments = said(commented)
  const disabled = ask.disabled ?? (ask.pending ? "Starting…" : null)
  const send = () => {
    if (disabled || !text.trim()) return
    ask.send({ text: text.trim(), items: commented })
    setText("")
    setShowing(false)
  }
  return (
    <form
      className={cn("rounded-card border border-line bg-surface p-1.5 shadow-float", className)}
      onSubmit={(e) => {
        e.preventDefault()
        send()
      }}
      data-testid="ask-sage"
    >
      {showing && comments.length ? (
        <div className="mb-1.5 border-b border-line px-1.5 pt-1">
          <CommentList
            comments={comments}
            tabs={tabs}
            actions={actions}
            onOpen={onOpen}
            onEdit={(item, id) => {
              setShowing(false)
              actions.openComment(item, id)
            }}
          />
        </div>
      ) : null}
      <Textarea
        ref={ask.focus ? focusInput : undefined}
        rows={1}
        value={text}
        disabled={disabled !== null}
        onFocus={() => ask.focus && ask.onFocused()}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault()
            send()
          }
          if (e.key === "Escape") (e.target as HTMLTextAreaElement).blur()
        }}
        placeholder={disabled ?? "Ask SAGE to add or fix something…"}
        aria-label="Ask SAGE"
        className="field-sizing-content max-h-32 min-h-control-sm w-full resize-none border-0 bg-transparent px-1.5 py-1.5 shadow-none focus-visible:shadow-none"
        data-testid="ask-input"
      />
      <div className="flex items-center gap-1">
        {comments.length ? (
          <Tip label="Comments sent with your request">
            <Button
              variant="quiet"
              size="compact"
              className="shrink-0 gap-1 px-2 text-caption text-muted-ink tabular-nums"
              aria-expanded={showing}
              aria-label={plural(comments.length, "comment")}
              onClick={() => setShowing((v) => !v)}
              data-testid="ask-comments-toggle"
            >
              <ChatCircleIcon className="size-3.5" />
              {comments.length}
              <CaretDownIcon
                className={cn("size-3 transition-transform duration-150", showing || "rotate-180")}
              />
            </Button>
          </Tip>
        ) : null}
        <span className="flex-1" />
        <RunSettings choice={ask.choice} />
        <Button
          type="submit"
          size="icon-sm"
          disabled={disabled !== null || !text.trim()}
          aria-label="Send to SAGE"
          data-testid="ask-send"
        >
          <ArrowUpIcon weight="bold" />
        </Button>
      </div>
    </form>
  )
}

type Kind = "reject" | "comment" | "ask"

/** What each shared action's form says: its prompt, its field's name and its button. */
const FORM: Record<Kind, { ask: (n: number) => string; label: string; confirm: string }> = {
  reject: { ask: (n) => `Why reject these ${n}?`, label: "Reason", confirm: "Reject" },
  comment: { ask: (n) => `Comment on these ${n}`, label: "Comment", confirm: "Comment" },
  ask: { ask: (n) => `What should SAGE fix in these ${n}?`, label: "Ask SAGE", confirm: "Send" },
}

/** What can be done to the rows picked in Data mode, all at once: one decision, one comment, or one request. */
export function SelectionBar({
  items,
  actions,
  askDisabled,
  onAsk,
  onClear,
}: {
  items: Item[]
  actions: ReviewActions
  askDisabled: string | null
  onAsk: (ask: Ask) => void
  onClear: () => void
}) {
  const [kind, setKind] = useState<Kind | null>(null)
  const submit = (text: string) => {
    if (kind === "reject") actions.reject(items, text)
    else if (kind === "comment") actions.comment(items, text, null)
    else onAsk({ text, items })
    setKind(null)
    onClear()
  }
  return (
    <div
      className="w-[min(640px,calc(100%-2*var(--spacing-gutter)))] space-y-2 rounded-card border border-line bg-surface p-2 shadow-float"
      data-testid="selection-bar"
    >
      {kind === null ? (
        <div className="flex items-center gap-2">
          <span className="px-2 text-label tabular-nums" data-testid="picked-count">
            {items.length} selected
          </span>
          <span className="flex-1" />
          <Button
            size="compact"
            disabled={actions.busy}
            onClick={() => {
              actions.acceptMany(items)
              onClear()
            }}
            data-testid="picked-accept"
          >
            Accept
          </Button>
          <Button
            size="compact"
            variant="outline"
            onClick={() => setKind("reject")}
            data-testid="picked-reject"
          >
            Reject
          </Button>
          <Button
            size="compact"
            variant="outline"
            onClick={() => setKind("comment")}
            data-testid="picked-comment"
          >
            Comment
          </Button>
          <Button
            size="compact"
            variant="outline"
            disabled={askDisabled !== null}
            onClick={() => setKind("ask")}
            data-testid="picked-ask"
          >
            Ask SAGE
          </Button>
          <Button variant="quiet" size="icon-sm" aria-label="Clear the selection" onClick={onClear}>
            <XIcon />
          </Button>
        </div>
      ) : (
        <PickedForm
          kind={kind}
          count={items.length}
          reasons={kind === "reject" ? actions.reasons : []}
          onSubmit={submit}
          onCancel={() => setKind(null)}
        />
      )}
    </div>
  )
}

/** The one line a shared rejection, comment or request takes, with the reasons used before offered for a rejection. */
function PickedForm({
  kind,
  count,
  reasons,
  onSubmit,
  onCancel,
}: {
  kind: Kind
  count: number
  reasons: string[]
  onSubmit: (text: string) => void
  onCancel: () => void
}) {
  const [text, setText] = useState("")
  const input = useRef<HTMLInputElement>(null)
  const copy = FORM[kind]
  return (
    <form
      className="space-y-2"
      onSubmit={(e) => {
        e.preventDefault()
        if (text.trim()) onSubmit(text.trim())
      }}
      onKeyDown={(e) => {
        if (e.key !== "Escape") return
        e.stopPropagation()
        onCancel()
      }}
      data-testid={`picked-${kind}-form`}
    >
      {reasons.length ? (
        <div className="flex flex-wrap gap-x-3 gap-y-1 px-1">
          {reasons.map((r) => (
            <Button
              key={r}
              variant="text"
              className="text-caption text-muted-ink hover:text-ink"
              onClick={() => {
                setText(r)
                input.current?.focus()
              }}
            >
              {r}
            </Button>
          ))}
        </div>
      ) : null}
      <div className="flex items-center gap-2">
        <Input
          ref={input}
          autoFocus
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={copy.ask(count)}
          aria-label={copy.label}
          data-testid="picked-input"
        />
        <Button size="compact" type="submit" disabled={!text.trim()} data-testid="picked-confirm">
          {copy.confirm}
        </Button>
        <Button size="compact" variant="ghost" type="button" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
