# SAGE

SAGE reads agronomy and ecology field-study papers (PDFs) and extracts the experiments into structured records: citations, sites, species, crops, methods, variables, treatments, management events and observations. Every value carries evidence, meaning the anchor and the verbatim text in the paper it came from. You check the results in a review screen that draws each value's source on the PDF.

## How it works

1. **Parse.** `sage add` reads the PDF's text layer with pdftext and stores every text block with its page and box.
2. **Transcribe tables.** At the start of an extraction, a model transcribes each table image into a grid of cells. Code then matches every cell to the PDF's own text, so each number has a box on the page.
3. **Extract.** One Claude agent reads the paper and writes records into a draft. A deterministic checker answers every write with exact errors: a quote that isn't in the paper, a number missing from its quote, a table not yet extracted, a treatment level with no record. The agent fixes them and finishes when no blocking problem is left, or when it runs out of its turn or cost budget.
4. **Settle.** Code, never the model, gives every value a status. A value is verified when it passed every check, flagged when a person should look, or unresolved with a cause such as "only shown in a figure". Code also recomputes every derived number: decimal coordinates, dates and unit conversions.
5. **Review.** You accept, edit or reject values against the PDF. Every decision is saved with the paper.

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

## Add your API key

SAGE calls Anthropic, OpenAI, Google or Amazon Bedrock. The default model is Claude Haiku 5.5, with a $1.00 cost limit per paper, so the Anthropic key is the one most people need. Set it in one of three ways:

- Open **Settings** (the gear in the top bar of `uv run sage ui`), paste the key, and click **Test** to check it.
- Run `uv run sage config set anthropic_api_key <key>`, then `uv run sage config test-key`.
- Export `SAGE_ANTHROPIC_API_KEY` or `ANTHROPIC_API_KEY`. A value set in the environment shows as read-only in Settings.

To use another provider, set `extraction_model` to one of its models and give it the matching key: `openai_api_key` for a `gpt` or `o`-series model, `google_api_key` for a `gemini` one. A model is named plainly (`gpt-5.1`), or with its provider when the name alone is new to SAGE (`openai:gpt-5.2`). Settings shows the key the chosen model needs; the others sit under Advanced.

For Amazon Bedrock, write the model with its `bedrock:` prefix, since Bedrock's own ids contain a `:` — `bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0`. It needs `bedrock_api_key` (a Bedrock API key from the AWS console, or `AWS_BEARER_TOKEN_BEDROCK`) and `bedrock_region`, which defaults to `us-east-1`. The model has to be enabled in that region on your account. An access key pair, an SSO profile or an instance role is not read.

Settings and `sage config` write `~/.config/sage/config.toml` with mode 0600. `sage ui` starts without a key. Only starting an extraction needs one.

## Extract your first paper

In the browser:

1. Run `uv run sage ui` and open http://127.0.0.1:8765.
2. Drop a PDF on the **Papers** list.
3. Open the paper and click **Extract data**.
4. When the extraction finishes, the paper opens in **Check** mode.

From the command line:

```bash
uv run sage add path/to/paper.pdf            # prints the paper id
uv run sage run <paper_id>                   # starts the extraction and follows its log
uv run sage show <run_id>                    # every record, its status and why
uv run sage export <run_id> -o paper.xlsx    # the PEcAn curation workbook
```

An extraction runs in a background process, so it keeps going if you close the terminal or stop `sage ui`. Ctrl-C in `sage run` stops watching, not the run. An extraction started from the CLI shows up live in the UI.

## The web UI

The UI has two screens and a Settings dialog.

| Screen | Address | What it does |
|---|---|---|
| Papers | `/` | Lists your papers, each with one status: not extracted, extracting, how many items are left to check, all checked, or how its last extraction ended when none found data. Drop PDFs anywhere on the page to add them. |
| Paper | `/papers/<paper_id>` | The paper in one of three modes. **Check** shows each data item as a card in the margin beside its highlight, like suggestions in Google Docs. Picking one scrolls the PDF to its source. Many values from one table share one card that opens to a grid shaped like the paper's table. **Data** shows the items as tables, one tab per type. **Read** shows the PDF with every highlight. **Export** downloads the PEcAn curation workbook (Excel, or one CSV per tab) or everything (JSON). Export also offers the original PDF. The **Runs** menu holds This run (the live log and usage), History, Extract again and Continue with guidance, which reads Resume for an extraction that failed or was stopped. While an extraction runs, Stop extraction takes the place of the last two. |
| Settings | `/settings`, or the gear | The API key, the model, the cost limit and the theme. Every other setting is under **Advanced**. |

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
| `sage run PAPER_ID [--model M] [--effort E]` | Starts an extraction and follows its log. `--no-follow` returns at once. `--effort` is `low`, `medium`, `high`, `xhigh` or `max`. |
| `sage run --resume RUN_ID [--guidance TEXT]` | Continues an unfinished extraction from its saved draft, under what is left of its budget. One that used it all is refused. |
| `sage run --continue RUN_ID [--guidance TEXT]` | Starts a new extraction from an ended one's records, with a budget of its own, told what to add or fix. Guidance is required unless the earlier one ran out of budget. Your edits, rejections and review notes go too, unless `--no-review-notes`. |
| `sage run-all [PAPER_ID...] [--jobs N] [--model M] [--effort E]` | Extracts many papers in one background batch, `--jobs` at a time (default: the `batch_jobs` setting). With no ids it takes every paper that has no records yet. Watch it with `sage runs`. |
| `sage stop RUN_ID` | Stops a running extraction. It keeps what it found, and `--resume` carries it on. |
| `sage runs [PAPER_ID]` | Lists extractions, newest first. |
| `sage show RUN_ID` | Shows an extraction's records, each one's status, and why anything isn't verified. |
| `sage logs RUN_ID [--follow]` | Prints an extraction's event log. `-f` keeps streaming until it ends. |
| `sage export RUN_ID [--format xlsx\|csv\|json] [-o PATH]` | Exports the records. `xlsx` (the default) is the PEcAn curation workbook, one tab per table, values in the paper's units, every row with its status (see `docs/pecan/workbook.md`). `csv` is the same tabs as CSVs in a zip. `json` holds every record as settled and goes to stdout without `-o`. |
| `sage queue [RUN_ID]` | Lists what is left to check. With no run, each paper's extraction with data and its counts. With a run, every record, missing first, then check this, then looks good, with each one's label, status and latest review. |
| `sage review RUN_ID RECORD_ID accept\|edit\|reject` | Records a review, like Check mode. `--field` and `--value` give an edit's correction, and `--note` says why. `sage review RUN_ID --accept-looks-good` accepts every record that looks good and has no review yet. |
| `sage config list` | Shows every setting, its value, and whether it comes from the environment, the config file or the default. |
| `sage config get KEY` | Prints one setting's value as JSON. |
| `sage config set KEY VALUE` | Changes a setting in `config.toml`. An empty value restores the default. |
| `sage config test-key` | Asks the extraction model's provider whether the saved API key works. It costs nothing. Only Anthropic has a free call to check with, so an OpenAI, Google or Bedrock key is reported as saved but unchecked. |
| `sage ui [--host H] [--port P]` | Serves the UI and the API on 127.0.0.1:8765 by default. |
| `sage openapi` | Prints the API's OpenAPI schema. |

`sage ui` has no login. Use `--host 0.0.0.0` only on a network you trust, because anyone who can reach the port can use it.

## Settings and environment variables

`sage config list` and the Settings dialog show every setting with its description. Each one reads the environment variable `SAGE_` plus its name in capitals, for example `SAGE_MAX_COST_USD`. The environment wins over `config.toml`, and `config.toml` wins over the defaults.

| Variable | Default | What it sets |
|---|---|---|
| `SAGE_HOME` | `~/sage-data` | Where papers, extractions and reviews live |
| `SAGE_CONFIG` | `~/.config/sage/config.toml` | The config file the Settings dialog and `sage config` write |
| `SAGE_ANTHROPIC_API_KEY` or `ANTHROPIC_API_KEY` | unset | The Anthropic API key |
| `SAGE_OPENAI_API_KEY` or `OPENAI_API_KEY` | unset | The OpenAI API key |
| `SAGE_GOOGLE_API_KEY` or `GOOGLE_API_KEY` | unset | The Google API key |
| `SAGE_BEDROCK_API_KEY` or `AWS_BEARER_TOKEN_BEDROCK` | unset | The Amazon Bedrock API key |
| `SAGE_BEDROCK_REGION`, `AWS_REGION` or `AWS_DEFAULT_REGION` | `us-east-1` | The region a `bedrock:` model is called in |

## Where the data lives

Each paper has a folder under `$SAGE_HOME/papers/<paper_id>/` that holds the PDF, the parsed text (`document.json`), the reviews (`reviews.jsonl`), and one folder per extraction under `runs/<run_id>/`. An extraction folder holds its settings and usage (`run.json`), its event log (`events.jsonl`), the agent's draft (`draft.json`), the settled records (`records.json`) and the parsed paper it read (`document.json`). These are plain JSON files. [docs/design/01-stack-and-architecture.md](docs/design/01-stack-and-architecture.md) lists every file.

## Development

[AGENTS.md](AGENTS.md) has the layer rules and the conventions. `scripts/check` runs every static check, and `scripts/dev` runs the whole stack live: the API with `uvicorn --reload`, the Vite dev server with hot reload, and a watcher (watchfiles, a dev dependency) that regenerates the web's API types whenever Python changes. Run `SAGE_WEB_HOST=0.0.0.0 scripts/dev` to open it from another device. There is no test suite. Quality comes from `scripts/check` and the runtime checker.

## License

BSD 3-Clause. See [LICENSE](LICENSE).
