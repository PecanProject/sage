---
name: verify-sage
description: Drive SAGE (the agronomy-paper extraction app in this repo) the way a user does and capture proof. Covers the `sage` CLI and the web UI served by `sage ui` (Home, the Paper screen with its Check, Data and Read modes, and the Settings dialog). Use when you need to prove a change to SAGE works end to end, reproduce a SAGE bug, or check a flow before handing work over.
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
$S/doctor "$root" --key    # also asks Anthropic whether the saved key works (free)
```

It checks four things:
- the launcher pid is alive and the port is held by our server's pid;
- the API reads this instance's `SAGE_HOME`;
- the app shell is served, and the UI build is newer than `web/src`;
- whether an API key is set.

Run it first whenever anything looks off.

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
- add (one result per file: a PDF with a non-PDF exits 1, adds the PDF and gives the other an `invalid` error), papers (with `to_check`, `looks_good` and `live_stage`), run, runs (none left `running` or `queued` once `logs --follow` returns), show, logs --follow, export, queue (every paper's data run, and one run, which holds every record), review, config list/get/set/test-key, openapi and ui;
- the run errors: `run` with no paper, `stop` on an ended run, and `--resume` or `--continue` on an unknown run. With a fake key it also resumes the failed run and follows it to the end;
- the run's own document: `GET /api/runs/<run>/document` answers with the parsed paper, and a run with records has `runs/<run>/document.json` on disk;
- a usage error under `--json` (exit 2, kind `invalid`), `runs` on an unknown paper (`not_found`), `config get` without `--json` (the bare JSON value), and `ui` on the instance's own port (`conflict`, before it prints a URL);
- the review argument errors (`review RUN` alone, and `--accept-looks-good` with a record), `review RUN --accept-looks-good` (one accept per unreviewed "looks good" item, then an empty list on a second call), and `POST /api/runs/<run>/accept-looks-good`.

With no key it sets a fake one, so the run fails fast and every command still runs. With a real key, `run` is a real extraction, which takes minutes and costs money (Haiku runs on Felipe cost cents). The log goes to `evidence/sweep.txt`.

**UI.** Use the `verify-browser` MCP tools (`browser_navigate`, `browser_snapshot`, `browser_click`, `browser_press_key`, `browser_file_upload`, `browser_drop`, `browser_take_screenshot`) against `$SAGE_VERIFY_URL`. Routes:

| Route | Screen |
|---|---|
| `/` | Home: the papers, search, Add papers, drop anywhere |
| `/settings` | Home with the Settings dialog open. Elsewhere `?settings=1` opens it |
| `/papers/<paperId>` | Paper. `mode=check\|data\|read`, `item=<recordId>`, `filter=all` (Check lists every item, not only those to check), `type=<record type>` (Data mode's tab), `run=<runId>` (an older extraction), `zoom=<multiple of Fit page>`, `details=1` (This run drawer), `extract=1` (Extract dialog, not while a run is live), `continue=<runId>` (Continue dialog), `stop=1` (Stop dialog, while a run is live) |
| `/runs/<runId>` | Redirect only, to `/papers/<paper>?run=<runId>&details=1`. An unknown run shows "There's no such extraction" |

When another session shares the `verify-browser` MCP, drive your own browser instead: a headless Playwright script per flow (Node, `playwright-core` with a cached Chromium), one context per flow, logging `console` errors, `pageerror`, failed requests and every `/api/` request to `evidence/ui/<flow>.{log,console.txt,network.txt}`. The network log is how you prove which route a screen used.

Prefer `data-testid` handles. Each feature file lists the ones it needs. Settings inputs are `#setting-<field name>`. Status shows as a shape whose word is only in `aria-label` ("Looks good", "Check this", "Missing"), so read status from `data-status` or the snapshot, not from screenshot text. Keyboard shortcuts are bound with tinykeys on `window`, so press real keys with `browser_press_key`. Synthetic `KeyboardEvent`s from `browser_evaluate` do not trigger them. Shortcuts ignore keys typed into inputs.

The recipes per feature are in [features/README.md](features/README.md).

## Evidence

Proof goes under `$root/evidence/`. CLI proof goes in `evidence/cli/`, the sweep log in `evidence/sweep.txt`, and the React Doctor reports in `evidence/react-doctor.json` and `evidence/react-doctor-design.json`. Screenshots go in `evidence/ui/`, because the browser tool only writes under `/tmp/verify` or the repo. For a UI change, take each screenshot in light and in dark (set `localStorage.theme` to `light` or `dark` and reload).

Proof standards:
- Drive the real user path, meaning the page's own buttons and keys or the real CLI command. Never seed state by writing files under `SAGE_HOME`.
- Capture the action and the resulting state, for example a screenshot after the click plus an ARIA snapshot (`browser_snapshot`).
- Verify side effects on disk alongside what the page shows. Uploads land in `$SAGE_HOME/papers/<id>/paper.pdf` and `paper.json`. Reviews append to `$SAGE_HOME/papers/<id>/reviews.jsonl`. Run events go to `$SAGE_HOME/papers/<id>/runs/<runId>/events.jsonl`, settled records to `records.json`, the settings the run used (`extraction_model`, `effort`, `max_cost_usd`, `max_turns`) to `run.json`, and the document it read to `document.json` beside them. Settings go to `$SAGE_CONFIG`.
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
