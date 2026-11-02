# SAGE

SAGE reads agronomy and ecology field-study papers (PDFs) and extracts the experiments into structured records: citations, sites, species, crops, methods, variables, treatments, management events and observations. Every value carries evidence, meaning the anchor and the verbatim text in the paper it came from. You check the results in a review screen that draws each value's source on the PDF.

## How it works

1. **Parse.** `sage add` reads the PDF's text layer with pdftext and stores every text block with its page and box. It also writes `paper.md`, the anchored text an agent reads, and an image of every page, table and figure, into the paper's folder.
2. **Extract.** An agent reads `paper.md` and the images and writes files in the paper's folder: the records as YAML, a CSV for each table it needs, and a transcription of each scanned page it cites. Code matches every table cell to the PDF's own text, so each number has a box on the page. `sage extract check` answers with every problem at the `file:line` to fix: a quote that isn't in the paper, a number missing from its quote, a table not yet mapped, a Variable with no Observation. The agent fixes them, checks again, and ends with `sage extract done` when no blocking problem is left.
3. **Settle.** Code, never the agent, gives every value a status. A value is verified when it passed every check, flagged when a person should look, or unresolved with a cause such as "only shown in a figure". Code also recomputes every derived number: decimal coordinates, dates and unit conversions.
4. **Review.** You accept, edit or reject values against the PDF. Every decision is saved with the paper.

Three kinds of agent can do step 2 behind the same `check` and `done`. SAGE's own agent runs on a model you reach with an API key. A coding agent that SAGE starts (Claude Code, Codex, Gemini CLI or OpenCode) works under its own sign-in, with no key in SAGE. And an agent in your own terminal works the folder with the SAGE skill.

The data model, the evidence rules and the statuses are defined in [docs/domain.md](docs/domain.md). The architecture is in [docs/design/](docs/design/).

## Install

You need Python 3.13, [uv](https://docs.astral.sh/uv/), Node.js and [pnpm](https://pnpm.io/). Node is needed only to build the UI.

```bash
git clone https://github.com/PecanProject/sage.git
cd sage
uv sync
cd web && pnpm install && pnpm build && cd ..
```

`pnpm build` writes the UI to `sage/web_dist/`, which `sage ui` serves. Without it, `sage ui` serves only the API.

To work on SAGE, also install [gitleaks](https://github.com/gitleaks/gitleaks) (`brew install gitleaks`). `scripts/check` runs every static check, including a secret scan, React Doctor and knip, and it fails if gitleaks is missing.

## Set up a provider

You need a provider only for SAGE's own agent. A coding agent signs in on its own (see [Extract with a coding agent](#extract-with-a-coding-agent)). SAGE reaches models through providers. Anthropic, OpenAI, Google Gemini and Amazon Bedrock are built in. A fresh install has no models: you set up a provider, then add the models you want, and the first one is what Extract uses unless you pick another. The cost limit is $1.00 per paper. Set up a provider in one of three ways, here with Anthropic:

- Open **Manage** (the chip icon in the top bar of `uv run sage ui`), click **Set up** on the Anthropic row under **Providers**, paste the key, and save it. SAGE checks it and says how many models the provider offers and how many of them can extract.
- Run `uv run sage providers set anthropic api_key`, which asks for the key at a hidden prompt, then `uv run sage models available` to check it.
- Export `SAGE_ANTHROPIC_API_KEY` or `ANTHROPIC_API_KEY`. A field set in the environment shows "Set by …" on the Manage page and can't be edited there.

`uv run sage providers` lists your providers and what each still needs. The other built-in providers take their key the same way, as `openai`, `google` and `bedrock`.

To reach models from somewhere else, add a provider of your own with **Add a provider** on the Manage page, or `sage providers add ID KIND`. The id is up to 32 lowercase letters, digits and `-`, and your models on it are written `id:name`. `sage providers kinds` lists the kinds and their fields:

| Kind | For | Fields |
|---|---|---|
| `openai` | OpenAI, or any server with an OpenAI-compatible API: a company gateway, LiteLLM, OpenRouter, vLLM, Ollama | `api_key`, `base_url` |
| `anthropic` | Anthropic, or a gateway that speaks its API | `api_key`, `base_url` |
| `azure` | OpenAI models deployed in your Azure resource. A model goes by its deployment name. | `base_url` (your endpoint), `api_key`, `api_version` |
| `google` | Gemini through the Gemini API | `api_key` |
| `vertex` | Gemini and Claude on Google Cloud Vertex AI | `project`, `region` (default `global`), `credentials_file` |
| `bedrock` | Amazon Bedrock | `region` (default `us-east-1`), `api_key`, `aws_profile`, `base_url` |

Vertex AI signs in with Application Default Credentials (`gcloud auth application-default login`), or with a service account key file. Bedrock signs in with a Bedrock API key from the AWS console, or with a profile from `~/.aws` such as an SSO login. It reads no other AWS credentials from the environment, and the model has to be enabled in the region on your account. For example, a company gateway:

```bash
uv run sage providers add work openai --name "Company gateway"
uv run sage providers set work base_url https://llm.example.com/v1
uv run sage providers set work api_key         # asks for the key
uv run sage models available work              # checks it and lists what it offers
uv run sage models add work:gpt-5.1 --price 1.25 10
```

Then add the models you want. `sage models available openai` lists what a provider offers, and `sage models add openai:gpt-5.1` adds one. **Add a model** on the Manage page does the same. It searches what your ready providers offer, and typing `provider:name` adds a model they don't list. Only your models appear in the extraction pickers, and a run refuses any other. To make one the default, use **Use by default** in its row, or run `sage config set extract_with openai:gpt-5.1`. A model is named plainly (`gpt-5.1`) or with its provider (`openai:gpt-5.1`), and SAGE saves it as `provider:name` either way. A plain name has to be one SAGE's pydantic-ai knows on a built-in provider, so a model newer than that, or on a provider of your own, has to be written with its provider. Many Bedrock ids contain a `:` of their own, such as a `-v1:0` suffix, so write those with the provider too, as in `bedrock:global.anthropic.claude-haiku-5-5`.

SAGE stops a run at its cost limit, so every model needs a price. It looks the price up by provider and endpoint (a model on OpenRouter gets OpenRouter's price), always at the base tier. A model it can't price, such as one on a gateway or a local server, can't be added until you give yours, per million tokens in and out. Use `--price IN OUT`, or **Set price…** in the model's row, and enter 0 for a model that costs nothing. Your price also overrides one SAGE has. Cache reads and writes have prices too. SAGE uses the model's usual discount, or the input price, unless you give them with `--cache-price READ WRITE` beside `--price`. SAGE refuses a model that can't call tools, because the agent works only through them.

The Manage page, the Settings page, `sage providers`, `sage models` and `sage config` write `~/.config/sage/config.toml` with mode 0600, and the keys sit under `[providers.<id>]`. `sage ui` starts without a key. Only starting an extraction on SAGE's own agent needs one.

## Extract your first paper

In the browser:

1. Run `uv run sage ui` and open http://127.0.0.1:8765.
2. Drop a PDF on the **Papers** list.
3. Open the paper and click **Extract data**. **Extract with** picks a model or a ready coding agent, and starts as your `extract_with` setting, else the first of your models.
4. When the extraction finishes, the paper opens in **Check** mode.

From the command line:

```bash
uv run sage providers set anthropic api_key  # once; asks for the key
uv run sage models available                 # checks the key
uv run sage add path/to/paper.pdf            # prints the paper id
uv run sage extract run <paper_id>           # starts the extraction and follows its log
uv run sage show <run_id>                    # every record, its status and why
uv run sage export <run_id> -o paper.xlsx    # the PEcAn curation workbook
```

Every extraction runs in one background process per data folder, which SAGE starts when needed. It runs `parallel_runs` extractions at a time (3 by default) and queues the rest, and it keeps going if you close the terminal or stop `sage ui`. Ctrl-C in `sage extract run` stops watching, not the run. `sage extract stop` stops it. An extraction started from the CLI shows up live in the UI, and the other way round.

## Extract with a coding agent

SAGE can hand a paper to Claude Code, Codex, Gemini CLI or OpenCode. The agent works under its own sign-in and tools, so SAGE needs no key for it and sets no cost limit, only a time limit (60 minutes by default). `uv run sage agents` shows which agents are installed and signed in, and the command that fixes one that isn't. Pick one with `--with`:

```bash
uv run sage agents                                  # which are ready
uv run sage extract run <paper_id> --with claude    # or codex, gemini, opencode
uv run sage extract run <paper_id> --with claude:sonnet   # an agent and its model
uv run sage agents offer claude --model sonnet      # its models, modes and that model's efforts; spends no tokens
```

`sage config set extract_with claude` makes an agent the default for **Extract** and `sage extract run`. By default the agent runs whatever it asks to. With `sage config set agents_ask true`, SAGE allows the agent to edit its own files in the paper's folder and to run `sage extract check` and `done`, refuses edits to SAGE's files, and waits for you on anything else. `sage extract status` shows what it is waiting on, and `sage extract answer <paper_id> --allow` (or `--deny`) answers, as the Paper screen does.

To work in your own terminal instead, install the SAGE skill once:

```bash
uv run sage skill install    # copies it to ~/.agents/skills/sage, linked from ~/.claude/skills/sage
```

Then open Claude Code, Codex or Gemini CLI and ask it to extract the paper, for example "Extract the paper `<paper_id>` with SAGE". The skill (`sage/agents/skill/`) tells the agent to open a run with `sage extract start`, edit the files in the paper's folder, run `sage extract check` until nothing blocks, and finish with `sage extract done -m "…"`. `sage` has to be on the agent's PATH. The run shows up in the UI as it goes, and `sage extract stop <paper_id>` ends it.

## The web UI

The UI has four screens.

| Screen | Address | What it does |
|---|---|---|
| Papers | `/` | Lists your papers, each with one status: not extracted, extracting, how many items are left to check, all checked, or how its last extraction ended when none found data. Drop PDFs anywhere on the page to add them. **Extract all** queues every paper with no data, and **Stop all** stops every live extraction. |
| Paper | `/papers/<paper_id>` | The paper in one of three modes. **Check** shows each data item as a card in the margin beside its highlight, like suggestions in Google Docs. Picking one scrolls the PDF to its source. Many values from one table share one card that opens to a grid shaped like the paper's table. **Data** shows the items as tables, one tab per type. **Read** shows the PDF with every highlight. **Export** downloads the PEcAn curation workbook (Excel, or one CSV per tab) or everything (JSON). Export also offers the original PDF. The **Runs** menu holds This run (the live log and usage), History, Extract again and Continue with guidance, which reads Resume for an extraction that failed or was stopped. While an extraction runs, Stop extraction takes the place of the last two. |
| Manage | `/manage`, or the chip icon | The SAGE skill for an agent in your own terminal, the coding agents SAGE can start, and your models with their price and which one Extract uses. **Add a model** lists what your ready providers offer. **Providers** has a row for each provider that opens to its key and other fields, checks it, and adds your own with **Add a provider**. |
| Settings | `/settings`, or the gear | The cost limit, the theme and a link to the Manage page, then a section for each group of settings. Changes save together from a bar at the bottom, which also discards them. |

`/runs/<run_id>` opens that extraction on its paper, with its details.

Check keys:

| Key | Action |
|---|---|
| `j` / `k` | Next or previous item, or cell in a table |
| Arrow keys | Move between a table's cells |
| `a` | Accept |
| `e` | Edit. Enter saves and Esc cancels. |
| `r` | Not in the paper, with an optional note |
| `Shift+A` | Accept a whole cluster, such as a table |
| `/` | Switch between To check and All |
| `c` | Change an item you already reviewed |
| `+` / `-` | Zoom the paper in or out. Ctrl or ⌘ with the wheel, or a pinch, zooms at the pointer. |
| `0` | Fit the page again |
| `?` | Every shortcut |

Every review is saved as soon as you make it, and the latest review of an item wins. "Accept N good" accepts every item that passed every check and has no review yet.

## Command line

Every command takes `--json` for machine-readable output. Under `--json` an error prints as `{"error": {"kind": …, "message": …}}`. `uv run sage <command> --help` gives the full options.

| Command | What it does |
|---|---|
| `sage add PDF...` | Adds PDFs to the library. The same file twice is added once. A file that can't be added prints an error and the command exits 1, but the others are still added. |
| `sage papers` | Lists the library like the Papers screen: each paper's title, pages, latest extraction and its status (with the step while it runs), how many items are left to check, and how many look good. |
| `sage extract run PAPER_ID... \| --all [--with AGENT\|MODEL] [--effort E]` | Queues extractions. A background process runs them, `parallel_runs` at a time. One paper is followed through its log, and several print a line as each ends. `--all` takes every paper with no data that isn't being extracted. `--no-follow` returns at once. `--with` is an agent (`claude`, `codex`, `gemini`, `opencode`, or `claude:sonnet` for its model) or one of your models for SAGE's own agent, and defaults to the `extract_with` setting, else the first of your models. `--effort` is the model's or the agent's own level (`sage models`, `sage agents offer`), and defaults to its `effort`. |
| `sage extract run --resume RUN_ID [--guidance TEXT]` | Continues an unfinished extraction by SAGE's own agent from the files in the folder, under what is left of its budget. One that used it all is refused. |
| `sage extract run --continue RUN_ID [--guidance TEXT]` | Starts a new extraction on the folder as an ended one left it, with a budget of its own, told what to add or fix. It keeps the earlier one's agent or model unless `--with` says otherwise. Your reviews reach the agent through `check`. |
| `sage extract start PAPER_ID [--guidance TEXT]` | Opens a run you drive from a terminal and prints the paper's folder. |
| `sage extract check PAPER_ID [--all]` | Checks the folder's files. Every problem is `file:line  target: what to do`, at most `error_list_cap` of each severity unless `--all`. Exits 1 while anything blocks, 2 when a file doesn't parse at all. |
| `sage extract done PAPER_ID -m SUMMARY` | Finishes the run. Refused while anything blocks, otherwise the records are settled. |
| `sage extract stop PAPER_ID... \| --all` | Stops extractions, queued ones included. The files stay for the next run, and a stopped run keeps what it found. |
| `sage extract answer PAPER_ID --allow\|--deny` | Answers what a coding agent SAGE started is waiting on. |
| `sage extract status [PAPER_ID]` | Each paper's folder and its open or latest run. |
| `sage agents` | Lists the coding agents SAGE can start, whether each is installed and signed in, and how to fix one that isn't. |
| `sage agents offer AGENT [--model M]` | Starts the agent in an empty folder and lists the models and modes it offers, and the efforts of the model (its default unless `--model`). Spends no tokens. |
| `sage agents set AGENT [--model M] [--effort E]` | Sets the model and effort the agent runs with, in its own words. An empty value goes back to its default. A run checks them when the agent starts. |
| `sage skill install` / `sage skill status` | Installs the SAGE skill where Claude Code, Codex and Gemini CLI look for skills, or says whether it is installed and current. |
| `sage runs [PAPER_ID]` | Lists extractions, newest first. |
| `sage show RUN_ID` | Shows an extraction's records, each one's status, and why anything isn't verified. |
| `sage logs RUN_ID [--follow]` | Prints an extraction's event log. `-f` keeps streaming until it ends. |
| `sage export RUN_ID [--format xlsx\|csv\|json] [-o PATH]` | Exports the records. `xlsx` (the default) is the PEcAn curation workbook, one tab per table, values in the paper's units, every row with its status (see `docs/pecan/workbook.md`). `csv` is the same tabs as CSVs in a zip. `json` holds every record as settled and goes to stdout without `-o`. |
| `sage queue RUN_ID` | Lists every record of a run, missing first, then check this, then looks good, with each one's label, status and latest review. |
| `sage review RUN_ID RECORD_ID accept\|edit\|reject` | Records a review, like Check mode. `--field` and `--value` give an edit's correction, and `--note` says why. `sage review RUN_ID --accept-looks-good` accepts every record that looks good and has no review yet. |
| `sage config list` | Shows every setting, its value, and whether it comes from the environment, the config file or the default. |
| `sage config get KEY` | Prints one setting's value as JSON. |
| `sage config set KEY VALUE` | Changes a setting in `config.toml`. An empty value restores the default. Providers and models have their own commands, and `config set` refuses them. |
| `sage providers` | Lists your providers with their kind and what each still needs. |
| `sage providers kinds` | Lists the kinds of provider you can add and the fields each takes. |
| `sage providers add ID KIND [--name N]` | Adds a provider of your own. Give it its fields with `providers set`. |
| `sage providers set ID FIELD [VALUE]` | Sets one field of a provider, such as `api_key`, `base_url`, `region`, `project`, `api_version` or `aws_profile`. An empty value clears it. With the value left out it asks for it at a hidden prompt, which keeps a key out of your shell history. A field set in the environment is refused. |
| `sage providers remove ID` | Removes a provider you added. The built-in ones can't be removed, and neither can one your models use. |
| `sage models` | Lists your models with their price per million tokens (marking yours), whether their provider is ready, and whether the extraction setting uses them. |
| `sage models available [PROVIDER]` | Asks a provider which models its credentials can use, through a call it doesn't charge for, so it tests them too. With no provider it asks each one that is ready. It exits 1 if a check fails or no provider is ready. |
| `sage models add ID [--price IN OUT [--cache-price READ WRITE]]` / `sage models remove ID...` | Adds a model to yours, as `provider:name`, or removes models. `--price` gives your dollars per million input and output tokens, which a model SAGE can't price needs. `--cache-price` gives your price per million tokens read from and written to the prompt cache, and needs `--price`. Adding a model again with a price changes it. Removing the model Extract uses makes the first model left the default. |
| `sage models set ID --effort E` | Sets how hard SAGE's own agent has the model think: one of the levels `sage models --json` lists under `efforts`. A model that doesn't think has none. |
| `sage ui [--host H] [--port P]` | Serves the UI and the API on 127.0.0.1:8765 by default. Only one runs per data folder. A second, on any port, exits 1 and names the first. |
| `sage openapi` | Prints the API's OpenAPI schema. |

`sage ui` has no login. Use `--host 0.0.0.0` only on a network you trust, because anyone who can reach the port can use it.

## Settings and environment variables

`sage config list` shows every setting with its description, and the Settings page shows them too. Providers and your models are on the Manage page. Each one reads the environment variable `SAGE_` plus its name in capitals, for example `SAGE_MAX_COST_USD`. The environment wins over `config.toml`, and `config.toml` wins over the defaults.

| Variable | Default | What it sets |
|---|---|---|
| `SAGE_HOME` | `~/sage-data` | Where papers, extractions and reviews live |
| `SAGE_CONFIG` | `~/.config/sage/config.toml` | The config file the Manage page, the Settings page and the `sage` commands write |
| `SAGE_ANTHROPIC_API_KEY` or `ANTHROPIC_API_KEY` | unset | The `anthropic` provider's API key |
| `SAGE_OPENAI_API_KEY` or `OPENAI_API_KEY` | unset | The `openai` provider's API key |
| `SAGE_GOOGLE_API_KEY`, `GEMINI_API_KEY` or `GOOGLE_API_KEY` | unset | The `google` provider's API key |
| `SAGE_BEDROCK_API_KEY` or `AWS_BEARER_TOKEN_BEDROCK` | unset | The `bedrock` provider's API key |
| `SAGE_BEDROCK_REGION`, `AWS_REGION` or `AWS_DEFAULT_REGION` | `us-east-1` | The `bedrock` provider's region |

The last five apply to the four built-in providers only, and the first variable set in each row wins. A field they set is locked: the Manage page shows "Set by …" and `sage providers set` refuses it. A provider you add reads no environment variables.

## Where the data lives

Each paper has a folder under `$SAGE_HOME/papers/<paper_id>/`, and an agent works in it. It holds the PDF (`paper.pdf`), `paper.md` and the page, table and figure images that SAGE writes for the agent to read, and the files the agent writes: `records/` (YAML, one file per entity type, and `records/observations/` for tables), `tables/<anchor>.csv` for each table it transcribed, and `pages/pN.md` for each scanned page it cites. SAGE's own files are under `.sage/`: the parsed text (`document.json`), the reviews (`reviews.jsonl`), a numbered snapshot of the agent's files for each check that found them changed, and one folder per extraction under `runs/<run_id>/`. An extraction folder holds its settings and usage (`run.json`), its event log (`events.jsonl`), the records the files compiled to at the last check (`draft.json`), the settled records (`records.json`) and the parsed paper it was checked against (`document.json`). These are plain JSON, YAML and CSV files. The data folder also holds `runner.log`, the background runner's output. [docs/design/01-stack-and-architecture.md](docs/design/01-stack-and-architecture.md) lists every file.

## Development

[AGENTS.md](AGENTS.md) has the layer rules and the conventions. `scripts/check` runs every static check, and `scripts/dev` runs the whole stack live: the API with `uvicorn --reload`, the Vite dev server with hot reload, and a watcher (watchfiles) that regenerates the web's API types whenever Python changes. Run `SAGE_WEB_HOST=0.0.0.0 scripts/dev` to open it from another device. There is no test suite. Quality comes from `scripts/check` and the runtime checker.

## License

BSD 3-Clause. See [LICENSE](LICENSE).
