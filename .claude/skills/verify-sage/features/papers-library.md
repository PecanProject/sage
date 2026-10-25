# Papers library

Home is a plain list of the library's papers, like Google Drive. A user adds PDFs with "Add papers", by dropping them anywhere on the page, or with `sage add`. SAGE reads each PDF's text layer, title and page count on the way in. Adding the same file again keeps one copy. Files that aren't PDFs are named in a warning toast. Each file has its own result, so one that can't be added gets its own error toast and the rest are still added. Each row says where the paper stands in at most three muted words.

## Sub-features

- `library-list` shows the title, `filename · N pages`, the status, the last activity time and a row ⋯ menu (Open, Extract again, Download PDF).
- `library-status` reads one of "Not extracted" with an inline Extract, "<step>…" with a spinner ("Queued…", "Starting…", "Reading the paper…", "Extracting…" or "Checking…"; an agent's or a terminal's run reads "Extracting…" throughout), "Needs your OK" with an amber hand (`data-state=waiting`) while an agent SAGE started waits for a permission, "N to check", "All checked", or, when no extraction has data, how the last one ended: "Stopped", "Hit its limit", "Nothing found" or "Failed" (only "Failed" in the danger colour), with an inline Extract again (`row-retry`). Over data, a newer attempt that failed or stopped, or a data run that was stopped or hit its limit, adds a warning icon (`latest-failed-hint`) whose `aria-label` is "Last attempt failed", "Last attempt stopped" or the limit.
- `library-search` filters by title and filename as you type. `/` focuses it.
- `library-upload-picker` adds PDFs through "Add papers".
- `library-upload-drop` adds PDFs dropped anywhere, with a "Drop to add" overlay while dragging.
- `library-duplicate` reports a re-added PDF as already in the library.
- `library-upload-errors` gives a file the server refuses its own error toast, "Couldn't add <file>", with the reason, while the other files are added.
- `library-cli` adds PDFs with `sage add`. `sage papers --json` returns the same `to_check`, `looks_good` and `live_stage` the rows show.
- `library-keys` moves the row cursor with j/k and opens a paper with Enter.

## How to get to it (user POV)

- Open `/`, or click the wordmark from any screen.
- Choose "Add papers", or the dashed drop zone when the library is empty.
- Drag PDFs from the desktop onto the page.
- Run `sage add <pdf>…` in a terminal.

## Driving it with verify-browser and cli

Preconditions:

- Baseline instance. Copy one more PDF into scratch, for example `cp ~/sage-test-pdfs/user/agronj2001.934896x.pdf.pdf /tmp/verify/upload/agronj2001.pdf`, plus a text file `/tmp/verify/upload/notes.txt`.

- **List.** `browser_navigate $SAGE_VERIFY_URL/`. One `[data-testid=paper-row]` (`data-paper=Felipe-2010-Cultivar`) reads "Cultivar mixtures of processing tomato in an organic agroecosystem" and "Felipe-2010-Cultivar.pdf · 14 pages". `[data-testid=paper-status]` has `data-state=none`, reads "Not extracted", and holds `[data-testid=row-extract]`.
- **Picker upload.** `browser_click [data-testid=add-papers]`. A file chooser opens. `browser_file_upload ["/tmp/verify/upload/agronj2001.pdf"]`. A toast reads "Added <file>.pdf", and a row with "6 pages" appears.
- **Drop upload with a non-PDF.** `browser_drop` with target `[data-testid=home]` and paths `[<another pdf>, /tmp/verify/upload/notes.txt]`. Toasts read "Added <file>.pdf" and "Skipped notes.txt: not a PDF".
- **Duplicate.** Drop the same PDF again. The toast reads "<file>.pdf is already in your library", and the row count is unchanged.
- **Search.** `browser_press_key /`, type "switchgrass". Only matching rows stay. A query with no match shows `[data-testid=no-match]`.
- **On disk.** `ls $SAGE_HOME/papers/` lists one folder per paper, each with `paper.pdf`, `paper.md`, and `.sage/` holding `paper.json` and `document.json`.
- **Refused file.** Copy a text file to `/tmp/verify/upload/fake.pdf`, which passes the browser's PDF filter but isn't one. Upload it with a real PDF through the picker. One toast reads "Added <file>.pdf", another reads "Couldn't add fake.pdf" with "fake.pdf is not a PDF", and the PDF's row appears.
- **CLI.** `$S/cli "$root" add add pdfs/Felipe-2010-Cultivar.pdf --json` exits 0 with `.[0].new == false`. `$S/cli "$root" add-mixed add pdfs/Felipe-2010-Cultivar.pdf /tmp/verify/upload/notes.txt --json` exits 1 with two results, the second `{"filename": "notes.txt", "error": {"kind": "invalid", …}}`. Without `--json` it prints one `error:` line for `notes.txt`.
- **Keys.** `browser_press_key j`, then `Enter`. The URL becomes `/papers/<that paper id>`.
- **Proof.** `evidence/ui/home-{light,dark}.png` after the uploads.

## Gotchas

- `browser_drop` dispatches to the element it targets. Target `[data-testid=home]`, which holds the drop handlers and fills the page.
- Toasts stack. Read toast text from `browser_snapshot`, not from the screenshot.
- The paper id is the filename slug, so `agronj2001.pdf` becomes `agronj2001`.
- The row shows the data's state even when a newer attempt failed. "Stopped", "Hit its limit", "Nothing found" and "Failed" appear only when no extraction has data.

## Extract all

Fake keys make Extract all cheap to drive: every run fails at its first model call. Run `sage config set parallel_runs 1` first to keep runs queued long enough to see "N queued". To keep a run live long enough to stop, start it from the CLI with `ANTHROPIC_BASE_URL=http://10.255.255.1:9`, so it hangs on the model call. The runner takes its environment from whoever starts it, so set that before the first run of the session.

- **Extract all.** With two or more papers that have no data, `[data-testid=extract-all]` opens `?extract=1` (`extract-all-dialog`), titled "Extract N papers". It holds the Extract with select (`extract-all-model`: ready agents and your models), "Thinking effort" (`extract-all-model-effort`) under More options, as in the Extract dialog, and for a model the cost line "Stops at $1.00 a paper, $N in all" (`extract-all-limit`); for an agent the line reads "Stops after 60 minutes". `extract-all-start` queues one run per paper with a single `POST /api/runs` that has no `paper_ids` and names what was picked in `using`.
- **Live.** `[data-testid=live-runs]` reads "1 extracting · 2 queued", and a part whose count is zero is left out. When more than one run is live it holds `stop-all`. A row whose run hasn't started reads "Queued…" (`paper-status`, `data-state=live`), and the paper's status line says the same. Queued runs start oldest first as places free up.
- **Stop all.** `stop-all` opens `?stop=1` (`stop-all-dialog`), "Stop N extractions?". `stop-all-confirm` stops every live run. Running ones end `interrupted` and keep what they found, and queued ones end `interrupted` without starting. `sage runs --json` shows it within a second or two.
- **One runner.** `ps aux | grep 'sage runner'` shows exactly one runner however many runs are queued. `$SAGE_HOME/runner.pid` holds its pid and `runner.log` its output. It leaves 30 seconds after its last run ends. The next queued run starts a new one, and so does `sage ui` at launch when queued runs are waiting.
- **One UI.** A second `sage ui` on the same `SAGE_HOME`, on another port, exits 1 with a `conflict`: "SAGE is already open for <home> at <url> (pid N); use that one, or stop it first". `$SAGE_HOME/ui.json` holds that URL and pid.
- **CLI.** `sage extract run PAPER... | --all` queues the runs. With several it prints a line as each ends, and `--no-follow` returns at once. `sage extract stop PAPER... | --all` stops them. `sage extract run --all` with nothing to extract is `invalid`. A `kill -9` of the runner reads its running runs as `interrupted`, while queued runs stay queued until `sage ui` or the next run starts a runner.

## First screen

- `onboarding` replaces the empty drop zone while the library is empty and nothing can extract (what Extract uses by default can't start, and the skill isn't installed). It reads "How should SAGE extract?" over one list: each ready agent (`onboarding-agent`, with its mark), "A model provider" (`onboarding-provider`, to `/manage#providers`) and "From your own terminal" (`onboarding-terminal`). Nothing about it is stored: it shows from the data and goes once something can extract.
- Picking an agent writes `extract_with = "<id>"`, and the drop zone shows. "From your own terminal" runs `POST /api/skill/install` and adds `?setup=terminal`, which shows `terminal-hint` (the line to say and the `sage extract` commands, × closes it) above the drop zone.
- To see it on a machine where the skill is already installed, mock `GET /api/skill` (and `POST /api/skill/install`, so nothing is written to `~/.agents`) with `browser_route` on an empty `SAGE_HOME` and a `SAGE_CONFIG` with no keys.
