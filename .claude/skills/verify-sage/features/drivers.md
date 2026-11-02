# Drivers: who extracts

Every extraction is an agent editing the paper's folder until `sage extract check` passes, then `sage extract done`. Three drivers do it: SAGE's own loop on a model from the Manage page (`sage`), an ACP agent SAGE starts in the folder, such as Claude Code or Codex (`agent`), and an agent in the user's own terminal with the SAGE skill (`here`). They share one boundary, so a record is judged the same whoever wrote it, and a run carries on from the folder as the last one left it.

## Sub-features

- `folder` is what a paper's folder holds after it is added: `paper.pdf`, `paper.md`, `pages/p<N>.png`, `tables/<anchor>.png` for each detected table, `figures/`, `records/` (empty) and `.sage/`. The agent writes `records/*.yaml`, `records/observations/*.yaml`, `records/unresolved.yaml`, `tables/<anchor>.csv` and `pages/p<N>.md`.
- `check` prints `file:line  target: message` lines, blocking first, at most `error_list_cap` of each severity, then totals; `--all` lists every one. Exit 0 clean, 1 blocking, 2 a file that won't parse. With a run open it records the check (`check.ran` in `events.jsonl`, a snapshot under `.sage/snapshots/` when the files changed). Without one it only answers.
- `done` runs check and is refused (exit 1) while anything blocks. Accepted, it writes the run's `records.json` and ends the run `finished`. With no open run it opens a `here` run first.
- `here` is `sage extract start <paper>` (prints the run and the folder), the agent's edits, `check`, `done -m`. `sage extract stop <paper>` ends it `interrupted` and settles what the folder compiles to.
- `native` is `sage extract run <paper> [--with <model>]`. Its log shows `read_file`, `write_file`, `edit_file`, `check` and `done` tool calls.
- `acp` is `sage extract run <paper> --with claude` (or `claude:sonnet`, `codex`, `gemini`, `opencode`). `run.json` has `driver: "agent"`, `agent` and `agent_model`; the log streams the agent's messages, thinking and tool calls. `sage agents` lists each agent as `ready`, `not_signed_in` or `not_installed` with the command that fixes it; `sage agents offer <id> [--model M]` lists its models, modes and that model's efforts without spending tokens (`efforts: []` where the agent has no effort choice for it, as Codex on `gpt-5.5`), and `sage agents set <id> --model M --effort E` writes `[agents.<id>]` in `$SAGE_CONFIG`. A run's `effort` lands in `run.json` with `effort_given`; an asked effort the model lacks fails the run with the levels the agent offers, and a configured one is a warning note and the agent's default.
- `permissions`: by default an ACP agent runs whatever it asks to. With `sage config set agents_ask true`, SAGE answers its tool requests itself: reads in the folder or the skill, edits of the agent's files, `mkdir` in the folder, `sage extract check|done|status` and the read-only `sage` commands, and read-only shell commands go ahead; an edit of `.sage/`, `paper.pdf` or `paper.md` is refused; anything else writes the run's `permission.json`, the run shows `waiting`, and `sage extract answer <paper> --allow|--deny` (or the UI) answers it.
- `continue` is `sage extract run --continue <run> --guidance "…"`: a new run on the folder as it stands, with the earlier run's driver unless `--with` says otherwise. Check prints the guidance first while it is open.
- `skill` is `sage skill status` and `sage skill install`, which put the skill in `~/.agents/skills/sage` and `~/.claude/skills/sage`.

## How to get to it (user POV)

- UI: the Extract dialog's driver picker (SAGE's own models, and each agent with its state). An agent's permission request shows in the run's log with Allow and Deny.
- CLI: `sage extract run|start|check|done|stop|answer|status`, `sage agents`, `sage skill`.
- Terminal agent: `sage skill install`, then ask the agent in that terminal to "extract <paper> with SAGE".

## Driving it with verify-browser and cli

Preconditions: a baseline instance. The native driver needs a real key. An ACP driver needs the agent installed and signed in on this machine (`$S/cli "$root" agents agents --json`); report "needs <agent> signed in" otherwise. Never answer a permission request you don't understand: deny it.

- **Folder.** `ls $SAGE_HOME/papers/Felipe-2010-Cultivar` shows the layout above; `head paper.md` names the anchors and the images.
- **Terminal, by hand.** `$S/cli "$root" start extract start Felipe-2010-Cultivar --json` gives the run. Write `records/citation.yaml` with one deliberate mistake, for example `year: [two thousand ten, p1.b1]`. `$S/cli "$root" check extract check Felipe-2010-Cultivar` exits 1 with `records/citation.yaml:3  <id>.year: … isn't a whole number`. Fix it and check again: that line is gone. `done -m "partial"` is refused while anything blocks.
- **Same-error rule.** Edit the file 3 times without fixing the error, checking after each: the third check lists the target as marked `repeated_error`, and the error stops showing. Fix it: the mark lifts on the next check.
- **Native.** `$S/cli "$root" run extract run Felipe-2010-Cultivar --with anthropic:claude-haiku-5-5`. It follows the log to the end. `run.json` reads `driver: "sage"` and ends `finished` or `budget_exhausted`; `records/` holds the files the model wrote.
- **ACP.** `$S/cli "$root" acp extract run Felipe-2010-Cultivar --with claude --no-follow --json`, then `$S/cli "$root" logs logs <run> --follow`. Answer `waiting for you` lines with `extract answer`. The run ends `finished` with `check:` lines in the log, and `sage show <run>` lists the records.
- **Stop.** `extract stop Felipe-2010-Cultivar` on a live run ends it `interrupted` within a few seconds; the folder keeps its files and `records.json` holds what they compile to.
- **Proof.** The CLI evidence set, `run.json`, the tail of `events.jsonl` and `ls records/`.

## Gotchas

- An ACP agent spends on its own account and runs under its own sign-in; a usage limit ends the run `failed` with the agent's message.
- The runner is a long-lived process: after you change Python, wait for it to leave (30 seconds idle, `runner.pid` gone) before the next run, or it runs the old code.
- Check output depends on the paper's text layer. Quote a value with a comma: `["35, 69 and 95 DAP", p5.b2]`.
- A run's agent works in `SAGE_HOME`'s folder. Run ACP checks on a copy of a library, never `~/sage-data`.
