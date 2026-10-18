import { XIcon } from "@phosphor-icons/react"
import type { CSSProperties } from "react"
import { type CardActions, ItemFields, ItemFooter, ItemPanel } from "@/components/paper/item-panel"
import { Button } from "@/components/ui/button"
import { SidePanel } from "@/components/ui/side-panel"
import type { Box, Item } from "@/lib/items"

export function ItemSheet({
  item,
  boxes,
  actions,
  onClose,
  onShow,
}: {
  item: Item
  boxes: Record<string, Box>
  actions: CardActions
  onClose: () => void
  onShow: () => void
}) {
  return (
    <ItemPanel item={item} boxes={boxes} actions={actions} onShow={onShow}>
      <SidePanel
        style={{ "--panel-x": "var(--spacing-inset)" } as CSSProperties}
        data-testid="item-sheet"
        data-item={item.id}
      >
        <div className="flex items-start gap-3 px-inset pt-5 pb-3">
          <div className="min-w-0 flex-1">
            <p className="text-caption text-muted-ink">{item.type}</p>
            <p className="text-value break-words">{item.label}</p>
          </div>
          <Button
            variant="quiet"
            size="icon-sm"
            onClick={onClose}
            aria-label="Close"
            data-testid="sheet-close"
          >
            <XIcon />
          </Button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto border-t border-line">
          <ItemFields />
        </div>
        <div className="border-t border-line px-inset py-4">
          <ItemFooter />
        </div>
      </SidePanel>
    </ItemPanel>
  )
}
