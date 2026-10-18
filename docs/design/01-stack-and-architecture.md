# SAGE: stack and architecture

SAGE reads agronomy research papers (PDFs) and extracts the experiment records into a typed schema, the IR. Every value carries evidence that points at the text it came from. Scientists review the results against the paper.

SAGE ships as one Python package, `sage`. It holds a Typer CLI and a FastAPI app, both on a shared `sage/core.py`, plus a prebuilt React UI that the same process serves. Data lives in plain files. There is no database, no extra service, and no Node at runtime.

This page covers the stack, the package layout and its layer rules, the event stream, settings, storage and the two surfaces. The agent loop is in `02-agent-design.md`. The UI is specified in `04-ui.md`. The IR, the evidence rules and the checker are defined in `../domain.md`.

---

## 1. Design principles

1. **One core, two surfaces.** Every user action is a function in `core.py`. `cli.py` and `api.py` only parse input, call `core`, and format the output. Whatever the UI can do, the CLI can do too.
2. **Code does the mechanical work, the model does the judgement.** Parsing cells, converting coordinates and units, reading dates, checking evidence and settling statuses are deterministic Python. The model reads the paper, interprets it and decides what each table means.
3. **Nothing unchecked gets saved as verified.** Every record passes the schema and the checker. Failures go back to the model as exact errors. Code, never the model, sets each value's status.
4. **Everything can be observed.** Each run writes a live event stream. The UI, `sage logs` and coding agents all read it.
5. **Mistakes become rules.** A mistake that repeats becomes a ruff or ast-grep rule, not a one-off patch.
6. **The model is a setting.** Claude Haiku 5.5 is the default. The extraction model and the table model are separate settings, and so is the effort for each.

---

## 2. Stack

### Backend: Python 3.13

| Concern | Choice | Notes |
|---|---|---|
| Env, packaging | **uv** with the **hatchling** backend | `uv.lock` is committed. The built UI in `sage/web_dist/` goes into the wheel. The package is named `sage`. That name is taken on PyPI, and SAGE is not published. |
| Lint and format | **ruff** | `select = ["ALL"]` with a short ignore list. Nested `ruff.toml` files enforce the layer rules (§3). |
| Types | **ty** | Every rule is an error. |
| LLM | **pydantic-ai-slim[anthropic]** | Typed outputs, Python functions as tools, `ModelRetry` for checker feedback, `UsageLimits` for the budgets, Anthropic prompt-cache breakpoints and cache diagnostics through model settings. |
| Default model | `anthropic:claude-haiku-5-5` through the Anthropic API | Only Anthropic model ids are accepted. Bedrock is out of scope. |
| Data models | **pydantic v2** | The IR, and the model of every file on disk |
| Settings | **pydantic-settings** | Environment, then `config.toml`, then defaults (§6) |
| CLI | **Typer** + **Rich** | Every command takes `--json`. |
| HTTP | **FastAPI** + **uvicorn** | One process serves the API and the built UI. |
| Live stream | `fastapi.sse` (`EventSourceResponse`, built into FastAPI) | The SSE route reads a run's `events.jsonl` from a byte offset (§5). There is no sse-starlette. |
| PDF | **pdftext** + **pypdfium2** | pdftext gives the text layer with boxes. pypdfium2 renders pages and crops. |

### Frontend: `web/`

| Concern | Choice |
|---|---|
| Package manager | **pnpm** |
| Build | **Vite** + **React 19** + **TypeScript** (strict, with `noUncheckedIndexedAccess`) |
| UI kit | **shadcn on Base UI** + **Tailwind v4** |
| Routing, server state | **TanStack Router** + **TanStack Query** |
| API client | **openapi-typescript** + **openapi-fetch** + **openapi-react-query**, generated from FastAPI's OpenAPI schema |
| Grid | **TanStack Table** |
| PDF | **react-pdf**, loaded lazily, with the evidence drawn over each page. **vite-plugin-static-copy** puts pdf.js's wasm, standard fonts, CMaps and colour profiles under `/pdfjs/`, so scanned papers and fonts a PDF doesn't embed render. |
| Icons, type | **Phosphor** icons, fonts from **@fontsource-variable** so the app works offline |
| Quality | **Biome**, **React Compiler** (Babel preset, no manual memo), **React Doctor** and **knip**, all in `scripts/check` |
| Keys | **tinykeys** |

### Repo tooling

There is no task runner and no git hook. Two POSIX shell scripts cover the work.

| Script | Job |
|---|---|
| `scripts/check` | Runs ruff check, ruff format `--check`, ty, ast-grep scan, a gitleaks secret scan, Biome, tsc, React Doctor, knip, and a diff that fails when `web/src/api/schema.d.ts` no longer matches `sage openapi`. It stops on the first failure. |
| `scripts/dev` | Runs uvicorn with `--reload`, the Vite dev server, and a watcher that regenerates `web/src/api/schema.d.ts` on every Python change. Vite proxies `/api` to the API. `SAGE_API_PORT` moves the API off port 8765 when `sage ui` already holds it, `SAGE_WEB_PORT` moves Vite off 5173, and `SAGE_WEB_HOST=0.0.0.0` serves it beyond this machine. |

ruff, ty, ast-grep (from the `ast-grep-cli` PyPI package) and watchfiles are uv dev dependencies. watchfiles is what `uvicorn --reload` and the schema watcher in `scripts/dev` watch files with. Biome is a pnpm dev dependency.

---

## 3. Architecture

```
┌──────────────────────────────── sage (one process) ──────────────────────────────────┐
│  cli.py  (Typer)  ──┐                                                                │
│                     ├──► core.py ──► pipeline/ ──┬──► agents/   pydantic-ai          │
│  api.py  (FastAPI) ─┘       │                    ├──► parse/    PDF -> Document      │
│    /api/*                   │                    ├──► domain/ + ir/   pure logic     │
│    /api/runs/{id}/stream    │                    │                                   │
│    /*  -> web_dist          └──────► store/ ◄────┘   files + events.jsonl            │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

A run does not execute inside the process that asked for it. `core.start_run` starts a detached worker (`python -m sage worker <run_id>`), which waits on its stdin. `core` writes `run.json` as queued with the worker's pid, then closes the pipe. Only then does the worker write `run.json`, so the two never overwrite each other, and a worker that dies early reads as interrupted. The worker outlives the terminal or the server that started it. `sage run` and the UI both follow the run through its `events.jsonl`.

### Package layout

```
sage/                         repo root
├─ pyproject.toml  uv.lock  ruff.toml  sgconfig.yml  AGENTS.md  CLAUDE.md -> AGENTS.md
├─ scripts/check  scripts/dev
├─ docs/domain.md  docs/design/
├─ rules/*.yml                ast-grep rules
├─ sage/
│  ├─ cli.py  api.py  core.py  settings.py  events.py  __main__.py
│  ├─ ir/        schema.py (12 entities, Field[T], Evidence), draft.py (the workspace), settled.py (records.json)
│  ├─ domain/    cell_values, coordinates, dates, depth, management, normalize, stats, units
│  ├─ parse/     document.py (types and anchors), pdf.py (pdftext -> Document), layout.py, grounding.py, quotes.py
│  ├─ agents/    guide.md, paper_view.py, extractor.py (the loop agent and its tools), tables.py (table pass),
│  │             models.py (model ids, settings, usage), contract.py (tool results and the Workspace protocol)
│  ├─ pipeline/  run.py (drive, budgets, resume), workspace.py, expand.py, checker/, settle.py, quotes.py, review.py, export.py
│  ├─ store/     paths.py, files.py (atomic JSON, JSONL appends, TOML), models.py, proc.py (detached workers)
│  └─ web_dist/  built UI (git-ignored, bundled in the wheel)
└─ web/          React source
```

### Layers

Each layer may import only what its row allows. The last column names the rule that enforces it. Nested `ruff.toml` files set `flake8-tidy-imports.banned-api` per folder, and a child file replaces the parent's list instead of extending it, so each one repeats every ban it still needs: pydantic-ai and the Anthropic SDK everywhere but `agents/`, pdftext and pypdfium2 everywhere but `parse/`. `ir/` and `domain/` share one rule set, so `sage/domain/ruff.toml` only extends `sage/ir/ruff.toml`.

| Layer | May import | Must not import | Enforced by |
|---|---|---|---|
| `ir/`, `domain/` | stdlib, pydantic, each other | `pathlib`, `time`, `random`, `datetime.now`, `store`, `parse`, `pipeline`, `agents`, `core`, pydantic-ai | `sage/ir/ruff.toml`, `sage/domain/ruff.toml` |
| `parse/` | `ir`, `domain`, pdftext, pypdfium2 | `store`, `pipeline`, `agents`, `core`, pydantic-ai | `sage/parse/ruff.toml` |
| `agents/` | `ir`, `domain`, `parse`, `events`, pydantic-ai, anthropic | `store`, `pipeline`, `core`, pdftext, pypdfium2 | `sage/agents/ruff.toml` |
| `store/` | stdlib, pydantic, tomli-w, `events` | `pipeline`, `agents`, `core`, pydantic-ai, pdftext, pypdfium2 | `sage/store/ruff.toml` |
| `pipeline/` | every layer above, `settings` | `core`, pydantic-ai, pdftext, pypdfium2 | `sage/pipeline/ruff.toml` |
| `settings.py`, `events.py` | stdlib, pydantic, pydantic-settings, each other | the rest of `sage` | `rules/leaf-modules.yml` |
| `core.py` | every layer | pydantic-ai, anthropic, pdftext, pypdfium2 | root `ruff.toml` |
| `cli.py`, `api.py` | `sage.core` only (`cli.py` also imports `sage.api` to print the schema) | anything else under `sage` | `rules/surfaces-call-core.yml` |

These rules cut across the layers:

- **pydantic-ai and the Anthropic SDK are imported only in `agents/`.** The root `ruff.toml` bans both, and only `sage/agents/ruff.toml` lifts the ban.
- **File I/O happens only in `store/`.** `rules/io-in-store.yml` flags `open()`, `shutil`, the `os` calls that write or delete, and the `Path` read, write and delete methods anywhere else under `sage/`. Callers use `sage.store.files`.
- **pdftext and pypdfium2 are imported only in `parse/`.** The root `ruff.toml` bans both, every nested file repeats the ban, and only `sage/parse/ruff.toml` leaves it out.
- **Pipeline drives agents, never the reverse.** `sage/agents/ruff.toml` bans `sage.pipeline`. `agents/contract.py` defines the `Workspace` protocol that `pipeline/workspace.py` implements, so the agent never needs the pipeline.
- **`core.py` reaches the provider only through `sage.agents.models`,** for `check_key`. Model ids are checked by the `ModelId` type in `events.py`, not by the provider code.

Library code has no `print`. ruff's `T201` enforces it everywhere except `cli.py`, and code emits events instead (§5).

---

## 4. Document parsing

Parsing has two passes behind one `Document` type (`sage/parse/document.py`).

1. **Text layer, at `sage add`.** pdftext gives characters with boxes. `sage/parse/layout.py` groups them into blocks per text direction, splits columns, and starts a new block at each caption or bold heading. `sage/parse/pdf.py` classifies blocks as text, caption, furniture (running heads, page numbers, margins) or references, and creates one pending table per `Table N` caption. The result goes to `document.json`.
2. **Table pass, at the start of a run.** For each pending table, the table model gets an upright crop of the table region plus the text-layer words inside it. It returns the grid as rows of cell strings, with the header row count and the notes. `sage/parse/grounding.py` then matches each cell string to characters in the region, in reading order, after normalisation. An exact match comes first, then a fuzzy match above `grounding_fuzzy_threshold`. Matched characters give each cell its box and its verbatim text. The text blocks mostly inside a grounded or partial table become kind `table` (`mark_table_blocks`), so the paper view shows the grid in their place.

A table is `grounded` when at least `table_min_grounded` of its non-empty cells match (default 90%). It is `partial` from 50% up and `failed` below 50%. A model call that errors leaves the table `pending`, so the next run tries it again. A region the model says is not a table is cached as `failed`. Every other result is cached in `document.json`. Changing `PARSER_VERSION` makes the next run parse the PDF again. A table whose anchor and label match the old one, with an overlapping region, keeps its transcription, so a reparse calls no model. Changing `GROUNDER_VERSION` grounds cached tables again from their transcriptions, also at no cost. Each run keeps its own copy of the document in `runs/<id>/document.json`.

**Quote boxes, at settle.** A block's box is a whole paragraph, so the paper would light up eight lines for "38.9 hue". When a run settles, `sage/parse/quotes.py` finds each quote inside its block's characters (one pdftext pass over the cited pages, about 35ms a page) and keeps one box per line it covers in `runs/<id>/quotes.json` (`sage/pipeline/quotes.py`). It matches the way the quote check does (`normalize.squash`), so a quote the check accepts is found. Its key covers the locator version, the document and the quotes, so a stale file is worked out again, and a run with no `quotes.json` gets one on its first view. The records view sends the boxes inside each anchor's `AnchorBox.quotes`. A quote with none draws as its block. A live run's view has none, since its quotes still change. Before a reparse replaces the paper's document, older runs without a copy get the old one. `sage show`, settle, `--resume` and the Paper screen (`/api/runs/{run_id}/document`) all read the run's copy, so a reparse never moves an old run's evidence.

Pages without a text layer are marked `has_text = false`. There is no OCR. The agent reads such pages and failed tables through the `view` tool, and settle flags any evidence on them.

Marker, Docling and torch are out. Local layout models cost 1 to 3 GB of weights and a harder install, and hosted Marker sends papers off the machine for a per-page fee. The text layer plus a model transcription that code grounds keeps the install small and keeps every cell checkable against the PDF.

---

## 5. Live view of every run

Every step appends one line to `runs/<run_id>/events.jsonl`. That file is the event bus.

```
worker ──append──► events.jsonl ──read from offset──► /api/runs/{id}/stream (SSE) ──► details drawer
                                └───────────────────► sage logs RUN --follow [--json]
```

**Events.** `Event` in `sage/events.py` is one pydantic discriminated union on `type`.

| Type | Carries |
|---|---|
| `run.started` | Run and paper ids, both models, the effort, whether it resumed, the run it continues, and the guidance |
| `stage.started`, `stage.finished` | The stage (`parse`, `tables`, `agent` or `settle`) and a detail line on finish. The settle stage's finish also carries `counts` (records, verified, flagged and unresolved). |
| `llm.request` | The role and the turn number. A turn the turn limit refuses emits none. |
| `llm.thinking` | The thinking text the model returns. Claude returns a summary, never the raw chain of thought. |
| `llm.text` | The model's text |
| `llm.tool_call` | The call id, the tool and its arguments |
| `llm.tool_result` | The call id, the tool, ok or not, the result text (capped at 2,000 characters), and a `tally` read from the whole result: rejected records for `write`, observations and failed cells for `expand_table`, and blocking issues and warnings for `check` |
| `check.failed` | The record or table id and the checker errors |
| `record.settled` | The record id, its type and its status |
| `usage` | The role, the turn, this request's tokens and cost, the run total, and the cache miss when `cache_diagnostics` is on |
| `note` | An info or warning line, such as a transcribed table or a stack trace |
| `run.finished` | The final status, the agent's summary, and for `budget_exhausted` the `limit` that stopped it (`turns` or `cost`) |
| `run.error` | The error message |

- The SSE route sends each batch of new events, with the byte offset as the event id, and polls the file every 0.4 seconds. A reconnecting `EventSource` resumes from its last id. An `end` event follows once the run is over.
- A run started from the CLI shows up live in the UI, because both read the same file.
- Opening an old run replays its file.
- `describe` in `sage/events.py` gives the one-line text `sage logs` prints. `usage` shows as a running cost line, and a run that ran out of budget ends with "run stopped at the turn limit" (or cost limit). `llm.thinking` and `record.settled` appear only with `--json`, and `logs --json` prints every field, nulls included.
- `STAGE_STEP` in `sage/events.py` words each stage ("Reading the paper", "Reading tables", "Extracting", "Checking") for `sage papers`. The UI words `PaperSummary.live_stage` the same way.

---

## 6. Settings

`Settings` in `sage/settings.py` is the single source of truth. It holds the API key, the model and effort for each role, the turn and cost budgets, `max_concurrency`, the agent limits, cache diagnostics, the grounding thresholds, and the data folder.

**Precedence.** Values come from the environment first, then the config file, then the defaults.

- Each field reads `SAGE_<FIELD_NAME>`, for example `SAGE_MAX_COST_USD`. The API key also reads `ANTHROPIC_API_KEY` when `SAGE_ANTHROPIC_API_KEY` is unset.
- The config file is `~/.config/sage/config.toml`, or the path in `SAGE_CONFIG`. `store/files.write_toml` writes it with mode 0600.
- There is no `.env` file support.

**Through the UI.** `GET /api/settings` returns every value with secrets masked, where each value comes from (env, file or default), the fields locked by an environment variable, and the model's JSON schema. The Settings dialog builds its form from that schema, so a new `Settings` field appears in the UI with no frontend change. Each field's `group` names its section, so a new group needs no frontend change either. `GROUPS` in `settings.py` requires every group to have a description and puts them in the schema under `groups`, but the dialog doesn't show them. `extraction_model` and `table_model` are `ModelId`s, so a value that isn't an Anthropic model id is refused when it is saved or loaded, before any run starts. `PUT /api/settings` validates the changes together with the rest of the file and rewrites `config.toml`. A field set through the environment shows a lock with the variable's name and refuses edits. `sage config get|set|list` does the same from the CLI.

`core.settings()` reads the settings fresh on every call, so a change takes effect without a restart. It reads `config.toml` through `files.read_toml` first, so a config file that is binary, unreadable or a folder is a `corrupt` error that names the file. A value that fails validation is `invalid`, and the message names its source, "(from SAGE_MAX_TURNS)" or "(in <config path>)". `core.update_settings` checks only the values being set (`StoredSettings`, which reads no environment and no file) and writes the rest of the file back as it was, unknown keys included. So a bad value elsewhere, in the file or the environment, never blocks a change, and `sage config set KEY ""` removes a bad value. When the saved settings still don't load, because of a bad value the change didn't touch, the error says "saved, but …".

A missing API key is not a startup error. `sage ui` starts without one. `sage run` fails with a message that says to add it in Settings and names the config key, and **Extract data** shows a toast with an **Open settings** action. `sage config test-key` and the **Test** link ask Anthropic whether the key works, at no cost.

---

## 7. Storage

```
$SAGE_HOME/                        default ~/sage-data
  add.lock                         held while a new paper is checked and written
  papers/<paper_id>/
    paper.pdf
    paper.json                     id, filename, sha256, pages, title          (PaperInfo)
    document.json                  text blocks, tables, grids and boxes         (Document)
    reviews.jsonl                  append-only scientist reviews                (Review)
    run.lock                       held while a run starts, so only one is live
    runs/<run_id>/
      run.json                     models, budgets, status, usage, counts       (RunInfo)
      events.jsonl                 the live event stream                        (Event)
      draft.json                   the agent's workspace, saved on every change (Draft)
      records.json                 settled records with statuses                (Settled)
      document.json                the document this run read, after its tables (Document)
      quotes.json                  each quote's line boxes on the page, derived (QuoteBoxes)
      worker.log                   the detached worker's stdout and stderr
~/.config/sage/config.toml         settings written by the UI or `sage config`
```

- The paper id is the PDF's file name without `.pdf`, with accents dropped ("Straße" is "Strasse"), unsafe characters replaced by `-`, and cut to 100 characters. A clash gets a `-2` suffix. Paper and run ids match `core.ID`: `[A-Za-z0-9][A-Za-z0-9._-]*`, at most 128 characters. `core` checks every id that comes from the CLI or the API before it reaches a path, and refuses any other as `invalid`.
- Adding the same bytes again returns the existing paper, matched on sha256. The PDF is parsed unlocked. The sha256 check and the writes then happen under an exclusive lock on `add.lock` (`Home.add_lock`), so the same file uploaded twice at once is added once.
- The run id is the UTC start time plus four random hex digits, for example `20261017T081101-3fa2`.
- JSON files are written to a temp file and moved into place with `os.replace`. JSONL files are append-only. A run writes inside its own folder, and also the paper's `document.json` when its table pass transcribes new tables or a reparse replaces it. Before a reparse, each older run without its own `document.json` gets a copy of the old one.
- `core` refuses a second live run on the same paper. Starting a run holds an exclusive lock on `papers/<paper_id>/run.lock` (`files.exclusive`, an `flock`), so the live-run check and the queued `run.json` are one step and two starts at once can't both pass. The OS drops the lock when its holder exits, so it never goes stale.
- Every file has a pydantic model, so each one has a schema and is validated on read. `sage/store/paths.py` owns the layout.
- Only a missing file reads as absent. A file that exists but won't parse raises `UnreadableError` in `store/files.py`, and `core` turns it into a `corrupt` error that names the file and the reason. A list that meets one, such as `sage runs` with a broken `run.json`, fails with that error instead of leaving the run out. Looking up one run or paper reads only its own files, so a broken file elsewhere doesn't hide it.
- Nothing is held in memory between requests. The paper and run lists come from scanning the folders.

---

## 8. Surfaces

### CLI

Every command takes `--json`. `uv run sage <command> --help` is the full reference.

`main()` in `cli.py` is the one error boundary. A `SageError` prints `error: <message>` and exits 1. Under `--json` it prints `{"error": {"kind", "message"}}` (`core.ErrorBody`), and so does a usage error (a missing argument, an unknown option or command, a bad value) with kind `invalid` and exit 2. `sage add` adds what it can, prints an error line for each file it couldn't add, and then exits 1. Under `--json` it prints the per-file list the API returns. `sage ui` binds its port (`core.listen`) before it prints the URL, so a port in use is a `conflict`.

```
sage add PDF...
sage papers
sage run PAPER [--model M] [--effort E] [--no-follow]
sage run --resume RUN [--guidance G] [--no-review-notes]
sage run --continue RUN [--guidance G] [--model M] [--effort E] [--no-review-notes]
sage stop RUN
sage runs [PAPER]
sage show RUN
sage logs RUN [--follow]
sage export RUN [--format xlsx|csv|json] [-o PATH]
sage queue [RUN]
sage review RUN RECORD accept|edit|reject [--field F] [--value V] [--note N]
sage review RUN --accept-looks-good
sage config list
sage config get KEY
sage config set KEY VALUE
sage config test-key
sage ui [--host H] [--port P]
sage openapi
```

### HTTP API

`api.py` serves the routes under `/api/`, one `core` call each. `sage openapi` prints the schema that the web client is generated from (`cd web && pnpm gen:api`). Errors come back as `core.ErrorBody` (`{kind, message}`) with status 400, 404, 409, 412 (missing API key) or 500 (`corrupt`, a file on disk that won't parse or can't be written). A request the routes' types refuse is a 422 in the same shape, with kind `invalid`. An unknown path under `/api/` is a 404, never the UI shell.

`POST /api/papers` answers per file, so one bad file doesn't stop the rest: `Added {filename, paper, new}` or `NotAdded {filename, error}`, where `error` is an `ErrorBody`.

`PaperSummary` is one paper as Home, the Paper screen and `sage papers` show it: `latest_run` (the live run, else the newest), `run_count`, `data_run` (the run whose records stand for the paper), `to_check` and `looks_good` (unreviewed items on `data_run`), and `live_stage` (the live run's stage, or null before it starts one). A resumed run keeps standing as `data_run` while it runs again, so its earlier records stay in view.

### UI

The UI is specified in `04-ui.md`. It has two screens, Home (the papers) and Paper (with Check, Data and Read modes, a This run drawer and History), and a Settings dialog. `/runs/<run_id>` redirects to that run on its paper, and an unknown one shows a Not found page. `sage ui` serves the build from `sage/web_dist/` on 127.0.0.1:8765 by default, and `--host` binds another interface.

### Deployment

SAGE is single-user, with no authentication. Anyone who can reach the port can use it, so bind beyond 127.0.0.1 only on a trusted network.
