# Navigation

SAGE has four screens: Home, Paper, Manage (at `/manage`) and Settings. The top bar (56px) carries the leaf mark and "sage", which link home, the breadcrumb on a paper, the screen's own controls, a Manage button (a chip icon, `manage-button`) and ⚙ for Settings. There is no sidebar and no command palette. A slim strip under the bar reads "No way to extract yet · Set up" only while nothing can extract: what Extract uses by default can't start and the SAGE skill isn't installed.

## Sub-features

- `nav-home` links home from the wordmark and the "Papers" crumb.
- `nav-modes` switches a paper between Check, Data and Read (`mode=`), and the choice lives in the URL.
- `nav-runs` is the paper's Runs menu: This run, a History submenu, Extract again… and Continue with guidance…. The last reads "Resume…" for an extraction that failed or was stopped and "Continue" for one that ran out of budget with items, and is disabled when the extraction shown found nothing and can't be resumed. Download PDF is Export › Original PDF.
- `nav-manage` opens the Manage page (`/manage`) from the Manage button (`manage-button`), left of ⚙, and from every "Set up" link. The setup strip is hidden there. Home's "A model provider" first-screen choice opens it at `#providers`.
- `nav-settings` opens the Settings page (`/settings`) from ⚙, on every screen. The Manage page has three search params of its own: `add=provider` opens the Add a provider dialog, `price=<model id>` the price dialog, and `provider=<id>` that provider's fields under its row. Closing any of them drops the param.
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
- **Manage.** `browser_click [data-testid=manage-button]`. The URL becomes `/manage`, `[data-testid=manage-page]` shows under the heading "Manage skills, agents & models", with "skills, agents & models" muted, and `[data-testid=setup-strip]` is gone. `browser_click [data-testid=home-link]` goes home.
- **Settings.** `browser_click [data-testid=settings-button]` from Home and from a paper. Each time the URL becomes `/settings` and `[data-testid=settings-page]` shows, with no `settings=1` on a paper's URL. `browser_click [data-testid=home-link]` goes home. Browser back returns to the screen it came from.
- **Manage dialogs.** `browser_navigate $SAGE_VERIFY_URL/manage?add=provider` opens `[data-testid=add-provider-dialog]`, and `…/manage?price=openai:gpt-5.1` opens `[data-testid=price-dialog]`. Closing either leaves `/manage` with no search. Neither opens at `/manage` alone.
- **Help.** `browser_press_key Shift+?`. `[data-testid=shortcuts]` lists the keys.
- **Old links.** `browser_navigate $SAGE_VERIFY_URL/runs/<runId>`. The URL becomes `/papers/<paperId>?run=<runId>&details=1`, and `[data-testid=details-drawer]` is open.
- **Not found.** `browser_navigate` to `/nowhere`, `/runs/no-such-run`, `/papers/no-such-paper` and `/papers/Felipe-2010-Cultivar?run=no-such-run` in turn. Each keeps the top bar, `[data-testid=crumb-title]` reads "Not found", and the line reads "There's no page at this address", "There's no such extraction", "no paper no-such-paper; see `sage papers`" and "This paper has no extraction no-such-run". The last loads no `/api/runs/no-such-run/…` request.
- **Setup strip.** On an instance where nothing can extract (no key, `extract_with` unset, and `GET /api/skill` mocked to `missing`), `[data-testid=setup-strip]` shows under the bar on Home with papers, on a paper and on Settings, and reads "No way to extract yet" with Set up. It is not on `/manage`, and not on Home's first screen. Pick an agent as the default, or install the skill, and it goes.
- **Scrollbar.** Home, Manage and Settings each have `[data-scroll=document]`. When a page grows past the window (add enough papers on Home, or open a provider's fields on the Manage page), its content doesn't shift sideways, because the root keeps room for the scrollbar.
- **Proof.** `evidence/ui/settings-{light,dark}.png` with the page open.

## Gotchas

- Shortcuts are ignored while a text field, dialog or menu has focus. Click the page body first.
