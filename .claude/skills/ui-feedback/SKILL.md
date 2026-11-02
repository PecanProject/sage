---
name: ui-feedback
description: Run a UI feedback session on SAGE's running web app: the user leaves notes on the page with the Agentation toolbar, and you act on them and answer in threads until they say stop. Use when the user says "watch mode", asks to start a feedback or annotation session, or asks you to pick up their notes.
---

# UI feedback session

The user annotates the live app in a browser, on this machine or over the tailnet. You act on each note and answer it on its **thread**, which they read in the **Threads** panel at the bottom left of the page. The session is a **loop** that runs until they say stop.

The pieces are already in the repo:

- `scripts/dev` runs the API, Vite, and the feedback server (agentation-mcp's HTTP API on 127.0.0.1:4747, with notes kept in `~/.agentation/store.db`).
- Vite proxies `/agentation` to that server, so the toolbar works over the tailnet too.
- The toolbar (`web/src/components/feedback-toolbar.tsx`) and the Threads panel (`agent-replies.tsx`) load only in dev.
- `scripts/feedback` is your whole side. Run `scripts/feedback --help` for its commands.

## Start

1. Check ports 8765, 5173 and 4747 with `lsof -nP -iTCP:<port> -sTCP:LISTEN`. If anything holds one, read its command line. If it's a stale SAGE stack, ask before killing it.
2. Run `SAGE_WEB_HOST=0.0.0.0 scripts/dev` with the Bash tool, `run_in_background: true` and timeout 7200000.
3. Wait until `curl -s localhost:5173` and `curl -s localhost:5173/agentation/sessions` both answer.
4. Give the user both URLs: `http://localhost:5173`, and the tailnet one, from `tailscale status --json` (`.Self.DNSName`, port 5173).
5. Tell them in a sentence how it works:
   - The toolbar sits bottom right. They click an element and type a note.
   - Threads sits bottom left, where your answers appear and where they can reply.
   - They can keep annotating while you work.

## Loop

1. Run `scripts/feedback wait` with `run_in_background: true` and timeout 7200000, then **end your turn**. It exits when a note or a reply needs you, and that wakes you.
2. Read its output file. Each item has an id, the page, the element and source file, the note, and the user's last message.
3. Work every item:
   - **Change:** `scripts/feedback ack ID`. Make the change following AGENTS.md's web rules. Then `scripts/feedback resolve ID "<one line on what changed>"`. Resolve it yourself once it's done; don't ask "ok?".
   - **Question:** `scripts/feedback reply ID "<answer>"`, and leave it open. A question stays open for the user.
   - **Decision:** when the right change is genuinely the user's call, reply with the concrete options and your recommendation. Act when they answer on the thread.
   - **Placement or rearrange note:** these are drags in the toolbar's layout mode, often experiments. Act on one only if the intent is clear from it; otherwise reply asking what they want.
4. After each batch of code changes, run `scripts/check` until it's clean, and `cd web && pnpm build` if `web/` changed.
5. In chat, say in a line or two what you did per note. The threads hold the detail.
6. Go back to step 1. Start a new `wait` before you end every turn, including turns the user's chat message started.

The loop ends when the user says stop. Then stop the dev stack's background task if they want it down.

## Gotchas

- **Only `scripts/feedback wait` hears replies.** Agentation's own MCP tool, `agentation_watch_annotations`, wakes only on new notes, never on thread replies. It also blocks your turn, so a chat message from the user cuts it off. This is why `.mcp.json` no longer registers agentation.
- **A note needs you until you act.** `wait` returns at once while any note is unacked, or open with the user's message last. Acking and replying clear that state. A reply on an already-acked note only counts once you've answered it.
- **Every wait starts with a fresh check** of everything waiting, then listens. Notes that arrive while you work are never lost, so it's safe to restart it.
- **The toolbar re-sends old notes** under a new id, sometimes long after: same comment, same original timestamp. `wait` and `list` dismiss these copies on their own, so treat whatever they print as real.
- **The toolbar shows no replies.** Agentation's toolbar only removes a marker once its note is resolved or dismissed. The user reads your words in the Threads panel, so every answer goes on the thread, not only in chat.
- **Notes can vanish** before you reach them, when the user deletes them in the toolbar. A 404 from `scripts/feedback` means the note is gone; move on.
- **Router navigation.** TanStack Router's `pushState` doesn't reach the toolbar, so `FeedbackToolbar` subscribes to the location with `"use no memo"`. Keep that, or notes get filed under the wrong page.
- **Your own browser checks create sessions too.** Driving the app with a verification browser makes extra empty sessions on the server. They're harmless.
- **Something else holding port 4747** (an old MCP-started server binds `*:4747`) can sit beside the dev one on `127.0.0.1`. The proxy and `scripts/feedback` use `127.0.0.1`, so stop the stray one to avoid confusion.
- **pnpm builds better-sqlite3 only because it's allowed** in `web/package.json` (`pnpm.onlyBuiltDependencies`). Without that, the feedback server can't open its store.
