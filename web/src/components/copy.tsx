import { CheckIcon, CopyIcon } from "@phosphor-icons/react"
import { useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Tip } from "@/components/ui/tooltip"

/** Copies `text`, and is true for a moment after, for a button to read "Copied". A failed copy toasts `failed`. */
export function useCopy(text: string, failed: string): { copied: boolean; copy: () => void } {
  const [copied, setCopied] = useState(false)
  const copy = () =>
    void navigator.clipboard.writeText(text).then(
      () => {
        setCopied(true)
        setTimeout(() => setCopied(false), 1500)
      },
      () => toast.error(failed),
    )
  return { copied, copy }
}

export function CopyButton({ text, failed, testid }: { text: string; failed: string; testid: string }) {
  const { copied, copy } = useCopy(text, failed)
  return (
    <Button variant="quiet" size="compact" disabled={!text} onClick={copy} data-testid={testid}>
      {copied ? <CheckIcon className="size-4 text-good" /> : <CopyIcon className="size-4" />}
      {copied ? "Copied" : "Copy"}
    </Button>
  )
}

/** One line to paste somewhere else, as it is, with its copy button. */
export function CopyLine({ text, testid }: { text: string; testid: string }) {
  return (
    <div className="flex h-control items-center gap-2 rounded-control border border-line bg-surface pr-1 pl-3">
      <code className="min-w-0 flex-1 truncate font-mono text-control text-ink" data-testid={testid}>
        {text}
      </code>
      <CopyButton text={text} failed="Couldn't copy it" testid={`${testid}-copy`} />
    </div>
  )
}

/** A command shown inline, in mono, with a bare copy icon after it: for a command inside a row or a caption line. */
export function InlineCommand({ text, testid }: { text: string; testid: string }) {
  const { copied, copy } = useCopy(text, "Couldn't copy it")
  const label = copied ? "Copied" : "Copy"
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5">
      <code className="min-w-0 truncate font-mono" data-testid={testid}>
        {text}
      </code>
      <Tip label={label}>
        <Button
          variant="quiet"
          size="inline"
          className="rounded-inline p-0.5"
          aria-label={label}
          onClick={copy}
          data-testid={`${testid}-copy`}
        >
          {copied ? <CheckIcon className="size-3.5 text-good" /> : <CopyIcon className="size-3.5" />}
        </Button>
      </Tip>
    </span>
  )
}

/** A command to run in a terminal, as a terminal shows it: a prompt, the command in mono on a tinted strip, and a copy
 * icon inside the strip's right end. */
export function TerminalCommand({ text, testid }: { text: string; testid: string }) {
  const { copied, copy } = useCopy(text, "Couldn't copy it")
  const label = copied ? "Copied" : "Copy"
  return (
    <span className="inline-flex h-control-sm max-w-full items-center gap-2 rounded-control bg-surface-2 pr-1 pl-3 font-mono text-control">
      <span className="text-muted-ink select-none">$</span>
      <code className="min-w-0 truncate text-ink" data-testid={testid}>
        {text}
      </code>
      <Tip label={label}>
        <Button
          variant="quiet"
          size="icon-sm"
          className="size-6 rounded-inline hover:bg-line"
          aria-label={label}
          onClick={copy}
          data-testid={`${testid}-copy`}
        >
          {copied ? <CheckIcon className="size-3.5 text-good" /> : <CopyIcon className="size-3.5" />}
        </Button>
      </Tip>
    </span>
  )
}
