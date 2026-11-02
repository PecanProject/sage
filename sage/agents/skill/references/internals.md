# How SAGE works

Everything here is SAGE's business, not yours. It is here so you know what your files feed and what each command does.

## The library

`$SAGE_HOME` (default `~/sage-data`) holds `papers/<paper id>/`, one folder per PDF. `sage papers` lists them and `sage extract status` shows which have an open run.

```
papers/<id>/
  paper.pdf                  the PDF as added
  paper.md                   the paper's text with anchors; rewritten when tables/*.csv or pages/*.md change
  pages/pN.png               every page at a fixed scale
  pages/pN.md                yours: scan pages only
  tables/<anchor>.png        each detected table, cropped and upright
  tables/<anchor>.csv        yours: the table's grid
  figures/<anchor>.png       each figure SAGE could locate
  records/                   yours: the records
  .sage/                     SAGE's own state; never edit it
    document.json            the parsed PDF: blocks, tables, the text layer every quote is checked against
    runs/<run id>/           run.json, events.jsonl, draft.json, records.json, and the run's document
    snapshots/<n>/           copies of records/ and tables/*.csv
    reviews.jsonl            the scientist's reviews
```

Check and done read only `.sage/document.json` and your files. Editing `paper.md` changes nothing: a quote is checked against the PDF's text layer.

## Runs and drivers

A run is one extraction session, from start to done or stop. It records who drove it:

| Driver | Who | How it starts |
|---|---|---|
| SAGE's loop | SAGE calls a model with an API key | Extract in the UI, or `sage extract run` |
| An agent | SAGE starts Claude Code, Codex, Gemini or another ACP agent in the paper's folder | Extract in the UI with an agent picked, or `sage extract run --with <agent>` |
| A terminal | You, in someone's terminal | `sage extract start <paper>` |

Every driver ends the same way, through `sage extract check` and `sage extract done`, so a record is judged the same whoever wrote it. A new run carries on from the folder as the last run left it. Nothing is reset.

Commands, each with `--json`:

| Command | Does | Exit |
|---|---|---|
| `sage extract start <paper> [--guidance "…"]` | opens a terminal run, or reuses the open one | 0, or 1 when SAGE's own run holds the paper |
| `sage extract check <paper> [--all]` | checks the folder; `--all` lists every finding, not the first `error_list_cap` blocking errors and warnings | 0 clean, 1 blocking, 2 a file won't parse |
| `sage extract done <paper> -m "…"` | checks, then settles the run | 0 accepted, 1 refused |
| `sage extract stop [<paper>…]` | ends open runs as interrupted and settles what the folder holds; your files stay | 0 |
| `sage extract status [<paper>]` | each paper's open or last run | 0 |

`done` opens a run itself when none is open.

## Check

Check reads your files, builds the records, and runs the checker at four levels:
- **Evidence:** every anchor exists and its text holds the value.
- **Record:** each record's own rules.
- **Dataset:** references resolve, ids aren't duplicated, a treatment has one value per variable, site, time and depth, and scopes agree with treatments.
- **Coverage:** every table is mapped or irrelevant, every study has a treatment, every variable is measured or marked, and no Results number goes uncited.

Check never rewrites your files. When the files changed since the last snapshot and they parse, it saves a snapshot, so earlier states can be restored.

**The same-error rule.** Check counts each blocking error by target and message, but only when you edited the file it points at since the last check: that was an attempt at a fix. When one survives several attempts, SAGE marks the target unresolved with cause `repeated_error`, and check lists it as marked. It stops blocking and stops being reported, so move on. The mark lifts as soon as the error is gone.

**Guidance and reviews.** A scientist can start a run with guidance, and review records in the UI. Check prints both first while the run is open.
- A rejected record blocks until you change it or delete it.
- An edit is a warning carrying the scientist's value. Find it in the paper and cite it, or keep yours and say why in a note.
- A review stops applying once the record changes.

## Done and settle

`done` checks once more and is refused while anything blocks. When it is accepted, SAGE settles the run: every field gets a status.

| Status | Means |
|---|---|
| verified | every check passed on stated or derived evidence |
| flagged | it passed, but a person should look: the basis is inferred, a warning stands, or the evidence comes from an image (a scan page or a table cell the text layer doesn't hold) |
| unresolved | a mark stands on it, or it is null; it carries a cause and a reason |

You never set a status, and you never compute what code computes: quotes, bases, parsed numbers, dates, depths, coordinates, event types and observation ids. Code recomputes them from what you point at, so a wrong pointer shows as a check error rather than a wrong value.

## The live UI

The scientist may have SAGE's UI open. It watches the paper's folder, so the records you write show up as you save them, with the statuses they would settle to, and each check appears in the run's log. They can stop the run from there, which ends it as interrupted. Your files stay, and the next run carries on from them.
