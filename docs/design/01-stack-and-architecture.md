# SAGE: stack and architecture

SAGE reads agronomy research papers (PDFs) and extracts the experiment records into a typed schema, the IR. Every value carries evidence that points at the text it came from. Scientists review the results against the paper.

SAGE ships as one Python package, `sage`. It holds a Typer CLI and a FastAPI app, both on a shared `sage/core.py`, plus a prebuilt React UI that the same process serves. Data lives in plain files. There is no database, no extra service, and no Node at runtime.

This page covers the stack, the package layout and its layer rules, the event stream, settings, storage and the two surfaces. The agent loop is in `02-agent-design.md`. The UI is specified in `04-ui.md`. The IR, the evidence rules and the checker are defined in `../domain.md`.

---

## 1. Design principles

1. **One core, two surfaces.** Every user action is a function in `core.py`. `cli.py` and `api.py` only parse input, call `core`, and format the output. Whatever the UI can do, the CLI can do too.
2. **Code does the mechanical work, the agent does the judgement.** Parsing cells, converting coordinates and units, reading dates, checking evidence and settling statuses are deterministic Python. The agent reads the paper, interprets it and decides what each table means.
3. **Nothing unchecked gets saved as verified.** Every record passes the schema and the checker. Failures go back to the agent as exact errors, each at the `file:line` to fix. Code, never the agent, sets each value's status.
4. **Everything can be observed.** Each run writes a live event stream. The UI, `sage logs` and coding agents all read it.
5. **Mistakes become rules.** A mistake that repeats becomes a ruff or ast-grep rule, not a one-off patch.
6. **The model is a setting, and so is the agent.** Extraction works on files in the paper's folder, and three drivers can do the work behind the same `sage extract check` and `sage extract done` (`02-agent-design.md`). `sage` is SAGE's own loop on a model you pay for through an API key. `agent` is a coding agent that SAGE starts over the Agent Client Protocol (Claude Code, Codex, Gemini CLI or OpenCode, or one you add in the `agents` setting) and that uses its own sign-in. `here` is an agent in your own terminal with the SAGE skill. The `extract_with` setting says which one Extract uses unless a run picks, and falls back to the first of your models.

   For the `sage` driver, the default is the first of your models. A fresh install has none, so you add a provider and a model, or use an agent. A model comes from a provider, a connection you configure: Anthropic, OpenAI, Google and Amazon Bedrock are built in, and you add your own (an OpenAI-compatible server, Azure OpenAI, Vertex AI, or another Anthropic or Bedrock endpoint). It may use only the models you list in the `models` setting, each with a price. No setting names a model of its own: the first of your models is what Extract uses when `extract_with` is empty. Each of your models has its own `effort`, one of the levels it has: only Anthropic has `max`, and a model that doesn't think has none. An agent's model, mode and effort are its own: `AgentConfig.model` and `AgentConfig.effort`, in the agent's words, or `claude:sonnet` after `--with`. `02-agent-design.md` says how a run picks its effort.

---

## 2. Stack

### Backend: Python 3.13

| Concern | Choice | Notes |
|---|---|---|
| Env, packaging | **uv** with the **hatchling** backend | `uv.lock` is committed. The built UI in `sage/web_dist/` goes into the wheel. The package is named `sage`. That name is taken on PyPI, and SAGE is not published. |
| Lint and format | **ruff** | `select = ["ALL"]` with a short ignore list. Nested `ruff.toml` files enforce the layer rules (§3). |
| Types | **ty** | Every rule is an error. |
| LLM | **pydantic-ai-slim[anthropic,bedrock,google,openai]** with **pydantic-ai-harness** | Python functions as tools, `ModelRetry` for the done gate's refusals, `UsageLimits` for the budgets, and one unified `cache` and `thinking` setting that each provider renders its own way. Anthropic's effort scale and cache diagnostics are still reached through its own settings. The harness's `FileSystem` capability gives SAGE's own loop the paper's folder. |
| Coding agents | **agent-client-protocol** (`acp`) | SAGE's client for the Agent Client Protocol. It spawns Claude Code, Codex, Gemini CLI or OpenCode over stdio in the paper's folder (`agents/acp_client.py`). The agents use their own sign-in. |
| Model ids | `provider:name` | Extract uses the `extract_with` setting, else the first of your models; no setting names a model of its own. Inside SAGE a model id is always the canonical `provider:name`, where `provider` is the id of one of your providers (§6), and `config.toml`, `run.json` and the UI hold nothing else. A person may write a bare name (`gpt-5.1`) on the CLI, in the API or in `sage config set`, and `core` resolves it against the ids pydantic-ai knows on a built-in provider. A `provider:name` passes whatever the name, so a model newer than the pin, or one on a gateway, still runs. Either way, `core` refuses a model with no price, because pydantic-ai can't hold a run to its cost limit without one, and a model that can't call tools, because the agent works only through them. A Bedrock model is written `bedrock:<id>`, because its ids carry a `:` of their own and only the first one splits off the provider. |
| Data models | **pydantic v2** | The IR, and the model of every file on disk |
| Agent files | **PyYAML** | Reads the agent's `records/` files with `BaseLoader`, so every scalar is a string the schema types and every node knows its line (`pipeline/compile.py`). |
| Settings | **pydantic-settings** | Environment, then `config.toml`, then defaults (§6) |
| CLI | **Typer** + **Rich** | Every command takes `--json`. |
| HTTP | **FastAPI** + **uvicorn** | One process serves the API and the built UI. |
| Live stream | `fastapi.sse` (`EventSourceResponse`, built into FastAPI) | The SSE route reads a run's `events.jsonl` from a byte offset (§5). There is no sse-starlette. |
| PDF | **pdftext** + **pypdfium2** | pdftext gives the text layer with boxes. pypdfium2 renders the page, table and figure images at `page_render_dpi`. |

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

ruff, ty and ast-grep (from the `ast-grep-cli` PyPI package) are uv dev dependencies. watchfiles is a runtime one: `store/watch.py` follows the library with it for `/api/changes` (§5), and `uvicorn --reload` and the schema watcher in `scripts/dev` watch files with it too. Biome is a pnpm dev dependency.

---

## 3. Architecture

```
┌──────────────────────────────── sage (one process) ──────────────────────────────────┐
│  cli.py  (Typer)  ──┐                                                                │
│                     ├──► core.py ──► pipeline/ ──┬──► agents/   pydantic-ai, ACP     │
│  api.py  (FastAPI) ─┘       │                    ├──► parse/    PDF -> Document      │
│    /api/*                   │                    ├──► domain/ + ir/   pure logic     │
│    /api/runs/{id}/stream    │                    │                                   │
│    /*  -> web_dist          └──────► store/ ◄────┘   files + events.jsonl            │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

A run that SAGE drives, with its own loop (`sage`) or through a coding agent it starts (`agent`), does not execute inside the process that asked for it. One runner process per data folder executes every such run, as asyncio tasks, up to `parallel_runs` at a time (default 3). The rest wait as `queued` and start oldest first. `core._queue` writes `run.json` as queued, under the paper's `run.lock`, and then calls `runner.wake`. That takes `runner.lock`, an `flock`, and only if no runner holds it spawns `sage runner --lock-fd N` detached (`proc.spawn_sage`). The child inherits the locked descriptor, so there is never a moment when a queued run has no runner, and `runner.pid` records its pid. The runner polls every 0.5 seconds for queued runs and stop requests. It leaves after 30 seconds with nothing to do, after one more look for a run queued as it left. Its output goes to `runner.log`. `sage ui` also calls `runner.wake` at launch when queued runs are waiting, so after a crash or reboot, opening SAGE carries the queue on.

`run.json` has one writer at a time: `core` while the run is queued, and the runner from the moment it claims the run. A queued run always means it will run: it waits in `run.json` until a runner takes it on. Only a `running` run whose pid is dead reads as `interrupted`. `core.stop_runs` writes a `stop` file in the folder of a run the runner drives. The runner cancels that task, and the run records `interrupted` after it settles what the paper's folder compiles to. A queued run with a stop request ends `interrupted` without starting, and one whose key is missing when its turn comes ends `failed` with the message. SIGINT or SIGTERM to the runner stops its running runs the same way and leaves queued ones queued. A run resumes instead of starting fresh when its `started_at` is set.

The runner is a separate process, not a task in the uvicorn server, for three reasons. Runs survive a server restart or a closed terminal. CPU-heavy parsing (pdfium) doesn't block the API. And the CLI and the UI share one runner. `sage extract run` and the UI both follow a run through its `events.jsonl`.

A terminal run (driver `here`) has no runner. `sage extract start` opens it as `running`, the agent in that terminal works the folder and calls `sage extract check` and `sage extract done`, and `sage extract stop` or `core.stop_runs` ends it `interrupted` at once, under the paper's `run.lock`, after settling what the folder compiles to. It never reads as interrupted because a pid is dead. An agent SAGE starts calls the same `sage extract` commands in its own process, so `done` takes the paper's `run.lock` while the runner writes the same `run.json`.

### Package layout

```
sage/                         repo root
├─ pyproject.toml  uv.lock  ruff.toml  sgconfig.yml  AGENTS.md  CLAUDE.md -> AGENTS.md
├─ scripts/check  scripts/dev
├─ docs/domain.md  docs/design/
├─ rules/*.yml                ast-grep rules
├─ sage/
│  ├─ cli.py  api.py  core.py  settings.py  events.py  __main__.py
│  ├─ ir/        schema.py (12 entities, Field[T], Evidence), draft.py (what the files compile to), files.py (FileIssue), settled.py (records.json)
│  ├─ domain/    cell_values, coordinates, dates, depth, management, normalize, stats, units
│  ├─ parse/     document.py (types and anchors), pdf.py (pdftext -> Document), layout.py, grounding.py, quotes.py
│  ├─ agents/    skill/ (SKILL.md and references/, what every driver reads), paper_view.py (paper.md),
│  │             extractor.py (SAGE's own loop), acp_client.py (the ACP client), models.py (model ids, settings, usage),
│  │             contract.py (the Workspace protocol and its results)
│  ├─ pipeline/  extraction.py (check, done, FolderWorkspace), compile.py (records/ -> Draft), shapes.py, observations.py,
│  │             gate.py (findings and the done gate), checker/, paper_files.py (adding a paper, paper.md, images,
│  │             CSV grounding), run.py (SAGE's loop: drive, budgets, resume), agent_run.py (an ACP agent),
│  │             runner.py (the runner process: waking it and its loop), run_state.py (live runs, the run that
│  │             stands for a paper, a run's records now), settle.py, quotes.py, review.py, export.py
│  ├─ store/     paths.py (the layout, ids, what a changed path is), files.py (atomic JSON, JSONL appends, TOML), models.py,
│  │             proc.py (the detached runner, an agent's environment and whether it is installed and signed in),
│  │             workspace.py (the agent's files and snapshots), skills.py (installing the skill), watch.py
│  └─ web_dist/  built UI (git-ignored, bundled in the wheel)
└─ web/          React source
```

### Layers

Each layer may import only what its row allows. The last column names the rule that enforces it. Nested `ruff.toml` files set `flake8-tidy-imports.banned-api` per folder, and a child file replaces the parent's list instead of extending it, so each one repeats every ban it still needs: pydantic-ai, pydantic-ai-harness, the ACP SDK (`acp`) and the provider SDKs (Anthropic, OpenAI, Google, boto3 and botocore) everywhere but `agents/`, pdftext and pypdfium2 everywhere but `parse/`. `ir/` and `domain/` share one rule set, so `sage/domain/ruff.toml` only extends `sage/ir/ruff.toml`.

| Layer | May import | Must not import | Enforced by |
|---|---|---|---|
| `ir/`, `domain/` | stdlib, pydantic, each other | `pathlib`, `time`, `random`, `datetime.now`, `store`, `parse`, `pipeline`, `agents`, `core`, pydantic-ai, pydantic-ai-harness, `acp` | `sage/ir/ruff.toml`, `sage/domain/ruff.toml` |
| `parse/` | `ir`, `domain`, pdftext, pypdfium2 | `store`, `pipeline`, `agents`, `core`, `settings`, `events`, pydantic-ai, pydantic-ai-harness, `acp` | `sage/parse/ruff.toml` |
| `agents/` | `ir`, `domain`, `parse`, `events`, pydantic-ai, pydantic-ai-harness, the ACP SDK (`acp`), the provider SDKs | `store`, `pipeline`, `core`, pdftext, pypdfium2 | `sage/agents/ruff.toml` |
| `store/` | stdlib, pydantic, tomli-w, watchfiles, `events` | `pipeline`, `agents`, `core`, pydantic-ai, pydantic-ai-harness, `acp`, the provider SDKs, pdftext, pypdfium2 | `sage/store/ruff.toml` |
| `pipeline/` | every layer above, `settings`, PyYAML, xlsxwriter | `core`, pydantic-ai, pydantic-ai-harness, `acp`, the provider SDKs, pdftext, pypdfium2 | `sage/pipeline/ruff.toml` |
| `settings.py`, `events.py` | stdlib, pydantic, pydantic-settings, each other | the rest of `sage` | `rules/leaf-modules.yml` |
| `core.py` | every layer | pydantic-ai, pydantic-ai-harness, `acp`, the provider SDKs, pdftext, pypdfium2 | root `ruff.toml` |
| `cli.py`, `api.py` | `sage.core` only (`cli.py` also imports `sage.api` to print the schema) | anything else under `sage` | `rules/surfaces-call-core.yml` |

These rules cut across the layers:

- **pydantic-ai, pydantic-ai-harness, the ACP SDK and the provider SDKs are imported only in `agents/`.** The root `ruff.toml` bans them, and only `sage/agents/ruff.toml` lifts the ban.
- **File I/O happens only in `store/`.** `rules/io-in-store.yml` flags `open()`, `shutil`, the `os` calls that write or delete, and the `Path` read, write and delete methods anywhere else under `sage/`. Callers use `sage.store.files`.
- **pdftext and pypdfium2 are imported only in `parse/`.** The root `ruff.toml` bans both, every nested file repeats the ban, and only `sage/parse/ruff.toml` leaves it out.
- **Pipeline drives agents, never the reverse.** `sage/agents/ruff.toml` bans `sage.pipeline`. `agents/contract.py` defines the `Workspace` protocol (the folder, `image`, `check` and `done`) that `FolderWorkspace` in `pipeline/extraction.py` implements, so SAGE's own loop never needs the pipeline. An agent SAGE starts reaches check and done through the `sage extract` commands instead.
- **`core.py` reaches the provider only through `sage.agents.models`,** for `Route`, `resolve`, `entry` and `list_models`, and an agent only through `sage.agents.acp_client`, to read what a coding agent offers. `ModelId` in `events.py` checks a model id's shape. `resolve` knows which built-in provider a bare name belongs to. A `Route` is a model id, the `Connection` of the provider it names and your price for it, if you gave one. `entry` takes a `Route` and describes the model (name, price per million tokens, whether it calls tools, the effort levels it has) without a request, and its price is None when neither you nor genai-prices has one. `list_models` takes a `Connection` and asks the provider which models those credentials can use. `_client` builds the pydantic-ai client for each kind of provider. The OpenAI, Google and AWS SDKs are banned outside `agents/` the way the Anthropic SDK is.

Library code has no `print`. ruff's `T201` enforces it everywhere except `cli.py`, and code emits events instead (§5).

---

## 4. Document parsing

Parsing has three steps behind one `Document` type (`sage/parse/document.py`). There is no table model: the agent transcribes the tables it needs, and code grounds what it wrote.

1. **Text layer, at `sage add`.** pdftext gives characters with boxes, in the page as shown; each span's direction is turned by the page's `/Rotate`, so it is the direction as shown too. `sage/parse/layout.py` groups them into blocks per text direction, splits columns, and starts a new block at each caption or bold heading. `sage/parse/pdf.py` classifies blocks as text, caption, furniture (running heads, page numbers, margins) or references, and creates one pending table per `Table N` caption, with its region (from the caption down to the next caption, body paragraph or margin, across half the page only where the page's paragraphs keep to two columns and no row below the caption crosses the middle), plus one per page a table runs on to (`Table.continues`). The result goes to `.sage/document.json`.
2. **Files for the agent, at `sage add` and when the parser changes.** `paper_files.materialize` renders `pages/p<N>.png` for every page, `tables/<anchor>.png` for every detected table, turned upright, and `figures/<anchor>.png` for each figure it can locate, all at `page_render_dpi`. It writes `paper.md` from the effective document. An image is kept unless it was rendered from another PDF, parser or resolution (`.sage/rendered.json`). A run calls it again at its start, and a run on SAGE's own loop parses the PDF again first when `PARSER_VERSION` changed. The tables keep their CSVs, so each cell keeps its anchor and is grounded again on the new text layer.
3. **Table grounding, at check.** The agent reads a table's PNG and writes `tables/<anchor>.csv`: a `# header_rows: N` line, optional `# notes:` lines, then the grid as printed. `paper_files.effective_document` grounds each CSV. `sage/parse/grounding.py` matches each cell to characters in the table's region, in reading order, after normalisation. An exact match comes first, then a fuzzy match above `grounding_fuzzy_threshold`. Matched characters give each cell its box and its verbatim text, and an unmatched cell keeps the CSV's text. The text blocks mostly inside a grounded or partial table become kind `table` (`mark_table_blocks`), so the paper view shows the grid in their place. `paper.md` is rewritten when a CSV changes.

A table is `grounded` when at least `table_min_grounded` of its non-empty cells match (default 90%). It is `partial` from 50% up and `failed` below 50%. A table with no CSV is `pending` and has no cells. The grounding is cached in `.sage/tables/<anchor>.json`, keyed on the CSV, the grounding code (`GROUNDER_VERSION`) and its settings, so an unchanged table is not grounded again. Each run keeps its own copy of the document it was last checked against in `runs/<id>/document.json`. Older papers whose tables a table model once transcribed get those grids written out as CSVs once (`paper_files.migrate`), so nothing is lost.

**Quote boxes, at settle.** A block's box is a whole paragraph, so the paper would light up eight lines for "38.9 hue". When a run settles, `sage/parse/quotes.py` finds each quote inside its block's characters (one pdftext pass over the cited pages, about 35ms a page) and keeps one box per line it covers in `runs/<id>/quotes.json` (`sage/pipeline/quotes.py`). It matches the way the quote check does (`normalize.squash`), so a quote the check accepts is found. Its key covers the locator version, the document and the quotes, so a stale file is worked out again, and a run with no `quotes.json` gets one on its first view. The records view sends the boxes inside each anchor's `AnchorBox.quotes`. A quote with none draws as its block. A live run's view has none, since its quotes still change. Before a reparse replaces the paper's document, older runs without a copy get the old one. `sage show`, settle, `sage extract run --resume` and the Paper screen (`/api/runs/{run_id}/document`) all read the run's copy, so a reparse never moves an old run's evidence.

Pages without a text layer are marked `has_text = false`. There is no OCR. The agent reads the page's PNG, transcribes the blocks it will cite into `pages/p<N>.md` as `[p<N>.b<K>] …`, and settle flags any evidence on them. SAGE's own loop looks at images through `view_image`. An agent SAGE starts or one in a terminal opens the PNG with its own tools.

Marker, Docling and torch are out. Local layout models cost 1 to 3 GB of weights and a harder install, and hosted Marker sends papers off the machine for a per-page fee. The text layer plus a transcription by the agent that code grounds keeps the install small and keeps every cell checkable against the PDF.

---

## 5. Live view of every run

Every step appends one line to `runs/<run_id>/events.jsonl`. That file is the event bus.

```
runner ──append──► events.jsonl ──read from offset──► /api/runs/{id}/stream (SSE) ──► details drawer
                                └───────────────────► sage logs RUN --follow [--json]
```

**Events.** `Event` in `sage/events.py` is one pydantic discriminated union on `type`.

| Type | Carries |
|---|---|
| `run.started` | Run and paper ids, the driver (`sage`, `agent` or `here`, the terminal), the agent and its model for an agent run, the extraction model for SAGE's own loop, the effort the run asked for (an `Effort` for SAGE's loop, the agent's own level for an agent), whether it resumed, the run it continues, and the guidance |
| `stage.started`, `stage.finished` | The stage (`parse`, `agent` or `settle`) and a detail line on finish. The settle stage's finish also carries `counts` (records, verified, flagged and unresolved). |
| `llm.request` | The role and the turn number. A turn the turn limit refuses emits none. An agent SAGE starts emits one for each prompt it sends. |
| `llm.thinking` | The thinking text the model or agent returns. Claude returns a summary, never the raw chain of thought. |
| `llm.text` | The model's or agent's text |
| `llm.tool_call` | The call id, the tool and its arguments |
| `llm.tool_result` | The call id, the tool, ok or not, and the result text (capped at 2,000 characters) |
| `check.ran` | One `sage extract check`, or the check `done` runs first: records, blocking issues and warnings, whether the files changed (and the snapshot taken), the targets the same-error rule marked, and the first findings as `file:line  target: message`. Every driver emits it. |
| `agent.asked`, `agent.answered` | An agent SAGE starts asks to do something its permission rule doesn't allow (the title, kind, command and paths), and the answer, from you or from the rule |
| `record.settled` | The record id, its type and its status |
| `usage` | From SAGE's own loop: the role, the turn, this request's tokens and cost, the run total, and the cache miss when `cache_diagnostics` is on. An agent's usage and cost come from its own reports into `run.json`. |
| `note` | An info or warning line, such as an agent's plan or a stack trace |
| `run.finished` | The final status, the agent's summary, and for `budget_exhausted` the `limit` that stopped it (`turns` or `cost` for SAGE's loop, `time` for an agent) |
| `run.error` | The error message |

- The SSE route sends each batch of new events, with the byte offset as the event id, and polls the file every 0.4 seconds. A reconnecting `EventSource` resumes from its last id. An `end` event follows once the run is over.
- A run started from the CLI shows up live in the UI, because both read the same file.
- **Every other screen follows the files too.** `/api/changes` is one SSE stream per window. `store/watch.py` watches `papers/` with watchfiles, and `core.changes` turns each batch of paths into `Change` notices by `Home.change`, which works each one out from the paths `Home` builds: `library` (a paper's folder came or went), `paper` (its PDF, document, `paper.md` or images), `files` (what the agent writes), `reviews`, `run` (a run's own files) and `log` (its `events.jsonl`). A `files` or `paper` notice names the paper's open run, if any. The web's `useLibraryChanges` invalidates the queries each notice touches, so nothing polls. An open run's records are compiled from the folder on each request (`extraction.live_draft`, which records nothing), so an agent's edit shows before its next check. When a file is mid-write, so that it vanishes between listing and reading or holds half a UTF-8 character, the last check's draft shows instead (`run_state.provisional`). Any other error surfaces. The stream never ends on its own, so `sage ui` and `scripts/dev` give a shutdown one second before closing it.
- `describe` in `sage/events.py` gives the one-line text `sage logs` prints. `usage` shows as a running cost line, and a run that ran out of budget ends with "run stopped at the turn limit" (or cost or time limit). `llm.thinking` and `record.settled` appear only with `--json`, and `logs --json` prints every field, nulls included.
- `STAGE_STEP` in `sage/events.py` words each stage ("Reading the paper", "Extracting", "Checking") for `sage papers`. The UI words `PaperSummary.live_stage` the same way.

---

## 6. Settings

`Settings` in `sage/settings.py` is the single source of truth. It holds the providers, the models extraction may use with their prices and efforts, the coding agents SAGE can start and which one Extract uses (`agents`, `extract_with`), the turn and cost budgets (`max_turns` defaults to 150), `parallel_runs`, the same-error and check-list limits, whether a coding agent asks before it acts (`agents_ask`), cache diagnostics, the grounding thresholds, the page image resolution (`page_render_dpi`), and the data folder.

**Precedence.** Values come from the environment first, then the config file, then the defaults.

- Each field reads `SAGE_<FIELD_NAME>`, for example `SAGE_MAX_COST_USD`.
- A field of a built-in provider also reads its provider's own variables, listed in `PROVIDER_ENV` (the first one set wins): `SAGE_ANTHROPIC_API_KEY` then `ANTHROPIC_API_KEY`, `SAGE_OPENAI_API_KEY` then `OPENAI_API_KEY`, `SAGE_GOOGLE_API_KEY`, `GEMINI_API_KEY` then `GOOGLE_API_KEY`, `SAGE_BEDROCK_API_KEY` then `AWS_BEARER_TOKEN_BEDROCK`, and for the Bedrock region `SAGE_BEDROCK_REGION`, `AWS_REGION` then `AWS_DEFAULT_REGION`. `_ProviderEnv` puts them into that provider's table, ahead of the file, and `provider_locked()` reports which fields they set. A provider you add reads no environment.
- The config file is `~/.config/sage/config.toml`, or the path in `SAGE_CONFIG`. `store/files.write_toml` writes it with mode 0600.
- There is no `.env` file support.

**Through the UI.** `GET /api/settings` returns every value with secrets masked, where each value comes from (env, file or default), the fields locked by an environment variable, and the model's JSON schema. The Settings page builds its form from that schema, so a new `Settings` field appears in the UI with no frontend change. Each field's `group` names its section, so a new group needs no frontend change either. `GROUPS` in `settings.py` requires every group to have a description, puts them in the schema under `groups`, and the page shows each under its section's title. Fields with `page="manage"` in their schema (`providers`, `models`, `agents`, `extract_with` and `cache_diagnostics`) are edited on the Manage page, and the Settings page leaves them out. `PUT /api/settings` validates the changes together with the rest of the file and rewrites `config.toml`. It refuses `providers` and `models`, which have their own routes and commands, and any key that isn't a setting. A field set through the environment shows "Set by <variable>" and refuses edits. `sage config get|set|list` does the same from the CLI.

**Providers.** `providers` is a table of `Connection`s (`sage/events.py`) by id. A `Connection` has a `kind` (`anthropic`, `openai`, `azure`, `google`, `vertex` or `bedrock`) and the fields its kind uses: `name`, `api_key`, `base_url`, `region`, `project`, `api_version`, `aws_profile` and `credentials_file`. `KINDS` in `settings.py` says, for each kind, its title and help text and its fields, each with a title, help, a placeholder, a default, and whether it is secret. Each kind's `needs` lists what it can't run without, as groups of fields of which one must be set, and a field in no group is optional. The Manage page shows that per field, and `missing()` says what a connection still needs, by field title: an API key for `anthropic` and `google`, an API key or an endpoint for `openai` (a local server needs no key), an endpoint and an API key for `azure`, a project for `vertex`, and a region plus an API key or an AWS profile for `bedrock`.

- The four built-in providers, `anthropic`, `openai`, `google` and `bedrock`, always exist, each of its own kind, and cannot be removed. A validator adds them when the file lacks them (Bedrock gets its default region) and refuses a built-in id of another kind.
- Someone adds others under an id of their own, `[a-z0-9][a-z0-9-]{0,31}`, of any kind. An OpenAI-compatible server (a company gateway, LiteLLM, OpenRouter, vLLM, Ollama) is an `openai` provider with a `base_url`. Azure OpenAI takes its endpoint, key and API version (default `2024-10-21`), and a model goes by its deployment name. Vertex AI takes a project, a location (default `global`), and Application Default Credentials or a service account key file, and runs Claude and Gemini. Bedrock takes a region and a Bedrock API key or an AWS profile, and optionally a VPC endpoint. No other AWS credentials are read. An `anthropic` provider with a `base_url` reaches a gateway that speaks Anthropic's API.
- `agents/models._client` builds the pydantic-ai client for each kind, importing its SDK only when used. `openai` with a `base_url` uses Chat Completions, since only OpenAI's own server has the Responses API for sure.
- `core.save_provider` (`PUT /api/providers/{provider}`) adds a provider, given its kind, or changes one. A key sent back masked stays as it is, an empty value clears its field, and a field the environment sets is refused. `core.remove_provider` refuses a built-in provider and one that your models use. `GET /api/provider-kinds` sends `KINDS` to the Manage page, so a new kind or field shows there with no frontend change. `GET /api/providers` lists the providers (`ProviderInfo`: id, kind, title, whether built in, each field's value with a key masked, the fields locked by the environment, and what it `missing`).

**Your models.** `models` is a list of `YourModel` (`id`, optional `name`, optional `input_usd` and `output_usd`, optional `cache_read_usd` and `cache_write_usd`), empty on a fresh install. Only they appear in the extraction pickers, and a run refuses any other ("add it on the Manage page, or `sage models add …`"). `core.add_model` saves each as its canonical `provider:name`. `core.update_settings` doesn't touch the list. Any model can be removed. Removing the one `extract_with` names empties `extract_with`, so Extract falls back to the first model left, and with no models and no agent a run is refused with "nothing to extract with". A bad value from the environment or an older file fails when a run starts, in the caller's terminal rather than in the runner.

- **Price.** The API and the runner keep genai-prices current: `core.fresh_prices` starts its `UpdatePrices`, which fetches the newest table at once and then hourly, unless the `fetch_prices` setting is off. Until a fetch lands, or offline, the table bundled with the package answers. `agents/models.entry` prices a model from that table. Your price wins. Otherwise genai-prices prices it by provider and endpoint URL, so a model on OpenRouter gets OpenRouter's price, from a probe of 1,000 tokens each way, which lands in the base tier even for a model that charges more past a long prompt. A model neither can price can't be added, since the cost limit needs a price: the error asks for yours, on the Manage page or with `sage models add ID --price IN OUT`. A price of 0 is allowed, for a local model. Both prices are given or neither. Cache reads and writes are priced separately: yours (`--cache-price READ WRITE`, which needs `--price`, or `cache_read_usd` and `cache_write_usd` in the API), else genai-prices' discount for the model, else the input price. A model that can't call tools is refused too. At run time `build` wraps a model that has your price in `_Priced`, which sets each response's cost from it, because pydantic-ai keeps a cost already set on a response, so `UsageLimits` holds the cost limit on it.
- **Title.** A model shows genai-prices' name, else the provider's own listing name (saved as `name` when the model was added), else the name part of its id. A genai-prices name that is only the id in lower case, such as "gpt 4.1", is ignored.
- `GET /api/models` lists your models as `ModelOption`s, each with its input, output and cache prices. It leaves out a model whose provider is gone or that has lost its price, such as after an update to genai-prices. `GET /api/models/describe?id=` shows any model as SAGE sees it before it is added (`Candidate`: title, price or none, whether it calls tools, whether it is yours). `POST /api/models` adds a model or changes its price and cache prices, and `POST /api/models/remove` removes some.

**Agents.** `agents` is a table of `AgentConfig`s (`sage/settings.py`) by id, with the four SAGE knows built in as `BUILTIN_AGENTS`: `claude` (Claude Code), `codex`, `gemini` (Gemini CLI) and `opencode`. `[agents.<id>]` in `config.toml` changes any field of one or adds an agent of your own that speaks ACP, under an id like a provider's. An `AgentConfig` has the `command` and `args` that start it over stdio, `env`, the programs it `requires`, how to `install` it, a `login_check` command that exits 0 when it is signed in and how to `login`, an optional `model` and `model_flag` (for an agent that takes the model at launch), the session `mode` that decides what it asks permission for, an optional `effort` (its `thought_level` value for the model), and `time_limit_minutes` (default 60). `core.set_agent` sets the model and the effort (`sage agents set ID --model M --effort E`, `PUT /api/agents/{id}`). SAGE passes an agent no key: it uses its own sign-in. `core.agents` says for each one whether it is `ready`, `not_installed` or `not_signed_in`, with the fix, and how many runs it is driving (`sage agents`, `GET /api/agents`). `core.agent_offer` starts an agent in an empty folder and lists the models and modes it offers, and the efforts of a model (its default, or `model`), which spends no tokens (`sage agents offer ID [--model M]`, `GET /api/agents/{id}/offer?model=M`). `extract_with` names the agent or model Extract uses when a run doesn't pick (`--with`, or `using` in the API). It is an agent id such as `claude`, `claude:sonnet` for an agent and its model, or a model id. Unset, it is the first of your models.

**Routes.** `core.route(settings, model_id)` resolves the one model a SAGE-driven run uses to a `Route(model_id, connection, price)`, so the CLI and the API fail in the caller's terminal rather than in the runner. A run, a continue and a resume on SAGE's own loop each need every field `missing()` names on the provider that model calls. Without one they fail with `missing_key`, whose message names the provider, the field and the model that needs it ("Anthropic has no API key set, which anthropic:claude-haiku-5-5 needs. Set it on the Manage page in `sage ui`, or run `sage providers set anthropic api_key` (or env SAGE_ANTHROPIC_API_KEY or ANTHROPIC_API_KEY)."). The UI says the same before anything starts (`04-ui.md`). A model on a provider that isn't yours is `invalid`. A run on an agent needs no key. It needs the agent installed and signed in. `core._driver` refuses one that isn't, and the runner ends a queued run `failed`, with `refused` set and the fix, when that changed since.

`core.settings()` reads the settings fresh on every call, so a change takes effect without a restart. It reads `config.toml` through `files.read_toml` first, so a config file that is binary, unreadable or a folder is a `corrupt` error that names the file. A value that fails validation is `invalid`, and the message names its source, "(from SAGE_MAX_TURNS)" or "(in <config path>)". `core.update_settings` checks only the values being set (`StoredSettings`, which reads no environment and no file) and writes the rest of the file back as it was, unknown keys included. So a bad value elsewhere, in the file or the environment, never blocks a change, and `sage config set KEY ""` removes a bad value. `sage providers set ID FIELD` with the value left out prompts for it hidden, which keeps a key out of shell history. When the saved settings still don't load, because of a bad value the change didn't touch, the error says "saved, but …".

A missing key is not a startup error. `sage ui` starts without one.

A provider is checked by asking it which models its credentials can use (`GET /api/providers/{provider}/models`, `sage models available`). The call costs nothing, and it differs by kind: `models.list` on Anthropic and OpenAI (OpenAI's own list is cut to the chat models pydantic-ai knows, and a compatible server lists what it serves), the models with `generateContent` on Google, Google's Gemini models on Vertex AI (Claude is added by name), `ListFoundationManage` plus `ListInferenceProfiles` on Bedrock, and the resource's deployments on Azure when it answers (otherwise each deployment is added by name). The answer is `Available {provider, ok, message, models}`, where each `Candidate` carries its price, if any, and whether it calls tools, so the Manage page can mark a model that needs your price or can't be used. So one call is the credentials check and the Manage page's list of models to add. A provider that still needs a field answers "needs <field>" without a request.

---

## 7. Storage

```
$SAGE_HOME/                        default ~/sage-data
  add.lock                         held while a new paper is checked and written
  runner.lock                      held by the one runner process while it lives
  runner.pid                       the live runner's pid
  runner.log                       the runner's stdout and stderr
  ui.lock                          held by `sage ui` while it serves
  ui.json                          where it serves: url and pid
  papers/<paper_id>/                 the folder the agent works in
    paper.pdf
    paper.md                       the paper view, anchored text         (SAGE, rewritten when the effective document changes)
    pages/pN.png                   every page                            (SAGE)
    pages/pN.md                    a scan page's cited blocks            (agent)
    tables/<anchor>.png            each detected table, upright          (SAGE)
    tables/<anchor>.csv            a table's grid as printed             (agent)
    figures/<anchor>.png           each figure SAGE could locate         (SAGE)
    records/*.yaml                 one file per entity type              (agent)
    records/observations/*.yaml    observation groups                    (agent)
    records/unresolved.yaml        unresolved marks                      (agent)
    .sage/                         SAGE's own state; agents never edit it
      paper.json                   id, filename, sha256, pages, title          (PaperInfo)
      document.json                text blocks and detected tables with boxes   (Document)
      reviews.jsonl                append-only scientist reviews                (Review)
      rendered.json                what the images were rendered from           (so a change renders them again)
      tables/<anchor>.json         a CSV grounded in the text layer, cached
      snapshots/<n>/               copies of the agent's files, one per check that found them changed
      run.lock                     held while a run starts, so only one is live
      runs/<run_id>/
        run.json                   driver, models, budgets, status, usage, counts (RunInfo)
        events.jsonl               the live event stream                        (Event)
        draft.json                 what the files compiled to at the last check (Draft)
        records.json               settled records with statuses                (Settled)
        document.json              the document the run was last checked against (Document)
        quotes.json                each quote's line boxes on the page, derived (QuoteBoxes)
        strikes.json, marks.json   the same-error rule's counts and the marks it set
        permission.json            what an agent SAGE started asks to do, and the answer
        stop                       a request to stop the run, gone once the runner has stopped it
~/.config/sage/config.toml         settings, providers and your models, written by the UI or the `sage` commands
```

- The paper id is the PDF's file name without `.pdf`, with accents dropped ("Straße" is "Strasse"), unsafe characters replaced by `-`, and cut to 100 characters. A clash gets a `-2` suffix. Paper and run ids match `ID` in `store/paths.py`: `[A-Za-z0-9][A-Za-z0-9._-]*`, at most 128 characters. `core` checks every id that comes from the CLI or the API before it reaches a path, and refuses any other as `invalid`.
- Adding the same bytes again returns the existing paper, matched on sha256. The PDF is parsed unlocked. The sha256 check and the writes then happen under an exclusive lock on `add.lock` (`Home.add_lock`), so the same file uploaded twice at once is added once.
- The run id is the UTC start time plus four random hex digits, for example `20261017T081101-3fa2`.
- JSON files are written to a temp file and moved into place with `os.replace`. JSONL files are append-only. A run writes inside its own folder under `.sage/runs/`, and also the paper's `.sage/document.json` when a reparse replaces it. Before a reparse, each older run without its own `document.json` gets a copy of the old one. The agent's files in `records/`, `tables/*.csv` and `pages/*.md` are the agent's to write, so a driver can edit them at any moment. Check reads them as they are and never rewrites them, and each check that finds them changed keeps a snapshot. SAGE rewrites `paper.md` and the PNGs but never an agent's file.
- `core` refuses a second live run on the same paper. Starting a run holds an exclusive lock on `papers/<paper_id>/.sage/run.lock` (`files.exclusive`, an `flock`), so the live-run check and the queued `run.json` are one step and two starts at once can't both pass. The OS drops the lock when its holder exits, so it never goes stale.
- Every SAGE file has a pydantic model, so each one has a schema and is validated on read. The agent's files are validated by compile, which reports a file that doesn't parse as a blocking finding at its line. `sage/store/paths.py` owns the layout.
- Only a missing file reads as absent. A file that exists but won't parse raises `UnreadableError` in `store/files.py`, and `core` turns it into a `corrupt` error that names the file and the reason. A list that meets one, such as `sage runs` with a broken `run.json`, fails with that error instead of leaving the run out. Looking up one run or paper reads only its own files, so a broken file elsewhere doesn't hide it.
- Nothing is held in memory between requests. The paper and run lists come from scanning the folders.

---

## 8. Surfaces

### CLI

Every command takes `--json`. The README's [Command line](../../README.md#command-line) table lists every command, and `uv run sage <command> --help` gives its options.

`main()` in `cli.py` is the one error boundary. A `SageError` prints `error: <message>` and exits 1. Under `--json` it prints `{"error": {"kind", "message"}}` (`core.ErrorBody`), and so does a usage error (a missing argument, an unknown option or command, a bad value) with kind `invalid` and exit 2. `sage add` adds what it can, prints an error line for each file it couldn't add, and then exits 1. Under `--json` it prints the per-file list the API returns. `sage ui` binds its port (`core.listen`) before it prints the URL, so a port in use is a `conflict`. It also holds `ui.lock` while it serves and writes `ui.json` with its URL and pid. A second `sage ui` on the same data folder, on any port, exits 1 with a `conflict` that names the first ("SAGE is already open for <home> at <url> (pid N); use that one, or stop it first"). The other commands work beside the UI, because they share its runner.

`sage extract` is the one verb for extraction. `run` queues runs for SAGE to drive, on its own loop or on a coding agent, and follows one paper's log; with several papers or `--all` it prints a line as each run ends. `--with` takes an agent id (`claude`), an agent and its model (`claude:sonnet`) or a model id, and defaults to the `extract_with` setting, else the first of your models. `--effort` is the model's or the agent's own level, and defaults to the model's or agent's `effort`. `start`, `check` and `done` are the boundary every driver uses on a paper's folder: `start` opens a terminal run and prints its folder, `check` prints each finding as `file:line  target: what to do` and exits 1 while anything blocks (2 when a file doesn't parse at all), and `done -m` is refused while anything blocks and otherwise settles the run. `done` with no run open opens one first. `stop` ends a terminal run at once and asks the runner to stop the rest. `answer` allows or refuses what an agent SAGE started is waiting on, as `status` shows it. `sage agents` lists the coding agents SAGE can start and whether each is ready, `agents offer` starts one in an empty folder to list its models, modes and efforts, and `agents set` sets its model and effort. `sage skill install` copies the SAGE skill to `~/.agents/skills/sage` and links it from `~/.claude/skills/sage`, and `skill status` says whether it is installed and current. `sage runner` is a hidden command: `core` starts it, and run by hand while another runner is live it leaves at once.

### HTTP API

`api.py` serves the routes under `/api/`, one `core` call each. `sage openapi` prints the schema that the web client is generated from (`cd web && pnpm gen:api`). Errors come back as `core.ErrorBody` (`{kind, message}`) with status 400, 404, 409, 412 (`missing_key`: a provider lacks a key or another field) or 500 (`corrupt`, a file on disk that won't parse or can't be written). A request the routes' types refuse is a 422 in the same shape, with kind `invalid`. An unknown path under `/api/` is a 404, never the UI shell.

Settings, providers and models:

| Route | Does |
|---|---|
| `GET /api/settings`, `PUT /api/settings` | Read and change settings (§6) |
| `GET /api/provider-kinds` | The kinds of provider and their fields |
| `GET /api/providers` | Your providers, each with what it still needs |
| `PUT /api/providers/{provider}` | Adds a provider (body `{kind, values}`) or changes one (`{values}`) |
| `DELETE /api/providers/{provider}` | Removes a provider you added |
| `GET /api/providers/{provider}/models` | Asks a provider which models its credentials can use (§6) |
| `GET /api/models` | Your models as `ModelOption`s: id, provider, title, input and output price per million tokens, whether the price is yours, the efforts it has and the one it runs at |
| `POST /api/models/effort` | Sets one of your models' effort: `{id, effort}` |
| `POST /api/models` | Adds a model, or changes its price (body `{id, title, input_usd, output_usd, cache_read_usd, cache_write_usd}`) |
| `POST /api/models/remove` | Removes models (body `{ids}`) |
| `GET /api/models/describe?id=` | A model as SAGE sees it before it is added |

`sage providers` lists the providers with what each needs, `sage providers kinds` the kinds and their fields, and `sage providers add`, `set` and `remove` change them. `sage models` prints your models with each one's price (marking yours), whether its provider is ready and and which one Extract uses. `sage models available` prints what the providers say, and exits 1 if a check fails or no provider is ready. `sage models add` and `remove` change the list, and `--cache-price` sets what cache reads and writes cost.

`POST /api/runs` queues runs. Its body is `{paper_ids, using, effort}`, where `using` is an agent id, `agent:model` or a model id (null means the `extract_with` setting), and it answers with a `RunInfo` per paper. A null `paper_ids` means every paper with no data and nothing live. It checks everything first, so a model that isn't one of yours, a missing key, an agent that isn't installed or signed in, or a paper with a live run queues nothing. `POST /api/runs/{id}/continue` takes `{guidance, using, effort}` (the earlier run's driver and model by default) and `POST /api/runs/{id}/resume` takes `{guidance}`. `POST /api/runs/stop` takes `{run_ids}`, null for every queued and running run, and answers with those runs. `GET /api/runs` and `GET /api/runs/{id}` list and read runs. Runs queued together are independent runs, which the runner takes `parallel_runs` at a time.

Extraction on a paper's folder, agents and the skill:

| Route | Does |
|---|---|
| `POST /api/papers/{id}/extract/start` | Opens a terminal run (body `{guidance}`). Answers with the folder and the run. |
| `POST /api/papers/{id}/extract/check` | Checks the folder, recording the check when a run is open. Answers with a `CheckReport`. |
| `POST /api/papers/{id}/extract/done` | Body `{summary}`. Refused while anything blocks, otherwise settles the run (`DoneReport`). |
| `POST /api/papers/{id}/extract/answer` | Body `{allow}`. Answers what an agent SAGE started is waiting on. |
| `POST /api/extract/stop` | Body `{paper_ids}`, null for every open run. |
| `GET /api/extract/status` | Each paper's folder and its open or latest run. `paper_id` narrows it. |
| `GET /api/agents` | The coding agents, each `ready`, `not_installed` or `not_signed_in`, with the fix and the runs it is driving |
| `GET /api/agents/{id}/offer` | The models and modes the agent offers, and the efforts of `?model=` (else its default) |
| `PUT /api/agents/{id}` | Sets the agent's `model` and `effort`; a field left out stays, an empty string clears it |
| `GET /api/skill`, `POST /api/skill/install` | Whether the skill is installed and current, and installing it |
| `GET /api/changes` | One SSE stream of `Change` notices (§5) |

`POST /api/papers` answers per file, so one bad file doesn't stop the rest: `Added {filename, paper, new}` or `NotAdded {filename, error}`, where `error` is an `ErrorBody`.

`PaperSummary` is one paper as Home, the Paper screen and `sage papers` show it: `latest_run` (the live run, else the newest), `run_count`, `data_run` (the run whose records stand for the paper), `to_check` and `looks_good` (unreviewed items on `data_run`), and `live_stage` (the live run's stage, or null before it starts one). A resumed run keeps standing as `data_run` while it runs again, so its earlier records stay in view.

### UI

The UI is specified in `04-ui.md`. It has four screens, Home (the papers), Paper (with Check, Data and Read modes, a This run drawer and History), Manage (the skill, the agents, your models and the providers) and Settings. `/runs/<run_id>` redirects to that run on its paper, and an unknown one shows a Not found page. `sage ui` serves the build from `sage/web_dist/` on 127.0.0.1:8765 by default, and `--host` binds another interface.

### Deployment

SAGE is single-user, with no authentication. Anyone who can reach the port can use it, so bind beyond 127.0.0.1 only on a trusted network.
