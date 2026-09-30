/**
 * ir-tools.ts — OpenCode custom tool plugin.
 *
 * Playbook Section 6: "Custom tools, exposed to the OpenCode agent via a
 * TypeScript plugin, each a thin call to a local Python `ir-service`
 * (FastAPI) wrapping the real Pydantic schema/validators/reconstruction
 * logic -- never reimplement that logic in TypeScript."
 *
 * CORRECTED (Sprint 2, verified via `opencode debug agent extractor`):
 * the first version of this file exported a plain object of tool
 * definitions built from a hand-written JSON-schema-ish shape. Running the
 * real installed OpenCode (`opencode-ai@1.18.30`) against it surfaced
 * `"failed to load plugin" ... error="Plugin export is not a function"` --
 * the actual `@opencode-ai/plugin` API (installed locally at
 * node_modules/@opencode-ai/plugin, confirmed by reading its .d.ts files
 * directly rather than guessing) requires:
 *   - a default export that is an async `Plugin` function
 *     `(input: PluginInput, options?) => Promise<Hooks>`
 *   - hooks.tool as `{ [name]: ToolDefinition }`, where each ToolDefinition
 *     comes from the `tool({ description, args, execute })` helper
 *   - `args` as a Zod raw shape (via the exported `tool.schema`, which IS
 *     zod), not a JSON-schema object
 * This version matches that real shape and was re-verified against the
 * installed OpenCode after the fix (see Playbook Section 3/9).
 *
 * Every tool body still does exactly one thing: build a request, call
 * `pipeline/ir_service.py` over HTTP, return the JSON body straight
 * through. No schema knowledge, no validation branching, no business
 * logic lives in this file.
 */

import { tool, type Plugin, type ToolContext, type ToolResult } from "@opencode-ai/plugin";
import { mkdir, writeFile } from "node:fs/promises";
import * as nodePath from "node:path";

const IR_SERVICE_URL = process.env.IR_SERVICE_URL ?? "http://127.0.0.1:8420";
const { schema } = tool;

// ---------------------------------------------------------------------------
// TEMPORARY DIAGNOSTIC INSTRUMENTATION -- throwaway, not the future
// raw -> normalize -> schema -> UI layer planned for later work.
//
// Added only to debug propose_record/commit_record payload failures for
// GPT-OSS 120B and Llama 4 Scout on paper_id "pecan" (malformed tool calls /
// retries on one model, a literal placeholder string on the other). Delete
// this whole block plus the two `withDebugCapture(...)` wrap calls below
// once that bug is resolved -- it has no role in the permanent architecture.
//
// Fully inert unless IR_TOOLS_DEBUG_CAPTURE=1 is set: with it unset,
// `withDebugCapture` returns the original `execute` untouched, so
// propose_record/commit_record's control flow, arguments, and response
// shape are all byte-for-byte identical to before this block existed.
//
// Known limitation: ToolContext (the installed @opencode-ai/plugin version)
// exposes `agent`, `sessionID`, and `messageID` but not the active model ID
// -- there is no synchronous way to read "which model is running" from
// inside a tool call in this SDK version. Model identity has to be
// cross-referenced afterwards from `sessionID`/`messageID` against
// `opencode --log-level DEBUG` output, not read directly here.
// ---------------------------------------------------------------------------

const DEBUG_CAPTURE_ENABLED = process.env.IR_TOOLS_DEBUG_CAPTURE === "1";
const DEBUG_CAPTURE_ROOT = "debug_captures";

// Attempt numbering for capture filenames only -- a reading convenience for
// this diagnostic, not the server-side turn-cap counter in ir_service.py,
// which remains the sole source of truth for the real attempt cap.
const _captureAttemptCounts = new Map<string, number>();

function nextCaptureAttempt(key: string): number {
  const n = (_captureAttemptCounts.get(key) ?? 0) + 1;
  _captureAttemptCounts.set(key, n);
  return n;
}

function captureResponseBody(result: ToolResult): unknown {
  const text = typeof result === "string" ? result : result.output;
  try {
    return JSON.parse(text);
  } catch {
    return { unparsed_output: text };
  }
}

/**
 * Wrap a propose_record/commit_record `execute` so that, when
 * IR_TOOLS_DEBUG_CAPTURE=1, every call that reaches this function is dumped
 * to disk after the real response is computed -- raw args exactly as
 * received, agent/session identifiers, and the service's own response.
 * Never throws and never alters the returned value: a capture failure is
 * logged to stderr and swallowed so it can't affect the real tool response
 * going back to the model.
 */
function withDebugCapture(
  toolName: "propose_record" | "commit_record",
  execute: (args: any, context: ToolContext) => Promise<ToolResult>
): (args: any, context: ToolContext) => Promise<ToolResult> {
  if (!DEBUG_CAPTURE_ENABLED) {
    return execute;
  }
  return async (args, context) => {
    const result = await execute(args, context);
    try {
      const paperId = String(args?.paper_id ?? "unknown_paper");
      const entityType = String(args?.entity_type ?? "unknown_entity");
      const recordId = String(args?.record_id ?? "unknown_record");
      const key = `${toolName}:${paperId}:${entityType}:${recordId}`;
      const attempt = nextCaptureAttempt(key);
      const ts = Math.floor(Date.now() / 1000);
      const dir = nodePath.join(DEBUG_CAPTURE_ROOT, toolName);
      await mkdir(dir, { recursive: true });
      const fileName = `${paperId}__${entityType}__${recordId}__attempt${attempt}__${ts}.json`;
      const record = {
        tool: toolName,
        captured_at: new Date().toISOString(),
        agent: context.agent,
        sessionID: context.sessionID,
        messageID: context.messageID,
        args_as_received: args,
        service_response: captureResponseBody(result),
      };
      await writeFile(nodePath.join(dir, fileName), JSON.stringify(record, null, 2), "utf-8");
    } catch (captureError) {
      console.error(`[ir-tools debug capture] failed to write capture for ${toolName}:`, captureError);
    }
    return result;
  };
}

async function callService(
  method: "GET" | "POST",
  path: string,
  params?: Record<string, string>,
  body?: unknown
): Promise<string> {
  let url = `${IR_SERVICE_URL}${path}`;
  if (params && Object.keys(params).length > 0) {
    url += `?${new URLSearchParams(params).toString()}`;
  }
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) {
    // Surface the service's own error body to the agent rather than a bare
    // HTTP status -- propose_record/commit_record rejections are meant to
    // be read and acted on, not just retried blindly.
    return JSON.stringify({ ok: false, status: res.status, error: json });
  }
  return JSON.stringify({ ok: true, status: res.status, ...(typeof json === "object" && json !== null ? json : { result: json }) });
}

const jsonObject = () => schema.record(schema.string(), schema.any());

export const IrToolsPlugin: Plugin = async (_input, _options) => {
  return {
    tool: {
      get_schema: tool({
        description:
          "Return the field semantics (JSON schema) and a filled worked example for an IR entity type " +
          "(Citation, Study, Site, Species, Method, Treatment, Management, Observation). Call this before " +
          "constructing a payload for propose_record -- do not guess field names or shapes from memory.",
        args: {
          entity_type: schema.string().describe("e.g. 'Treatment', 'Study', 'Observation'"),
        },
        async execute(args) {
          return callService("GET", "/get_schema", { entity_type: args.entity_type });
        },
      }),

      read_section: tool({
        description:
          "Read a section of a paper's already-rendered content.md by (case-insensitive substring) heading " +
          "match. Returns the section text with its content.md block anchors (b:NNNN) intact, so you can cite " +
          "them directly as locators in propose_record. Returns found=false, not an error, if no heading matches.",
        args: {
          paper_id: schema.string(),
          section_name: schema.string().describe("Substring to match against a content.md heading, e.g. 'Materials and Methods'."),
        },
        async execute(args) {
          return callService("GET", "/read_section", args);
        },
      }),

      read_document_start: tool({
        description:
          "Read the beginning of a paper's already-rendered content.md. Use this " +
          "to locate front matter such as the paper's actual title, authors, publication " +
          "details, and other metadata that may appear before the first section heading. " +
          "Returns the text with content.md block anchors (b:NNNN) intact. Do not use " +
          "this to infer values that are not actually visible in the returned text.",
        args: {
          paper_id: schema.string(),
          max_lines: schema.number().optional().describe(
            "Maximum number of content.md lines to return; defaults to 80."
          ),
        },
        async execute(args) {
            return callService("GET", "/read_document_start", {
                paper_id: args.paper_id,
                ...(args.max_lines !== undefined
                  ? { max_lines: String(args.max_lines) }
                  : {}),
            });
        },
      }),

      read_table: tool({
        description:
          "Read a rendered markdown table from a paper's content.md by its block anchor id (e.g. 'b:0048', the " +
          "same anchor that appears inline in content.md and in provenance.json). Use this instead of re-reading " +
          "the whole content.md when you already know which table you need. For citing a SPECIFIC row or value " +
          "inside a table, prefer read_table_row / read_table_cell instead -- they hand you the exact anchor to " +
          "cite for that row, so you never have to work it out yourself from a large rendered table. Downstream, " +
          "a Sage IR locator with kind='table' requires BOTH block_anchor and table_id set to this same anchor " +
          "value -- a kind='text' locator (block_anchor only) is always valid too and simpler when in doubt.",
        args: {
          paper_id: schema.string(),
          table_id: schema.string().describe("Block anchor, e.g. 'b:0048' (brackets optional)."),
        },
        async execute(args) {
          return callService("GET", "/read_table", args);
        },
      }),

      list_sections: tool({
        description:
          "List the paper's real section outline (each entry: section_path as an array of heading strings, and " +
          "the anchor where that section begins) -- computed once during document preparation, not guessed from " +
          "heading text. Call this BEFORE read_section_by_path when you don't already know the exact section " +
          "path to ask for.",
        args: {
          paper_id: schema.string(),
        },
        async execute(args) {
          return callService("GET", "/list_sections", args);
        },
      }),

      read_section_by_path: tool({
        description:
          "Read every rendered block whose real section path (from list_sections) matches exactly, or has it as " +
          "a prefix (so a top-level heading also returns its subsections). Prefer this over read_section when " +
          "you already know the section path from list_sections -- it matches the paper's actual computed " +
          "structure rather than a substring guess against heading text.",
        args: {
          paper_id: schema.string(),
          section_path: schema.string().describe(
            "Section path segments joined with ' > ', e.g. 'Materials and methods > Site description' -- use " +
            "the exact segments list_sections returned for this section."
          ),
        },
        async execute(args) {
          return callService("GET", "/read_section_by_path", args);
        },
      }),

      read_table_row: tool({
        description:
          "Read one specific row of a table by its row_index (0-based, top to bottom as the table appears -- " +
          "geometrically clustered during document preparation, not something you need to count yourself). " +
          "Returns the row's cell texts AND the exact anchor to cite for this row (always the table's own block " +
          "anchor -- the same one read_table returns, and the only one that can ever be validated as a locator). " +
          "Use this instead of read_table plus counting rows yourself when you need to cite one specific row's " +
          "value precisely. Downstream, a Sage IR locator with kind='table' requires BOTH block_anchor and " +
          "table_id set to this same table_anchor value.",
        args: {
          paper_id: schema.string(),
          table_anchor: schema.string().describe("The table's own block anchor, e.g. 'b:0059' (brackets optional)."),
          row_index: schema.number().describe("0-based row index, including header rows."),
        },
        async execute(args) {
          return callService("GET", "/read_table_row", {
            paper_id: args.paper_id, table_anchor: args.table_anchor, row_index: String(args.row_index),
          });
        },
      }),

      read_table_cell: tool({
        description:
          "Read one exact cell of a table by (row_index, col_index), both 0-based. Same anchor-citation behavior " +
          "as read_table_row -- always cite the returned table_anchor, never a made-up per-cell anchor. " +
          "Downstream, a Sage IR locator with kind='table' requires BOTH block_anchor and table_id set to this " +
          "same table_anchor value.",
        args: {
          paper_id: schema.string(),
          table_anchor: schema.string().describe("The table's own block anchor, e.g. 'b:0059' (brackets optional)."),
          row_index: schema.number().describe("0-based row index, including header rows."),
          col_index: schema.number().describe("0-based column index, left to right."),
        },
        async execute(args) {
          return callService("GET", "/read_table_cell", {
            paper_id: args.paper_id, table_anchor: args.table_anchor,
            row_index: String(args.row_index), col_index: String(args.col_index),
          });
        },
      }),

      read_nearby: tool({
        description:
          "Read the rendered blocks immediately before/after a given anchor, in real document order -- use this " +
          "to double-check what a candidate anchor's surrounding context actually says before citing it, without " +
          "re-reading the whole document.",
        args: {
          paper_id: schema.string(),
          anchor: schema.string().describe("The anchor to center the window on, e.g. 'b:0051' (brackets optional)."),
          before: schema.number().optional().describe("How many rendered blocks before `anchor` to include (default 1)."),
          after: schema.number().optional().describe("How many rendered blocks after `anchor` to include (default 1)."),
        },
        async execute(args) {
          return callService("GET", "/read_nearby", {
            paper_id: args.paper_id, anchor: args.anchor,
            ...(args.before !== undefined ? { before: String(args.before) } : {}),
            ...(args.after !== undefined ? { after: String(args.after) } : {}),
          });
        },
      }),

      lookup_vocab: tool({
        description:
          "Advisory-only fuzzy lookup against a seed controlled vocabulary for event_type or variable_name. " +
          "NEVER blocks or gates anything -- variable_name and event_type are free text at the IR layer " +
          "(Playbook Section 5). Use this only as a naming-consistency nudge, never as a required step.",
        args: {
          term: schema.string(),
          entity_type: schema.enum(["event_type", "variable_name"]),
        },
        async execute(args) {
          return callService("GET", "/lookup_vocab", args);
        },
      }),

      propose_record: tool({
        description:
          "Validate a candidate IR record without committing it. payload is a JSON OBJECT containing the record " +
          "object -- a native structured object, not a string. " +
          "Construct the complete record object according to get_schema and pass it directly as payload. " +
          "get_schema's json_schema distinguishes two field shapes -- check each field's own schema entry, do not assume " +
          "they all match: (1) an identifier field such as Citation.id, Treatment.citation_id, or Site.id is a BARE " +
          "STRING (e.g. \"id\": \"smukler2012a\") -- never wrap it in {value, provenance_label, source}. " +
          "(2) a field typed ExtractedField in the schema (author, year, title, name, etc.) MUST use the structure " +
          "{value, provenance_label, source}, and source must contain source_document_id, page_number, and locators. " +
          "Each locator MUST be an OBJECT such as {kind:'text', block_anchor:'b:0123'}, never a bare anchor string. " +
          "Use the exact field names and shapes returned by get_schema. Do not invent or rename fields. " +
          "Citation.persistent_identifier is REQUIRED and must always be structurally present, like author/year/title -- " +
          "never omitted. If the source states a real DOI/PID, set it EXTRACTED with that value and a locator. " +
          "If no DOI/PID appears anywhere in the visible content, still include the field as UNRESOLVED " +
          "(value: null, a real unresolved_reason, and a source with at least one locator showing where you looked) -- " +
          "never omit the field and never fabricate a DOI/PID that isn't in the source. " +
          "Never invent bibliographic metadata merely to satisfy this tool shape. " +
          "The server/Pydantic validator is authoritative. Use this tool to iterate before commit_record. " +
          "There is a hard cap of 4 failed proposals per (paper_id, entity_type, record_id).",
        args: {
          paper_id: schema.string(),
          entity_type: schema.string(),
          record_id: schema.string(),
          payload: jsonObject().describe(
            "The complete candidate record object, constructed according to the exact schema returned by " +
            "get_schema. Pass it as a native JSON object -- do not JSON-encode it into a string. " +
            "Do not omit required fields. Do not invent values."
          ),
          dataset_context: jsonObject()
            .optional()
            .describe(
              "Optional partial IRDataset (other already-known citations/studies/treatments/etc.) so whole-graph checks (e.g. Study-scoped Treatment.name uniqueness) can run."
            ),
        },
        execute: withDebugCapture("propose_record", async (args) => {
          return callService("POST", "/propose_record", undefined, args);
        }),
      }),

      apply_reconstruction: tool({
        description:
          "Deterministic reconstruction helpers: 'date_mapping' (raw date/date-range text -> DateRange shape), " +
          "'stat_encoding' (raw {label: value} stats -> StatisticalSummary entries), 'factorial_expansion' " +
          "(factor-level table -> reported_effect_scope/aggregated_over_factors payload fragment, following IR " +
          "spec Table 16 exactly). Returns a payload FRAGMENT you still must run through propose_record -- this " +
          "does not validate or commit anything itself.",
        args: {
          kind: schema.enum(["date_mapping", "stat_encoding", "factorial_expansion"]),
          payload: jsonObject().describe("Kind-specific arguments, e.g. {reported_text: '...'} for date_mapping."),
        },
        async execute(args) {
          return callService("POST", "/apply_reconstruction", undefined, args);
        },
      }),

      flag_unresolved: tool({
        description:
          "Mark a field UNRESOLVED with a documented reason, for the review case the whole provenance design " +
          "exists for. REQUIRED: blocks_examined (every content.md block anchor you actually looked at) and " +
          "conflict_explanation (why those blocks conflict or fall short) -- a bare reason string is rejected " +
          "server-side. This is also the mandatory off-ramp once propose_record's turn cap is hit.",
        args: {
          paper_id: schema.string(),
          entity_type: schema.string(),
          record_id: schema.string(),
          field: schema.string(),
          reason: schema.string(),
          blocks_examined: schema.array(schema.string()).describe("content.md block anchors, e.g. ['b:0012','b:0013']."),
          conflict_explanation: schema.string().describe("Why the examined blocks conflict or fall short of resolving the field."),
        },
        async execute(args) {
          return callService("POST", "/flag_unresolved", undefined, args);
        },
      }),

      commit_record: tool({
        description:
          "Commit a record to the append-only ir-store. status MUST be exactly 'ready' or 'unresolved' " +
          "(enforced server-side). Both statuses re-run structural (Pydantic) and provenance validation server-side " +
          "and are REJECTED if the payload shape is invalid -- passing propose_record earlier does not exempt " +
          "commit_record from validating again. status='unresolved' means specific fields may carry " +
          "provenance_label='UNRESOLVED' (value=null, a real unresolved_reason) -- it does NOT mean the payload's " +
          "shape goes unchecked; identifier fields such as id are still required as bare strings. Only whole-graph " +
          "dataset_context checks (e.g. duplicate names) are 'ready'-only.",
        args: {
          paper_id: schema.string(),
          entity_type: schema.string(),
          record_id: schema.string(),
          payload: jsonObject().describe(
            "The complete candidate record object, constructed according to the exact schema returned by " +
            "get_schema. Pass it as a native JSON object -- do not JSON-encode it into a string."
          ),
          status: schema.enum(["ready", "unresolved"]),
          dataset_context: jsonObject().optional().describe("Optional, same as propose_record — enables whole-graph checks before commit."),
        },
        execute: withDebugCapture("commit_record", async (args) => {
          return callService("POST", "/commit_record", undefined, args);
        }),
      }),
    },
  };
};

export default IrToolsPlugin;
