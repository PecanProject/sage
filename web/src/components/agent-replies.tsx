import { ArrowLeftIcon, ArrowUpIcon, CaretDownIcon, ChatsIcon, TrashIcon } from "@phosphor-icons/react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useState } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"

// Dev only: the Agentation toolbar never shows the agent's side of a note, so this lists every note as a thread, live,
// with its replies and a box to answer. It reads agentation-mcp through the same /agentation proxy. Its open state is
// plain component state on purpose: it is a dev overlay on every route, not part of the app's URL.

type Message = { id: string; role: "human" | "agent"; content: string; timestamp: number }
type Note = {
  id: string
  comment: string
  url?: string
  status?: string
  timestamp: number
  thread?: Message[]
}

const notesKey = ["agentation", "notes"]
const events = ["annotation.created", "annotation.updated", "annotation.deleted", "thread.message"]

async function call(path: string, init?: RequestInit) {
  const response = await fetch(`/agentation${path}`, init)
  if (!response.ok) throw new Error(`agentation-mcp answered ${response.status}`)
  return response
}

async function fetchNotes(): Promise<Note[]> {
  const sessions: { id: string }[] = await (await call("/sessions")).json()
  const full: { annotations?: Note[] }[] = await Promise.all(
    sessions.map(async (s) => (await call(`/sessions/${s.id}`)).json()),
  )
  return full.flatMap((s) => s.annotations ?? []).filter((n) => n.comment)
}

const lastAt = (note: Note) => note.thread?.at(-1)?.timestamp ?? note.timestamp
const closed = (note: Note) => note.status === "resolved" || note.status === "dismissed"
const yourTurn = (note: Note) => !closed(note) && note.thread?.at(-1)?.role === "agent"
const page = (note: Note) => (note.url ? new URL(note.url, window.location.origin).pathname : "")

function state(note: Note) {
  if (note.status === "resolved") return "Resolved"
  if (note.status === "dismissed") return "Dismissed"
  if (yourTurn(note)) return "Your turn"
  return note.status === "acknowledged" ? "Agent is on it" : "Sent"
}

export function AgentReplies() {
  const queryClient = useQueryClient()
  const notes = useQuery({ queryKey: notesKey, queryFn: fetchNotes })
  const [open, setOpen] = useState(false)
  const [openId, setOpenId] = useState<string | null>(null)
  useEffect(() => {
    const source = new EventSource("/agentation/events")
    const refresh = () => void queryClient.invalidateQueries({ queryKey: notesKey })
    for (const type of events) source.addEventListener(type, refresh)
    return () => source.close()
  }, [queryClient])
  const all = (notes.data ?? []).toSorted(
    (a, b) => Number(closed(a)) - Number(closed(b)) || lastAt(b) - lastAt(a),
  )
  const waiting = all.filter(yourTurn).length
  const current = all.find((n) => n.id === openId)
  if (!open) {
    return (
      <Button
        variant="outline"
        className="fixed bottom-4 left-4 z-50 shadow-float"
        onClick={() => setOpen(true)}
      >
        <ChatsIcon />
        Threads{waiting > 0 && ` · ${waiting} your turn`}
      </Button>
    )
  }
  return (
    <section className="fixed bottom-4 left-4 z-50 flex max-h-[70svh] w-96 flex-col rounded-card border border-line bg-surface shadow-float">
      <header className="flex h-12 shrink-0 items-center gap-1 border-b border-line px-2">
        {current && (
          <Button variant="quiet" size="icon-sm" aria-label="All threads" onClick={() => setOpenId(null)}>
            <ArrowLeftIcon />
          </Button>
        )}
        <h2 className="flex-1 truncate px-2 text-name">{current ? current.comment : "Threads"}</h2>
        <Button variant="quiet" size="icon-sm" aria-label="Hide threads" onClick={() => setOpen(false)}>
          <CaretDownIcon />
        </Button>
      </header>
      {current ? (
        <Thread note={current} onRemoved={() => setOpenId(null)} />
      ) : (
        <ThreadList notes={all} onOpen={setOpenId} />
      )}
    </section>
  )
}

function ThreadList({ notes, onOpen }: { notes: Note[]; onOpen: (id: string) => void }) {
  const queryClient = useQueryClient()
  const done = notes.filter(closed)
  const clear = useMutation({
    mutationFn: () => Promise.all(done.map((n) => call(`/annotations/${n.id}`, { method: "DELETE" }))),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: notesKey }),
  })
  if (notes.length === 0) {
    return <p className="p-4 text-body text-muted-ink">No notes yet. Leave one with the toolbar.</p>
  }
  return (
    <>
      <ul className="flex-1 overflow-y-auto py-1">
        {notes.map((note) => (
          <li key={note.id}>
            <button
              type="button"
              className="w-full cursor-pointer px-4 py-2 text-left hover:bg-surface-2"
              onClick={() => onOpen(note.id)}
            >
              <p
                className={
                  closed(note) ? "truncate text-value text-muted-ink" : "truncate text-value text-ink"
                }
              >
                {note.comment}
              </p>
              <p className={yourTurn(note) ? "text-caption text-sage" : "text-caption text-muted-ink"}>
                {page(note)} · {state(note)}
              </p>
            </button>
          </li>
        ))}
      </ul>
      {done.length > 0 && (
        <footer className="shrink-0 border-t border-line p-2">
          <Button variant="quiet" size="compact" onClick={() => clear.mutate()} disabled={clear.isPending}>
            Clear {done.length} finished
          </Button>
        </footer>
      )}
    </>
  )
}

function Thread({ note, onRemoved }: { note: Note; onRemoved: () => void }) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState("")
  const send = useMutation({
    mutationFn: (content: string) =>
      call(`/annotations/${note.id}/thread`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role: "human", content }),
      }),
    onSuccess: () => {
      setDraft("")
      return queryClient.invalidateQueries({ queryKey: notesKey })
    },
  })
  const remove = useMutation({
    mutationFn: () => call(`/annotations/${note.id}`, { method: "DELETE" }),
    onSuccess: () => {
      onRemoved()
      return queryClient.invalidateQueries({ queryKey: notesKey })
    },
  })
  return (
    <>
      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        <p className="text-caption text-muted-ink">
          {page(note)} · {state(note)}
        </p>
        <Said who="You" text={note.comment} />
        {note.thread?.map((m) => (
          <Said key={m.id} who={m.role === "agent" ? "Agent" : "You"} text={m.content} />
        ))}
      </div>
      <footer className="flex shrink-0 gap-2 border-t border-line p-2">
        {!closed(note) && (
          <form
            className="flex flex-1 gap-2"
            onSubmit={(e) => {
              e.preventDefault()
              if (draft.trim()) send.mutate(draft.trim())
            }}
          >
            <Input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="Answer…" />
            <Button type="submit" variant="outline" size="icon" aria-label="Send" disabled={send.isPending}>
              <ArrowUpIcon />
            </Button>
          </form>
        )}
        <Button
          variant="quiet"
          size="icon"
          aria-label="Remove thread"
          className="ml-auto"
          onClick={() => remove.mutate()}
          disabled={remove.isPending}
        >
          <TrashIcon />
        </Button>
      </footer>
    </>
  )
}

function Said({ who, text }: { who: string; text: string }) {
  return (
    <div>
      <p className="text-label text-muted-ink">{who}</p>
      <p className="text-body whitespace-pre-wrap text-ink">{text}</p>
    </div>
  )
}
