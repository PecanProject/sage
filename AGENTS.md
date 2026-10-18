# SAGE

SAGE extracts experiment records from agronomy papers, with evidence for every value. It is one Python package, `sage`: a Typer CLI and a FastAPI app on `sage/core.py`, plus a React UI in `web/` built into `sage/web_dist/`.

Read `docs/domain.md` before you touch the IR, the checker, settle, `expand_table` or `sage/agents/guide.md`. It defines the entities, anchors, evidence rules, statuses and causes. Architecture is in `docs/design/01-stack-and-architecture.md`, the agent loop in `docs/design/02-agent-design.md`, and the UI in `docs/design/04-ui.md`. Read `docs/design/05-prompt-caching.md` before you change anything that goes into a model request: the tools, `guide.md`, the paper view, the model settings or the history.

## Scripts

- `scripts/check` runs every static check: ruff, ruff format, ty, ast-grep, a gitleaks secret scan, Biome, tsc, React Doctor, knip, and a diff of the generated API types. Run it before you hand work over. It must pass clean. It needs gitleaks (`brew install gitleaks`) and fails without it.
- `scripts/dev` runs the whole stack live, with no builds: uvicorn `--reload` (Python and `.md`, so `guide.md` edits reload too), the Vite dev server with HMR, and a watcher that regenerates `web/src/api/schema.d.ts` on every Python change. `SAGE_API_PORT` (default 8765) and `SAGE_WEB_PORT` (default 5173) set the ports, and `SAGE_WEB_HOST=0.0.0.0` serves it on the LAN or tailnet. Use it for all UI work. Run `pnpm build` only to refresh `sage/web_dist` for `sage ui`.

## Rules

- There is no test suite. Quality comes from `scripts/check` and the runtime checker. Encode a repeated mistake as a rule in `rules/*.yml` or a ruff ban.
- After you change an API route or a response model, regenerate the web types with `cd web && pnpm gen:api`.
- After you change `web/`, build it with `cd web && pnpm build`. That writes `sage/web_dist/`, which `sage ui` serves.
- `cli.py` and `api.py` call only `core.py`. Put a new user action in `core`, then expose it as both a command and a route. Every command takes `--json`.
- File I/O happens only in `sage/store/`. Call `sage.store.files`.
- Library code emits events from `sage/events.py` instead of printing.
- A value someone may want to tune is a `Settings` field with a description, so it shows in the Settings dialog and in `sage config`.
- Settle sets every status, and `sage/domain/` recomputes every derived value. The model sets neither.
- `docs/domain.md` and `sage/agents/guide.md` change together.
- To prove a change works end to end, use the `verify-sage` skill.

## Web

- **State placement.** Server state goes only through TanStack Query (`$api`). UI state that should survive a reload or a link (mode, item, filter, run, zoom, and which drawer or dialog is open) lives in TanStack Router search params, so the URL is the store. `useState` is only for local, short-lived input such as edit drafts or a drag in progress. There is no Zustand, no Redux and no context used as a store.
- **Derive, don't sync.** Compute values from props, query data and search params during render. No effect sets state from them, and no query data is copied into state.
- **Effects only for external systems.** Use effects for EventSource, key bindings and DOM observers, each with its cleanup. Use `useEffectEvent` for a handler that needs the latest render. Never read or write a ref during render.
- **Composition.** Write small components with one job. Prefer compound components and `children` to boolean props, and don't pass props down more than two levels. Keys come from ids, never indexes.
- **Design tokens.** Style with the tokens in `web/src/index.css`, whose comments say what each is for, and with the components in `components/ui`. Never copy values from another component. Type is a role class (`text-title`, `text-value`, `text-label`, `text-caption` and so on) that sets weight and tracking too, so don't add `font-semibold` to one. Pad bars, lists, dialogs and side panels with `inset`, pad the paper's panes with `gutter`, and size controls with `h-control`. `rules/web-tokens.yml` rejects Tailwind defaults (`text-sm`, `rounded-lg`, `bg-zinc-100`) and arbitrary values a token covers. If a value is missing, add a token to `index.css`, `lib/utils.ts` (the `cn` merge config) and `docs/design/04-ui.md`.
- **React Compiler** memoizes for you through the Babel preset in `vite.config.ts`. Don't add `useMemo`, `useCallback` or `memo` by hand.
- `scripts/check` runs React Doctor (`pnpm run doctor`, at `--blocking warning`; plain `pnpm doctor` is pnpm's own command) and knip (`pnpm knip`). Both must report nothing. Fix the code rather than disabling a rule, and remove whatever knip reports as unused.

## Layers

| Layer | May import | Enforced by |
|---|---|---|
| `ir/`, `domain/` | stdlib (not `pathlib`), pydantic, each other | `sage/ir/ruff.toml`, `sage/domain/ruff.toml` |
| `parse/` | `ir`, `domain`, pdftext, pypdfium2 | `sage/parse/ruff.toml` |
| `agents/` | `ir`, `domain`, `parse`, `events`, pydantic-ai, anthropic | `sage/agents/ruff.toml` |
| `store/` | stdlib, pydantic, tomli-w, `events` | `sage/store/ruff.toml` |
| `pipeline/` | everything above, `settings` | `sage/pipeline/ruff.toml` |
| `settings.py`, `events.py` | stdlib, pydantic, pydantic-settings, each other | `rules/leaf-modules.yml` |
| `core.py` | every layer, but not pydantic-ai, anthropic, pdftext or pypdfium2 directly | root `ruff.toml` |
| `cli.py`, `api.py` | `sage.core` only | `rules/surfaces-call-core.yml` |

- The root `ruff.toml` bans pydantic-ai and the Anthropic SDK, and only `sage/agents/ruff.toml` lifts the ban. It also bans pdftext and pypdfium2, and only `sage/parse/ruff.toml` lifts that ban.
- `sage/agents/ruff.toml` bans `sage.pipeline`. The pipeline drives the agent through the `Workspace` protocol in `agents/contract.py`, never the reverse.
- `rules/io-in-store.yml` enforces the file I/O rule. Ruff `T201` enforces the no-print rule everywhere except `cli.py`.
- A nested `ruff.toml` replaces its parent's `banned-api` list instead of extending it, so each one repeats every ban it still needs: pydantic-ai and anthropic outside `agents/`, pdftext and pypdfium2 outside `parse/`. Copy those lines when you add a layer.
