---
name: deslop-sage
description: "Deslop a change in the SAGE repo. Strips slop, fixes what the change broke, enforces the rules the repo's own docs state, and flags anything that weakens the evidence checks. Use for 'deslop' or 'clean this up' in this repo, or after generating or editing SAGE code. In this repo it replaces the global deslop skill."
---

# Deslop for SAGE

Strip the slop out of the change just made, fix what it broke, and report what you shouldn't decide yourself. Fixing is the default. This is a cleanup, not a review.

This skill holds no code structure on purpose. The repo's docs own that, so the skill stays correct as the code moves.

## Read first

- `CLAUDE.md` or `AGENTS.md`, whichever exists. Every rule in it is a tell. Any import, call or file that breaks one is slop.
- The docs those files point to for the area the diff touches. When the diff touches extraction, checking or the data model, that includes the domain doc.

If the docs and the code disagree, the docs win, unless the diff is updating the docs on purpose.

## The one thing that matters most

SAGE is only useful if nothing unchecked gets saved. Hold every change to these:

- **Code decides, the model proposes.** Statuses and derived values (conversions, dates, statistics) come from code. A change that lets the model's output stand in for either is a bug.
- **Missing stays missing.** A gap in the paper becomes an unresolved value with a reason. Code that fills it with a default or a guess is a bug.
- **Checks only get stricter without asking.** Any change that lets through something a check used to stop goes to the user, even when it looks right. That covers looser text matching, lower thresholds, a blocking rule turned into a warning, or a skipped check.
- **Every action reaches the user.** A new capability or tunable value that the CLI or the UI can't reach is half done.

## Rules

- **Scope is the diff.** Covers `git diff` plus untracked files. Code that was already there stays untouched, unless this change broke it.
- **Related changes only.** Leave renames, refactors, dependency bumps and reformatting outside the change's intent alone.
- **Fix quietly, flag loudly.** Apply cleanups without narrating each one. Surface only the calls you couldn't make.
- **Trace before deleting.** Run `rg -w <name>` on every symbol before you remove it.
- **Keep it uncommitted.** Deslop edits the working tree. The user commits.

## The hunt

### 1. Map

Read the whole diff and write its intent as one sentence. Anything that doesn't serve it is a removal candidate. Find the entry point (a command, a route, a UI action) and follow it to the new code. Wired-up code that nothing calls is the largest slop there is.

**Done when** you can name the intent and the entry point that reaches the new code.

### 2. Trace

This is the only pass that reads outside the diff. For every function, model, field, event, setting, route or component the diff modified, run `rg -w <name>` and read each call site. Breakage hides where one shape is shared across a boundary: the data model and everything that reads it, events and whatever renders or replays them, files that old runs already wrote, API responses and the generated client, settings and the screens that show them. Fix what broke.

**Done when** every modified symbol has had its call sites read.

### 3. Strip

Read every hunk against these tells, plus the rules from the docs you read.

- **One-use abstraction.** A helper, wrapper, protocol or options object with one caller, or a wrapper that only forwards. If inlining makes nothing worse, inline it.
- **Dead weight.** A symbol `rg -w` finds only at its definition, unread params, unreachable branches, leftover imports. Delete it.
- **Impossible guard.** A check for a state the types or validators already rule out, or a `try` that only re-raises. Name the input that would make it fire. If you can't, delete it.
- **Half-done work.** `TODO`, stub returns, hardcoded values someone may want to tune, swallowed errors. Finish it or flag it.
- **Comment noise.** If the diff adds or edits comments, read `.claude/skills/no-comments/SKILL.md` and follow it over the diff's files. It spawns Comment Sicko with the prompt in `.claude/skills/no-comments/references/comment-sicko.md`. It can't be invoked as a skill, so read the file.
- **Verbosity.** Single-use temporaries, if/else that assigns the same name both ways, near-identical blocks. Shorten when nothing is lost.
- **Prose the code could enforce.** An instruction in a prompt or doc that a check could enforce instead. Add the check and drop the prose.
- **AI prose.** Any prose the diff adds or edits: comments, docstrings, docs, agent prompts, checker and error messages, UI copy. Read `.claude/skills/unslop/SKILL.md` and apply its patterns to that text. It can't be invoked as a skill, so read the file.
- **Doesn't look like the neighbors.** Open the closest sibling module, route or screen first and compare naming, error handling, placement and reuse. This tell finds the most.

**Done when** every hunk has been read against every tell.

### 4. Verify

Run the check command the docs name and fix what it reports. If behaviour changed, prove it on the real thing with the `verify-sage` skill. If the diff repeats a mistake that has happened before, encode it as a lint or ast-grep rule rather than a comment.

**Done when** the checks are clean and any behaviour change has been seen working.

### 5. Flag

Report these and leave them for the user:

- **Security.** Secrets logged, stored loosely or returned unmasked. Path traversal through user-supplied names. Paper text rendered as HTML.
- **Scientific integrity.** Anything that breaks the four points under "The one thing that matters most".
- **Architecture.** A half-migrated shape, or a shortcut that will need a rewrite. Describe the better shape. Don't build it.
- **Ambiguity.** Any case where the right answer depends on intent you don't have.

## Large diffs

Over about 10 files or about 500 changed lines, fan out. Split the files into disjoint groups along the repo's layers, keeping callers with their callees. Spawn one agent per group in a single message, each with its file list, the intent sentence, and this boundary: fix only inside your files, and report anything that crosses them. Afterwards, do the Trace pass and the neighbours tell yourself across the whole diff.

## Output

1. **Stripped.** What you removed and fixed, grouped, one line each.
2. **Flagged.** Pass 5 findings, most severe first, each with `file:line`.
3. **Verified.** The check result and how you saw any behaviour change working.
4. **Open questions.** Only for a decision that's genuinely the user's.
