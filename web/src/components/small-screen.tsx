import { CheckIcon, LinkIcon } from "@phosphor-icons/react"
import { useCopy } from "@/components/copy"
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
  const { copied, copy } = useCopy(window.location.href, "Couldn't copy the link")
  return (
    <Button variant="outline" onClick={copy} data-testid="copy-link">
      {copied ? <CheckIcon className="text-good" /> : <LinkIcon />}
      {copied ? "Copied" : "Copy link"}
    </Button>
  )
}
