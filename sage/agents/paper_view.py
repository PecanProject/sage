"""The paper as the agent reads it, `paper.md` in the paper's folder: anchored text, with tables shown as their
grounded grids and the files to write for what the text layer lacks."""

import re

from sage.parse.document import Block, Document, Table

DOI = re.compile(r"\bdoi\b|10\.\d{4,9}/", re.IGNORECASE)


def full_view(doc: Document, *, title: str | None) -> str:
    header = (
        f"# {title or doc.paper_id}\n"
        f"{len(doc.pages)} pages, {len(doc.tables)} tables. "
        "Anchors: [p3.b4] text block; [p7.t1] table, cell p7.t1.r2.c1. "
        "Images: pages/p3.png, tables/p7.t1.png, figures/.\n\n"
    )
    return header + "\n".join(_lines(doc))


def _lines(doc: Document) -> list[str]:
    captions = {t.caption_anchor: t for t in doc.tables if t.continues is None}
    continued = {t.page: t for t in doc.tables if t.continues is not None}
    by_page: dict[int, list[Block]] = {}
    for b in doc.blocks:
        if _shown(b):
            by_page.setdefault(b.page, []).append(b)
    out: list[str] = []
    for page in doc.pages:
        blocks = by_page.get(page.number, [])
        if not page.has_text and not blocks:
            out.append(f"\n--- page {page.number} (no text layer) ---")
            out.append(
                f"  read pages/p{page.number}.png and transcribe what you cite into pages/p{page.number}.md "
                f"as [p{page.number}.b1] …"
            )
            continue
        if blocks or page.number in continued:
            out.append(f"\n--- page {page.number} ---")
        if page.number in continued:
            out += _table_lines(continued[page.number])
        for b in blocks:
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
        todo = f"  not transcribed: read tables/{t.anchor}.png and write tables/{t.anchor}.csv"
        if t.continues is not None:
            todo += (
                f", with only the rows this page prints (# header_rows: 0); its columns are {t.continues}'s, whose "
                "header its values cite"
            )
        return [head, todo]
    width = max(len(r) for r in t.rows)
    lines = [head, f"  {t.header_rows} header row(s), {len(t.rows) - t.header_rows} data rows, {width} columns"]
    for i, row in enumerate(t.rows):
        if i == 0:
            cells = " | ".join(f"[c{j}] {c.text}" for j, c in enumerate(row))
        else:
            cells = " | ".join(c.text for c in row)
        lines.append(f"  r{i}: {cells}")
    if t.notes is not None and t.notes:
        lines.append(f"  notes (from tables/{t.anchor}.csv): {t.notes}")
    return lines


def _status(t: Table) -> str:
    """How much of the grid the PDF text layer backs, so the agent knows how far to trust it."""
    cells = [c for row in t.rows for c in row if c.text.strip()]
    if t.status == "pending" or not cells:
        return "not transcribed"
    exact = sum(c.match == "exact" for c in cells)
    fuzzy = sum(c.match == "fuzzy" for c in cells)
    counts = (
        f"{exact} of {len(cells)} cells match the PDF text exactly, {fuzzy} approximately, "
        f"{len(cells) - exact - fuzzy} not at all"
    )
    match t.status:
        case "grounded":
            return f"grounded: {counts}" + _unmatched(t)
        case "partial":
            return f"partly grounded: {counts}; check the unmatched cells against tables/{t.anchor}.png"
        case _:
            return f"not grounded: {counts}; check tables/{t.anchor}.csv against tables/{t.anchor}.png"


def _unmatched(t: Table) -> str:
    """The cells the PDF text doesn't hold, by name: a header among them flags every value under it."""
    missed = [(i, c) for i, row in enumerate(t.rows) for c in row if c.text.strip() and c.match != "exact"]
    if not missed:
        return ""
    shown = ", ".join(f"{c.anchor.removeprefix(t.anchor + '.')} '{c.transcribed.strip()}'" for _, c in missed[:6])
    more = f" and {len(missed) - 6} more" if len(missed) > 6 else ""
    header = any(i < t.header_rows for i, _ in missed)
    why = (
        "; copy each header row as printed: a header the PDF text doesn't hold flags every value under it"
        if header
        else ""
    )
    return f"; not matched exactly, so the values they give are flagged: {shown}{more}{why}"
