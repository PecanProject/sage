# SAGE agent: files and a check

An extraction is an agent editing files in the paper's folder until a deterministic check passes.

1. **One agent per paper.** It reads `paper.md` and the images, and writes the records as YAML, the tables it needs as CSV, and the scan pages it cites as text.
2. **`sage extract check`** compiles the files, runs the checker, and says what is wrong or missing, each at the `file:line` to fix.
3. The agent fixes them and checks again until nothing blocks, then runs **`sage extract done`**. Code settles every value's status. Scientists get each value with its evidence, its status and the agent's notes.

```
PDF ─► text layer (.sage/document.json) ─► paper.md + pages/*.png + tables/*.png   (code, before the agent)
                                                   ▼
                          ┌──────────────── the agent, any driver ────────────────┐
                          │  edit records/*.yaml, tables/*.csv, pages/*.md          │
                          │    ▲                           │                        │
                          │    └── fix ◄── sage extract check (compile ► checker)   │
                          └───────────────────── sage extract done ─────────────────┘
                                                   ▼  accepted only when no open blocking issue is left
                          settle ─► records.json ─► scientist review
```

The folder, the field shapes, the observation groups, the IR, the anchors, the evidence rules, the checker levels, the statuses and the causes are defined in `../domain.md`. The agent learns them from the SAGE skill, `sage/agents/skill/` (`SKILL.md` plus `references/`). This page covers the drivers and the loop. The code is in `sage/agents/` and `sage/pipeline/`.

## Drivers

Every driver works on the same folder and passes the same boundary: `sage extract check <paper>` and `sage extract done <paper> -m "…"`, which call `pipeline/extraction.py`. A run records its driver (`RunInfo.driver`).

| Driver | What runs | Started by | Code |
|---|---|---|---|
| `sage` | SAGE's own pydantic-ai loop, on a model from the Manage page | Extract in the UI, `sage extract run <paper> [--with <model>]` | `pipeline/run.py`, `agents/extractor.py` |
| `agent` | An ACP agent SAGE spawns in the folder: Claude Code, Codex, Gemini, OpenCode, or one added in `agents` settings | Extract with an agent picked, `sage extract run <paper> --with claude[:model]` | `pipeline/agent_run.py`, `agents/acp_client.py` |
| `here` | An agent in someone's own terminal, with the SAGE skill installed | `sage extract start <paper>`, or `done` with no open run | `pipeline/extraction.py` |

`--with` takes an agent id, `agent:model`, or a model id. The `extract_with` setting is its default, else the first of your models.

### SAGE's own loop

`extractor.build` gives the model the folder through pydantic-ai-harness's `FileSystem` capability, rooted at the folder:

| Tool | From | What it does |
|---|---|---|
| `read_file`, `list_directory`, `find_files`, `search_files`, `file_info` | `FileSystem` | Read and search the folder |
| `write_file`, `edit_file`, `create_directory` | `FileSystem` | Write the agent's files. `.sage/` and `paper.pdf` are denied; `paper.md`, the PNGs and `figures/` are read-only. |
| `view_image(path)` | SAGE | A page, table or figure PNG as an image |
| `check()` | SAGE | `sage extract check`'s text. Runs alone, never beside an edit. |
| `done(summary)` | SAGE, the output tool | `sage extract done`. A refusal goes back to the model through `ModelRetry`. |

The instructions are the skill, its references and `paper.md`, in that order (`extractor.instructions`), so they are the same for every run of a paper and the prompt cache holds them. The first message says what the run is for: start, resume with what the folder holds, or the guidance.

### An agent SAGE starts

`acp_client.session` spawns the agent's command (`AgentConfig.command` and `args`) over stdio with a trimmed environment: `PATH` with SAGE's own `sage` first, `HOME`, `SAGE_HOME`, and the agent's own variables. It opens one session in the folder, sets the mode, the model and the effort from the options the agent offers, matched on category, and prompts it to extract the paper with the SAGE skill, pointing at the installed skill when it is current and at the shipped one otherwise. When the agent ends its turn before `done`, SAGE prompts it again, at most `NUDGES` times. Its messages, thinking, tool calls and plan go to the run's log as they stream; usage and cost come from the agent's own reports.

**Effort.** Each thinker has its own effort, in its own words. SAGE's own loop runs one of your models at that model's `effort` (`YourModel.effort`), one of the levels the model has (`ModelOption.efforts`; none for a model that doesn't think). An agent thinks at its `AgentConfig.effort`, a value of the agent's own `thought_level` session option, or at its own default when that is unset. An agent's levels depend on its model, so `_configure` sets the mode and the model first and reads the levels from the options the agent returned for the last change, not from the ones the session opened with. A run's `effort` overrides either (`--effort`, `effort` in the API) and is kept in `run.json` with `effort_given`:

- For SAGE's own loop, a run's effort the model lacks is refused when the run is queued. A continued run takes the earlier run's effort while the new model has it, else the model's own.
- For an agent, an effort asked for the run (`effort_given`) that the model lacks fails the run with an error that lists what the agent offers, like a model or mode it doesn't offer. An effort that came from `AgentConfig.effort` or from the run it continues, on a model that lacks it, is a warning note in the log, and the agent thinks at its default, so changing an agent's model never stops its runs. An agent that offers no `thought_level` option for the model, such as Codex on its default model, has no effort choice.
- `sage agents offer AGENT --model M` (`GET /api/agents/{id}/offer?model=M`) starts the agent, sets the model and lists its levels, without prompting it.

The agent runs under its own sign-in, so SAGE holds no key for it. `core.agents` says whether each one is installed and signed in, using `AgentConfig.requires` and `login_check`, and gives the command that fixes it.

**Permissions.** By default SAGE allows every request the agent makes: it runs whatever it wants, under its own sign-in. The prompt tells it never to edit `.sage/`, `paper.pdf` or `paper.md`. With the `agents_ask` setting on, SAGE answers each request itself (`agent_run._rule`):

- reads inside the folder or the skill directories, edits of the agent's own files, `mkdir` inside the folder, `sage extract check|done|status` with the read-only `sage` commands, and read-only shell commands (a `for` loop over them included) that name only those paths are allowed. A `$` other than a loop's own variable, a `cd` out of the folder, a read that names no path, or an option that writes or runs something (`find -delete`, `sort -o`, a `sed` script beyond printing lines) asks;
- an edit of `.sage/`, `paper.pdf` or `paper.md` is refused;
- a web fetch, a script, a shell write, and any other `sage` command (settings, providers, starting or stopping runs) ask. They are written to the run's `permission.json` and waits for the scientist, in the UI or with `sage extract answer <paper> --allow|--deny`. The run shows `waiting` meanwhile. One request is open at a time.

**Time limit.** `AgentConfig.time_limit_minutes` (default 60) ends the run `budget_exhausted` with limit `time`. The agent spends on its own account, so SAGE sets no turn or cost budget.

### A terminal run

`sage extract start` opens a run with driver `here` and prints the run and the folder. The agent in that terminal works the folder like any other and ends with `done`. `sage extract stop` ends it `interrupted` and settles what the folder holds. The skill installs with `sage skill install` into `~/.agents/skills/sage` and `~/.claude/skills/sage`.

## Paper view

`sage/agents/paper_view.py` renders the parsed paper as `paper.md`. `[p6.b4]` is a text block. A transcribed table shows as its grid, with `p6.t1` for the table and `p6.t1.r1.c2` for a cell. Every anchor maps to a page and a box.

- Running heads, page numbers, margin text and the reference list are dropped. Page-1 header lines and lines that hold a DOI stay, so the Citation can be filled.
- A table with no CSV yet points at `tables/<anchor>.png` and the CSV to write. Each transcribed table says how well it grounded.
- A page with no text layer says so and points at its PNG and `pages/p<N>.md`.
- `paper.md` is rewritten when a CSV or a page transcription changes, so the anchors the agent cites exist.

## Tables and grounding

There is no table model. The agent transcribes a table it needs into `tables/<anchor>.csv`: a `# header_rows: N` line, optional `# notes:` lines, then the grid as printed. Check grounds it (`paper_files.effective_document`):

1. `sage/parse/grounding.py` matches each cell to text-layer characters inside the table's region, in reading order, after normalisation. An exact match comes first. A fuzzy match needs a similarity of at least `grounding_fuzzy_threshold`.
2. A table whose matched share falls under `table_min_grounded` is `partial` or `failed`. Its unmatched cells keep the CSV's text. Settle flags any evidence that cites an unmatched cell or a cell that matched only fuzzily.

The grounding is cached in `.sage/tables/` until the CSV, the grounding code or its settings change. Each run keeps the document its records were checked against.

A table group (`../domain.md`, Observation groups) maps a grounded table to Observations in code: rows × columns, with what each row and column shares. A table of 300 cells is a dozen lines of YAML.

## Checker

The checker is code in `sage/pipeline/checker/`. `../domain.md` lists every rule. Compile (`sage/pipeline/compile.py`) comes first: a file that doesn't parse, a field in no known shape, or a record that breaks a construction invariant is a blocking finding at its line, and the record is left out.

| Level | Catches |
|---|---|
| Evidence | A missing anchor, a quote that is not verbatim, a number or text value that is not in its quote, a unit that is not stated, a null value or an inference with no note |
| Record | Required content per entity, such as an Observation with no value, variable or method. A record whose fields are all null. A derived value code could not recompute. |
| Dataset | References that do not resolve or point at the wrong type, duplicate Observations, and records that share a name (a warning) |
| Coverage | A table neither mapped nor marked irrelevant, a Study with no Treatment, a Variable with no Observations, and a body row of a mapped table that holds a number but is neither mapped nor skipped. A Results number that no record covers is a warning only. |

Every message says what to do next, for example `records/observations/p6.t1.yaml:9  obs-p6-t1-r1-c2: quote '85.3' not found in p6.t1.r1.c2; the text there is '85.2 a'`.

`gate.evaluate` applies the same-error rule and the agent's marks. A check during an open run snapshots the files when they changed and compile, records the count and the draft, and emits `check.ran`, which the UI shows in the run's log.

## Context engineering

- **The folder holds the state.** The conversation does not. Any driver can pick up a folder another left, and a run that stops keeps its work.
- **Point, don't transcribe.** The agent writes a value and the anchors that hold it. Code reads the quote, decides the basis, and recomputes derived values (coordinates, dates, numbers in `reported_text`, unit conversions). Arithmetic from the model is overwritten and logged.
- **The native conversation is append-only.** SAGE never clears, summarises or rewrites the history (`rules/append-only-history.yml`), so every turn's prompt starts with the whole of the previous one.
- **Check output is capped.** Findings are listed blocking first, at most `error_list_cap` (default 12) of each severity, then a count of the rest.
- **No context limit of SAGE's own.** The model's context window is the only limit, and the API enforces it. On Anthropic, for example, a Haiku 5.5 request whose prompt passes 100,000 tokens costs five times as much per token, cache reads included.
- **Guidance and reviews come through check.** A run's guidance goes in its first message, and check prints it and the scientist's standing reviews first while the run is open. A continued run is a new run on the folder as it stands; it doesn't replay the earlier conversation.

## Prompt cache

`models.cached` in `sage/agents/models.py` turns on pydantic-ai's unified cache: the tool definitions, the instructions and the growing conversation. Each turn reads everything the turn before cached and writes only what it added. A resumed run, or a new run of the same paper within 5 minutes, reads the instructions again. `05-prompt-caching.md` explains how the cache works and the rules that keep it hitting. ACP agents cache on their own.

## Edge cases

| Case | Handling |
|---|---|
| Scanned or image-only pages | The parser marks pages with no text layer. There is no OCR. The agent reads the page PNG and transcribes the blocks it cites into `pages/p<N>.md`. Settle flags evidence on them. |
| Rotated tables | Grounding and the table PNG work in the table's upright frame, so a landscape table grounds like any other. |
| Tables that fail to ground | `paper.md` says so. A `partial` or `failed` table still maps, and settle flags evidence on a cell that matched only approximately or not at all. |
| Data shown only in figures | Not extracted as values. The agent marks each such Variable unresolved with cause `figure_only`. |
| Several studies, sites or years in one paper | One Study per experiment. Treatments point at their Study. |
| Text and table disagree | The skill tells the agent to use the table's value and put the conflict in the note. |
| Review or modelling paper with no primary data | `done -m "no extractable experiments: …"` |
| Very large tables | One table group maps the grid. Check reports counts and the first findings only. |
| Pooled rows such as "Mean" or "LSD" | The agent lists them in the group's `skip`. An unmapped row with a number blocks until it is mapped or skipped. |
| The agent repeats the same error | When the same error survives 3 edits of its file (`gate.SAME_ERROR_LIMIT`), the target is marked unresolved with cause `repeated_error` and the agent moves on. The mark lifts once the error is gone. |
| The agent stops too early | The done gate and the coverage rules refuse it. An ACP agent that ends its turn before `done` is prompted again. |
| Made-up anchors or quotes | Caught by the evidence rules |
| The agent edits SAGE's files | `FileSystem` denies it in the native loop; the ACP permission rule refuses it. Check reads only `.sage/document.json`, so editing `paper.md` changes nothing. |
| Provider error | The run fails with a message a scientist can act on. `models.explain` names the provider that answered (a gateway or local server by its host), takes its own message out of the error body and says what to do: check the key, check that a Bedrock model is enabled in the region, pick another model for a 404, add credit for an exhausted quota, or wait for a plain rate limit. `sage extract run --resume <run_id>` continues on the folder. |
| The provider refuses the key or the model | A 401, 403, 404, Google's bad-key 400 or an exhausted-quota 429 (`models.refused`) would fail every call the same way, so the run stops at once. `RunInfo.refused` is set, and the Paper screen offers Fix setup before the resume action. |
| An agent isn't installed or signed in | `sage extract run` refuses it. A queued run whose agent stopped being ready ends `failed`, with `refused` set, before it starts, and with the command that fixes it. |
| An agent's own error, such as a usage limit | The run ends `failed` with the agent's message, and settles what the folder holds. |
| Crash or killed runner | A running run reads as `interrupted`, since its pid is dead. Continue or resume it; the folder keeps its work. A queued run stays queued, and the next runner takes it on: `sage ui` starts one at launch when queued runs are waiting. |
| Stopped by the scientist | `sage extract stop` writes a stop request in the run's folder. The runner cancels the run, which settles what the folder compiles to and records `interrupted`. A queued run with a stop request ends `interrupted` without starting. |
| The agent missed something | `sage extract run --continue <run_id> --guidance "…"` starts a new run on the folder, with the guidance. |
| Cost running away | The native loop's turn and cost budgets stop it; an ACP agent's time limit stops it. Code settles the run. |
| A file that is not a PDF, or a PDF that will not open | `sage add` refuses it. |
| The same PDF twice | `sage add` returns the existing paper, matched on sha256. |

## Stopping and limits

- **Done** is accepted when no open blocking issue is left. An issue is open until it is fixed or its target is marked unresolved with a cause and a reason. The agent can mark only a target that holds no value.
- **The native loop's budget per paper:** `max_turns` 150 and `max_cost_usd` $1.00, both settings. pydantic-ai's `UsageLimits` enforces them. Each file read, edit or check is a turn.
- **An ACP agent's budget** is its time limit.
- **A continued run has a budget of its own.** It is a new run, so it gets the full maximums.
- **A resumed run shares the budget.** It gets the maximums less the turns and cost the run already spent. `core.resume_run` refuses a run with nothing left and points to raising the limit or continuing the run.
- **When the budget runs out, code settles the run.** Fields that pass keep their status. Fields with a blocking issue become unresolved with cause `budget_exhausted` and carry their last error.
- **A failed or stopped run settles too**, from what its folder compiles to, or with no records when the paper never parsed.
- **The agent never sets a status.** Settle does, from the checker, the evidence and the unresolved marks.

## What the scientist gets

- **Each field:** the value, its evidence drawn as boxes on the PDF, its basis and note, and its status (verified, flagged, or unresolved with a cause and a reason). `records.json` also lists every value code overwrote.
- **Live progress.** The UI watches the folder, so records show up as the agent saves them, with the statuses they would settle to, and each check shows in the run's log.
- **Queue order:** every record, unresolved first, then flagged, then verified, and by entity type within each.
- **Review actions:** accept, edit and reject append to `reviews.jsonl`. "Accept N good" accepts every unreviewed verified record at once. During a run, check passes them on to the agent.

## Measured

On the three-paper gate (2026-10-25), the native loop on Haiku 5.5 spent $0.10 to $0.33 a paper. Claude Code and Codex, over ACP or in a terminal, extracted more values per paper with fewer flagged. These figures come from three papers, so treat them as a rough guide.
