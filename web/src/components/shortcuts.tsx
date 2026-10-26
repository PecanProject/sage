import { Dialog, DialogBody, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"

const KEYS: [string, string][] = [
  ["j  k", "Next and previous item"],
  ["Arrows", "Move between a table's cells"],
  ["a", "Accept"],
  ["e", "Edit"],
  ["r", "Not in the paper"],
  ["c", "Change a reviewed item"],
  ["Shift A", "Accept a whole cluster"],
  ["/", "To check or All"],
  ["Esc", "Cancel an edit"],
  ["+  −", "Zoom the paper in and out"],
  ["Ctrl scroll", "Zoom at the pointer, as does a pinch"],
  ["0", "Fit the page"],
  ["?", "These shortcuts"],
]

export function Shortcuts({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-[360px]" data-testid="shortcuts">
        <DialogHeader>
          <DialogTitle>Shortcuts</DialogTitle>
        </DialogHeader>
        <DialogBody className="pb-6">
          <dl className="grid grid-cols-[88px_1fr] gap-y-2 text-control">
            {KEYS.map(([k, what]) => (
              <div key={k} className="contents">
                <dt className="text-label text-ink">{k}</dt>
                <dd className="text-muted-ink">{what}</dd>
              </div>
            ))}
          </dl>
        </DialogBody>
      </DialogContent>
    </Dialog>
  )
}
