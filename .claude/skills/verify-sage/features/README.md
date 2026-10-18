# SAGE verification map

This folder is the maintained source for verifying what a SAGE user can do. Read this index, then use the matching feature file as the recipe. The launch, doctor, evidence and cleanup steps are in [../SKILL.md](../SKILL.md).

## Baseline preconditions

- An instance from `up`, with `S=.claude/skills/verify-sage/scripts`, `root=$($S/up pdfs/Felipe-2010-Cultivar.pdf)` and `. "$root/instance.env"`.
- `$S/doctor "$root"` exits 0.
- The instance has one paper, `Felipe-2010-Cultivar` (14 pages, one table on page 7), and no runs.
- Never drive an instance this run did not start. The user's own `sage ui` on port 8765 is off limits.

## Key-dependent state

Records and checking need a run made with a real Anthropic key (`up --key-from ~/.config/sage/config.toml`). Without one, a run fails at the first model call, which still proves the failure paths. A feature that needs records reports "needs a real key" rather than passing on a fake one.

## Driving conventions

- Browser actions use the `verify-browser` MCP tools against `$SAGE_VERIFY_URL`. Terminal actions use `$S/cli "$root" <label> …`.
- Prefer `data-testid` handles and accessible names over CSS position.
- Press shortcuts with `browser_press_key`. They are window-level tinykeys bindings and ignore focused inputs.
- Start from the baseline unless the file says otherwise.

## Proof and skip reporting

- UI proof is a screenshot in `$root/evidence/ui/` plus the snapshot line or on-disk file that shows the resulting state.
- CLI proof is the `evidence/cli/NN-<label>.*` set: command, stdout, stderr, exit code.
- Pair every mutation with a second, read-only view, such as the file on disk, `sage show`, or a reload.
- Name the entry point used. A path you could not reach is reported with the unmet precondition, never covered by another path.

## Feature entry contract

Each feature file has an H1, a paragraph on the user-visible behaviour, then four H2s in this order: `Sub-features`, `How to get to it (user POV)`, `Driving it with verify-browser and cli`, `Gotchas`.

## Features

- [Papers library](./papers-library.md) covers upload by picker, drag and drop and CLI, duplicates, and non-PDF files.
- [Extract and watch a run](./extract-and-watch.md) covers starting a run from the Paper page with a chosen model, from the CLI, and watching it live.
- [Check](./check.md) covers the margin cards, the PDF following the active item, table clusters, the keys and the zoom, Accept N good, Data mode's side sheet, and reviews.jsonl.
- [Settings](./settings.md) covers the Settings dialog, the API key and its test, env locks, and the CLI config commands.
- [Navigation](./navigation.md) covers the top bar, the mode switch, the Runs menu, the Settings route, the ? shortcut sheet, old `/runs/` links and the Not found pages.
