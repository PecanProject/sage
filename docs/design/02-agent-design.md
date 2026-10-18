# SAGE agent: one loop

The extraction is one loop.

1. **One agent per paper.** It reads the paper view and builds the dataset in a draft workspace.
2. **A deterministic checker** tells it exactly what is wrong or missing after every change.
3. The agent fixes the problems and repeats until the checker passes, then finishes. Code settles every value's status. Scientists get each value with its evidence, its status and the agent's notes.

```
PDF ─► text layer (document.json) ─► table pass + grounding ─► paper view (anchored text, cached prefix)
                                                                    ▼
                                           ┌───────────── agent loop ─────────────┐
                                           │  write ──► checker ──► errors / gaps │
                                           │    ▲                        │        │
                                           │    └────────── fix ◄────────┘        │
                                           └──────────────── finish ──────────────┘
                                                                    ▼  accepted only when no open blocking issue is left
                                           settle ─► records.json ─► scientist review
```

The IR, the anchors, the evidence rules, the checker levels, the statuses and the causes are defined in `../domain.md`. This page covers how the loop uses them. The code is in `sage/agents/` and `sage/pipeline/`.

## Paper view

`sage/agents/paper_view.py` renders the parsed paper once as anchored text. `[p6.b4]` is a text block. A table shows as its grid, with `p6.t1` for the table and `p6.t1.r1.c2` for a cell. Every anchor maps to a page and a box.

- Running heads, page numbers, margin text and the reference list are dropped. Page-1 header lines and lines that hold a DOI stay, so the Citation can be filled.
- Each table line says how well it grounded: every cell matched, a share matched, or not transcribed. A table with no cells points the agent at `view` with the table's region.
- The domain guide (`sage/agents/guide.md`) comes first, then the paper view. Together they are the agent's instructions.

## Table pass and grounding

The run's `tables` stage runs before the agent, on every table still `pending` in `document.json`.

1. The table model (`table_model`, default Haiku 5.5 at `low` effort) gets an upright crop of the table region at `table_render_scale`, plus the text-layer words inside it. It returns the rows as cell strings, the header row count and the table notes. Calls run in parallel up to `max_concurrency`.
2. `sage/parse/grounding.py` matches each cell string to text-layer characters inside the region, in reading order, after normalisation. An exact match comes first. A fuzzy match needs a similarity of at least `grounding_fuzzy_threshold`.
3. A table whose matched share falls under `table_min_grounded` is `partial` or `failed`. Its unmatched cells keep the transcribed text. Settle flags any evidence that cites an unmatched cell or a cell that matched only fuzzily.

The result is cached in `document.json`. A reparse after a `PARSER_VERSION` change carries each matching table's transcription across, and a `GROUNDER_VERSION` change grounds it again, so neither calls the model. Each run keeps the document it was extracted against. The table pass counts toward the run's cost budget.

## Tools

The agent's tools are in `sage/agents/extractor.py`. Each one acts on the workspace (`sage/pipeline/workspace.py`) through the `Workspace` protocol in `sage/agents/contract.py`.

| Tool | What it does | Returns |
|---|---|---|
| `write(records)` | Adds or replaces records of any type, many per call. `{"delete": "<id>"}` removes one. Each record is checked at once. | Per id, pass or fail with the exact errors, warnings, and any targets the same-error rule marked unresolved. Plus the count of open blocking issues. |
| `expand_table(table_id, roles, irrelevant_reason)` | The agent names each column's role (treatment, factor, variable, time, depth, unit, n, value or ignore) and the shared ids. Code writes one Observation per value cell, with cell-level evidence. A second call replaces the table's earlier Observations. `irrelevant_reason` instead marks a table that holds no experiment values. | The count, failed and empty cells, skipped rows, the errors and 3 sample rows |
| `check()` | Runs the full checker | Open blocking issues and warnings, with totals |
| `read(ids)` | Reads records back from the workspace, at most 40 per call | The records in full, and any ids the workspace doesn't hold |
| `view(page, bbox)` | Renders a page, or a region of it, as an image | A line naming the region and the image, both in the tool result, for figures and for tables that did not ground |
| `finish(summary, unresolved)` | Ends the run. It is the agent's output tool. `unresolved` lists marks on records, null fields or tables, each with a cause the agent may set and a reason. | Accepted, or refused with the valued targets it can't mark or the open blocking issues listed |

- The workspace is `draft.json`, saved after every change. The conversation does not hold the state.
- Code recomputes derived values (coordinates, dates, numbers in `reported_text`, unit conversions) on every write. Arithmetic from the model is overwritten and logged. A derived value code cannot recompute is dropped, and the record level says what to fix. A date takes its year only from the field's own quotes.
- `expand_table` names Observations `obs-` plus the cell anchor with dots turned into dashes, for example `obs-p6-t1-r1-c2`.
- `finish` refuses a mark on a field that holds a value, so a mark never hides a failing value. It then stores the marks and refuses while any open blocking issue remains. Each refusal goes back to the model through `ModelRetry`.

## Checker

The checker is code in `sage/pipeline/checker/`. `../domain.md` lists every rule. Before it runs, `write` rejects a record that does not parse or breaks a construction invariant.

| Level | Catches |
|---|---|
| Evidence | A missing anchor, a quote that is not verbatim, a number or text value that is not in its quote, a unit that is not stated, a null value or an inference with no note |
| Record | Required content per entity, such as an Observation with no value, variable or method. A record whose fields are all null. A derived value code could not recompute. |
| Dataset | References that do not resolve or point at the wrong type, duplicate Observations, and records that share a name (a warning) |
| Coverage | A table neither expanded nor marked irrelevant, a Variable with no Observations, and a factor level with no Treatment. A Results number that no record covers is a warning only. |

Every message says what to do next, for example `quote '85.3' not found in p6.t1.r1.c2; the text there is '85.2 a'`.

## Context engineering

- **The conversation is append-only.** SAGE never clears, summarises or rewrites the history, so every turn's prompt starts with the whole of the previous one. Tool results and `view` images stay in context.
- **The workspace holds the state.** A tool returns ids, counts and errors, never full records echoed back. The exception is `read(ids)`, which returns the records the agent asks for, at most 40 at a time, so it can change one without rewriting it blind. Error lists are deduplicated and capped at `error_list_cap` (default 12), then a count of the rest.
- **No context limit of SAGE's own.** The whole paper view always goes into the instructions, however long the paper. The model's context window (1M tokens on Haiku 5.5, Sonnet 5.5 and Opus 5.5) is the only limit, and the API enforces it. A paper or conversation past it fails with the API's error. On Haiku 5.5, a request whose prompt passes 100,000 tokens costs five times as much per token, cache reads included.
- **Resume.** A resumed run starts with a message that lists the records and table decisions the draft already holds.
- **Guidance.** A resumed or continued run's first message adds the scientist's guidance and review notes after that list. A continued run is a new run that starts from a copy of an earlier run's draft and document (`core.continue_run`). It doesn't replay the earlier conversation.

## Prompt cache

`models.cached` in `sage/agents/models.py` puts 5-minute cache breakpoints on the last tool definition and the last instruction block, plus an automatic one on the conversation. Each turn reads everything the turn before cached and writes only what it added. A resumed run, or a new run of the same paper within 5 minutes, reads the prefix again. The table pass caches only its instructions. `05-prompt-caching.md` explains how the cache works and the rules that keep it hitting.

## Edge cases

| Case | Handling |
|---|---|
| Scanned or image-only pages | The parser marks pages with no text layer. There is no OCR. The agent reads them with `view`, and settle flags evidence on them. |
| Rotated tables | Grounding and cropping work in the table's upright frame, so a landscape table grounds like any other. |
| Tables that fail to ground | The paper view says so. A `partial` or `failed` table that has cells still expands, and settle flags evidence on a cell that matched only approximately or not at all. A table with no transcribed cells can't be expanded: `expand_table` refuses and points at `view`. The agent reads it with `view`, writes its Observations by hand with block anchors, and marks the table with an `irrelevant_reason` that says so. |
| Data shown only in figures | Not extracted as values. The agent marks each such Variable unresolved with cause `figure_only`. |
| Several studies, sites or years in one paper | One Study per experiment. Treatments point at their Study. |
| Text and table disagree | The guide tells the agent to use the table's value and put the conflict in the note. |
| Review or modelling paper with no primary data | `finish("no extractable experiments", ...)` with the reason |
| Very large tables | `expand_table` does the work in code. The tool returns counts and samples only. |
| Pooled rows such as "Mean" or "LSD" | `expand_table` skips statistic rows and reports summary rows as errors, so the agent adds them to `skip_rows`. |
| The agent repeats the same error | After `same_error_limit` (default 3) repeats, the target is marked unresolved with cause `repeated_error` and the agent moves on. |
| The agent finishes too early | The finish gate and the coverage rules refuse it. |
| Made-up anchors or quotes | Caught by the evidence rules |
| Provider error | The run fails with a message a scientist can act on, such as a rejected key or rate limiting. `sage run --resume <run_id>` continues from the draft. |
| Crash or killed worker | The run reads as `interrupted`. `sage run --resume <run_id>` continues from the draft. |
| Stopped by the scientist | `sage stop` interrupts the worker, which settles the draft and records `interrupted`. |
| The agent missed something | `sage run --continue <run_id> --guidance "…"` starts a new run from that run's draft, with the guidance and review notes. |
| Cost running away | The turn and cost budgets stop the agent, and code settles the run. |
| A file that is not a PDF, or a PDF that will not open | `sage add` refuses it. |
| The same PDF twice | `sage add` returns the existing paper, matched on sha256. |

## Stopping and limits

- **Finish** is accepted when no open blocking issue is left. An issue is open until it is fixed or its target is marked unresolved with a cause and a reason. The agent can mark only a target that holds no value.
- **Default budget per paper:** `max_turns` 40 and `max_cost_usd` $1.00, both settings. pydantic-ai's `UsageLimits` enforces them. The table pass spends from the same cost budget first.
- **A continued run has a budget of its own.** It is a new run, so it gets the full maximums.
- **A resumed run shares the budget.** It gets the maximums less the turns and cost the run already spent. `core.resume_run` refuses a run with nothing left and points to raising the limit or continuing the run. If the table pass uses up the rest, the run ends `budget_exhausted` with a note and the agent makes no request.
- **When the budget runs out, code settles the run.** Fields that pass keep their status. Fields with a blocking issue become unresolved with cause `budget_exhausted` and carry their last error.
- **A failed run settles too.** It writes `records.json` from whatever draft it has, or with no records when the paper never parsed. The stage that raised emits no `stage.finished`.
- **The agent never sets a status.** Settle does, from the checker, the evidence and the unresolved marks.

## What the scientist gets

- **Each field:** the value, its evidence drawn as boxes on the PDF, its basis and note, and its status (verified, flagged, or unresolved with a cause and a reason). `records.json` also lists every value code overwrote.
- **Queue order:** every record, unresolved first, then flagged, then verified, and by entity type within each.
- **Review actions:** accept, edit and reject append to `reviews.jsonl`. "Accept N good" accepts every unreviewed verified record at once.

## Estimate

A paper takes about 8 to 15 tool calls, $0.10 to $0.50 on Haiku 5.5, and 3 to 8 minutes. These numbers are projections, not measurements.
