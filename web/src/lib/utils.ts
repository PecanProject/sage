import { type ClassValue, clsx } from "clsx"
import { extendTailwindMerge } from "tailwind-merge"

// The named steps in index.css. Unregistered, `text-caption` reads as a colour and loses to `text-ink`, and
// `h-control` doesn't replace `h-8`.
const twMerge = extendTailwindMerge({
  extend: {
    theme: {
      text: ["wordmark", "display", "title", "value", "body", "label", "control", "caption"],
      spacing: ["inset", "gutter", "control", "control-sm", "icon"],
      radius: ["control", "card", "page", "inline", "bar", "mark", "highlight"],
      shadow: ["card", "page", "float", "focus", "cursor"],
    },
  },
})

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
