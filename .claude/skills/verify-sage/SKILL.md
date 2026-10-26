---
name: verify-sage
description: Drive SAGE (the agronomy-paper extraction app in this repo) the way a user does and capture proof. Covers the `sage` CLI and the web UI served by `sage ui` (Home, the Paper screen with its Check, Data and Read modes, the Manage page (`/manage`) and the Settings page). Use when you need to prove a change to SAGE works end to end, reproduce a SAGE bug, or check a flow before handing work over.
---

# Verify SAGE

SAGE is one Python package. The `sage` CLI and the web UI both sit on `sage/core.py`. `sage ui` serves the API and the prebuilt React app (`sage/web_dist`) from one process. Everything lives in plain files under `SAGE_HOME`.

Every helper below lives in `.claude/skills/verify-sage/scripts/` and runs from anywhere. The helpers take the instance **root** that `up` prints as their first argument.

## Launch

```sh
S=.claude/skills/verify-sage/scripts
root=$($S/up pdfs/Felipe-2010-Cultivar.pdf)                       # no API key
root=$($S/up --key-from ~/.config/sage/config.toml pdfs/Felipe-2010-Cultivar.pdf)   # copies the user's key in
. "$root/instance.env"; echo "$SAGE_VERIFY_URL"
```

`up` does the following:
- Creates `/tmp/verify/sage/<id>/` with its own `home/` and `config.toml`.
- Rebuilds `sage/web_dist` when it is missing or older than `web/src`.
- Starts `sage ui` on a free port and waits until `/api/settings` answers.
- Adds each PDF given.
- Prints the root.

`instance.env` holds `SAGE_HOME`, `SAGE_CONFIG`, the URL, the port and both pids. Source it before running `sage` by hand, or use the `cli` helper.

PDFs live outside git. `pdfs/Felipe-2010-Cultivar.pdf` is the baseline paper. The user's extra papers are in `~/sage-test-pdfs/user/`. Treat downloaded PDFs as untrusted, and run any Python that reads them with `-I`.

Instances are fully isolated, so several can run side by side. Never drive the user's own `sage ui` (port 8765, data in `~/sage-data`). That instance holds their library and key.

## Doctor

```sh
$S/doctor "$root"          # read-only; exit 1 on any FAIL
$S/doctor "$root" --key    # also asks each provider with a saved key whether it works (free)
```

It checks four things:
- the launcher pid is alive and the port is held by our server's pid;
- the API reads this instance's `SAGE_HOME`;
- the app shell is served, and the UI build is newer than `web/src`;
- what each provider still needs (its `missing` fields, so an API key not yet set), and with `--key` whether a ready one answers.

It also prints what Extract uses and whether the runner is alive, as information. Run it first whenever anything looks off.

When the change touched `web/`, also run React Doctor, both the full scan and the design audit, and keep their JSON:

```sh
mkdir -p "$root/evidence"
(cd web && pnpm exec react-doctor . --blocking warning --no-telemetry --no-supply-chain --json --json-out "$root/evidence/react-doctor.json")
(cd web && pnpm exec react-doctor design --no-telemetry --no-supply-chain --json --json-out "$root/evidence/react-doctor-design.json")
jq '.diagnostics | length' "$root/evidence/react-doctor.json" "$root/evidence/react-doctor-design.json"   # both 0
```

The full scan is the same gate `scripts/check` runs. The design audit is advisory, but a finding in a file you changed is yours to fix.

## Drive

**CLI.** `$S/cli "$root" <label> <sage args…>` runs one command against the instance. It saves `evidence/cli/NN-<label>.{cmd,out,err,code}` and exits with the command's code. Every command takes `--json`. Errors come back as `{"error": {"kind", "message"}}` with exit 1. A usage error (a missing argument, an unknown option or command, a bad value) comes back the same way under `--json`, with kind `invalid` and exit 2.

`$S/sweep "$root" <pdf>` drives every command with `--json` and checks that each output parses and exits as expected:
- add (one result per file: a PDF with a non-PDF exits 1, adds the PDF and gives the other an `invalid` error), papers (with `to_check`, `looks_good` and `live_stage`), extract run, runs (none left `running` or `queued` once `logs --follow` returns), show, logs --follow, export, queue (a run's every record; with no run it exits 1), review, config list/get/set, models (your models; `add` of a priced model, an unpriced one refused, removing the default emptying `extract_with`, a `config set extract_with` to a model that isn't yours refused, `available` with no key exiting 1, and `GET /api/models`, `/api/provider-kinds`, `/api/providers` and `/api/providers/<provider>/manage`), providers (`kinds`, then `add` of a provider of your own, a taken id and a malformed id refused, `set` of an endpoint making it ready, `models available` on an unreachable endpoint exiting 1, `models add` of a model on it refused without a price and accepted with `--price 0 0`, `providers remove` refused while that model is yours and for a built-in provider, then allowed once the model is gone, and `config set anthropic_api_key` refused as moved), extract stop (a paper with no open run, no paper, and `--all` with nothing live), openapi and ui;
- the run errors: `extract run` with no paper, `extract stop` on a paper with no open run, and `--resume` or `--continue` on an unknown run. With a fake key it also queues the failed paper again with `extract run --all` and follows that run, then resumes the failed run and follows it to the end;
- the run's own document: `GET /api/runs/<run>/document` answers with the parsed paper, and a run with records has `.sage/runs/<run>/document.json` on disk;
- a usage error under `--json` (exit 2, kind `invalid`), `runs` on an unknown paper (`not_found`), `config get` without `--json` (the bare JSON value), `ui` on a port in use and `ui --json`, each on a data folder of its own, and a second `ui` on the instance's data folder (`conflict`, before it prints a URL);
- the review argument errors (`review RUN` alone, and `--accept-looks-good` with a record), `review RUN --accept-looks-good` (one accept per unreviewed "looks good" item, then an empty list on a second call), and `POST /api/runs/<run>/accept-looks-good`.

With no key it sets a fake one with `sage providers set anthropic api_key`, so the run fails fast and every command still runs. With a real key, `extract run` is a real extraction, which takes minutes and costs money (Haiku runs on Felipe cost cents). The log goes to `evidence/sweep.txt`.

**UI.** Use the `verify-browser` MCP tools (`browser_navigate`, `browser_snapshot`, `browser_click`, `browser_press_key`, `browser_file_upload`, `browser_drop`, `browser_take_screenshot`) against `$SAGE_VERIFY_URL`. Routes:

| Route | Screen |
|---|---|
| `/` | Home: the papers, search, Extract all, Add papers, drop anywhere. `extract=1` opens Extract all and `stop=1` Stop all |
| `/manage` | Manage: the SAGE skill, the agents, your models with their prices and the add-a-model list, and a card per provider. `add=provider` opens the Add a provider dialog and `price=<model id>` the price dialog |
| `/settings` | Settings: the cost limit, the theme and a section per group of settings, with a save bar while there are edits |
| `/papers/<paperId>` | Paper. `mode=check\|data\|read`, `item=<recordId>`, `filter=all` (Check lists every item, not only those to check), `type=<record type>` (Data mode's tab), `run=<runId>` (an older extraction), `zoom=<multiple of Fit page>`, `details=1` (This run drawer), `extract=1` (Extract dialog, not while a run is live), `continue=<runId>` (Continue dialog), `stop=1` (Stop dialog, while a run is live) |
| `/runs/<runId>` | Redirect only, to `/papers/<paper>?run=<runId>&details=1`. An unknown run shows "There's no such extraction" |

When another session shares the `verify-browser` MCP, drive your own browser instead: a headless Playwright script per flow (Node, `playwright-core` with a cached Chromium), one context per flow, logging `console` errors, `pageerror`, failed requests and every `/api/` request to `evidence/ui/<flow>.{log,console.txt,network.txt}`. The network log is how you prove which route a screen used.

Prefer `data-testid` handles. Each feature file lists the ones it needs. Settings inputs are `#setting-<field name>`. A provider's fields on the Manage page carry the field's name (`field-base_url`, `secret-input-api_key`) on every card, so scope them with `[data-provider=<id>]`. Status shows as a shape whose word is only in `aria-label` ("Looks good", "Check this", "Missing"), so read status from `data-status` or the snapshot, not from screenshot text. Keyboard shortcuts are bound with tinykeys on `window`, so press real keys with `browser_press_key`. Synthetic `KeyboardEvent`s from `browser_evaluate` do not trigger them. Shortcuts ignore keys typed into inputs.

The recipes per feature are in [features/README.md](features/README.md).

## Evidence

Proof goes under `$root/evidence/`. CLI proof goes in `evidence/cli/`, the sweep log in `evidence/sweep.txt`, and the React Doctor reports in `evidence/react-doctor.json` and `evidence/react-doctor-design.json`. Screenshots go in `evidence/ui/`, because the browser tool only writes under `/tmp/verify` or the repo. For a UI change, take each screenshot in light and in dark (set `localStorage.theme` to `light` or `dark` and reload).

Proof standards:
- Drive the real user path, meaning the page's own buttons and keys or the real CLI command. Never seed state by writing files under `SAGE_HOME`.
  - The one exception is the permission card (`agent-asks`) when no ACP agent is signed in or a real run never asks. [extract-and-watch](features/extract-and-watch.md) says which files to write. Report it as a render-only check: it shows the card's look, and proves nothing about the request reaching you or your answer reaching the agent.
- Capture the action and the resulting state, for example a screenshot after the click plus an ARIA snapshot (`browser_snapshot`).
- Verify side effects on disk alongside what the page shows. Uploads land in `$SAGE_HOME/papers/<id>/paper.pdf` and `.sage/paper.json`. Reviews append to `$SAGE_HOME/papers/<id>/.sage/reviews.jsonl`. Queued runs are taken on by the runner (`$SAGE_HOME/runner.pid`, output in `runner.log`). Run events go to `$SAGE_HOME/papers/<id>/.sage/runs/<runId>/events.jsonl`, settled records to `records.json`, the settings the run used (`extraction_model` or `agent` and `agent_model`, `effort` and `effort_given`, `max_cost_usd`, `max_turns`) to `run.json`, and the document it read to `document.json` beside them. Settings, providers (keys included, under `[providers.<id>]`) and your models (with any price you gave) go to `$SAGE_CONFIG`.
- A fake key exercises every failure path but no extraction. Say so when a proof used one. Records and review need a run made with a real key.

## Cleanup

```sh
$S/down "$root"
```

It kills only the two pids this root started, then deletes `home/` and `config.toml`. `evidence/` and `instance.env` stay at `/tmp/verify/sage/<id>/`. Run it after every attempt, failed ones included.

## Helpers

| Script | Use |
|---|---|
| `up [--key-from <config.toml>] [pdf…]` | Start an isolated instance, print its root |
| `doctor <root> [--key]` | Read-only health check |
| `cli <root> <label> <args…>` | One `sage` command with saved proof |
| `sweep <root> <pdf>` | Every command with `--json` |
| `down <root>` | Stop and delete scratch data, keep evidence |

Static checks are separate from this skill: `scripts/check` runs ruff, ruff format, ty, ast-grep, gitleaks, Biome, tsc, React Doctor, knip and the API-types drift check.
