from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

DEFAULT_PAPERS_ROOT = Path(os.environ.get("IR_PAPERS_ROOT", "paper"))

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_ANCHOR_RE = re.compile(r"⟦(b:\d+)⟧")
_ANCHOR_NUM_RE = re.compile(r"b:(\d+)$")


def _content_md_path(paper_id: str, papers_root: Path = DEFAULT_PAPERS_ROOT) -> Path:
    return papers_root / paper_id / "content.md"


def _provenance_path(paper_id: str, papers_root: Path = DEFAULT_PAPERS_ROOT) -> Path:
    return papers_root / paper_id / "provenance.json"


def _load_provenance(paper_id: str, papers_root: Path = DEFAULT_PAPERS_ROOT) -> Optional[dict]:
    path = _provenance_path(paper_id, papers_root)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _anchor_sort_key(anchor: str) -> int:
    m = _ANCHOR_NUM_RE.search(anchor)
    return int(m.group(1)) if m else 0


def _normalize_anchor(anchor: str) -> str:
    normalized = anchor.strip("⟦⟧").strip()
    if not normalized.startswith("b:"):
        normalized = f"b:{normalized}"
    return normalized


def _rendered_block_texts(paper_id: str, papers_root: Path = DEFAULT_PAPERS_ROOT) -> dict[str, str]:
    content = _content_md_path(paper_id, papers_root).read_text(encoding="utf-8")
    blocks: dict[str, str] = {}
    prev_end = 0
    for match in _ANCHOR_RE.finditer(content):
        blocks[match.group(1)] = content[prev_end:match.start()].strip()
        prev_end = match.end()
    return blocks


def read_section(paper_id: str, section_name: str, papers_root: Path = DEFAULT_PAPERS_ROOT) -> dict:
    path = _content_md_path(paper_id, papers_root)
    if not path.exists():
        return {"found": False, "error": f"no content.md for paper_id '{paper_id}'"}

    lines = path.read_text().splitlines()
    start_idx = None
    start_level = None
    target = section_name.strip().lower()
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m and target in m.group(2).strip().lower():
            start_idx = i
            start_level = len(m.group(1))
            break

    if start_idx is None:
        return {"found": False, "error": f"no section heading matching '{section_name}'"}

    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        m = _HEADING_RE.match(lines[j])
        if m and len(m.group(1)) <= start_level:
            end_idx = j
            break

    section_lines = lines[start_idx:end_idx]
    text = "\n".join(section_lines).strip()
    anchors = _ANCHOR_RE.findall(text)
    return {
        "found": True,
        "paper_id": paper_id,
        "section_name": section_name,
        "text": text,
        "block_anchors": anchors,
    }

def read_document_start(
    paper_id: str,
    max_lines: int = 80,
    papers_root: Path = DEFAULT_PAPERS_ROOT,
) -> dict:
    path = _content_md_path(paper_id, papers_root)
    if not path.exists():
        return {"found": False, "error": f"no content.md for paper_id '{paper_id}'"}

    lines = path.read_text().splitlines()
    text = "\n".join(lines[:max_lines]).strip()
    anchors = _ANCHOR_RE.findall(text)

    return {
        "found": True,
        "paper_id": paper_id,
        "text": text,
        "block_anchors": anchors,
        "lines_returned": min(max_lines, len(lines)),
    }

def read_table(paper_id: str, table_id: str, papers_root: Path = DEFAULT_PAPERS_ROOT) -> dict:
    path = _content_md_path(paper_id, papers_root)
    if not path.exists():
        return {"found": False, "error": f"no content.md for paper_id '{paper_id}'"}

    normalized = table_id.strip("⟦⟧").strip()
    if not normalized.startswith("b:"):
        normalized = f"b:{normalized}"

    lines = path.read_text().splitlines()
    anchor_line_idx = None
    for i, line in enumerate(lines):
        if line.strip() == f"⟦{normalized}⟧":
            anchor_line_idx = i
            break

    if anchor_line_idx is None:
        return {"found": False, "error": f"no block anchor '{normalized}' found in content.md"}

    start = anchor_line_idx - 1
    while start >= 0 and lines[start].strip().startswith("|"):
        start -= 1
    start += 1
    table_lines = lines[start:anchor_line_idx]

    if not table_lines:
        return {"found": False, "error": f"anchor '{normalized}' found but no preceding markdown table detected"}

    return {
        "found": True,
        "paper_id": paper_id,
        "table_id": normalized,
        "markdown_table": "\n".join(table_lines),
    }


def read_table_full(paper_id: str, table_anchor: str, papers_root: Path = DEFAULT_PAPERS_ROOT) -> dict:
    rendered = read_table(paper_id, table_anchor, papers_root)
    if not rendered.get("found"):
        return rendered

    provenance = _load_provenance(paper_id, papers_root)
    if provenance is None:
        return {"found": False, "error": f"no provenance.json for paper_id '{paper_id}'"}

    normalized = rendered["table_id"]
    by_row: dict[int, list[tuple]] = {}
    for entry in provenance.values():
        if entry.get("parent_table_anchor") != normalized:
            continue
        row_idx = entry.get("row_index")
        col_idx = entry.get("col_index") if entry.get("col_index") is not None else -1
        if row_idx is None:
            continue
        by_row.setdefault(row_idx, []).append((col_idx, entry.get("cell_text", "")))

    rows = [
        [text for _, text in sorted(by_row[r])]
        for r in sorted(by_row)
    ]
    return {
        "found": True,
        "paper_id": paper_id,
        "table_anchor": normalized,
        "markdown_table": rendered["markdown_table"],
        "rows": rows,
    }


def list_tables(paper_id: str, papers_root: Path = DEFAULT_PAPERS_ROOT) -> dict:
    provenance = _load_provenance(paper_id, papers_root)
    if provenance is None:
        return {"found": False, "error": f"no provenance.json for paper_id '{paper_id}'"}

    ordered_anchors = sorted(
        (a for a, e in provenance.items() if e.get("block_type") == "Table"),
        key=_anchor_sort_key,
    )
    tables = [
        {
            "table_anchor": anchor,
            "page": provenance[anchor].get("page_id"),
            "section_path": provenance[anchor].get("section_path"),
        }
        for anchor in ordered_anchors
    ]
    if not tables:
        return {"found": False, "error": f"no Table blocks found for paper_id '{paper_id}'"}
    return {"found": True, "paper_id": paper_id, "tables": tables}


def raw_table_cells(paper_id: str, table_anchors: list[str], papers_root: Path = DEFAULT_PAPERS_ROOT) -> list[str]:
    provenance = _load_provenance(paper_id, papers_root)
    if provenance is None:
        return []
    wanted = set(table_anchors)
    return [
        text for e in provenance.values()
        if e.get("parent_table_anchor") in wanted and (text := (e.get("cell_text") or "").strip())
    ]


def list_sections(paper_id: str, papers_root: Path = DEFAULT_PAPERS_ROOT) -> dict:
    provenance = _load_provenance(paper_id, papers_root)
    if provenance is None:
        return {"found": False, "error": f"no provenance.json for paper_id '{paper_id}'"}

    ordered_anchors = sorted(provenance.keys(), key=_anchor_sort_key)
    sections: list[dict] = []
    seen_paths: set[tuple] = set()
    for anchor in ordered_anchors:
        entry = provenance[anchor]
        path = tuple(entry.get("section_path") or [])
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        sections.append({"section_path": list(path), "first_anchor": anchor})

    if not sections:
        return {"found": False, "error": f"no section_path metadata found for paper_id '{paper_id}'"}
    return {"found": True, "paper_id": paper_id, "sections": sections}


def read_section_by_path(
    paper_id: str, section_path: list[str], papers_root: Path = DEFAULT_PAPERS_ROOT
) -> dict:
    provenance = _load_provenance(paper_id, papers_root)
    if provenance is None:
        return {"found": False, "error": f"no provenance.json for paper_id '{paper_id}'"}
    if not _content_md_path(paper_id, papers_root).is_file():
        return {"found": False, "error": f"no content.md for paper_id '{paper_id}'"}

    target = [s.strip().lower() for s in section_path if s.strip()]
    if not target:
        return {"found": False, "error": "section_path must have at least one non-empty segment"}

    rendered_texts = _rendered_block_texts(paper_id, papers_root)
    ordered_anchors = sorted(provenance.keys(), key=_anchor_sort_key)

    blocks: list[dict] = []
    for anchor in ordered_anchors:
        entry = provenance[anchor]
        if not entry.get("rendered_in_content_md"):
            continue
        path = [s.strip().lower() for s in (entry.get("section_path") or [])]
        if path[: len(target)] != target:
            continue
        text = rendered_texts.get(anchor, "")
        if not text:
            continue
        blocks.append({
            "block_anchor": anchor,
            "text": text,
            "page": entry.get("page_id"),
            "block_type": entry.get("block_type"),
            "section_path": entry.get("section_path"),
        })

    if not blocks:
        return {"found": False, "error": f"no rendered blocks found under section_path {section_path!r}"}
    return {"found": True, "paper_id": paper_id, "section_path": section_path, "blocks": blocks}


def read_table_row(
    paper_id: str, table_anchor: str, row_index: int, papers_root: Path = DEFAULT_PAPERS_ROOT
) -> dict:
    provenance = _load_provenance(paper_id, papers_root)
    if provenance is None:
        return {"found": False, "error": f"no provenance.json for paper_id '{paper_id}'"}

    normalized = _normalize_anchor(table_anchor)
    if normalized not in provenance:
        return {"found": False, "error": f"no block anchor '{normalized}' found in provenance.json"}

    row_cells = [
        entry for entry in provenance.values()
        if entry.get("parent_table_anchor") == normalized and entry.get("row_index") == row_index
    ]
    if not row_cells:
        return {"found": False, "error": f"no row {row_index} found for table anchor '{normalized}'"}
    row_cells.sort(key=lambda e: e.get("col_index") if e.get("col_index") is not None else -1)

    table_entry = provenance[normalized]
    return {
        "found": True,
        "paper_id": paper_id,
        "table_anchor": normalized,
        "row_index": row_index,
        "cells": [e.get("cell_text", "") for e in row_cells],
        "block_type": "TableRow",
        "page": table_entry.get("page_id"),
        "section_path": table_entry.get("section_path"),
    }


def read_table_cell(
    paper_id: str, table_anchor: str, row_index: int, col_index: int,
    papers_root: Path = DEFAULT_PAPERS_ROOT,
) -> dict:
    row = read_table_row(paper_id, table_anchor, row_index, papers_root)
    if not row.get("found"):
        return row

    cells = row["cells"]
    if col_index < 0 or col_index >= len(cells):
        return {
            "found": False,
            "error": f"row {row_index} of table '{row['table_anchor']}' has no column {col_index} "
                     f"(row has {len(cells)} column(s))",
        }
    return {
        "found": True,
        "paper_id": paper_id,
        "table_anchor": row["table_anchor"],
        "row_index": row_index,
        "col_index": col_index,
        "cell_text": cells[col_index],
        "block_type": "TableCell",
        "page": row["page"],
        "section_path": row["section_path"],
    }


def read_nearby(
    paper_id: str, anchor: str, before: int = 1, after: int = 1,
    papers_root: Path = DEFAULT_PAPERS_ROOT,
) -> dict:
    provenance = _load_provenance(paper_id, papers_root)
    if provenance is None:
        return {"found": False, "error": f"no provenance.json for paper_id '{paper_id}'"}
    if not _content_md_path(paper_id, papers_root).is_file():
        return {"found": False, "error": f"no content.md for paper_id '{paper_id}'"}

    normalized = _normalize_anchor(anchor)
    if normalized not in provenance:
        return {"found": False, "error": f"no block anchor '{normalized}' found in provenance.json"}

    rendered_anchors = sorted(
        (a for a, e in provenance.items() if e.get("rendered_in_content_md")),
        key=_anchor_sort_key,
    )
    if normalized not in rendered_anchors:
        return {"found": False, "error": f"anchor '{normalized}' is not a rendered content.md block"}

    idx = rendered_anchors.index(normalized)
    window = rendered_anchors[max(0, idx - before): idx + 1 + after]
    rendered_texts = _rendered_block_texts(paper_id, papers_root)

    blocks = []
    for a in window:
        entry = provenance[a]
        blocks.append({
            "block_anchor": a,
            "text": rendered_texts.get(a, ""),
            "page": entry.get("page_id"),
            "block_type": entry.get("block_type"),
            "section_path": entry.get("section_path"),
            "is_target": a == normalized,
        })
    return {"found": True, "paper_id": paper_id, "anchor": normalized, "blocks": blocks}
