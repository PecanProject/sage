import { useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { toast } from "sonner"
import { errorMessage, fetchClient } from "@/api/client"

export function pdfsOf(list: FileList | null): File[] {
  const all = [...(list ?? [])]
  const isPdf = (f: File) => f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf")
  const pdfs = all.filter(isPdf)
  const skipped = all.filter((f) => !isPdf(f)).map((f) => f.name)
  if (skipped.length)
    toast.warning(
      skipped.length === 1 ? `${skipped[0]} isn't a PDF` : `${skipped.length} files aren't PDFs`,
      {
        description: skipped.length === 1 ? undefined : skipped.join(", "),
      },
    )
  return pdfs
}

/** A file on its way to the server, shown as a row until the upload answers. */
export type Adding = { id: number; name: string }

let lastId = 0
function nextId(): number {
  lastId += 1
  return lastId
}

type AddedFile = { filename: string }

/** One file by the name it was picked by, else how many. */
function sayFiles(
  files: AddedFile[],
  one: (name: string) => string,
  many: (n: number) => string,
): string | null {
  const [first] = files
  if (first === undefined) return null
  return files.length === 1 ? one(first.filename) : many(files.length)
}

export function useUpload() {
  const queryClient = useQueryClient()
  const [adding, setAdding] = useState<Adding[]>([])
  const upload = async (files: File[]) => {
    if (!files.length) return
    const rows: Adding[] = files.map((f) => ({ id: nextId(), name: f.name }))
    setAdding((a) => [...a, ...rows])
    // A dropped connection rejects instead of returning an error, and must still clear the rows being added.
    const { data, error } = await fetchClient
      .POST("/api/papers", {
        body: { files: [] },
        bodySerializer: () => {
          const form = new FormData()
          for (const f of files) form.append("files", f)
          return form
        },
      })
      .catch((e: unknown) => ({ data: undefined, error: errorMessage(e) }))
    const done = new Set(rows.map((row) => row.id))
    setAdding((a) => a.filter((row) => !done.has(row.id)))
    await queryClient.invalidateQueries({ queryKey: ["get", "/api/papers"] })
    if (error !== undefined) {
      toast.error(`Couldn't add: ${errorMessage(error)}`)
      return
    }
    const added = data.flatMap((a) => ("new" in a && a.new ? [a] : []))
    const known = data.flatMap((a) => ("new" in a && !a.new ? [a] : []))
    const summary = [
      sayFiles(
        added,
        (one) => `Added ${one}`,
        (n) => `Added ${n} papers`,
      ),
      sayFiles(
        known,
        (one) => `${one} is already in your library`,
        (n) => `${n} were already in your library`,
      ),
    ]
      .filter(Boolean)
      .join(". ")
    if (summary) toast(summary)
    // Each file stands alone: one that can't be added says why, and the rest are still added.
    for (const a of data)
      if ("error" in a) toast.error(`Couldn't add ${a.filename}`, { description: a.error.message })
  }
  return { adding, upload }
}
