# Check

Check mode works like Google Docs suggestions. The PDF is on the left and a 380px margin sits on its right, inside the same scroll container, so the cards move with the paper. Each card lines up with its own highlight (the one the PDF scrolls to), and the active card sits exactly beside its highlight while the others move out of its way. Three or more items on one table form a "Table 1 · 22 values" cluster that opens to a grid shaped like the paper's table. Items the paper can't place form a "Not located" cluster at the top. Selecting an item scrolls its highlight and card to the middle of the pane, and every other highlight fades. The zoom is manual and holds across items. Each decision is appended to the paper's `reviews.jsonl`. Data mode shows the same items as tables with a side sheet per row.

## Sub-features

- `check-header` is the margin's sticky header: a sage progress bar with "N of M reviewed", the To check / All filter (`filter=all`), and "Accept N good". It reads "All checked" with an Export menu when nothing is left.
- `check-cards` are one 32px line per collapsed card, with a status shape, the label and the problem in a few words ("Year guessed"). Reviewed items show a check or a cross.
- `check-pin` lines each card up with its first highlight, in page, y, then column order. The active card snaps to its highlight, the others move with an 8px gap and a 150ms animation, and nothing overlaps.
- `check-open` expands the active card with a 1px sage border and the item panel Data mode's sheet also uses: one collapsible `sheet-field` row per field (those to check open), each with its input, the problem, the quotes and a "Page N" link, then Accept, Edit and Not in the paper.
- `check-clusters` covers the table cluster (grid with a status per cell, "Accept table", arrow keys), and "Not located" ("Accept all"). A wide grid scrolls sideways inside the card while its row-key and label columns stay pinned.
- `check-hover` links a card and its highlights: hovering either marks the other.
- `check-pdf` scrolls to the source, outlines a table, and fades the other highlights. It never changes the zoom. The reader zooms with `+`, `-`, Ctrl or ⌘ with the wheel, or a pinch, and `0` or "Fit page" (top right of the PDF pane while zoomed) goes back to Fit page.
- `check-keys` binds j/k to move in document order (the highlight and its open card land centred together), a to accept, e to edit, r to "Not in the paper", c to change a reviewed item, Shift+A to accept a whole cluster, / to flip the filter, `+`, `-` and `0` to zoom, and ? for help. The key hint at the bottom of the margin hides after ten key presses.
- `check-good` accepts every unreviewed "looks good" item in one call, with a toast "N accepted".
- `check-write` appends each decision to `$SAGE_HOME/papers/<paperId>/reviews.jsonl`.
- `data-mode` makes the row the only click target. A row click, Enter or `item=` in the URL opens the 440px sheet with one collapsible section per field of the item. The open row scrolls into view beside the sheet, however it was picked. j/k move between rows.
- `check-cli` records a decision with `sage review <run> <record> accept|edit|reject`, or `sage review <run> --accept-looks-good`.

## How to get to it (user POV)

- Open a paper that has data. It opens in Check mode on the first item that needs a look.
- Choose Check, Data or Read in the top bar's mode switch.
- Click a highlight in Read mode, or a field's "Page N" link in Data mode's sheet, to land in Check on that item.

## Driving it with verify-browser and cli

Preconditions:

- A finished run with records. This needs a real key (see extract-and-watch). Note its `<runId>` and a table group, for example `p7.t1` on Felipe.

- **Margin.** `browser_navigate $SAGE_VERIFY_URL/papers/Felipe-2010-Cultivar`. `[data-testid=margin]` has `data-filter=check`. `[data-testid=review-progress]` reads "0 of N reviewed". Each `[data-testid=card]` carries `data-kind` (`item`, `table` or `unlocated`) and `data-unit`. The open one has `data-active` and holds `[data-testid=item-detail]`.
- **Pinned.** The active card's top is within a few px of `[data-testid=highlight][data-active]`'s top, and that highlight sits about 30% down `[data-testid=pdf-pane]`. No two `[data-testid=margin] section` boxes overlap.
- **PDF follows.** Other highlights carry `data-dim`. `[data-testid=pdf-pane]` carries `data-zoom` (1 at Fit page); pressing `=` or `-` steps it and puts `?zoom=` in the URL, and `0` or `browser_click [data-testid=fit-page]` brings it back to 1. Moving between items never changes it.
- **Hover.** Hover a collapsed card. Its `[data-testid=card]` gains `data-hover`, and its highlights gain `data-hover` too.
- **Accept.** `browser_press_key a`. `[data-testid=review-progress]` rises by one, and the URL's `item=` moves to the next item still to check.
- **Edit.** `browser_press_key e`. The first problem field's `[data-testid=sheet-field]` opens and its `[data-testid=sheet-input]` has focus. Type a value: `[data-testid=sheet-save]` appears. Press `Enter`.
- **Not in the paper.** `browser_press_key r`, type a note into `[data-testid=sheet-note]` and press `Enter`.
- **Table.** `browser_click [data-testid=cluster][data-unit=p7.t1]`. `[data-testid=table-grid]` shows `[data-testid=grid-cell]` buttons with `data-status`, and `[data-testid=table-outline]` is drawn on the PDF. Press `ArrowRight` and `ArrowUp`, and the URL's `item=` moves to the neighbouring cell. `browser_click [data-testid=accept-table]` (or `Shift+A`) accepts every unreviewed cell.
- **Pinned columns.** The stub cells carry `data-pinned` and compute to `position: sticky` (left `0px`, then `40px`). Set `[data-testid=table-grid-scroll]`'s `scrollLeft`: the pinned cells keep their `getBoundingClientRect().left` while the first value cell moves under them. Felipe's Table 1 fits the 380px margin, so either use a paper with a wider table or narrow the scroll box from the test (`style.width = "240px"`) and say so.
- **Quoted words.** With a prose item active, its `[data-testid=highlight][data-active]` carries `data-precise` and holds `[data-testid=quote-line]` spans, one per line its quotes cover. `GET /api/runs/<runId>/records` has those boxes under `boxes[<anchor>].quotes[<quote>]`, and the run folder has `quotes.json`. Felipe's `obs-hue-fallow` marks "Fruit were redder … P <0.05" on page 6, not the whole paragraph.
- **Filter.** `browser_press_key /`. The URL gains `filter=all`, and reviewed items appear with `data-reviewed`.
- **Accept good.** `browser_click [data-testid=accept-looks-good]`. A toast reads "N accepted".
- **Not located.** A run whose evidence cites anchors the paper doesn't have shows `[data-testid=card][data-kind=unlocated]` first in the margin. Opening one of its items scrolls the pane to the top.
- **Done.** Once nothing needs a look, `[data-testid=all-checked]` shows in the header with `[data-testid=done-export]`.
- **Data mode.** `browser_click [data-testid=mode-data]`, pick a tab with many rows (`[data-testid=data-tab-Variable]`), then a `[data-testid=data-row]`. `[data-testid=item-sheet]` opens with one `[data-testid=sheet-field]` per entry in that record's `fields` (records.json), and the row has `data-selected`. Open a late row by URL (`?mode=data&item=<an observation near the end>`): the row is inside its scroll box and left of the sheet. `[data-testid=sheet-page]` (inside an expanded section; click a section header first when all are collapsed) returns to Check on that item. Narrow the window until the tabs don't fit (`browser_resize`, no narrower than 768px, where the app stops). The tab row then scrolls sideways inside itself, and `document.scrollingElement.scrollWidth` equals `clientWidth`, so the page never scrolls sideways. If they fit even at 768px, say so.
- **Reload.** After the key actions, reload. The URL's `item=` stays, `review-progress` reads the same, and with `filter=all` each reviewed item carries `data-reviewed` (`accept`, `edit`, `reject`) from the server.
- **On disk.** `tail -3 $SAGE_HOME/papers/<paperId>/reviews.jsonl` shows the accept, edit (with `field` and `value`) and reject (with `note`) lines, each with the run and record ids.
- **CLI.** `$S/cli "$root" review review <runId> <recordId> accept --json` exits 0. `$S/cli "$root" good review <runId> --accept-looks-good --json` returns one accept per unreviewed "looks good" item. A second call returns `[]`.
- **Proof.** `evidence/ui/check-item-{light,dark}.png` with an item open, `evidence/ui/check-table-{light,dark}.png` with the table cluster open, `evidence/ui/data-sheet-{light,dark}.png` with a late row open, and the `reviews.jsonl` tail.

## Gotchas

- Keys typed into a field's input or the note belong to it. Enter saves, and Escape drops that field's draft or closes the note.
- The PDF follows only after the PDF has loaded. Wait for a `[data-testid=highlight]` before you read scroll positions.
- In To check, a card leaves the margin once reviewed (a table cluster stays whole). Use `filter=all` to see the marks, and to find a table cluster whose cells are all reviewed.
- A paper opens on the first item to check without `item=` in the URL. The URL gains `item=` on the first j, k or action.
- The Data mode table's columns are the union of the type's fields, so a row can have fewer sheet sections than the table has columns. Compare sections to the record's own `fields`.
- The key hint counts real key presses in `localStorage["sage.key-hint.uses"]`. Remove that key to see the hint again.
- Without a real key there are no records, and the paper opens in Read mode with the Extract card. Report "needs a real key", not a pass.
