# Settings

Settings is a 560px dialog over the current screen (`/settings` on Home, `?settings=1` elsewhere). It shows the Anthropic API key with a Test button, the extraction model, the cost limit per paper and Appearance. Every other `Settings` field sits under Advanced, grouped by the schema's groups, each with its title, an ⓘ tooltip holding the description, and a reset icon when the value differs from the default. The footer has the dialog's only Save. It writes only the changed fields, a newly typed key included, to `config.toml` (mode 0600), shows "Saved" and closes. With nothing changed it closes without a request. A value set by an environment variable is read-only and names the variable. The CLI edits the same settings with `sage config`.

## Sub-features

- `settings-basic` covers the key, model, cost limit and Appearance (System, Light or Dark).
- `settings-advanced` lists every other field, collapsed by default (`[data-testid=advanced-toggle]`).
- `save-settings` is the one Save. It writes only changed fields, and sends nothing when the draft is empty. Cancel discards the draft.
- `settings-reset` (`[data-testid=setting-reset-<field>]`) puts one field back to its default.
- `settings-key` shows the saved key masked. Change opens an input whose text joins the form's draft, and `secret-keep-…` (✕) backs out and drops it from the draft. Test asks Anthropic about the saved key and reports a check or a short error.
- `settings-lock` shows an env-set field as read-only with "Set by SAGE_…".
- `settings-strip` shows `[data-testid=key-strip]` under the top bar when no key is set.
- `settings-errors` shows the server's message under each invalid field, never the browser's bubble (the form has `noValidate`). Editing the field clears its error. A Save that fails on an Advanced field opens Advanced, which stays open while the field is fixed.
- `settings-cli` covers `sage config list|get|set|test-key`. `config get` prints the bare JSON value (`40`, `null`, `false`).

## How to get to it (user POV)

- Click ⚙ in the top bar, or "Open settings" in the key strip.
- Run `sage config …` in a terminal.

## Driving it with verify-browser and cli

Preconditions:

- Baseline instance, no key.

- **Strip.** `browser_navigate $SAGE_VERIFY_URL/`. `[data-testid=key-strip]` is visible.
- **Open.** `browser_click [data-testid=settings-button]`. `[data-testid=settings-dialog]` opens, and the URL is `/settings`.
- **Edit and save.** Copy `$SAGE_CONFIG` aside. Fill `#setting-max_cost_usd` with `2.5`, pick another model in `#setting-extraction_model` (a Select: click it, then the `option` by name), open Advanced and fill `#setting-max_turns`. Click `[data-testid=save-settings]`. A "Saved" toast shows, and the dialog closes.
- **Persisted.** `diff` the copy against `$SAGE_CONFIG`: exactly those three keys changed. `$S/cli "$root" get config get max_cost_usd --json` prints `{"max_cost_usd": 2.5}`.
- **Takes effect.** Open `/papers/Felipe-2010-Cultivar?extract=1`. `[data-testid=extract-model]` shows the new model and `[data-testid=extract-limit]` reads "Stops at $2.50". The next run's `run.json` has the new `max_cost_usd` and `max_turns`, and a run started without a model pick (the CLI, Home's inline Extract) has the new `extraction_model`.
- **Advanced.** Click `[data-testid=advanced-toggle]`. Change a field such as `#setting-max_turns`, save, and read it back with `sage config get max_turns --json`. Then reopen, click `[data-testid=setting-reset-max_turns]`, save, and the default returns.
- **Theme.** Click `[data-testid=theme-dark]`. `<html>` gains the `dark` class at once.
- **Key.** With no key, `[data-testid=secret-input-anthropic_api_key]` shows at once: fill it with `sk-ant-fake` and click `[data-testid=save-settings]` (or press Enter in the field); the key saves with the rest of the form. With a key saved, Change opens the input and `[data-testid=secret-keep-anthropic_api_key]` throws the typed key away. **Back out.** Take `shasum $SAGE_CONFIG`, click Change, type a key, click ✕, then Save: the dialog closes, no `PUT /api/settings` is sent, and the hash is unchanged. The key then shows as text in `[data-testid=secret-anthropic_api_key]` (`••••fake`, never the key) with Change (`secret-change-…`), Test (`test-key`) and Remove (`secret-remove-…`). Test puts `data-ok=false` and "Anthropic rejected this key (HTTP 401)" in `[data-testid=test-key-result]`. Remove drops the line from `$SAGE_CONFIG`. A key set by the environment shows as text with "Set by …" and only Test.
- **Invalid value.** Open Advanced, fill `#setting-max_turns` with `0` and Save. The dialog stays open, "Input should be greater than or equal to 1" shows under the field, and no browser validation bubble appears. Collapse Advanced with `[data-testid=advanced-toggle]`: it stays open. Type in the field: the message goes. `$SAGE_CONFIG` is unchanged.
- **Bad config file.** Work on a copy, never on the instance's own `$SAGE_CONFIG` while its UI runs. After `. "$root/instance.env"`, `cp "$SAGE_CONFIG" "$root/bad.toml"` and append `max_turns = "lots"`. `SAGE_CONFIG="$root/bad.toml" uv run sage papers --json` exits 1 with an `invalid` error naming `max_turns (in $root/bad.toml)`. With `SAGE_MAX_COST_USD=abc` also set, the message adds `max_cost_usd (from SAGE_MAX_COST_USD)`. `SAGE_CONFIG="$root/bad.toml" uv run sage config set max_turns "" --json` succeeds and removes the line. A binary file in its place gives kind `corrupt`, naming the file.
- **Env lock.** Relaunch with `SAGE_MAX_TURNS=12` exported before `up`. Under Advanced, Turn budget (`max_turns`) is read-only and names `SAGE_MAX_TURNS`.
- **CLI.** `$S/cli "$root" list config list --json` shows `values`, `sources`, `locked` and `config_path`.
- **Proof.** `evidence/ui/settings-{light,dark}.png` with Advanced open, and the `config.toml` contents with the key masked.

## Gotchas

- The model and effort controls are Base UI Selects, not `<select>`. Click the trigger, then the `option` role by name.
- `home` is a Paths setting. Changing it on an `up` instance has no effect, because `up` exports `SAGE_HOME`, which wins.
- Never type the user's real key into a verification instance. Use `up --key-from` to copy it.
- An empty value restores the default (`sage config set <name> ""`).
