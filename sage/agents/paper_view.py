"""The paper as the agent reads it: anchored text, with tables shown as their grounded grids."""

import re

from sage.parse.document import Block, Document, Table

DOI = re.compile(r"\bdoi\b|10\.\d{4,9}/", re.IGNORECASE)


def full_view(doc: Document, *, title: str | None) -> str:
    return _header(doc, title) + "\n".join(_lines(doc))


def _header(doc: Document, title: str | None) -> str:
    return (
        f"# {title or doc.paper_id}\n"
        f"{len(doc.pages)} pages, {len(doc.tables)} tables. "
        "Anchors: [p3.b4] text block; [p7.t1] table, cell p7.t1.r2.c1.\n\n"
    )


def _lines(doc: Document) -> list[str]:
    captions = {t.caption_anchor: t for t in doc.tables}
    out: list[str] = []
    page = 0
    for b in doc.blocks:
        if not _shown(b):
            continue
        if b.page != page:
            page = b.page
            out.append(f"\n--- page {page} ---")
        table = captions.get(b.anchor)
        if table is not None:
            out += _table_lines(table)
        else:
            prefix = "(page header) " if b.kind == "furniture" else ""
            out.append(f"[{b.anchor}] {prefix}{b.text}")
    return out


def _shown(b: Block) -> bool:
    if b.kind in ("text", "caption"):
        return True
    return b.kind == "furniture" and (b.page == 1 or DOI.search(b.text) is not None) and len(b.text) > 3


def _table_lines(t: Table) -> list[str]:
    head = f"[{t.anchor}] {t.label} ({_status(t)}). Caption [{t.caption_anchor}]: {t.caption}"
    if not t.rows:
        return [head, f"  not transcribed: its text blocks follow; call view({t.page}, {list(t.region)}) to see it"]
    width = max(len(r) for r in t.rows)
    lines = [head, f"  {t.header_rows} header row(s), {len(t.rows) - t.header_rows} data rows, {width} columns"]
    for i, row in enumerate(t.rows):
        if i == 0:
            cells = " | ".join(f"[c{j}] {c.text}" for j, c in enumerate(row))
        else:
            cells = " | ".join(c.text for c in row)
        lines.append(f"  r{i}: {cells}")
    if t.notes is not None and t.notes:
        lines.append(f"  notes (from the transcription): {t.notes}")
    return lines


def _status(t: Table) -> str:
    match t.status:
        case "grounded":
            return "every cell matched to the PDF text"
        case "partial":
            return f"{round(t.grounded_share * 100)}% of cells matched to the PDF text; check the rest with view"
        case "failed":
            return "transcription did not match the PDF text; use view"
        case _:
            return "not transcribed"
