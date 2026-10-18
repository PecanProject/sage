# Prompt caching

The extraction agent sends the whole conversation on every turn. Anthropic's prompt cache makes all but the newest part of it cost a tenth of the input price, or less. A healthy run reads more than 90% of its prompt tokens from the cache. One change in the wrong place can drop that to 40% without any error, so read this page before you change anything that goes into a request: the tools, `guide.md`, the paper view, the model settings, or how SAGE builds the history.

The code is `models.cached` in `sage/agents/models.py`. `02-agent-design.md` describes the loop it serves.

## How the cache works

- **It is a prefix match.** A request renders as `tools`, then `system`, then `messages`. A cache entry covers every byte up to a breakpoint. A change at any position invalidates every entry at or after it. ([prompt caching][pc])
- **Entries exist only at breakpoints.** A request writes an entry at each `cache_control` breakpoint, and nowhere else. A later request reads by walking back from each of its own breakpoints to an entry an earlier request wrote. It checks at most 20 positions per breakpoint. A run of consecutive `tool_use` blocks counts as one position, and so does a run of `tool_result` blocks. ([prompt caching][pc])
- **At most 4 breakpoints per request.** Automatic caching (a top-level `cache_control`) uses one of them and moves forward to the last block each turn. ([prompt caching][pc])
- **Minimum length.** A prefix under 512 tokens is not cached on Haiku 5.5, Sonnet 5.5 or Opus 5.5. The API returns no error; it just writes nothing. ([prompt caching][pc])
- **Lifetime.** An entry lasts 5 minutes by default, or 1 hour with `ttl: "1h"`. The clock starts at the **start** of the request that writes or reads it, not at the end of its response. A read restarts it for free. A 1-hour entry must come before any 5-minute entry in the same request. ([prompt caching][pc])
- **Cache entries belong to one model and one workspace.** A run on another model, or with another API key's workspace, starts cold. ([prompt caching][pc])
- **Concurrent requests don't share.** An entry is readable only once the first response that writes it has begun. Requests sent at the same moment all pay to write. ([prompt caching][pc])

### What invalidates what

| Change between two requests | Tools entry | System entry | Messages entries |
|---|---|---|---|
| Tool definitions added, removed, reordered or reworded | lost | lost | lost |
| Model | lost | lost | lost |
| System text (the guide or the paper view) | kept | lost | lost |
| `tool_choice`, or images added or removed | kept | kept | lost |
| Thinking settings or `effort` | depends on the model | depends on the model | lost |
| Any earlier message edited, cleared or dropped, thinking blocks included | kept | kept | lost from that message on |

Source: [prompt caching][pc], invalidation table. Treat the "depends on the model" rows as a full restart. Setting `effort` explicitly to the model's default is the same as leaving it out. Haiku 5.5, Sonnet 5.5 and Opus 5.5 keep earlier thinking blocks in context, so passing them back unchanged keeps the cache. ([context windows][cw])

### Prices

Per million tokens, from the [pricing page][price]:

| Model | Input | 5-minute write | Cache read | Output |
|---|---|---|---|---|
| Haiku 5.5, prompt up to 100K | $0.10 | $0.125 | $0.01 | $0.50 |
| Haiku 5.5, prompt over 100K | $0.50 | $0.625 | $0.05 | $2.50 |
| Sonnet 5.5 | $2 | $2.50 | $0.10 | $10 |
| Opus 5.5 | $4 | $5 | $0.20 | $20 |

- A write costs 1.25 times the input price for 5 minutes and 2 times for 1 hour. A read costs 0.1 times on Haiku 5.5 and 0.05 times on Sonnet 5.5 and Opus 5.5.
- **Haiku 5.5's 100K cliff.** Haiku 5.5's prompt length counts every input token, cache reads and writes included. A request whose prompt passes 100,000 tokens pays the higher row for all of it, output too. A SAGE run starts near 35,000 tokens and often passes 100,000 by turn 4. From then on, every turn costs five times as much, whatever the hit rate. ([pricing][price])
- Sonnet 5.5 and Opus 5.5 have no length tier. All three models have a 1M-token context window by default, with no beta header. ([context windows][cw], [models overview][models])

## How SAGE uses it

`models.cached` sets three breakpoints through pydantic-ai:

| Breakpoint | pydantic-ai setting | Covers | Lifetime |
|---|---|---|---|
| Last tool definition | `anthropic_cache_tool_definitions` | The tools, the same for every paper | 5 minutes |
| Last instruction block | `anthropic_cache_instructions` | `guide.md` and the paper view | 5 minutes |
| Automatic, on the last block | `anthropic_cache` | The conversation so far | 5 minutes |

- pydantic-ai trims breakpoints past 4 (`_limit_cache_points` in `pydantic_ai/models/anthropic.py`).
- Every breakpoint lasts 5 minutes. Across the saved Haiku runs, the longest gap between the starts of two turns was 189 seconds, so each turn reads the last one's entries well inside it. A resumed run, or a new run of the same paper with the same model and effort, sends identical tools and instructions, so it reads them too when it starts within 5 minutes.
- SAGE doesn't use the 1-hour lifetime. On a 1-hour entry, each write costs 0.75 times the input price more. It pays off only when a run reuses the prefix after a gap of 5 to 60 minutes. On Haiku 5.5 that saves about $0.004 per reuse and costs $0.0027 on every run. A turn longer than 5 minutes, which a long Sonnet or Opus turn can be, makes the next turn write the conversation again. If those turns become common, measure the gaps before choosing 1 hour. ([extended thinking][think])
- The table pass (`models.cached_instructions`) has one breakpoint, on its instructions, for 5 minutes. It covers its output tool as well, 784 tokens in all. Each call sends a different table image after that prefix, so an automatic breakpoint would only pay to write an image no other call reads. The first `max_concurrency` calls all write the prefix, because they start together. Later calls, and the next paper's table pass within 5 minutes, read it. The table pass transcribes each table once and keeps it in `document.json`, so the saving is small. On cropsci 2004's 10 tables, 4 calls wrote the prefix and 6 read it, and the pass cost $0.00317 instead of $0.00351.
- `cache_diagnostics` (off by default) has the API compare each request with the previous one. It names where the prompt first differed. SAGE logs the answer as `cache_miss` on the turn's `usage` event, and `sage logs` prints it. It is free, but Anthropic rejects it for HIPAA-enabled organizations. ([cache diagnostics][diag])

## Rules

Keep every one of these. Each has broken caching in a real agent.

1. **Keep the history append-only.** Never clear, summarise, truncate or reorder earlier messages, tool results or thinking blocks. That includes server-side context editing (`clear_tool_uses_*`, `clear_thinking_*`) and pydantic-ai history processors. An edit at turn 3 makes every later turn write everything after turn 3 again. See [What clearing cost](#what-clearing-cost). `rules/append-only-history.yml` fails `scripts/check` on context editing, compaction and history processors.
2. **Keep the prefix static.** Nothing that varies between runs of the same paper goes into the tools or the instructions: no time, date, run id, budget, setting or counter. Put per-run facts, such as the resume summary and the scientist's guidance, in the first user message, which comes after the cached prefix.
3. **Build the tools the same way every time.** Same list, same order, same descriptions, same JSON schema, for every paper and every turn. Never add or remove a tool mid-run. To steer the agent, change what a tool returns, not which tools exist. ([Manus][manus], [Claude Code][cc])
4. **Serialise deterministically.** Anything rendered into the prefix comes from ordered data. Sort sets and dicts before you join or dump them. ([Manus][manus], [prompt caching][pc])
5. **Fix the model, effort and thinking for the whole run.** A run on another model never shares a cache. To change effort mid-run, use the per-message effort system message under its beta, not the top-level setting. ([effort][effort])
6. **Keep `tool_choice` the same on every turn.**
7. **Keep each turn under 20 blocks.** A turn that adds more than 20 positions, not counting the collapsed tool runs, pushes the previous entry out of the lookback. Then every request writes the whole conversation again. Add an explicit breakpoint if a tool ever returns many separate blocks.
8. **Never send the same prefix in parallel and expect hits.** To fan out, send one request, wait for its first streamed token, then send the rest.
9. **Check the numbers after any change to prompt assembly.** A broken cache raises nothing. Requests succeed and the bill grows.

## Measuring

- pydantic-ai's `input_tokens` already includes cache reads and writes. In SAGE's `Usage`:
  - hit rate = `cache_read_tokens / input_tokens`
  - write share = `cache_write_tokens / input_tokens`
  - uncached ≈ `input_tokens − cache_read_tokens − cache_write_tokens`
- The run totals are in `run.json` (`usage`, and `table_usage` for the table pass). Each turn's usage is in `events.jsonl`, as a `type: "usage"` event with `role: "extractor"`.
- **A healthy turn** reads all of the previous turn's prompt and writes only what that turn added: the last response, which is mostly thinking and `write` arguments, and the new tool results. The uncached tail is a few tokens.
- **A broken turn** has its read fall back to a fixed number, often the size of the prefix, while it writes most of the conversation again. Turn on `cache_diagnostics` and read `cache_miss` to see whether tools, system or messages changed.
- The first turn of a cold run always writes the whole prefix. A run that ends writes a last tail no turn reads. SAGE can avoid neither.

## What clearing cost

Run `20261017T182326-deb2` on cropsci 2004 (Haiku 5.5) used Anthropic's `clear_tool_uses_20250919` edit. Past a threshold, the API cleared every tool result but the newest 8. The run shows what that costs:

- Turns 3 to 8 read 95 to 99% of their prompt from the cache.
- From turn 9, each turn cleared one more tool result, in the middle of the conversation. No message entry still matched, so each turn read only the 35,616-token prefix and wrote 73,000 to 80,000 tokens again. Turn 17 cleared nothing new and read 99.7%.
- Those turns cost about $0.05 each instead of about $0.008. That came to $0.39 of the run's $0.56, all above Haiku's 100K line.
- The prompt barely shrank: 107,490 tokens at turn 8, 108,757 at turn 9. Tool results are small, and most of the context is the model's own `write` calls and thinking, which clearing never touched.

Anthropic's own [context-editing docs][ce] say clearing invalidates cached prefixes. They suggest `clear_at_least` so a clear is big enough to pay for the rewrite. SAGE does not clear at all.

## Sources

- [Prompt caching][pc], [cache diagnostics][diag], [context editing][ce], [context windows][cw], [models overview][models], [pricing][price], [effort][effort] and [extended thinking][think], on platform.claude.com
- Thariq Shihipar, [Lessons from building Claude Code: prompt caching is everything][cc] (April 2026)
- Yichao Ji, [Context engineering for AI agents: lessons from building Manus][manus] (July 2025). Its KV-cache section argues for a stable prefix, append-only context, deterministic serialisation, and masking tools instead of removing them.
- ProjectDiscovery, [How we cut LLM costs by 59% with prompt caching][pd] (April 2026). Moving per-step state out of the prefix raised their hit rate from 7% to 84%.
- [pydantic-ai Anthropic models][pyd], and `pydantic_ai/models/anthropic.py` in `.venv`

[pc]: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
[diag]: https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics
[ce]: https://platform.claude.com/docs/en/build-with-claude/context-editing
[cw]: https://platform.claude.com/docs/en/build-with-claude/context-windows
[models]: https://platform.claude.com/docs/en/about-claude/models/overview
[price]: https://platform.claude.com/docs/en/about-claude/pricing
[effort]: https://platform.claude.com/docs/en/build-with-claude/effort
[think]: https://platform.claude.com/docs/en/build-with-claude/thinking
[cc]: https://claude.dev/blog/lessons-from-building-claude-code-prompt-caching-is-everything/
[manus]: https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
[pd]: https://projectdiscovery.io/blog/how-we-cut-llm-cost-with-prompt-caching
[pyd]: https://pydantic.dev/docs/ai/models/anthropic/
