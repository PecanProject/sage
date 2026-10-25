"""The parsed paper. Every anchor resolves to a page and a box, which is what evidence boxes are drawn from.

Anchors: `p{page}.b{n}` is a text block, `p{page}.t{n}` a table, `.r{i}` a row and `.c{j}` a cell. Pages, blocks
and tables count from 1; rows and columns count from 0, so header rows come first.
"""

import re
from collections.abc import Sequence
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
    notes: str | None = Field(default=None, description="The table's footnotes, from its CSV's `# notes:` lines.")
    notes_anchors: list[str] = Field(
        default_factory=list,
        description="The text blocks under the table that hold those footnotes, so they can be cited as evidence.",
    )
    rows: list[list[Cell]] = Field(default_factory=list)
    header_rows: int = 1
    status: TableStatus = "pending"
    grounded_share: float = 0.0

    @property
    def bbox(self) -> Box:
        boxes = [c.bbox for row in self.rows for c in row if c.bbox is not None]
        return union(boxes) if boxes else self.region


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
    _index: dict[str, Resolved] | None = PrivateAttr(default=None)

    def table(self, anchor: str) -> Table | None:
        return next((t for t in self.tables if t.anchor == anchor), None)

    def resolve(self, anchor: str) -> Resolved | None:
        if self._index is None:
            self._index = _build_index(self)
        return self._index.get(anchor)

    def replace(self, **update: object) -> "Document":
        """A new Document with `update` applied. Not `model_copy`, which would carry over an anchor index built from the
        old blocks and tables."""
        return Document.model_validate({**dict(self), **update})


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


def rounded(b: Sequence[float]) -> Box:
    """A box to the hundredth of a point, as documents store them."""
    return (round(b[0], 2), round(b[1], 2), round(b[2], 2), round(b[3], 2))


def union(boxes: list[Box]) -> Box:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )
