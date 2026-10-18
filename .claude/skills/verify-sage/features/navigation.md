# Navigation

SAGE has two screens and one dialog. The top bar (56px) carries the leaf mark and "sage", which link home, the breadcrumb on a paper, the screen's own controls, and ⚙ for Settings. There is no sidebar and no command palette. A slim strip under the bar asks for an API key when none is set.

## Sub-features

- `nav-home` links home from the wordmark and the "Papers" crumb.
- `nav-modes` switches a paper between Check, Data and Read (`mode=`), and the choice lives in the URL.
- `nav-runs` is the paper's Runs menu: This run, a History submenu, Extract again… and Continue with guidance…. The last reads "Resume…" for an extraction that failed or was stopped and "Continue" for one that ran out of budget with items, and is disabled when the extraction shown found nothing and can't be resumed. Download PDF is Export › Original PDF.
- `nav-settings` opens the Settings dialog from ⚙ (`/settings` on Home, `?settings=1` elsewhere). Closing goes back.
- `nav-help` opens the shortcut sheet with `?`.
- `nav-old-links` redirects `/runs/<runId>` to its paper with the details drawer open.
- `nav-not-found` shows a Not found page with the top bar and a "Papers / Not found" crumb: "There's no page at this address" for an unknown path, "There's no such extraction" for an unknown `/runs/<runId>`, the server's message for an unknown `/papers/<paperId>`, and "This paper has no extraction <runId>" for a `run=` that isn't the paper's.

## How to get to it (user POV)

- Click the wordmark, the crumb, the mode switch, Runs or ⚙.
- Press `?` anywhere outside a text field.

## Driving it with verify-browser and cli

Preconditions:

- Baseline instance.

- **Modes.** On `/papers/Felipe-2010-Cultivar` with data, `browser_click [data-testid=mode-data]`. The URL gains `mode=data`. `[data-testid=mode-read]` gives `mode=read`.
- **Runs menu.** `browser_click [data-testid=runs-menu]`, then hover or click `[data-testid=menu-history]`. The submenu `[data-testid=history]` lists `[data-testid=history-row]` with `data-current` on the extraction shown. Picking an older one adds `run=` and the status line shows `[data-testid=viewing-older]`. Its records, evidence boxes and table grid come from that run: the network shows `GET /api/runs/<that runId>/records` and `GET /api/runs/<that runId>/document`, not the paper's document. `[data-testid=back-to-latest]` reads "Back to latest" and drops `run=`. When the extraction that stands for the paper is older than the one picked, it reads "Back to <date>" instead.
- **Settings.** `browser_click [data-testid=settings-button]`. `[data-testid=settings-dialog]` opens, and on Home the URL is `/settings`. `browser_click [data-testid=cancel-settings]` goes back.
- **Help.** `browser_press_key Shift+?`. `[data-testid=shortcuts]` lists the keys.
- **Old links.** `browser_navigate $SAGE_VERIFY_URL/runs/<runId>`. The URL becomes `/papers/<paperId>?run=<runId>&details=1`, and `[data-testid=details-drawer]` is open.
- **Not found.** `browser_navigate` to `/nowhere`, `/runs/no-such-run`, `/papers/no-such-paper` and `/papers/Felipe-2010-Cultivar?run=no-such-run` in turn. Each keeps the top bar, `[data-testid=crumb-title]` reads "Not found", and the line reads "There's no page at this address", "There's no such extraction", "no paper no-such-paper; see `sage papers`" and "This paper has no extraction no-such-run". The last loads no `/api/runs/no-such-run/…` request.
- **Key strip.** On an instance without a key, `[data-testid=key-strip]` shows under the bar.
- **Proof.** `evidence/ui/settings-{light,dark}.png` with the dialog open.

## Gotchas

- Shortcuts are ignored while a text field, dialog or menu has focus. Click the page body first.
