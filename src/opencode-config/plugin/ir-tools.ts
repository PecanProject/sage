/**
 * ir-tools.ts — OpenCode custom tool plugin: the read and schema tools the agents use. Each tool builds a request,
 * calls `pipeline/ir_service.py` over HTTP and returns the JSON body; no validation or business logic lives here.
 * Commits and validation are called by pipeline/orchestrator.py directly, never by an agent.
 */

import { tool, type Plugin } from "@opencode-ai/plugin";

const IR_SERVICE_URL = process.env.IR_SERVICE_URL ?? "http://127.0.0.1:8420";
const { schema } = tool;

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
    // Surface the service's own error body rather than a bare HTTP status.
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
          "NEVER blocks or gates anything -- variable_name and event_type are free text at the IR layer. " +
          "Use this only as a naming-consistency nudge, never as a required step.",
        args: {
          term: schema.string(),
          entity_type: schema.enum(["event_type", "variable_name"]),
        },
        async execute(args) {
          return callService("GET", "/lookup_vocab", args);
        },
      }),

      apply_reconstruction: tool({
        description:
          "Deterministic reconstruction helpers: 'date_mapping' (raw date/date-range text -> DateRange shape), " +
          "'stat_encoding' (raw {label: value} stats -> StatisticalSummary entries), 'factorial_expansion' " +
          "(factor-level table -> reported_effect_scope/aggregated_over_factors payload fragment, following IR " +
          "spec Table 16 exactly). Returns a payload FRAGMENT; it does not validate or commit anything.",
        args: {
          kind: schema.enum(["date_mapping", "stat_encoding", "factorial_expansion"]),
          payload: jsonObject().describe("Kind-specific arguments, e.g. {reported_text: '...'} for date_mapping."),
        },
        async execute(args) {
          return callService("POST", "/apply_reconstruction", undefined, args);
        },
      }),

    },
  };
};

export default IrToolsPlugin;
