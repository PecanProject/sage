"""
Marker JSON -> (content.md, provenance.json) adapter.

content.md and provenance.json are both produced from ONE walk of Marker's JSON block tree. What the walk relies on:

  - Every block (leaf or container) has the SAME shape:
      {id, block_type, html, polygon, bbox, children, section_hierarchy, images}
    Content lives in `html` (HTML, not plain text) for leaf blocks.
  - `id` is already a globally-unique, stable string of the form
      /page/{page_idx}/{BlockType}/{seq}
    with `seq` unique across the WHOLE document (not reset per page), reused
    directly as the stable block_id.
  - `section_hierarchy` is already computed BY MARKER on every single block
    (not just headers): {"<level>": "<SectionHeader block id active at that
    level>"}. We resolve this to a list of heading strings for section_path --
    we do not need to reconstruct heading nesting ourselves during the walk.
    Levels are not guaranteed contiguous (e.g. {"1": ..., "4": ...} with no
    "2"/"3") -- this reflects Marker's own layout-based heading-level detection
    and is preserved as-is, not renumbered.
  - Container/wrapper blocks (TableGroup, FigureGroup, ListGroup) do NOT carry
    real content in their own `html` -- it's a templated skeleton of
    <content-ref src='...'/> placeholders; children[] order matches the
    content-ref order, so we recurse into children and never render a Group's
    own `html`.
  - Table.html (unlike Group.html) IS the fully-rendered real table (actual
    <td> content, not content-refs). We render content.md's table anchor from
    Table.html directly, converted to a markdown table. The TableCell children
    are flat and carry no row/col index, only geometry, so row/column
    provenance is derived by geometric clustering (rows by y-overlap, columns
    by x within a row).
  - Figure blocks carry the actual base64 image bytes directly in their own
    `images` dict (keyed by their own id) -- there is no separate image-extraction
    pass needed to get pixel data for a future source_panel vision pass.
  - Citable leaves: Text, TableCell, Table, Figure, Caption, SectionHeader,
    ListItem, Footnote and Equation.
  - PageHeader/PageFooter are running furniture: excluded from content.md but
    still logged in provenance.json.
  - `--use_llm` can double-HTML-escape <sup> tags in Footnote blocks; that
    signature is repaired by _fix_marker_llm_sup_corruption.
"""

from __future__ import annotations

import html as html_module
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Block-type policy
# ---------------------------------------------------------------------------

# Rendered into content.md with a visible anchor AND logged in provenance.json.
CITABLE_LEAF_TYPES = {
    "Text",
    "SectionHeader",
    "Table",       # rendered as a whole table from Table.html
    "Figure",      # rendered as a placeholder + caption text
    "Picture",     # same placeholder+image-bytes handling as Figure, but the
                    # a Picture may stand alone with no Caption: render it anyway
    "Caption",
    "ListItem",
    "Footnote",    # widened from the original Section 4 list -- see module docstring
    "Equation",    # widened from the original Section 4 list -- see module docstring
}

# Logged in provenance.json for row/col-level citation, but NOT given their own
# anchor in content.md (their content is already visible via the parent Table's
# single rendered anchor).
PROVENANCE_ONLY_TYPES = {
    "TableCell",
}

# Logged in provenance.json (never silently drop information) but excluded from
# the rendered content.md the extraction agent reads, because they are running
# page furniture with no informational content.
PROVENANCE_ONLY_SILENT_TYPES = {
    "PageHeader",
    "PageFooter",
}

# Pure structural containers: never rendered, never separately logged in
# provenance.json (their content is fully represented by their children).
STRUCTURAL_CONTAINER_TYPES = {
    "Document",
    "Page",
    "TableGroup",
    "FigureGroup",
    "PictureGroup",  # same content-ref-skeleton shape as FigureGroup, pairing
                     # a Picture with its Caption; handled like FigureGroup
    "ListGroup",
}


# ---------------------------------------------------------------------------
# HTML -> text/markdown cleanup
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_MATH_RE = re.compile(r"<math[^>]*>(.*?)</math>", re.DOTALL)
# An HTML-ESCAPED formatting tag as the --use_llm postprocessing leaves it in body text ("&lt;sup&gt;-2&lt;/sup&gt;").
# Only these tag names: an escaped "&lt;" followed by anything else is a real less-than sign in the paper ("P &lt; 0.05",
# "&lt;0.5 mm") and must survive as text.
_ESCAPED_FORMAT_TAG_RE = re.compile(r"&lt;(/?)(sup|sub|u|b|i|em|strong|span|br|p)\b([^&<>]*?)(/?)&gt;", re.IGNORECASE)
_CONTENT_REF_RE = re.compile(r"<content-ref\s+src=['\"]([^'\"]+)['\"]\s*/?>")

# Exact signature of the --use_llm Footnote <sup> double-escaping bug observed
# in light-use-2007.json (confirmed on all 7 footnotes in that file). Repairs
# "<sup>&amp;</sup>lt;sup&gt;N&lt;/sup&gt;" back to "<sup>N</sup>".
_SUP_CORRUPTION_RE = re.compile(
    r"<sup>&amp;</sup>lt;sup&gt;(.*?)&lt;/sup&gt;"
)


def _fix_marker_llm_sup_corruption(raw_html: str) -> tuple[str, bool]:
    """Repair the known --use_llm footnote <sup> DOUBLE-escape corruption
    signature (severity 1, observed exclusively in Footnote blocks so far).

    Returns (possibly-repaired html, whether a repair was applied). The bool
    is surfaced into provenance.json as a flag so curators know to spot-check
    that particular block against the source PDF rather than trusting the
    repair blindly (P5: make uncertainty explicit -- this applies to our own
    cleanup heuristics too, not just to extracted field values).
    """
    fixed, n = _SUP_CORRUPTION_RE.subn(r"<sup>\1</sup>", raw_html)
    return fixed, (n > 0)


# Severity-2 signature: a PLAIN single HTML-escape of a pseudo <sup>/<sub> tag
# (e.g. literal "&lt;sup&gt;-2&lt;/sup&gt;"), confirmed appearing inline within
# ordinary body Text blocks, mixed unpredictably with correctly-formed real
# <math> tags in the same sentence. This is a distinct, milder corruption from
# the Footnote double-escape above (severity 1) -- both are the same
# underlying --use_llm bug class, but the regex signatures don't overlap, so
# they're detected and reported separately rather than conflated into one
# check. Generic: matches the escape pattern itself, not any paper-1-specific
# text content.
_SINGLE_ESCAPE_SUP_SUB_RE = re.compile(r"&lt;su[bp]&gt;.*?&lt;/su[bp]&gt;")


def _detect_single_escape_sup_sub(raw_html: str) -> bool:
    """Detect (does not itself repair -- html_to_text's unescape-before-strip
    ordering already handles this generically) the severity-2 single-escape
    signature, purely so it can be surfaced as `repaired: true` in provenance
    like the severity-1 case, giving reviewers one consistent field to filter
    on regardless of which corruption class fired."""
    if not raw_html:
        return False
    return bool(_SINGLE_ESCAPE_SUP_SUB_RE.search(raw_html))


def _math_to_text(raw_html: str) -> str:
    """Keep inline/block math as raw TeX-ish text (Marker's own <math> payload
    is already close to LaTeX, e.g. '\\varepsilon', '\\tag{1}'). We do not
    attempt to render it -- the extraction agent reads text, and faithfully
    keeping the source markup is more useful than a lossy re-render.
    """
    return _MATH_RE.sub(lambda m: f"${m.group(1).strip()}$", raw_html)


def html_to_text(raw_html: Optional[str]) -> str:
    """Best-effort HTML -> plain text for a single leaf block's `html` field.

    Order matters:
      1. Fix the Footnote double-escape signature.
      2. Turn escaped formatting tags ("&lt;sup&gt;") back into real tags.
      3. Strip real tags.
      4. Unescape entities last, so a literal "P < 0.05" survives as text.
      5. Collapse whitespace.
    """
    if not raw_html:
        return ""
    fixed, _ = _fix_marker_llm_sup_corruption(raw_html)
    with_math = _math_to_text(fixed)
    real_tags = _ESCAPED_FORMAT_TAG_RE.sub(lambda m: f"<{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}>", with_math)
    no_tags = _TAG_RE.sub(" ", real_tags)
    text = html_module.unescape(no_tags)
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text


def table_html_to_markdown(raw_html: Optional[str]) -> str:
    """Convert a Table block's own (fully-rendered) html into a markdown table.

    Table.html is real <table><tr><td> markup; a lightweight row/cell
    extraction, no HTML parser dependency.
    """
    if not raw_html:
        return "*[empty table]*"

    row_re = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
    cell_re = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.DOTALL)

    rows = []
    for row_match in row_re.finditer(raw_html):
        row_html = row_match.group(1)
        cells = [html_to_text(c) for c in cell_re.findall(row_html)]
        # normalize embedded <br/> line breaks (common in this real file, e.g.
        # multi-line cell content) into " / " so a markdown table row stays
        # single-line and doesn't break table rendering.
        cells = [re.sub(r"\s*/\s*/\s*", " / ", c) for c in cells]
        rows.append(cells)

    if not rows:
        return "*[table with no parseable rows]*"

    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]

    lines = []
    header, *body = rows
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * width) + "|")
    for r in body:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Row/column derivation for TableCell provenance (geometric clustering)
# ---------------------------------------------------------------------------

def derive_row_col(cells: list[dict]) -> dict[str, tuple[int, int]]:
    """Cluster TableCell blocks into (row_index, col_index) by bbox geometry.

    Rows by y-center overlap, columns left-to-right within a row. A heuristic:
    merged/spanning cells are not specially handled.
    """
    if not cells:
        return {}

    def y_center(c):
        b = c["bbox"]
        return (b[1] + b[3]) / 2.0

    def x_left(c):
        return c["bbox"][0]

    sorted_by_y = sorted(cells, key=y_center)
    rows: list[list[dict]] = []
    row_tol = 5.0  # points; small vertical jitter within the same row

    for c in sorted_by_y:
        placed = False
        for row in rows:
            if abs(y_center(row[0]) - y_center(c)) <= row_tol:
                row.append(c)
                placed = True
                break
        if not placed:
            rows.append([c])

    rows.sort(key=lambda row: y_center(row[0]))

    result = {}
    for r_idx, row in enumerate(rows):
        row_sorted = sorted(row, key=x_left)
        for c_idx, cell in enumerate(row_sorted):
            result[cell["id"]] = (r_idx, c_idx)
    return result


# ---------------------------------------------------------------------------
# Section path resolution
# ---------------------------------------------------------------------------

def build_section_path(section_hierarchy: dict, header_titles: dict[str, str]) -> list[str]:
    """Resolve a block's section_hierarchy dict (level -> SectionHeader block id)
    into an ordered list of heading text, ascending by level number.

    Levels are not guaranteed contiguous (Marker's own layout detection can
    skip levels, e.g. {"1":..., "4":...} with no "2"/"3" -- confirmed on page 0
    of light-use-2007.json). We preserve that gap rather than renumbering --
    a curator reviewing provenance should see the same heading structure
    Marker itself detected.
    """
    if not section_hierarchy:
        return []
    ordered_levels = sorted(section_hierarchy.keys(), key=lambda k: int(k))
    path = []
    for lvl in ordered_levels:
        header_id = section_hierarchy[lvl]
        title = header_titles.get(header_id, f"[unresolved header {header_id}]")
        path.append(title)
    return path


# ---------------------------------------------------------------------------
# Main single-pass walk
# ---------------------------------------------------------------------------

@dataclass
class WalkResult:
    content_md: str
    provenance: dict[str, Any]
    stats: dict[str, Any] = field(default_factory=dict)


def _resolve_content_refs(group_html: str, children: list[dict]) -> list[dict]:
    """Given a Group block's templated html and its children, return the
    children in the order the content-refs specify. Falls back to raw
    children order if refs can't be parsed (defensive; not expected to
    trigger given the light-use-2007.json evidence that order already
    matches, but we don't want a parsing edge case to silently reorder or
    drop content).
    """
    refs = _CONTENT_REF_RE.findall(group_html or "")
    if not refs:
        return children
    by_id = {c["id"]: c for c in children}
    ordered = [by_id[r] for r in refs if r in by_id]
    if len(ordered) != len(children):
        # Some referenced id wasn't found among children, or vice versa --
        # don't silently drop content; fall back to raw order and flag it
        # via the caller's stats counter instead.
        return children
    return ordered


def walk_document(doc: dict) -> WalkResult:
    stats = {
        "blocks_by_type": {},
        "footnote_corruption_repairs": 0,
        "single_escape_repairs": 0,
        "content_ref_order_mismatches": 0,
        "table_cells_without_row_col": 0,
    }

    # Pass 0: index every SectionHeader's resolved title text, so
    # section_hierarchy (level -> header block id) can be resolved to text
    # during the main walk without a second full tree traversal.
    header_titles: dict[str, str] = {}

    def index_headers(node: dict):
        bt = node.get("block_type")
        stats["blocks_by_type"][bt] = stats["blocks_by_type"].get(bt, 0) + 1
        if bt == "SectionHeader":
            header_titles[node["id"]] = html_to_text(node.get("html"))
        for c in node.get("children") or []:
            index_headers(c)

    index_headers(doc)

    content_lines: list[str] = []
    provenance: dict[str, Any] = {}
    anchor_counter = [0]

    def next_anchor() -> str:
        anchor_counter[0] += 1
        return f"b:{anchor_counter[0]:04d}"

    def record_provenance(anchor_id: str, node: dict, extra: Optional[dict] = None, render: bool = True):
        entry = {
            "marker_block_id": node["id"],
            "block_type": node["block_type"],
            "page_id": _page_id_from_block_id(node["id"]),
            "polygon": node.get("polygon"),
            "bbox": node.get("bbox"),
            "section_path": build_section_path(node.get("section_hierarchy") or {}, header_titles),
            "rendered_in_content_md": render,
        }
        if extra:
            entry.update(extra)
        provenance[anchor_id] = entry

    def render_table(node: dict):
        anchor = next_anchor()
        md_table = table_html_to_markdown(node.get("html"))
        content_lines.append(md_table)
        content_lines.append(f"⟦{anchor}⟧")
        content_lines.append("")
        record_provenance(anchor, node)

        cells = node.get("children") or []
        rc = derive_row_col(cells)
        for cell in cells:
            cell_anchor = next_anchor()
            row_col = rc.get(cell["id"])
            if row_col is None:
                stats["table_cells_without_row_col"] += 1
            record_provenance(
                cell_anchor,
                cell,
                extra={
                    "parent_table_anchor": anchor,
                    "row_index": row_col[0] if row_col else None,
                    "col_index": row_col[1] if row_col else None,
                    "cell_text": html_to_text(cell.get("html")),
                },
                render=False,  # cell content already visible via the table anchor
            )

    def render_figure(node: dict, caption_node: Optional[dict]):
        anchor = next_anchor()
        label = node.get("block_type") or "Figure"
        caption_text = html_to_text(caption_node.get("html")) if caption_node else ""
        has_image = bool(node.get("images"))
        content_lines.append(
            f"*[{label} — image data captured separately; not rendered as text. "
            f"Caption: {caption_text or '(none)'}]*"
        )
        content_lines.append(f"⟦{anchor}⟧")
        content_lines.append("")
        record_provenance(anchor, node, extra={
            "caption_text": caption_text,
            "has_embedded_image_bytes": has_image,
        })

    def render_simple_leaf(node: dict, prefix: str = ""):
        bt = node["block_type"]
        text = html_to_text(node.get("html"))
        if not text:
            return
        _, repaired = _fix_marker_llm_sup_corruption(node.get("html") or "")
        if repaired:
            stats["footnote_corruption_repairs"] += 1
        if _detect_single_escape_sup_sub(node.get("html") or ""):
            stats["single_escape_repairs"] += 1

        anchor = next_anchor()
        if bt == "SectionHeader":
            level = _heading_level(node)
            content_lines.append(f"{'#' * level} {text}")
        elif bt == "Equation":
            content_lines.append(f"> {text}")
        elif bt == "Footnote":
            content_lines.append(f"[^footnote] {text}")
        elif bt == "ListItem":
            content_lines.append(f"- {text}")
        elif bt == "Caption":
            content_lines.append(f"*{text}*")
        else:
            content_lines.append(text)
        content_lines.append(f"⟦{anchor}⟧")
        content_lines.append("")

        # `repaired` is a literal boolean on EVERY citable-leaf provenance entry
        # (not just Footnotes) so a reviewer or the QC gate can filter on one
        # consistent field name regardless of which cleanup step fired. Checked
        # generically against the raw html of every leaf node -- not gated on
        # block_type or any paper-1-specific text -- against BOTH known
        # --use_llm corruption signatures:
        #   severity 1: Footnote double-escape (_fix_marker_llm_sup_corruption)
        #   severity 2: plain single-escape sup/sub, seen inline in body Text
        #               (_detect_single_escape_sup_sub) -- already corrected by
        #               html_to_text's unescape-before-strip ordering, but
        #               still flagged here so it surfaces the same way.
        raw_html = node.get("html") or ""
        single_escape_hit = _detect_single_escape_sup_sub(raw_html)
        was_repaired = bool(repaired) or single_escape_hit
        extra = {"repaired": was_repaired}
        if repaired:
            extra["cleanup_applied"] = "use_llm_sup_double_escape_repair"
            extra["cleanup_confidence_note"] = (
                "Automated repair of a known --use_llm double-escaping bug; "
                "spot-check against source PDF, do not trust blindly."
            )
        elif single_escape_hit:
            extra["cleanup_applied"] = "use_llm_sup_single_escape_repair"
            extra["cleanup_confidence_note"] = (
                "Automated repair of a known --use_llm single-escaping bug "
                "(sup/sub mixed inline with real <math> tags); spot-check "
                "against source PDF, do not trust blindly."
            )
        record_provenance(anchor, node, extra=extra)

    def walk(node: dict):
        bt = node.get("block_type")

        if bt in STRUCTURAL_CONTAINER_TYPES:
            children = node.get("children") or []
            if bt in ("TableGroup", "FigureGroup", "PictureGroup", "ListGroup"):
                ordered = _resolve_content_refs(node.get("html") or "", children)
                if ordered is children and _CONTENT_REF_RE.findall(node.get("html") or ""):
                    # ref parsing fell back to raw order despite refs existing
                    stats["content_ref_order_mismatches"] += 1
                children = ordered

            if bt == "TableGroup":
                caption = next((c for c in children if c["block_type"] == "Caption"), None)
                table = next((c for c in children if c["block_type"] == "Table"), None)
                if caption:
                    render_simple_leaf(caption)
                if table:
                    render_table(table)
                # anything else unexpected in a TableGroup: walk normally
                for c in children:
                    if c is not caption and c is not table:
                        walk(c)
            elif bt == "FigureGroup":
                caption = next((c for c in children if c["block_type"] == "Caption"), None)
                figure = next((c for c in children if c["block_type"] == "Figure"), None)
                if figure:
                    render_figure(figure, caption)
                    # caption already folded into the figure's provenance/text;
                    # still record it as its own citable block per Section 4's
                    # leaf-type list, but don't duplicate it in content.md.
                    if caption:
                        cap_anchor = next_anchor()
                        record_provenance(cap_anchor, caption, extra={
                            "note": "duplicate of figure's inline caption_text; not separately rendered"
                        }, render=False)
                for c in children:
                    if c is not caption and c is not figure:
                        walk(c)
            elif bt == "PictureGroup":
                # A Picture with its Caption: rendered like a FigureGroup.
                caption = next((c for c in children if c["block_type"] == "Caption"), None)
                picture = next((c for c in children if c["block_type"] == "Picture"), None)
                if picture:
                    render_figure(picture, caption)
                    if caption:
                        cap_anchor = next_anchor()
                        record_provenance(cap_anchor, caption, extra={
                            "note": "duplicate of figure's inline caption_text; not separately rendered"
                        }, render=False)
                for c in children:
                    if c is not caption and c is not picture:
                        walk(c)
            else:  # ListGroup and any other plain container
                for c in children:
                    walk(c)
            return

        if bt in PROVENANCE_ONLY_SILENT_TYPES:
            anchor = next_anchor()
            record_provenance(anchor, node, render=False)
            return

        if bt == "Picture":
            # Standalone image block -- unlike Figure, not reliably wrapped in
            # a group with a paired Caption, so never look for one: render the
            # placeholder + embedded image bytes regardless (see
            # CITABLE_LEAF_TYPES comment above).
            render_figure(node, caption_node=None)
            return

        if bt in CITABLE_LEAF_TYPES:
            render_simple_leaf(node)
            return

        if bt in PROVENANCE_ONLY_TYPES:
            # TableCells encountered outside of the render_table() path (should
            # not normally happen since Table handles its own children) --
            # log defensively rather than silently drop.
            anchor = next_anchor()
            record_provenance(anchor, node, render=False)
            return

        # Unknown block type: logged, never dropped. render=False because nothing is written to content.md.
        anchor = next_anchor()
        record_provenance(anchor, node, extra={"unhandled_block_type": True}, render=False)

    walk(doc)

    content_md = "\n".join(content_lines).strip() + "\n"
    return WalkResult(content_md=content_md, provenance=provenance, stats=stats)


def _page_id_from_block_id(block_id: str) -> Optional[str]:
    m = re.match(r"^/page/(\d+)/", block_id)
    return f"page_{m.group(1)}" if m else None


def _heading_level(node: dict) -> int:
    """Derive a markdown heading level (1-6) from the block's own html tag
    (<h1>..<h6>) when present; fall back to the deepest level number in its
    own section_hierarchy (i.e. treat this header as one level below whatever
    it nests under)."""
    html_field = node.get("html") or ""
    m = re.match(r"<h([1-6])>", html_field)
    if m:
        return int(m.group(1))
    sh = node.get("section_hierarchy") or {}
    if sh:
        return min(6, max(int(k) for k in sh.keys()) + 1)
    return 2


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def process_paper(marker_json_path: str, out_dir: str) -> WalkResult:
    import os
    with open(marker_json_path) as f:
        doc = json.load(f)
    result = walk_document(doc)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "content.md"), "w") as f:
        f.write(result.content_md)
    with open(os.path.join(out_dir, "provenance.json"), "w") as f:
        json.dump(result.provenance, f, indent=2)
    with open(os.path.join(out_dir, "_walk_stats.json"), "w") as f:
        json.dump(result.stats, f, indent=2)
    return result


if __name__ == "__main__":
    import sys
    marker_json_path, out_dir = sys.argv[1], sys.argv[2]
    r = process_paper(marker_json_path, out_dir)
    print("Wrote:", out_dir)
    print("Stats:", json.dumps(r.stats, indent=2))