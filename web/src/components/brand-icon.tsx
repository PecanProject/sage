import anthropic from "@lobehub/icons-static-svg/icons/anthropic.svg?url"
import azure from "@lobehub/icons-static-svg/icons/azure-color.svg?url"
import bedrock from "@lobehub/icons-static-svg/icons/bedrock-color.svg?url"
import claudecode from "@lobehub/icons-static-svg/icons/claudecode-color.svg?url"
import codex from "@lobehub/icons-static-svg/icons/codex-color.svg?url"
import gemini from "@lobehub/icons-static-svg/icons/gemini-color.svg?url"
import geminicli from "@lobehub/icons-static-svg/icons/geminicli-color.svg?url"
import openai from "@lobehub/icons-static-svg/icons/openai.svg?url"
import opencode from "@lobehub/icons-static-svg/icons/opencode.svg?url"
import vertexai from "@lobehub/icons-static-svg/icons/vertexai-color.svg?url"
import { type Icon, TerminalWindowIcon } from "@phosphor-icons/react"
import { cn } from "@/lib/utils"

/** Marks that come only in one colour, drawn in the text colour so they follow the theme. */
const MONO = new Set(["anthropic", "openai", "opencode"])

/** The marks from @lobehub/icons-static-svg (MIT), one file each, so only these reach the bundle: in colour where the
 * brand has one, else monochrome. Keys are the built-in agents' ids and the provider kinds. */
const MARKS: Record<string, string> = {
  claude: claudecode,
  codex,
  gemini: geminicli,
  opencode,
  anthropic,
  openai,
  google: gemini,
  vertex: vertexai,
  bedrock,
  azure,
}

/** A brand's mark: its own colours, or the text colour through a mask for a monochrome one. One without a mark, such
 * as an agent you added, gets `fallback`. */
export function BrandIcon({
  brand,
  fallback: Fallback = TerminalWindowIcon,
  className,
}: {
  brand: string
  fallback?: Icon
  className?: string
}) {
  const mark = MARKS[brand]
  if (mark === undefined) return <Fallback aria-hidden className={cn("size-4 shrink-0", className)} />
  if (!MONO.has(brand))
    return <img src={mark} alt="" aria-hidden className={cn("size-4 shrink-0", className)} />
  return (
    <span
      aria-hidden
      className={cn("inline-block size-4 shrink-0 bg-current", className)}
      style={{ mask: `url("${mark}") center / contain no-repeat` }}
    />
  )
}
