import { ChatCircleIcon, FileTextIcon, PencilSimpleIcon, XIcon } from "@phosphor-icons/react"
import { createContext, Fragment, type ReactNode, use, useId, useRef, useState } from "react"
import { Reviewed } from "@/components/paper/card-parts"
import { type CommentActions, EditableComment } from "@/components/paper/comment"
import type { ReviewActions } from "@/components/paper/margin"
import { Quote } from "@/components/paper/source-excerpt"
import { ToCheck } from "@/components/status-mark"
import { Button } from "@/components/ui/button"
import { DropdownMenuItem, MoreMenu } from "@/components/ui/dropdown-menu"
import { Fold } from "@/components/ui/fold"
import { Input } from "@/components/ui/input"
import { Tip } from "@/components/ui/tooltip"
import {
  BASIS_LABEL,
  fieldTitle,
  friendlyValue,
  problemHelp,
  problemWords,
  reasonText,
  STATUS_LABEL,
} from "@/lib/format"
import { type Box, editText, type FieldRow, type Item, leadQuote } from "@/lib/items"
import { useKeys } from "@/lib/keys"
import type { Edits } from "@/lib/review"
import { cn } from "@/lib/utils"
import { type Fact, type Placement, typeName } from "@/lib/workbook"

export type CardActions = Pick<
  ReviewActions,
  | "busy"
  | "accept"
  | "edit"
  | "reject"
  | "comment"
  | "editComment"
  | "removeComment"
  | "commentOpen"
  | "openComment"
  | "closeComment"
  | "reasons"
>

/** A fact the panel shows, with the field behind it when the record has one to show. */
type Line = Fact & { row: FieldRow | null }

/** What the footer is asking for: a reason to reject, or a comment on the item or one of its fields. */
type Form = { kind: "reject"; text: string } | { kind: "comment"; text: string; field: string | null } | null

type PanelValue = {
  state: {
    item: Item
    placement: Placement
    boxes: Record<string, Box>
    /** The quote the card shows above the fields: the open field's, else the record's lead. */
    shown: { row: FieldRow; quote: FieldRow["quotes"][number] } | null
    lines: Line[]
    /** Fields that look fine and aren't in the workbook's reading order. */
    rest: Line[]
    /** The field shown open under the grid, with its evidence and editor. */
    field: string | null
    draft: Edits
    form: Form
    changes: (readonly [string, string])[]
    reviewing: boolean
    /** The field Edit sent the cursor to. `n` grows on every Edit, so that field's input remounts and takes it. */
    cursor: { field: string; n: number } | null
  }
  actions: {
    pick: (field: string | null) => void
    setDraft: (name: string, text: string | undefined) => void
    setForm: (form: Form) => void
    change: () => void
    discard: () => void
    save: () => void
    edit: () => void
    accept: () => void
    submit: () => void
    show: () => void
    comments: CommentActions
  }
  meta: { busy: boolean; reasons: string[] }
}

const PanelContext = createContext<PanelValue | null>(null)

function usePanel(): PanelValue {
  const value = use(PanelContext)
  if (!value) throw new Error("ItemFields and ItemFooter must sit inside ItemPanel")
  return value
}

type PanelProps = {
  item: Item
  /** Where the item goes in the export: its tab, its row's columns, and the column each field fills. */
  placement: Placement
  /** The columns to show up front, when the rest are shown elsewhere, as a table row shows what its cells share.
   * Anything that needs a look stays up front too. */
  own?: string[]
  /** Whether the card shows the paper's words above the fields, as `ItemExcerpt`. */
  excerpt?: boolean
  boxes: Record<string, Box>
  actions: CardActions
  /** Scroll the paper to where the item came from. */
  onShow: () => void
  onKey?: () => void
  children: ReactNode
}

/** A field as a line, named by the export column it fills, or by its own title when the export has none for it. */
const fromRow =
  (titles: Record<string, string>) =>
  (r: FieldRow): Line => ({
    key: r.name,
    label: titles[r.name] ?? fieldTitle(r.name),
    text: r.value === null ? "–" : friendlyValue(r.name, r.value),
    field: r.name,
    status: r.status,
    row: r,
  })

/** The workbook's facts first, each with its field; then any other field that needs a look; the rest wait behind
 * "Other fields". A record with no workbook row shows its fields. */
function linesOf(item: Item, { facts, titles }: Placement, own?: string[]): { lines: Line[]; rest: Line[] } {
  const line = fromRow(titles)
  if (facts.length === 0) return { lines: item.rows.map(line), rest: [] }
  const byName = new Map(item.rows.map((r) => [r.name, r]))
  const covered = new Set(facts.map((f) => f.field))
  const others = item.rows.filter((r) => !covered.has(r.name))
  const all = facts.map((f) => ({ ...f, row: (f.field && byName.get(f.field)) || null }))
  const front = (l: Line) => own === undefined || own.includes(l.key) || l.status !== "verified"
  return {
    lines: [...all.filter(front), ...others.filter((r) => r.status !== "verified").map(line)],
    rest: [...all.filter((l) => !front(l)), ...others.filter((r) => r.status === "verified").map(line)],
  }
}

/** One item's facts, comments and review buttons, shared by the Check margin and Data mode's sheet. Its open
 * fields, drafts and forms start fresh for each item. */
export function ItemPanel(props: PanelProps) {
  return <PanelState key={props.item.id} {...props} />
}

function PanelState({
  item,
  placement,
  own,
  excerpt = false,
  boxes,
  actions,
  onShow,
  onKey,
  children,
}: PanelProps) {
  const { lines, rest } = linesOf(item, placement, own)
  // The first field that needs a look opens on its own, with what is wrong and the words it rests on, until the
  // reviewer picks another or closes it.
  const [picked, pick] = useState<string | null | undefined>(undefined)
  const field =
    picked !== undefined
      ? picked
      : item.review === null
        ? (lines.find((l) => l.status !== "verified")?.key ?? null)
        : null
  const open = [...lines, ...rest].find((l) => l.key === field)?.row
  const shown = !excerpt ? null : open?.quotes[0] ? { row: open, quote: open.quotes[0] } : leadQuote(item)
  const [draft, setDraft] = useState<Edits>({})
  const [form, setForm] = useState<Form>(null)
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
    const editable = [...lines, ...rest].filter((l) => l.row !== null)
    const first = editable.find((l) => l.status !== "verified") ?? editable[0]
    if (!first?.row) return
    pick(first.key)
    setCursor((c) => ({ field: first.row?.name ?? "", n: (c?.n ?? 0) + 1 }))
  }
  const submit = () => {
    const text = form?.text.trim() ?? ""
    if (!form || !text) return
    if (form.kind === "reject") actions.reject([item], text)
    else actions.comment([item], text, form.field)
    setForm(null)
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
      r: key(() => reviewing && setForm({ kind: "reject", text: "" })),
      c: key(() => setChanging(true)),
      m: key(() => setForm({ kind: "comment", text: "", field: null })),
    },
    !actions.busy && form === null,
  )
  const value: PanelValue = {
    state: { item, placement, boxes, shown, lines, rest, field, draft, form, changes, reviewing, cursor },
    actions: {
      pick,
      setDraft: (name, text) =>
        setDraft((d) => {
          const next = { ...d }
          if (text === undefined) delete next[name]
          else next[name] = text
          return next
        }),
      setForm,
      change: () => setChanging(true),
      discard: () => setDraft({}),
      save,
      edit,
      accept: () => actions.accept(item),
      submit,
      show: onShow,
      comments: actions,
    },
    meta: { busy: actions.busy, reasons: actions.reasons },
  }
  return <PanelContext value={value}>{children}</PanelContext>
}

function focusAndSelect(input: HTMLInputElement | null) {
  input?.focus()
  input?.select()
}

const pageOf = (anchor: string): number | null => {
  const m = /^p(\d+)\./.exec(anchor)
  return m ? Number(m[1]) : null
}

function Problem({ row }: { row: FieldRow }) {
  const problem = problemWords(row.cause, row.reason)
  const why = reasonText(row.reason)
  const help = problemHelp(row.cause, row.reason)
  if (!problem && !why) return null
  return (
    <div className="space-y-0.5" data-testid="sheet-problem">
      {problem ? <p className="text-label text-ink">{problem}</p> : null}
      {why ? <p className="text-caption leading-5 text-ink">{why}</p> : null}
      {help ? <p className="text-caption leading-5 text-muted-ink">{help}</p> : null}
    </div>
  )
}

function FieldInput({ line, row }: { line: Line; row: FieldRow }) {
  const { state, actions } = usePanel()
  const id = useId()
  const draft = state.draft[row.name]
  const cursor = state.cursor?.field === row.name ? state.cursor : null
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-caption text-muted-ink">
        {line.label}
      </label>
      <Input
        id={id}
        key={cursor?.n ?? 0}
        ref={cursor ? focusAndSelect : undefined}
        value={draft ?? editText(row)}
        placeholder={row.cause === "absent" ? "Not in the paper" : "Empty"}
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
    </div>
  )
}

/** A fact opened: its field's value to edit, what is wrong, the quotes it rests on, and a comment on it. */
function LineBody({ line }: { line: Line }) {
  const { state, actions } = usePanel()
  const row = line.row
  const comment = () => actions.setForm({ kind: "comment", text: "", field: line.field })
  if (!row)
    return (
      <div className="px-(--panel-x) pt-1 pb-4">
        <Button variant="text" className="text-caption" onClick={comment} data-testid="fact-comment">
          Comment
        </Button>
      </div>
    )
  const pages = [...new Set(row.quotes.map((q) => pageOf(q.anchor)).filter((p) => p !== null))]
  const look = row.status !== "verified"
  const basis = look || row.value === null || !row.basis ? null : BASIS_LABEL[row.basis]
  // The excerpt above already shows the first.
  const quotes = row.quotes.filter((q) => q !== state.shown?.quote)
  return (
    <div className="space-y-3 border-y border-line bg-surface-2/40 px-(--panel-x) py-3">
      {look ? <Problem row={row} /> : null}
      <FieldInput line={line} row={row} />
      {quotes.map((q) => (
        <p
          key={`${q.anchor}:${q.quote}`}
          className="border-l-2 border-line pl-3 text-caption leading-5 text-ink"
          data-testid="sheet-quote"
        >
          <Quote quote={q.quote} context={state.boxes[q.anchor]?.text} status={row.status} />
        </p>
      ))}
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
        <span className="flex-1" />
        <Button variant="text" className="text-caption" onClick={comment} data-testid="fact-comment">
          Comment
        </Button>
      </div>
    </div>
  )
}

/** One field of the proposed record: its export column over its value, amber while it needs a look. Clicking it
 * opens its evidence under its row of the grid. */
function Cell({ line }: { line: Line }) {
  const { state, actions } = usePanel()
  const look = line.status !== "verified" && state.item.review === null
  const on = state.field === line.key
  return (
    <button
      type="button"
      onClick={() => actions.pick(on ? null : line.key)}
      aria-expanded={on}
      className={cn(
        "-mx-2 min-w-0 cursor-pointer self-start rounded-control px-2 py-1.5 text-left outline-none hover:bg-surface-2 focus-visible:shadow-cursor",
        on && "bg-surface-2",
      )}
      data-testid="sheet-field"
      data-field={line.field ?? line.key}
      data-status={line.status}
      data-open={on || undefined}
    >
      <span className="block text-caption break-all text-muted-ink">{line.label}</span>
      <span className="block text-control break-words tabular-nums">
        {look ? <ToCheck>{line.text}</ToCheck> : line.text}
      </span>
      <span className="sr-only">{STATUS_LABEL[line.status]}</span>
    </button>
  )
}

/** The proposed record as key:value pairs, two to a row, with the open field's evidence under its row. */
function Grid({ lines }: { lines: Line[] }) {
  const { state } = usePanel()
  const pairs = Array.from({ length: Math.ceil(lines.length / 2) }, (_, i) => lines.slice(i * 2, i * 2 + 2))
  return (
    <div className="grid grid-cols-2 gap-x-4 px-(--panel-x)" data-testid="record-grid">
      {pairs.map((pair) => {
        const open = pair.find((l) => l.key === state.field)
        return (
          <Fragment key={pair.map((l) => l.key).join(":")}>
            {pair.map((l) => (
              <Cell key={l.key} line={l} />
            ))}
            {pair.length === 1 ? <span /> : null}
            {open ? (
              <div className="col-span-2 -mx-(--panel-x)" data-testid="field-detail">
                <LineBody line={open} />
              </div>
            ) : null}
          </Fragment>
        )
      })}
    </div>
  )
}

/** What the item is, and the export tab it goes to only when the tab is named for something else: "Species →
 * crops", "Study → README". "Management event" goes to managements, which says nothing new. */
function ItemPlace() {
  const { state } = usePanel()
  const name = typeName(state.item.type)
  const tab = state.placement.tab ?? "README"
  const same = tab.toLowerCase().startsWith(name.toLowerCase().split(" ")[0] ?? "")
  return (
    <p className="truncate text-caption text-muted-ink" data-testid="item-place">
      {name}
      {same ? null : <> → {tab}</>}
    </p>
  )
}

function ItemAsk() {
  const { state } = usePanel()
  if (state.item.review !== null) return null
  const open = [...state.lines, ...state.rest].filter((l) => l.status !== "verified")
  if (open.length === 0)
    return (
      <p className="text-caption text-muted-ink" data-testid="item-ask" data-status="verified">
        SAGE matched every value to the paper
      </p>
    )
  return (
    <p className="text-caption leading-5 text-ink" data-testid="item-ask" data-status="check">
      <span className="text-muted-ink">Check </span>
      {open.map((l, i) => (
        <Fragment key={l.key}>
          {i ? ", " : null}
          <ToCheck className="px-0.5">{l.label}</ToCheck>
        </Fragment>
      ))}
    </p>
  )
}

export function ItemTitle({ children }: { children?: ReactNode }) {
  const { state } = usePanel()
  return (
    <div className="space-y-1" data-testid="item-title">
      <ItemPlace />
      <div className="flex items-start gap-3">
        <p className="min-w-0 flex-1 text-value break-words">{state.item.label}</p>
        {children}
      </div>
      <ItemAsk />
    </div>
  )
}

/** The paper's words the open field rests on, or the record's when none is open, in the sentence around them. */
export function ItemExcerpt() {
  const { state } = usePanel()
  const shown = state.shown
  if (!shown) return null
  return (
    <p
      className="rounded-control border border-line bg-paper-canvas p-3 text-control leading-5 text-ink"
      data-testid="source-excerpt"
    >
      <Quote
        quote={shown.quote.quote}
        context={state.boxes[shown.quote.anchor]?.text}
        status={shown.row.status}
      />
    </p>
  )
}

/** The proposed record, its other fields folded away, then its comments. The side padding is the `--panel-x` the
 * frame around the panel sets. */
export function ItemFields() {
  const { state } = usePanel()
  return (
    <>
      <div className="pt-2" />
      <Grid lines={state.lines} />
      {state.rest.length ? (
        <div className="px-(--panel-x) py-2.5">
          <Fold label={`${state.rest.length} more`} className="-mx-(--panel-x) pt-1.5">
            <Grid lines={state.rest} />
          </Fold>
        </div>
      ) : null}
      <Comments />
    </>
  )
}

type Comment = Item["comments"][number]

function Comments() {
  const { state } = usePanel()
  const said = state.item.comments
  if (said.length === 0) return null
  return (
    <ul className="space-y-1.5 border-t border-line px-(--panel-x) py-3" data-testid="comments">
      {said.map((c) => (
        <CommentLine key={c.id ?? `${c.ts}:${c.note}`} comment={c} />
      ))}
    </ul>
  )
}

function CommentLine({ comment: c }: { comment: Comment }) {
  const { state, actions } = usePanel()
  const label = c.field ? (state.lines.find((l) => l.field === c.field)?.label ?? fieldTitle(c.field)) : null
  return (
    <li data-testid="comment">
      <EditableComment
        id={c.id}
        note={c.note ?? ""}
        editing={c.id === actions.comments.commentOpen}
        onEdit={(id) => actions.comments.openComment(state.item, id)}
        actions={actions.comments}
        className="text-caption leading-5"
      >
        <ChatCircleIcon className="mt-0.5 size-3.5 shrink-0 text-muted-ink" aria-hidden />
        <p className="min-w-0 flex-1 text-ink">
          {label ? <span className="text-muted-ink">{label} · </span> : null}
          {c.note}
        </p>
      </EditableComment>
    </li>
  )
}

/** Reject's reason, or a comment. A reason used before is one click away. */
function NoteForm() {
  const { state, actions, meta } = usePanel()
  const input = useRef<HTMLInputElement>(null)
  const form = state.form
  if (!form) return null
  const reject = form.kind === "reject"
  const on = form.kind === "comment" && form.field ? state.lines.find((l) => l.field === form.field) : null
  const set = (text: string) => actions.setForm({ ...form, text })
  return (
    <form
      className="space-y-2"
      onSubmit={(e) => {
        e.preventDefault()
        actions.submit()
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation()
          actions.setForm(null)
        }
      }}
      data-testid={reject ? "reject-form" : "comment-form"}
    >
      {on ? (
        <p className="flex items-center gap-1 text-caption text-muted-ink">
          On {on.label}
          <Button
            variant="quiet"
            size="icon-sm"
            className="size-5"
            aria-label="Comment on the whole item"
            onClick={() => actions.setForm({ kind: "comment", text: form.text, field: null })}
          >
            <XIcon className="size-3" />
          </Button>
        </p>
      ) : null}
      <Input
        ref={input}
        autoFocus
        value={form.text}
        onChange={(e) => set(e.target.value)}
        placeholder={reject ? "Why?" : "Comment"}
        aria-label={reject ? "Reason" : "Comment"}
        data-testid={reject ? "sheet-note" : "comment-input"}
      />
      {reject && meta.reasons.length ? (
        <div className="flex flex-col items-start gap-1" data-testid="reasons">
          {meta.reasons
            .filter((r) => r !== form.text)
            .map((r) => (
              <Button
                key={r}
                variant="text"
                className="max-w-full truncate text-caption text-muted-ink hover:text-ink"
                // A reason fills the field and hands the keyboard back to it, so Enter rejects.
                onClick={() => {
                  set(r)
                  input.current?.focus()
                }}
              >
                {r}
              </Button>
            ))}
        </div>
      ) : null}
      <div className="flex gap-2">
        <Button
          size="compact"
          type="submit"
          disabled={meta.busy || form.text.trim() === ""}
          data-testid={reject ? "sheet-reject-confirm" : "comment-save"}
        >
          {reject ? "Reject" : "Comment"}
        </Button>
        <Button size="compact" variant="ghost" type="button" onClick={() => actions.setForm(null)}>
          Cancel
        </Button>
      </div>
    </form>
  )
}

function CommentButton() {
  const { actions, meta } = usePanel()
  return (
    <Tip label="M">
      <Button
        variant="quiet"
        size="icon-sm"
        disabled={meta.busy}
        aria-label="Comment"
        onClick={() => actions.setForm({ kind: "comment", text: "", field: null })}
        data-testid="sheet-comment"
      >
        <ChatCircleIcon />
      </Button>
    </Tip>
  )
}

/** The record field of the open line, which a comment goes on. A line is keyed by its export column, not its field. */
const openField = (state: PanelValue["state"]) =>
  [...state.lines, ...state.rest].find((l) => l.key === state.field)?.field ?? null

export function ItemFooter() {
  const { state, actions, meta } = usePanel()
  if (state.form !== null) return <NoteForm />
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
    return (
      <div className="flex items-center gap-2">
        <div className="min-w-0 flex-1">
          <Reviewed review={state.item.review} onChange={actions.change} />
        </div>
        <CommentButton />
      </div>
    )
  return (
    <div className="space-y-2">
      <p className="text-caption text-muted-ink" data-testid="decision-hint">
        Accept keeps it in the export as shown. Reject leaves it out.
      </p>
      <div className="flex items-center gap-2">
        <Tip label="Keep it in the export · A">
          <Button size="compact" disabled={meta.busy} onClick={actions.accept} data-testid="sheet-accept">
            Accept
          </Button>
        </Tip>
        <Tip label="Leave it out of the export · R">
          <Button
            size="compact"
            variant="outline"
            disabled={meta.busy}
            onClick={() => actions.setForm({ kind: "reject", text: "" })}
            data-testid="sheet-reject"
          >
            Reject
          </Button>
        </Tip>
        <Tip label="M">
          <Button
            size="compact"
            variant="quiet"
            disabled={meta.busy}
            onClick={() => actions.setForm({ kind: "comment", text: "", field: openField(state) })}
            data-testid="sheet-comment"
          >
            Comment
          </Button>
        </Tip>
        <span className="flex-1" />
        <MoreMenu testid="sheet-more">
          <DropdownMenuItem onClick={actions.edit} data-testid="sheet-edit">
            <PencilSimpleIcon />
            Edit
            <span className="ml-auto text-caption text-muted-ink">E</span>
          </DropdownMenuItem>
          <DropdownMenuItem onClick={actions.show} data-testid="sheet-source">
            <FileTextIcon />
            Show on the page
          </DropdownMenuItem>
        </MoreMenu>
      </div>
    </div>
  )
}
