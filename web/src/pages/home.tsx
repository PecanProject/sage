import {
  ArrowClockwiseIcon,
  CheckIcon,
  CircleNotchIcon,
  DotsThreeIcon,
  DownloadSimpleIcon,
  MagnifyingGlassIcon,
  PlusIcon,
  WarningIcon,
} from "@phosphor-icons/react"
import { Link, useNavigate } from "@tanstack/react-router"
import { type DragEvent, type ReactNode, useRef, useState } from "react"
import { $api, errorMessage } from "@/api/client"
import { Logo } from "@/components/logo"
import { TopBar } from "@/components/top-bar"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLinkItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { TextSkeleton } from "@/components/ui/skeleton"
import { Tip } from "@/components/ui/tooltip"
import { useDismissed } from "@/lib/dismissed"
import { endedWord, isLive, lastAttempt, pollMs, when } from "@/lib/format"
import { useKeys } from "@/lib/keys"
import { type PaperState, paperState, type Summary, useStartRun } from "@/lib/paper-state"
import { type Adding, pdfsOf, useUpload } from "@/lib/upload"
import { cn } from "@/lib/utils"

const STATUS = "flex items-center gap-2 text-caption text-muted-ink"

function lastActivity(s: Summary): string {
  const r = s.latest_run
  return when(r ? (r.finished_at ?? r.started_at ?? r.created_at) : s.paper.added_at)
}

function InlineAction({
  children,
  onClick,
  busy,
  testid,
}: {
  children: ReactNode
  onClick: () => void
  busy: boolean
  testid: string
}) {
  return (
    <Button
      variant="outline"
      size="compact"
      className="relative z-10"
      disabled={busy}
      onClick={onClick}
      data-testid={testid}
    >
      {children}
    </Button>
  )
}

function DataStatus({ state }: { state: Extract<PaperState, { kind: "data" }> }) {
  const dismissed = useDismissed()
  return (
    <span className={STATUS} data-testid="paper-status" data-state={state.toCheck ? "to-check" : "checked"}>
      {state.warn && !dismissed.has(state.warn.id) ? (
        <Tip label={lastAttempt(state.warn)}>
          <span
            role="img"
            aria-label={lastAttempt(state.warn)}
            className="relative z-10 flex"
            data-testid="latest-failed-hint"
          >
            <WarningIcon weight="fill" className="size-4 text-check" />
          </span>
        </Tip>
      ) : null}
      {state.toCheck ? (
        <span className="text-ink">{state.toCheck} to check</span>
      ) : (
        <>
          <CheckIcon weight="bold" className="size-4 text-good" aria-hidden />
          All checked
        </>
      )}
    </span>
  )
}

/** One status per row, in at most three words, from `PaperSummary` alone. */
function RowStatus({ s, onExtract, busy }: { s: Summary; onExtract: () => void; busy: boolean }) {
  const state = paperState(s)
  switch (state.kind) {
    case "live":
      return (
        <span className={STATUS} data-testid="paper-status" data-state="live">
          <CircleNotchIcon className="size-4 animate-spin text-muted-ink" />
          {state.step.word}…
        </span>
      )
    case "none":
      return (
        <span className={STATUS} data-testid="paper-status" data-state="none">
          Not extracted
          <InlineAction onClick={onExtract} busy={busy} testid="row-extract">
            Extract
          </InlineAction>
        </span>
      )
    case "failed":
      return (
        <span
          className={cn(STATUS, state.run.status === "failed" && "text-danger")}
          data-testid="paper-status"
          data-state="failed"
        >
          {endedWord(state.run)}
          <InlineAction onClick={onExtract} busy={busy} testid="row-retry">
            Extract again
          </InlineAction>
        </span>
      )
    case "data":
      return <DataStatus state={state} />
  }
}

function RowMenu({
  s,
  busy,
  onOpen,
  onExtract,
}: {
  s: Summary
  busy: boolean
  onOpen: () => void
  onExtract: () => void
}) {
  const live = s.latest_run ? isLive(s.latest_run) : false
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button
            variant="quiet"
            size="icon-sm"
            className="relative z-10 -mr-2"
            aria-label="More"
            data-testid="row-menu"
          />
        }
      >
        <DotsThreeIcon weight="bold" />
      </DropdownMenuTrigger>
      <DropdownMenuContent>
        <DropdownMenuItem onClick={onOpen}>Open</DropdownMenuItem>
        <DropdownMenuItem disabled={live || busy} onClick={onExtract}>
          <ArrowClockwiseIcon /> {s.run_count ? "Extract again" : "Extract"}
        </DropdownMenuItem>
        <DropdownMenuLinkItem
          href={`/api/papers/${encodeURIComponent(s.paper.id)}/pdf`}
          download={s.paper.filename}
        >
          <DownloadSimpleIcon /> Download PDF
        </DropdownMenuLinkItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function PaperRow({ s, cursor, onHover }: { s: Summary; cursor: boolean; onHover: () => void }) {
  const navigate = useNavigate()
  const open = () => void navigate({ to: "/papers/$paperId", params: { paperId: s.paper.id } })
  const start = useStartRun(open)
  const extract = () => start.mutate({ params: { path: { paper_id: s.paper.id } }, body: {} })
  return (
    <li
      className={cn(
        "relative flex h-16 items-center gap-6 rounded-control px-inset transition-colors duration-150 hover:bg-surface-2",
        cursor && "bg-surface-2",
      )}
      onMouseEnter={onHover}
      data-testid="paper-row"
      data-paper={s.paper.id}
      data-cursor={cursor || undefined}
    >
      <div className="min-w-0 flex-1">
        <Link
          to="/papers/$paperId"
          params={{ paperId: s.paper.id }}
          className="block truncate text-body font-semibold outline-none after:absolute after:inset-0 after:rounded-control focus-visible:after:outline-2 focus-visible:after:outline-sage"
        >
          {s.paper.title ?? s.paper.filename}
        </Link>
        <div className="truncate text-caption text-muted-ink">
          {s.paper.filename} · {s.paper.pages} pages
        </div>
      </div>
      <div className="flex w-48 shrink-0 justify-end">
        <RowStatus s={s} onExtract={extract} busy={start.isPending} />
      </div>
      <span className="w-28 shrink-0 text-right text-caption text-muted-ink">{lastActivity(s)}</span>
      <RowMenu s={s} busy={start.isPending} onOpen={open} onExtract={extract} />
    </li>
  )
}

function PaperListSkeleton() {
  return (
    <ul className="flex flex-col" data-testid="paper-list-loading">
      {["w-96", "w-80", "w-[26rem]", "w-72"].map((title) => (
        <li key={title} className="flex h-16 items-center gap-6 px-inset">
          <div className="min-w-0 flex-1">
            <div className="text-body">
              <TextSkeleton className={title} />
            </div>
            <div className="text-caption">
              <TextSkeleton className="w-44" />
            </div>
          </div>
          <div className="flex w-48 shrink-0 justify-end text-caption">
            <TextSkeleton className="w-20" />
          </div>
          <div className="flex w-28 shrink-0 justify-end text-caption">
            <TextSkeleton className="w-14" />
          </div>
          <span className="-mr-2 size-control-sm shrink-0" />
        </li>
      ))}
    </ul>
  )
}

function Search({ value, onChange }: { value: string; onChange: (q: string) => void }) {
  const input = useRef<HTMLInputElement>(null)
  useKeys({
    "/": (e) => {
      e.preventDefault()
      input.current?.focus()
    },
  })
  return (
    <div className="relative w-full max-w-[440px]">
      <MagnifyingGlassIcon className="pointer-events-none absolute top-1/2 left-3 size-icon -translate-y-1/2 text-muted-ink" />
      <Input
        ref={input}
        type="search"
        placeholder="Search papers"
        aria-label="Search papers"
        className="border-transparent bg-surface-2 pl-10 hover:border-transparent focus-visible:bg-surface"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") e.currentTarget.blur()
        }}
        data-testid="search-papers"
      />
    </div>
  )
}

function EmptyDrop({ onPick }: { onPick: () => void }) {
  return (
    <button
      type="button"
      onClick={onPick}
      className="flex w-full cursor-pointer flex-col items-center gap-5 rounded-card border-2 border-dashed border-line px-8 py-24 text-center transition-colors duration-150 hover:border-line-accent"
      data-testid="empty-drop"
    >
      <Logo size={56} />
      <span className="text-title">Drop papers here</span>
      <span className="inline-flex h-control items-center gap-2 rounded-control bg-sage px-4 text-control font-medium text-on-sage">
        <PlusIcon weight="bold" className="size-icon" /> Add papers
      </span>
    </button>
  )
}

function DropOverlay() {
  return (
    <div
      className="pointer-events-none fixed inset-0 z-40 flex items-center justify-center bg-canvas/85 p-8"
      data-testid="drop-overlay"
    >
      <div className="flex size-full flex-col items-center justify-center gap-4 rounded-card border-2 border-dashed border-sage">
        <Logo size={48} />
        <span className="text-title text-ink">Drop to add</span>
      </div>
    </div>
  )
}

/** The rows, with j/k moving a cursor and Enter opening it. The cursor holds a paper, not a place, so it stays on
 * that paper as uploads, searches and live runs change the list, and falls back to the top when the paper leaves. */
function PaperList({ papers, adding, query }: { papers: Summary[]; adding: Adding[]; query: string }) {
  const navigate = useNavigate()
  const [cursor, setCursor] = useState<string | null>(null)
  const at = Math.max(
    papers.findIndex((s) => s.paper.id === cursor),
    0,
  )
  const step = (by: 1 | -1) => {
    const s = papers[Math.min(Math.max(at + by, 0), papers.length - 1)]
    if (!s) return
    setCursor(s.paper.id)
    document.querySelector(`[data-paper="${CSS.escape(s.paper.id)}"]`)?.scrollIntoView({ block: "nearest" })
  }
  useKeys({
    j: () => step(1),
    k: () => step(-1),
    Enter: () => {
      const s = papers[at]
      if (s) void navigate({ to: "/papers/$paperId", params: { paperId: s.paper.id } })
    },
  })
  return (
    <ul className="flex flex-col" data-testid="paper-list">
      {adding.map(({ id, name }) => (
        <li
          key={id}
          className="flex h-16 items-center gap-4 px-inset text-muted-ink"
          data-testid="paper-adding"
        >
          <CircleNotchIcon className="size-4 animate-spin" />
          <span className="truncate">{name}</span>
        </li>
      ))}
      {papers.map((s, i) => (
        <PaperRow key={s.paper.id} s={s} cursor={i === at} onHover={() => setCursor(s.paper.id)} />
      ))}
      {papers.length === 0 && query ? (
        <li className="px-inset py-8 text-muted-ink" data-testid="no-match">
          No papers match “{query}”
        </li>
      ) : null}
    </ul>
  )
}

function useDrop(onFiles: (files: File[]) => void) {
  const [dragging, setDragging] = useState(false)
  return {
    dragging,
    handlers: {
      onDragOver: (e: DragEvent) => {
        if (!e.dataTransfer.types.includes("Files")) return
        e.preventDefault()
        setDragging(true)
      },
      onDragLeave: (e: DragEvent) => {
        if (e.relatedTarget === null || !e.currentTarget.contains(e.relatedTarget as Node)) setDragging(false)
      },
      onDrop: (e: DragEvent) => {
        e.preventDefault()
        setDragging(false)
        onFiles(pdfsOf(e.dataTransfer.files))
      },
    },
  }
}

export function HomePage() {
  const { adding, upload } = useUpload()
  const input = useRef<HTMLInputElement>(null)
  const [query, setQuery] = useState("")
  const drop = useDrop((files) => void upload(files))
  const { data, isLoading, error } = $api.useQuery(
    "get",
    "/api/papers",
    {},
    {
      refetchInterval: (q) =>
        pollMs(q.state.data?.some((p) => p.latest_run && isLive(p.latest_run)) ?? false),
    },
  )
  const all = data ?? []
  const q = query.trim().toLowerCase()
  const papers = q
    ? all.filter((s) => `${s.paper.title ?? ""} ${s.paper.filename}`.toLowerCase().includes(q))
    : all
  const pick = () => input.current?.click()
  // A failed poll keeps the list it already has; the error shows only when there is nothing to show.
  const failed = error && !data ? error : null
  const empty = !isLoading && !failed && all.length === 0 && adding.length === 0

  return (
    <div className="relative min-h-svh" data-testid="home" {...drop.handlers}>
      <input
        ref={input}
        type="file"
        accept="application/pdf,.pdf"
        multiple
        className="hidden"
        data-testid="upload-input"
        onChange={(e) => {
          void upload(pdfsOf(e.target.files))
          e.target.value = ""
        }}
      />
      <TopBar center={empty ? null : <Search value={query} onChange={setQuery} />} />
      <main className="mx-auto w-full max-w-[960px] px-gutter pt-10 pb-16">
        <div className="mb-6 flex h-10 items-center justify-between px-inset">
          <h1 className="text-display">Papers</h1>
          {empty ? null : (
            <Button onClick={pick} data-testid="add-papers">
              <PlusIcon weight="bold" /> Add papers
            </Button>
          )}
        </div>
        {failed ? (
          <p className="px-inset text-body text-danger">Couldn't load papers: {errorMessage(failed)}</p>
        ) : isLoading ? (
          <PaperListSkeleton />
        ) : empty ? (
          <EmptyDrop onPick={pick} />
        ) : (
          <PaperList papers={papers} adding={adding} query={query} />
        )}
      </main>
      {drop.dragging ? <DropOverlay /> : null}
    </div>
  )
}
