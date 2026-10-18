import { CheckIcon, LinkIcon } from "@phosphor-icons/react"
import { useState } from "react"
import { toast } from "sonner"
import { Logo } from "@/components/logo"
import { Button } from "@/components/ui/button"

/** Shown in place of the app on a screen too narrow for the paper and its margin side by side. */
export function SmallScreen() {
  return (
    <main
      className="flex min-h-svh flex-col items-center justify-center gap-5 p-inset text-center"
      data-testid="small-screen"
    >
      <Logo size={48} />
      <p className="text-title text-ink">SAGE needs a bigger screen</p>
      {/* The clipboard exists only on https or localhost, and on a plain LAN address the URL bar does the job. */}
      {window.isSecureContext ? <CopyLink /> : null}
    </main>
  )
}

function CopyLink() {
  const [copied, setCopied] = useState(false)
  const copy = () =>
    navigator.clipboard.writeText(window.location.href).then(
      () => {
        setCopied(true)
        setTimeout(() => setCopied(false), 1500)
      },
      () => toast.error("Couldn't copy the link"),
    )
  return (
    <Button variant="outline" onClick={() => void copy()} data-testid="copy-link">
      {copied ? <CheckIcon className="text-good" /> : <LinkIcon />}
      {copied ? "Copied" : "Copy link"}
    </Button>
  )
}
