# Papers library

Home is a plain list of the library's papers, like Google Drive. A user adds PDFs with "Add papers", by dropping them anywhere on the page, or with `sage add`. SAGE reads each PDF's text layer, title and page count on the way in. Adding the same file again keeps one copy. Files that aren't PDFs are named in a warning toast. Each file has its own result, so one that can't be added gets its own error toast and the rest are still added. Each row says where the paper stands in at most three muted words.

## Sub-features

- `library-list` shows the title, `filename · N pages`, the status, the last activity time and a row ⋯ menu (Open, Extract again, Download PDF).
- `library-status` reads one of "Not extracted" with an inline Extract, "<step>…" with a spinner ("Starting…", "Reading the paper…", "Reading tables…", "Extracting…" or "Checking…"), "N to check", "All checked", or, when no extraction has data, how the last one ended: "Stopped", "Hit its limit", "Nothing found" or "Failed" (only "Failed" in the danger colour), with an inline Extract again (`row-retry`). Over data, a newer attempt that failed or stopped, or a data run that was stopped or hit its limit, adds a warning icon (`latest-failed-hint`) whose `aria-label` is "Last attempt failed", "Last attempt stopped" or the limit.
- `library-search` filters by title and filename as you type. `/` focuses it.
- `library-upload-picker` adds PDFs through "Add papers".
- `library-upload-drop` adds PDFs dropped anywhere, with a "Drop to add" overlay while dragging.
- `library-duplicate` reports a re-added PDF as already in the library.
- `library-upload-errors` gives a file the server refuses its own error toast, "Couldn't add <file>", with the reason, while the others in the batch are added.
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
- **Picker upload.** `browser_click [data-testid=add-papers]`. A file chooser opens. `browser_file_upload ["/tmp/verify/upload/agronj2001.pdf"]`. A toast reads "1 paper added", and a row with "6 pages" appears.
- **Drop upload with a non-PDF.** `browser_drop` with target `[data-testid=home]` and paths `[<another pdf>, /tmp/verify/upload/notes.txt]`. Toasts read "1 paper added" and "Skipped notes.txt: not a PDF".
- **Duplicate.** Drop the same PDF again. The toast reads "1 already in your library", and the row count is unchanged.
- **Search.** `browser_press_key /`, type "switchgrass". Only matching rows stay. A query with no match shows `[data-testid=no-match]`.
- **On disk.** `ls $SAGE_HOME/papers/` lists one folder per paper, each with `paper.pdf`, `paper.json` and `document.json`.
- **Refused file.** Copy a text file to `/tmp/verify/upload/fake.pdf`, which passes the browser's PDF filter but isn't one. Upload it with a real PDF through the picker. One toast reads "1 paper added", another reads "Couldn't add fake.pdf" with "fake.pdf is not a PDF", and the PDF's row appears.
- **CLI.** `$S/cli "$root" add add pdfs/Felipe-2010-Cultivar.pdf --json` exits 0 with `.[0].new == false`. `$S/cli "$root" add-mixed add pdfs/Felipe-2010-Cultivar.pdf /tmp/verify/upload/notes.txt --json` exits 1 with two results, the second `{"filename": "notes.txt", "error": {"kind": "invalid", …}}`. Without `--json` it prints one `error:` line for `notes.txt`.
- **Keys.** `browser_press_key j`, then `Enter`. The URL becomes `/papers/<that paper id>`.
- **Proof.** `evidence/ui/home-{light,dark}.png` after the uploads.

## Gotchas

- `browser_drop` dispatches to the element it targets. Target `[data-testid=home]`, which holds the drop handlers and fills the page.
- Toasts stack. Read toast text from `browser_snapshot`, not from the screenshot.
- The paper id is the filename slug, so `agronj2001.pdf` becomes `agronj2001`.
- The row shows the data's state even when a newer attempt failed. "Stopped", "Hit its limit", "Nothing found" and "Failed" appear only when no extraction has data.
