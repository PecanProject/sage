"""The parsed paper. Every anchor resolves to a page and a box, which is what evidence boxes are drawn from.

Anchors: `p{page}.b{n}` is a text block, `p{page}.t{n}` a table, `.r{i}` a row and `.c{j}` a cell. Pages, blocks
and tables count from 1; rows and columns count from 0, so header rows come first.
"""

import re
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr

PARSER_VERSION = "pdftext-0.7.1+sage.6"

Box = tuple[float, float, float, float]
"""x0, y0, x1, y1 in PDF points, origin at the top left of the page."""

BlockKind = Literal["text", "caption", "table", "furniture", "references"]


class Page(BaseModel):
    number: int
    width: float
    height: float
    has_text: bool


class Block(BaseModel):
    anchor: str
    page: int
    bbox: Box
    text: str
    kind: BlockKind = "text"
    size: float = Field(default=0.0, description="Median line height in points, a stand-in for the font size.")
    rotation: int = Field(default=0, description="Text direction in degrees clockwise; 270 reads bottom to top.")


CellMatch = Literal["exact", "fuzzy", "none"]


class Cell(BaseModel):
    anchor: str
    text: str = Field(description="The text layer's own text when the cell grounded, else the transcription.")
    transcribed: str
    bbox: Box | None = None
    match: CellMatch = "none"


TableStatus = Literal["pending", "grounded", "partial", "failed"]


class Table(BaseModel):
    anchor: str
    page: int
    label: str
    caption_anchor: str
    caption: str
    region: Box
    rotation: int = 0
    notes: str | None = Field(default=None, description="The table's footnotes as the table pass transcribed them.")
    notes_anchors: list[str] = Field(
        default_factory=list,
        description="The text blocks under the table that hold those footnotes, so they can be cited as evidence.",
    )
    rows: list[list[Cell]] = Field(default_factory=list)
    header_rows: int = 1
    status: TableStatus = "pending"
    grounded_share: float = 0.0
    grounder_version: str | None = Field(
        default=None,
        description="The grounding code that matched the cells. A table grounded by older code is grounded again from "
        "its stored transcription, with no new table pass.",
    )

    @property
    def bbox(self) -> Box:
        boxes = [c.bbox for row in self.rows for c in row if c.bbox is not None]
        return union(boxes) if boxes else self.region


class TablePass(BaseModel):
    model: str
    cost_usd: float = 0.0


class Resolved(BaseModel):
    anchor: str
    page: int
    bbox: Box
    text: str


class Document(BaseModel):
    paper_id: str
    sha256: str
    parser_version: str = PARSER_VERSION
    pages: list[Page]
    blocks: list[Block]
    tables: list[Table] = Field(default_factory=list)
    table_pass: TablePass | None = None
    _index: dict[str, Resolved] | None = PrivateAttr(default=None)

    def table(self, anchor: str) -> Table | None:
        return next((t for t in self.tables if t.anchor == anchor), None)

    def resolve(self, anchor: str) -> Resolved | None:
        if self._index is None:
            self._index = _build_index(self)
        return self._index.get(anchor)

    def with_tables(self, tables: list[Table], table_pass: TablePass | None) -> "Document":
        """A new Document rather than `model_copy`, which would carry over an anchor index built from the old tables."""
        return Document(
            paper_id=self.paper_id,
            sha256=self.sha256,
            parser_version=self.parser_version,
            pages=self.pages,
            blocks=self.blocks,
            tables=tables,
            table_pass=table_pass,
        )


def carry_tables(old: Document, new: Document) -> Document:
    """The new parse with each table's transcription from the old one, so a reparse makes no table call and every cell
    keeps its anchor. Clearing `grounder_version` grounds a carried table again on the new text layer."""
    before = {t.anchor: t for t in old.tables}
    tables: list[Table] = []
    for t in new.tables:
        prev = before.get(t.anchor)
        if prev is None or prev.status == "pending" or prev.label != t.label or _overlap(prev.region, t.region) < 0.5:
            tables.append(t)
            continue
        transcribed = {"rows": prev.rows, "header_rows": prev.header_rows, "notes": prev.notes}
        result = {"status": prev.status, "grounded_share": prev.grounded_share, "grounder_version": None}
        tables.append(t.model_copy(update=transcribed | result))
    return new.with_tables(tables, old.table_pass)


def _overlap(a: Box, b: Box) -> float:
    """The share of the smaller box that the two have in common."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return max(0.0, w) * max(0.0, h) / smaller if smaller > 0 else 0.0


ANCHOR = re.compile(r"^p(\d+)\.(?:b\d+|t\d+(?:\.r\d+(?:\.c\d+)?)?)$")


def _build_index(doc: Document) -> dict[str, Resolved]:
    index = {b.anchor: Resolved(anchor=b.anchor, page=b.page, bbox=b.bbox, text=b.text) for b in doc.blocks}
    for t in doc.tables:
        lines = [" | ".join(c.text for c in row) for row in t.rows]
        index[t.anchor] = Resolved(anchor=t.anchor, page=t.page, bbox=t.bbox, text="\n".join(lines) or t.caption)
        for i, (row, line) in enumerate(zip(t.rows, lines, strict=True)):
            boxes = [c.bbox for c in row if c.bbox is not None]
            anchor = f"{t.anchor}.r{i}"
            index[anchor] = Resolved(anchor=anchor, page=t.page, bbox=union(boxes) if boxes else t.region, text=line)
            for c in row:
                index[c.anchor] = Resolved(anchor=c.anchor, page=t.page, bbox=c.bbox or t.region, text=c.text)
    return index


def union(boxes: list[Box]) -> Box:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )
