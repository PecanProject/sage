import mark from "@/assets/sage-mark.svg?raw"

/** Rendered from assets/sage-mark.svg as it was delivered, so a new logo is a file copy. The leaf's knockout
 * outline and veins read `--sage-mark-bg`, the colour behind the mark. */
export function Logo({ size = 20 }: { size?: number }) {
  return (
    <span
      aria-hidden="true"
      className="inline-flex shrink-0 text-sage [&>svg]:size-full"
      style={{ width: size, height: size, ["--sage-mark-bg" as string]: "var(--canvas)" }}
      // biome-ignore lint/security/noDangerouslySetInnerHtml: our own static SVG asset, bundled at build time
      dangerouslySetInnerHTML={{ __html: mark }}
    />
  )
}

export function Wordmark() {
  return (
    // Baseline-aligned so the word lines up with text beside it; the mark centres itself.
    <span className="flex items-baseline gap-2">
      <span className="flex self-center">
        <Logo />
      </span>
      <span className="text-wordmark text-ink">sage</span>
    </span>
  )
}
