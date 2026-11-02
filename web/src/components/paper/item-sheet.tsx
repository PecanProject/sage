import { XIcon } from "@phosphor-icons/react"
import type { CSSProperties } from "react"
import { type CardActions, ItemFields, ItemFooter, ItemPanel, ItemTitle } from "@/components/paper/item-panel"
import { Button } from "@/components/ui/button"
import { SidePanel } from "@/components/ui/side-panel"
import type { Box, Item } from "@/lib/items"
import type { Placement } from "@/lib/workbook"

export function ItemSheet({
  item,
  placement,
  boxes,
  actions,
  onClose,
  onShow,
}: {
  item: Item
  placement: Placement
  boxes: Record<string, Box>
  actions: CardActions
  onClose: () => void
  onShow: () => void
}) {
  return (
    <ItemPanel item={item} placement={placement} boxes={boxes} actions={actions} onShow={onShow}>
      <SidePanel
        style={{ "--panel-x": "var(--spacing-inset)" } as CSSProperties}
        data-testid="item-sheet"
        data-item={item.id}
      >
        <div className="px-inset pt-5 pb-3">
          <ItemTitle>
            <Button
              variant="quiet"
              size="icon-sm"
              onClick={onClose}
              aria-label="Close"
              data-testid="sheet-close"
            >
              <XIcon />
            </Button>
          </ItemTitle>
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
