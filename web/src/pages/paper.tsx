import { CaretDownIcon, ClockCounterClockwiseIcon, DownloadSimpleIcon } from "@phosphor-icons/react"
import { Link, useNavigate } from "@tanstack/react-router"
import { useState } from "react"
import { errorMessage } from "@/api/client"
import { type AskRun, AskSage } from "@/components/paper/ask"
import { PaperCanvas } from "@/components/paper/canvas"
import { DataMode, DataModeSkeleton } from "@/components/paper/data-mode"
import { EndedCard, ExtractDialog, NewCard, StopDialog } from "@/components/paper/extract"
import { HistoryMenu } from "@/components/paper/history"
import type { Filter, ReviewActions } from "@/components/paper/margin"
import { DetailsDrawer, RunDock } from "@/components/paper/run-details"
import { StatusLine } from "@/components/paper/status-line"
import { Shortcuts } from "@/components/shortcuts"
import { TopBar } from "@/components/top-bar"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLinkItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Segmented } from "@/components/ui/segmented"
import { runUsing } from "@/lib/agents"
import { useDismissed } from "@/lib/dismissed"
import { carryOnLabel, endedAlert, type RunInfo, startsOver } from "@/lib/format"
import { type Item, nextOpen } from "@/lib/items"
import { type KeyHint, useKeyHint } from "@/lib/key-hint"
import { useKeys } from "@/lib/keys"
import { useMedia, wide } from "@/lib/media"
import { type PaperState, useCarryOn, useContinueRun } from "@/lib/paper-state"
import { usePaperView, useRunFinished } from "@/lib/paper-view"
import { reasonsOf, useReview } from "@/lib/review"
import { useRunChoice } from "@/lib/run-choice"
import { buildUnits, type Direction, gridMove, type Unit, visibleUnits } from "@/lib/units"
import { TAB_OF, useWorkbook, type Workbook } from "@/lib/workbook"
import { type PaperSearch, paperRoute } from "@/router"

type Mode = "check" | "data" | "read"
type View = ReturnType<typeof usePaperView>

function useSetSearch() {
  const navigate = useNavigate()
  return (patch: Partial<PaperSearch>) =>
    void navigate({ to: ".", search: (s: PaperSearch) => ({ ...s, ...patch }), replace: true })
}

/** The review actions, each of which moves on at once to the next item in `order` that still needs a look. The
 * review shows before the server answers; a failed save toasts and comes back as open. */
function useActions(
  view: View,
  paperId: string,
  order: Item[],
  activate: (id: string | null) => void,
  comment: string | undefined,
  setSearch: (patch: Partial<PaperSearch>) => void,
): ReviewActions {
  const review = useReview(view.viewId ?? "", paperId)
  const advance = (from: Item, done: Item[] = [from]) =>
    activate(nextOpen(order, from.id, new Set(done.map((i) => i.id)))?.id ?? null)
  return {
    busy: review.busy,
    accept: (item) => {
      review.accept(item)
      advance(item)
    },
    edit: (item, edits) => {
      if (Object.keys(edits).length === 0) return
      review.edit(item, edits)
      advance(item)
    },
    reject: (items, note) => {
      const last = items.at(-1)
      if (!last) return
      review.reject(items, note)
      advance(last, items)
    },
    comment: review.comment,
    editComment: review.editComment,
    removeComment: review.removeComment,
    commentOpen: comment ?? null,
    openComment: (item, id) => setSearch({ item: item.id, comment: id }),
    closeComment: () => setSearch({ comment: undefined }),
    acceptMany: (items) => {
      const last = items.at(-1)
      if (!last) return
      review.acceptMany(items)
      advance(last, items)
    },
    reasons: reasonsOf(view.items),
  }
}

function useCheckKeys(
  units: Unit[],
  order: Item[],
  active: Item | null,
  enabled: boolean,
  on: {
    activate: (id: string | null) => void
    actions: ReviewActions
    filter: () => void
    used: () => void
  },
) {
  const at = active ? order.indexOf(active) : -1
  const unit = active ? units.find((u) => u.items.includes(active)) : undefined
  const key = (run: () => void) => (e: KeyboardEvent) => {
    e.preventDefault()
    on.used()
    run()
  }
  const move = (dir: Direction) =>
    key(() => {
      const next = unit?.table && active ? gridMove(unit, active.id, dir) : null
      if (next) on.activate(next)
    })
  useKeys(
    {
      j: key(() => on.activate(order[Math.min(at + 1, order.length - 1)]?.id ?? null)),
      k: key(() => on.activate(order[Math.max(at - 1, 0)]?.id ?? null)),
      "Shift+A": key(() => unit && on.actions.acceptMany(unit.all.filter((i) => i.review === null))),
      "/": key(on.filter),
      ArrowUp: move("up"),
      ArrowDown: move("down"),
      ArrowLeft: move("left"),
      ArrowRight: move("right"),
    },
    enabled,
  )
}

function useSelection(view: View, search: PaperSearch) {
  const hasData = view.items.length > 0
  const itemsComing = view.loading && view.viewId !== null
  const mode: Mode = search.mode ?? (hasData || itemsComing ? "check" : "read")
  const filter: Filter = search.filter ?? "check"
  const units = buildUnits(view.items, view.doc, view.boxes)
  const picked = view.items.find((i) => i.id === search.item) ?? null
  const visible = visibleUnits(units, filter, picked?.id ?? null)
  const order = visible.flatMap((u) => u.items)
  const active = mode === "check" ? (picked ?? nextOpen(order, null)) : picked
  // Where a review moves on from: the margin in Check mode, the open item's tab in Data mode.
  const tabOf = picked ? TAB_OF[picked.type] : undefined
  const next = mode === "data" ? view.items.filter((i) => TAB_OF[i.type] === tabOf) : order
  const allColumns = search.cols === "all"
  return {
    hasData,
    itemsComing,
    mode,
    filter,
    units,
    picked,
    visible,
    order,
    next,
    active,
    tab: search.tab,
    allColumns,
  }
}
type Selection = ReturnType<typeof useSelection>

/** Ask SAGE: a request about some records starts a new pass on the run in view, with what the composer shows it runs
 * with. `ask=1` puts the cursor in it. */
function useAsk(view: View, focus: boolean, setSearch: (patch: Partial<PaperSearch>) => void): AskRun {
  const shown = view.runs.find((r) => r.id === view.viewId) ?? null
  const choice = useRunChoice(shown ? runUsing(shown) : null, shown?.effort)
  const proceed = useContinueRun()
  const disabled =
    view.state?.kind === "live"
      ? "SAGE is extracting this paper…"
      : !shown
        ? "No extraction yet"
        : choice.blocker.blocked
          ? (choice.blocker.line ?? "Set up on the Manage page")
          : null
  return {
    disabled,
    choice,
    pending: proceed.isPending,
    focus,
    onFocused: () => setSearch({ ask: undefined }),
    send: (a) => {
      if (!shown || disabled) return
      proceed.mutate({
        params: { path: { run_id: shown.id } },
        body: {
          guidance: a.text,
          using: choice.using,
          effort: choice.effortAsked,
          records: a.items.length ? a.items.map((i) => i.id) : null,
        },
      })
    },
  }
}

export function PaperPage() {
  const { paperId } = paperRoute.useParams()
  const search = paperRoute.useSearch()
  const setSearch = useSetSearch()
  const view = usePaperView(paperId, search)
  const sel = useSelection(view, search)
  const [shortcuts, setShortcuts] = useState(false)
  const hint = useKeyHint()
  // Data mode keeps its tab as the item changes, so a review that leaves nothing open there stays on that table.
  const activate = (id: string | null) =>
    setSearch(
      sel.mode === "data"
        ? {
            item: id ?? undefined,
            tab: sel.picked ? TAB_OF[sel.picked.type] : search.tab,
            comment: undefined,
          }
        : { item: id ?? undefined, comment: undefined },
    )
  const actions = useActions(view, paperId, sel.next, activate, search.comment, setSearch)
  const workbook = useWorkbook(sel.hasData ? view.viewId : null)
  const ask = useAsk(view, search.ask === 1, setSearch)
  const carryOn = useCarryOn({
    startOver: () => setSearch({ extract: 1, details: undefined }),
    guide: () => setSearch({ ask: 1, mode: sel.mode === "data" ? "data" : "check", details: undefined }),
  })
  useRunFinished(view.latest, (run) => setSearch(afterRun(run)), carryOn)
  useCheckKeys(sel.units, sel.order, sel.active, sel.mode === "check" && !actions.busy, {
    activate,
    actions,
    filter: () => setSearch({ filter: sel.filter === "all" ? undefined : "all", item: undefined }),
    used: hint.used,
  })
  useKeys({ "Shift+?": () => setShortcuts(true), "?": () => setShortcuts(true) })

  const info = view.summary.data?.paper
  const title = info ? (info.title ?? info.filename) : undefined
  const pdf = `/api/papers/${encodeURIComponent(paperId)}/pdf`
  const roomy = useMedia(wide)
  const dock = dockedRun(view.state, sel.mode, roomy)
  // The PDF mounts only for a paper that exists: pdf.js throws its own 404 past the screen otherwise.
  if (!view.summary.data)
    return view.summary.error ? <NotFound message={errorMessage(view.summary.error)} /> : <NotFound />
  if (view.missingRun) return <NotFound message={`This paper has no extraction ${view.missingRun}`} />
  return (
    <div className="flex h-svh flex-col" data-testid="paper">
      <TopBar title={title} crumb={<Crumb title={title ?? ""} />}>
        {sel.hasData || sel.itemsComing ? (
          <>
            <ModeSwitch mode={sel.mode} onMode={(m) => setSearch({ mode: m })} />
            <span aria-hidden="true" className="mx-1 h-5 w-px bg-line" />
          </>
        ) : null}
        <RunsMenu
          view={view}
          docked={dock !== null}
          onSearch={setSearch}
          onStop={() => setSearch({ stop: 1 })}
          onContinue={carryOn}
        />
        <ExportMenu runId={sel.hasData ? view.viewId : null} pdf={{ href: pdf, filename: info?.filename }} />
      </TopBar>
      <StatusLine
        state={view.state}
        older={view.older}
        standing={view.dataRun}
        onDetails={dock ? null : () => setSearch({ details: 1 })}
        onRetry={carryOn}
        onLatest={() => setSearch({ run: undefined, item: undefined })}
      />
      <PaperBody
        view={view}
        sel={sel}
        paperId={paperId}
        pdf={pdf}
        actions={actions}
        workbook={workbook}
        ask={ask}
        hint={hint}
        dock={dock}
        zoom={search.zoom ?? 1}
        onActivate={activate}
        onSearch={setSearch}
        onRetry={carryOn}
      />
      <DetailsDrawer
        run={view.drawerRun}
        open={search.details === 1 && !dock}
        onClose={() => setSearch({ details: undefined })}
        onContinue={carryOn}
        onStop={() => setSearch({ stop: 1 })}
      />
      <RunDialogs view={view} search={search} paperId={paperId} onSearch={setSearch} />
      <Shortcuts open={shortcuts} onClose={() => setShortcuts(false)} />
    </div>
  )
}

/** A live run docks beside the paper in Read mode when the window has room for both. */
function dockedRun(state: PaperState | null, mode: Mode, roomy: boolean): RunInfo | null {
  return state?.kind === "live" && mode === "read" && roomy ? state.run : null
}

/** A run that found data shows it. A Stop dialog the run outlived closes, or the next run would open it again. */
function afterRun(r: RunInfo): Partial<PaperSearch> {
  return r.counts?.records
    ? { mode: undefined, run: undefined, item: undefined, stop: undefined }
    : { stop: undefined }
}

function RunDialogs({
  view,
  search,
  paperId,
  onSearch,
}: {
  view: View
  search: PaperSearch
  paperId: string
  onSearch: (patch: Partial<PaperSearch>) => void
}) {
  const live = view.state?.kind === "live" ? view.state.run : null
  return (
    <>
      <ExtractDialog
        open={search.extract === 1 && live === null}
        title="Extract data"
        paperIds={[paperId]}
        testid="extract"
        startTestid="start-extraction"
        onClose={() => onSearch({ extract: undefined })}
      />
      <StopDialog
        body={search.stop === 1 && live ? { run_ids: [live.id] } : null}
        title="Stop this extraction?"
        confirm="Stop"
        testid="stop"
        onClose={() => onSearch({ stop: undefined })}
      >
        It keeps what it has found so far, and you can resume it later.
      </StopDialog>
    </>
  )
}

function PaperBody({
  view,
  sel,
  paperId,
  pdf,
  actions,
  workbook,
  ask,
  hint,
  dock,
  zoom,
  onActivate,
  onSearch,
  onRetry,
}: {
  view: View
  sel: Selection
  paperId: string
  pdf: string
  actions: ReviewActions
  workbook: Workbook
  ask: AskRun
  hint: KeyHint
  dock: RunInfo | null
  zoom: number
  onActivate: (id: string | null) => void
  onSearch: (patch: Partial<PaperSearch>) => void
  onRetry: (run: RunInfo) => void
}) {
  const [nonce, setNonce] = useState(0)
  if (sel.mode === "data" && (sel.hasData || sel.itemsComing))
    return (
      <div className="min-h-0 flex-1">
        {sel.hasData && !workbook.loading ? (
          <DataMode
            items={view.items}
            tabs={workbook.tabs}
            boxes={view.boxes}
            actions={actions}
            selected={sel.picked}
            tab={sel.tab}
            allColumns={sel.allColumns}
            ask={ask}
            onSelect={onActivate}
            onTab={(tab) => onSearch({ tab, item: undefined })}
            onColumns={(all) => onSearch({ cols: all ? "all" : undefined })}
            onShow={(id) => onSearch({ mode: "check", item: id })}
          />
        ) : (
          <DataModeSkeleton />
        )}
      </div>
    )
  const check = sel.mode === "check"
  return (
    <div className="flex min-h-0 flex-1">
      <div className="min-w-0 flex-1">
        <PaperCanvas
          mode={check ? "check" : "read"}
          loading={view.loading}
          url={pdf}
          pages={view.doc?.pages}
          items={view.items}
          boxes={view.boxes}
          units={sel.units}
          active={sel.active}
          followKey={`${sel.active?.id ?? ""}:${nonce}`}
          zoom={zoom}
          onZoom={(z) => onSearch({ zoom: z === 1 ? undefined : z })}
          overlay={<Overlay view={view} paperId={paperId} onSearch={onSearch} onRetry={onRetry} />}
          margin={
            check && sel.hasData
              ? {
                  items: view.items,
                  units: sel.visible,
                  filter: sel.filter,
                  active: sel.active,
                  boxes: view.boxes,
                  tabs: workbook.tabs,
                  actions,
                  hint,
                  ask: (
                    <AskSage
                      items={view.items}
                      tabs={workbook.tabs}
                      ask={ask}
                      actions={actions}
                      onOpen={onActivate}
                    />
                  ),
                  done: <ExportMenu runId={view.viewId} testid="done-export" />,
                  onFilter: (f) => onSearch({ filter: f === "all" ? "all" : undefined, item: undefined }),
                  onActivate,
                  onSource: () => setNonce((n) => n + 1),
                }
              : null
          }
          onHighlight={(id) => onSearch({ mode: "check", item: id })}
        />
      </div>
      {dock ? <RunDock run={dock} onStop={() => onSearch({ stop: 1 })} /> : null}
    </div>
  )
}

/** A page for something that isn't there. Without a message it is the blank frame shown while the lookup runs. */
export function NotFound({ message }: { message?: string }) {
  return (
    <div className="flex h-svh flex-col">
      <TopBar title={message ? "Not found" : ""} crumb={message ? <Crumb title="Not found" /> : null} />
      {message ? <p className="mx-auto mt-24 text-body text-muted-ink">{message}</p> : null}
    </div>
  )
}

function ModeSwitch({ mode, onMode }: { mode: Mode; onMode: (m: Mode) => void }) {
  return (
    <Segmented
      label="Mode"
      value={mode}
      onChange={onMode}
      options={[
        { value: "check", label: "Check", testid: "mode-check" },
        { value: "data", label: "Data", testid: "mode-data" },
        { value: "read", label: "Read", testid: "mode-read" },
      ]}
    />
  )
}

function Overlay({
  view,
  paperId,
  onSearch,
  onRetry,
}: {
  view: View
  paperId: string
  onSearch: (patch: Partial<PaperSearch>) => void
  onRetry: (run: RunInfo) => void
}) {
  const dismissed = useDismissed()
  const s = view.state
  if (view.items.length || view.loading || !s || s.kind === "live" || s.kind === "data") return null
  if (s.kind === "failed" && endedAlert(s.run) !== null && !dismissed.has(s.run.id))
    return (
      <EndedCard
        run={s.run}
        onRetry={onRetry}
        onDetails={() => onSearch({ details: 1 })}
        onDismiss={(r) => dismissed.dismiss(r.id)}
      />
    )
  return <NewCard paperId={paperId} />
}

/** The paper's own PDF. The top bar's Export offers it; the margin's, shown once all is checked, doesn't. */
type PdfLink = { href: string; filename: string | undefined }

const EXPORTS = [
  ["xlsx", "Curation workbook (Excel)"],
  ["csv", "Curation workbook (CSV)"],
  ["json", "Everything (JSON)"],
] as const

function ExportMenu({
  runId,
  pdf,
  testid = "export-menu",
}: {
  runId: string | null
  pdf?: PdfLink
  testid?: string
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={<Button variant="outline" className="px-3" disabled={!runId && !pdf} data-testid={testid} />}
      >
        <DownloadSimpleIcon className="size-4" />
        Export
        <CaretDownIcon className="size-3.5 text-muted-ink" />
      </DropdownMenuTrigger>
      <DropdownMenuContent>
        {EXPORTS.map(([format, label]) => (
          <DropdownMenuLinkItem
            key={format}
            href={`/api/runs/${runId}/export?format=${format}`}
            disabled={!runId}
            data-testid={`export-${format}`}
          >
            {label}
          </DropdownMenuLinkItem>
        ))}
        {pdf ? (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuLinkItem href={pdf.href} download={pdf.filename} data-testid="menu-download">
              Original PDF
            </DropdownMenuLinkItem>
          </>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function RunsMenu({
  view,
  docked,
  onSearch,
  onStop,
  onContinue,
}: {
  view: View
  docked: boolean
  onSearch: (patch: Partial<PaperSearch>) => void
  onStop: () => void
  onContinue: (run: RunInfo) => void
}) {
  const shown = view.runs.find((r) => r.id === view.viewId) ?? null
  const pick = (r: RunInfo) =>
    onSearch({ run: r.id === view.dataRun?.id ? undefined : r.id, item: undefined })
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="ghost" className="px-3" data-testid="runs-menu" />}>
        <ClockCounterClockwiseIcon className="size-4" />
        Runs
        <CaretDownIcon className="size-3.5 text-muted-ink" />
      </DropdownMenuTrigger>
      <DropdownMenuContent>
        <DropdownMenuItem
          disabled={!view.drawerRun || docked}
          onClick={() => onSearch({ details: 1 })}
          data-testid="menu-details"
        >
          This run
        </DropdownMenuItem>
        <HistoryMenu runs={view.runs} shown={view.viewId} onPick={pick} />
        <DropdownMenuSeparator />
        {view.state?.kind === "live" ? (
          <DropdownMenuItem onClick={onStop} data-testid="menu-stop">
            Stop extraction…
          </DropdownMenuItem>
        ) : (
          <>
            <DropdownMenuItem onClick={() => onSearch({ extract: 1 })} data-testid="menu-extract-again">
              {view.runs.length ? "Extract again…" : "Extract…"}
            </DropdownMenuItem>
            <DropdownMenuItem
              disabled={!shown || startsOver(shown)}
              onClick={() => shown && onContinue(shown)}
              data-testid="menu-continue"
            >
              {shown && !startsOver(shown) ? carryOnLabel(shown) : "Continue with guidance…"}
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function Crumb({ title }: { title: string }) {
  return (
    <>
      <Link to="/" className="shrink-0 rounded-inline hover:text-ink">
        Papers
      </Link>
      <span className="text-line">/</span>
      <span className="truncate font-medium text-ink" title={title} data-testid="crumb-title">
        {title}
      </span>
    </>
  )
}
