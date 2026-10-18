import { Accordion } from "@base-ui/react/accordion"
import { createContext, type ReactNode, use, useState } from "react"
import { Reviewed } from "@/components/paper/card-parts"
import type { ReviewActions } from "@/components/paper/margin"
import { ToCheck } from "@/components/status-mark"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Tip } from "@/components/ui/tooltip"
import { BASIS_LABEL, fieldTitle, friendlyValue, problemWords, STATUS_LABEL } from "@/lib/format"
import { type Box, editText, type FieldRow, firstProblemField, type Item } from "@/lib/items"
import { useKeys } from "@/lib/keys"
import type { Edits } from "@/lib/review"
import { cn } from "@/lib/utils"

export type CardActions = Pick<ReviewActions, "busy" | "accept" | "edit" | "reject">

/** Characters of the paper's text kept on each side of a quote. */
const AROUND = 90

type PanelValue = {
  state: {
    item: Item
    boxes: Record<string, Box>
    open: string[]
    draft: Edits
    /** The "Not in the paper" note being written, or null when its form is closed. */
    note: string | null
    changes: (readonly [string, string])[]
    reviewing: boolean
    /** The field Edit sent the cursor to. `n` grows on every Edit, so that field's input remounts and takes it. */
    cursor: { field: string; n: number } | null
  }
  actions: {
    setOpen: (open: string[]) => void
    setDraft: (name: string, text: string | undefined) => void
    setNote: (note: string | null) => void
    change: () => void
    discard: () => void
    save: () => void
    edit: () => void
    accept: () => void
    reject: () => void
    show: () => void
  }
  meta: { busy: boolean }
}

const PanelContext = createContext<PanelValue | null>(null)

function usePanel(): PanelValue {
  const value = use(PanelContext)
  if (!value) throw new Error("ItemFields and ItemFooter must sit inside ItemPanel")
  return value
}

type PanelProps = {
  item: Item
  boxes: Record<string, Box>
  actions: CardActions
  /** Scroll the paper to where the item came from. */
  onShow: () => void
  onKey?: () => void
  children: ReactNode
}

/** One item's fields and review buttons, shared by the Check margin and Data mode's sheet. Its open fields, drafts
 * and note start fresh for each item. */
export function ItemPanel(props: PanelProps) {
  return <PanelState key={props.item.id} {...props} />
}

function PanelState({ item, boxes, actions, onShow, onKey, children }: PanelProps) {
  const [open, setOpen] = useState(() => item.rows.filter((r) => r.status !== "verified").map((r) => r.name))
  const [draft, setDraft] = useState<Edits>({})
  const [note, setNote] = useState<string | null>(null)
  const [changing, setChanging] = useState(false)
  const [cursor, setCursor] = useState<{ field: string; n: number } | null>(null)
  const reviewing = item.review === null || changing
  const changes = item.rows.flatMap((r) => {
    const text = draft[r.name]?.trim()
    return text !== undefined && text !== "" && text !== editText(r) ? [[r.name, text] as const] : []
  })
  const save = () => {
    if (changes.length) actions.edit(item, Object.fromEntries(changes))
  }
  const edit = () => {
    const first = firstProblemField(item) ?? item.rows[0]?.name
    if (!first) return
    setOpen((o) => (o.includes(first) ? o : [...o, first]))
    setCursor((c) => ({ field: first, n: (c?.n ?? 0) + 1 }))
  }
  const key = (run: () => void) => (e: KeyboardEvent) => {
    e.preventDefault()
    onKey?.()
    run()
  }
  useKeys(
    {
      a: key(() => reviewing && actions.accept(item)),
      e: key(() => reviewing && edit()),
      r: key(() => reviewing && setNote("")),
      c: key(() => setChanging(true)),
    },
    !actions.busy && note === null,
  )
  const value: PanelValue = {
    state: { item, boxes, open, draft, note, changes, reviewing, cursor },
    actions: {
      setOpen,
      setDraft: (name, text) =>
        setDraft((d) => {
          const next = { ...d }
          if (text === undefined) delete next[name]
          else next[name] = text
          return next
        }),
      setNote,
      change: () => setChanging(true),
      discard: () => setDraft({}),
      save,
      edit,
      accept: () => actions.accept(item),
      reject: () => actions.reject(item, note ?? ""),
      show: onShow,
    },
    meta: { busy: actions.busy },
  }
  return <PanelContext value={value}>{children}</PanelContext>
}

function Quote({ quote, context, status }: { quote: string; context: string | undefined; status: string }) {
  const at = context ? context.toLowerCase().indexOf(quote.toLowerCase()) : -1
  const tint = status === "verified" ? "bg-good-tint" : "bg-check-tint"
  if (!context || at < 0) return <mark className={cn("rounded-mark px-0.5 text-ink", tint)}>{quote}</mark>
  const from = Math.max(at - AROUND, 0)
  const to = Math.min(at + quote.length + AROUND, context.length)
  return (
    <>
      {from > 0 ? "…" : null}
      {context.slice(from, at)}
      <mark className={cn("rounded-mark px-0.5 text-ink", tint)}>{context.slice(at, at + quote.length)}</mark>
      {context.slice(at + quote.length, to)}
      {to < context.length ? "…" : null}
    </>
  )
}

function focusAndSelect(input: HTMLInputElement | null) {
  input?.focus()
  input?.select()
}

const pageOf = (anchor: string): number | null => {
  const m = /^p(\d+)\./.exec(anchor)
  return m ? Number(m[1]) : null
}

function FieldBody({ row }: { row: FieldRow }) {
  const { state, actions } = usePanel()
  const draft = state.draft[row.name]
  const cursor = state.cursor?.field === row.name ? state.cursor : null
  const pages = [...new Set(row.quotes.map((q) => pageOf(q.anchor)).filter((p) => p !== null))]
  /** How the value was arrived at; a value the paper doesn't give has none to show. */
  const basis = row.value === null || !row.basis ? null : (BASIS_LABEL[row.basis] ?? row.basis)
  return (
    <div className="space-y-3 px-(--panel-x) pt-1 pb-5">
      <Input
        key={cursor?.n ?? 0}
        ref={cursor ? focusAndSelect : undefined}
        value={draft ?? editText(row)}
        placeholder="Not given"
        aria-label={fieldTitle(row.name)}
        className="tabular-nums"
        data-testid="sheet-input"
        onChange={(e) => actions.setDraft(row.name, e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault()
            actions.save()
          } else if (e.key === "Escape" && draft !== undefined) {
            e.stopPropagation()
            actions.setDraft(row.name, undefined)
          }
        }}
      />
      {row.status === "verified" ? null : (
        <p className="text-caption text-ink" data-testid="sheet-problem">
          {problemWords(row.cause, row.reason)}
        </p>
      )}
      {row.quotes.map((q) => (
        <p
          key={`${q.anchor}:${q.quote}`}
          className="border-l-2 border-line pl-3 text-caption leading-5 text-ink"
          data-testid="sheet-quote"
        >
          <Quote quote={q.quote} context={state.boxes[q.anchor]?.text} status={row.status} />
        </p>
      ))}
      {basis || pages.length ? (
        <div className="flex items-center gap-3 text-caption">
          {basis ? <span className="text-muted-ink">{basis}</span> : null}
          {pages.map((p) => (
            <Button
              key={p}
              variant="text"
              className="text-caption"
              onClick={actions.show}
              data-testid="sheet-page"
            >
              Page {p}
            </Button>
          ))}
        </div>
      ) : null}
    </div>
  )
}

/** The rows' side padding is the `--panel-x` the frame around the panel sets. */
export function ItemFields() {
  const { state, actions } = usePanel()
  return (
    <Accordion.Root multiple value={state.open} onValueChange={(v) => actions.setOpen(v.map(String))}>
      {state.item.rows.map((row) => (
        <Accordion.Item
          key={row.name}
          value={row.name}
          className="border-b border-line"
          data-testid="sheet-field"
          data-field={row.name}
          data-status={row.status}
        >
          <Accordion.Header>
            <Accordion.Trigger className="flex h-11 w-full cursor-pointer items-center gap-3 px-(--panel-x) text-left outline-none hover:bg-surface-2 focus-visible:bg-surface-2 focus-visible:shadow-cursor">
              <span className="w-[min(132px,40%)] shrink-0 truncate text-caption text-muted-ink">
                {fieldTitle(row.name)}
              </span>
              <span className="min-w-0 flex-1 truncate text-control tabular-nums">
                {row.value === null ? (
                  <span className="text-muted-ink">–</span>
                ) : row.status === "verified" ? (
                  friendlyValue(row.name, row.value)
                ) : (
                  <ToCheck>{friendlyValue(row.name, row.value)}</ToCheck>
                )}
              </span>
              <span className="sr-only">{STATUS_LABEL[row.status]}</span>
            </Accordion.Trigger>
          </Accordion.Header>
          <Accordion.Panel>
            <FieldBody row={row} />
          </Accordion.Panel>
        </Accordion.Item>
      ))}
    </Accordion.Root>
  )
}

function NoteForm() {
  const { state, actions, meta } = usePanel()
  return (
    <form
      className="space-y-2"
      onSubmit={(e) => {
        e.preventDefault()
        actions.reject()
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape") actions.setNote(null)
      }}
    >
      <Input
        autoFocus
        value={state.note ?? ""}
        onChange={(e) => actions.setNote(e.target.value)}
        placeholder="Note (optional)"
        aria-label="Note"
        data-testid="sheet-note"
      />
      <div className="flex gap-2">
        <Button size="compact" type="submit" disabled={meta.busy} data-testid="sheet-reject-confirm">
          Not in the paper
        </Button>
        <Button size="compact" variant="ghost" type="button" onClick={() => actions.setNote(null)}>
          Cancel
        </Button>
      </div>
    </form>
  )
}

export function ItemFooter() {
  const { state, actions, meta } = usePanel()
  if (state.note !== null) return <NoteForm />
  if (state.changes.length)
    return (
      <div className="flex items-center gap-2">
        <Button size="compact" disabled={meta.busy} onClick={actions.save} data-testid="sheet-save">
          Save
        </Button>
        <Button size="compact" variant="ghost" onClick={actions.discard} data-testid="sheet-discard">
          Cancel
        </Button>
      </div>
    )
  if (state.item.review && !state.reviewing)
    return <Reviewed review={state.item.review} onChange={actions.change} />
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Tip label="A">
        <Button size="compact" disabled={meta.busy} onClick={actions.accept} data-testid="sheet-accept">
          Accept
        </Button>
      </Tip>
      <Tip label="E">
        <Button
          size="compact"
          variant="outline"
          disabled={meta.busy}
          onClick={actions.edit}
          data-testid="sheet-edit"
        >
          Edit
        </Button>
      </Tip>
      <Tip label="R">
        <Button
          size="compact"
          variant="quiet"
          disabled={meta.busy}
          onClick={() => actions.setNote("")}
          data-testid="sheet-reject"
        >
          Not in the paper
        </Button>
      </Tip>
    </div>
  )
}
