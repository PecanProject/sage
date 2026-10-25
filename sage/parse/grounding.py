"""Ground a model-transcribed table in the text layer: every cell string is matched to characters inside the table
region, in reading order, and the matched characters give the cell its box and its verbatim text.

A value cell grounds on its own characters, once. A header or a row label may also be composed from page text that
governs it: a spanning header printed once over several columns, or a stub label printed once beside several rows,
which the CSV repeats in every cell it covers."""

import re
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from itertools import pairwise
from statistics import median

from sage.domain.normalize import norm
from sage.parse.document import Block, Box, Cell, CellMatch, Document, Table, TableStatus, union
from sage.parse.layout import center_in, from_upright, same_line, spaced, to_upright
from sage.parse.pdf import Char

GROUNDER_VERSION = "3"
"""Bump when grounding changes what a cell matches, so cached tables are grounded again from their transcription."""
OUT_OF_ORDER = 40.0
"""Points of geometric distance a candidate before the reading cursor must beat to win anyway."""
LABEL = re.compile(r"(?=[^\W\d_]).*[^\W\d_]{3}")
"""A body cell that starts with a letter and holds a word of three letters is a label, not a value ("Experiment 1",
"Location (L)"); "ns", "ab" and "12.8 a" are values."""


@dataclass(frozen=True)
class Grid:
    rows: list[list[str]]
    header_rows: int
    notes: str | None


@dataclass
class _Stream:
    text: str
    owner: list[int]
    """Index into `chars` for each character of `text`."""
    starts: list[bool]
    """True where a word starts: the token boundary test for a match."""


def ground(
    table: Table,
    grid: Grid,
    chars: list[Char],
    *,
    caption: Box | None,
    page_size: tuple[float, float],
    fuzzy_threshold: float,
    min_share: float,
) -> Table:
    """Rows and columns are matched in the table's upright frame, so a landscape table grounds like any other.
    `caption` is the caption block's box: the region holds it, but its words are not the table's."""
    w, h = page_size
    rot = table.rotation
    if rot == 0:
        return _ground(table, grid, chars, caption=caption, fuzzy_threshold=fuzzy_threshold, min_share=min_share)
    upright = [replace(c, bbox=to_upright(c.bbox, rot, w, h)) for c in chars]
    grounded = _ground(
        table.model_copy(update={"region": to_upright(table.region, rot, w, h)}),
        grid,
        upright,
        caption=to_upright(caption, rot, w, h) if caption is not None else None,
        fuzzy_threshold=fuzzy_threshold,
        min_share=min_share,
    )
    rows = [
        [c.model_copy(update={"bbox": from_upright(c.bbox, rot, w, h) if c.bbox is not None else None}) for c in row]
        for row in grounded.rows
    ]
    return grounded.model_copy(update={"rows": rows, "region": table.region})


def _ground(
    table: Table, grid: Grid, chars: list[Char], *, caption: Box | None, fuzzy_threshold: float, min_share: float
) -> Table:
    inside = [c for c in chars if center_in(c.bbox, table.region) and not _in_caption(c.bbox, caption)]
    stream = _stream(inside)
    used = [False] * len(stream.text)
    # The characters a value cell grounded on, which no header or label may be composed from.
    reserved = [False] * len(stream.text)
    rows: list[list[Cell]] = []
    row_y: dict[int, list[float]] = {}
    col_x: dict[int, list[float]] = {}
    cursor = 0
    for i, row in enumerate(grid.rows):
        cells: list[Cell] = []
        for j, raw in enumerate(row):
            anchor = f"{table.anchor}.r{i}.c{j}"
            key = _despace(raw)
            hit = _find(stream, used, key, cursor, row_y.get(i), col_x.get(j), inside, fuzzy_threshold) if key else None
            if hit is None:
                cells.append(Cell(anchor=anchor, text=raw.strip(), transcribed=raw))
                continue
            start, end, match = hit
            for k in range(start, end):
                used[k] = True
                reserved[k] = reserved[k] or not _label(raw, header=i < grid.header_rows)
            cursor = end
            picked = sorted({stream.owner[k] for k in range(start, end)})
            bbox = union([inside[k].bbox for k in picked])
            row_y.setdefault(i, []).append((bbox[1] + bbox[3]) / 2)
            col_x.setdefault(j, []).append((bbox[0] + bbox[2]) / 2)
            cells.append(Cell(anchor=anchor, text=_text(inside, picked), transcribed=raw, bbox=bbox, match=match))
        rows.append(cells)
    layout = _layout(rows, grid.header_rows, table.region)
    if layout is not None:
        governing = _Governing(stream=stream, chars=inside, reserved=reserved, layout=layout, rows=rows)
        rows = [[governing.cell(i, j, c, header=i < grid.header_rows) for j, c in enumerate(row)]
                for i, row in enumerate(rows)]  # fmt: skip
    filled = [c for row in rows for c in row if c.transcribed.strip()]
    share = sum(c.match != "none" for c in filled) / len(filled) if filled else 0.0
    status: TableStatus = "grounded" if share >= min_share else "partial" if share >= 0.5 else "failed"
    return table.model_copy(
        update={
            "rows": rows,
            "header_rows": grid.header_rows,
            "notes": grid.notes,
            "status": status,
            "grounded_share": round(share, 3),
        }
    )


def _in_caption(b: Box, caption: Box | None) -> bool:
    return caption is not None and center_in(b, caption)


def _label(raw: str, *, header: bool) -> bool:
    return header or LABEL.match(norm(raw)) is not None


def _text(chars: list[Char], picked: list[int]) -> str:
    out = ""
    for n, k in enumerate(picked):
        c = chars[k]
        if n and c.boundary:
            prev = chars[picked[n - 1]]
            if spaced(prev.bbox, prev.text, c.bbox, written=c.space):
                out += " "
        out += c.text
    return out


@dataclass(frozen=True)
class _Layout:
    """Where the grid sits on the page, from the cells that grounded exactly on their own text."""

    top: float
    """Top of the body: header text sits above it."""
    bands: dict[int, tuple[float, float]]
    """Each column's x extent over its body cells."""
    rows: dict[int, tuple[float, float]]
    """Each body row's y extent over its cells."""
    region: Box

    def over(self, j: int, box: Box) -> bool:
        """On the column, or centred within its gutters: from the right edge of the column to the left to the left
        edge of the column to the right. A gutter belongs to both neighbours, so text centred over it governs both."""
        band = self.bands[j]
        x = (box[0] + box[2]) / 2
        left = max((b[1] for c, b in self.bands.items() if c < j), default=self.region[0])
        right = min((b[0] for c, b in self.bands.items() if c > j), default=self.region[2])
        return (box[0] < band[1] and box[2] > band[0]) or left < x < right


def _layout(rows: list[list[Cell]], header_rows: int, region: Box) -> _Layout | None:
    """Median edges, so one cell grounded out of place does not stretch its column or its row."""
    body = [(i, j, c.bbox) for i, row in enumerate(rows) if i >= header_rows for j, c in enumerate(row)
            if c.match == "exact" and c.bbox is not None]  # fmt: skip
    if not body:
        return None
    cols: dict[int, list[Box]] = {}
    lines: dict[int, list[Box]] = {}
    for i, j, b in body:
        cols.setdefault(j, []).append(b)
        lines.setdefault(i, []).append(b)
    bands = {j: (median(b[0] for b in bs), median(b[2] for b in bs)) for j, bs in cols.items()}
    spans = {i: (median(b[1] for b in bs), median(b[3] for b in bs)) for i, bs in lines.items()}
    return _Layout(top=min(y0 for y0, _ in spans.values()), bands=bands, rows=spans, region=region)


_Piece = tuple[int, int]
"""A run of stream characters: one line or part of one."""


@dataclass(frozen=True)
class _Governing:
    """Composes a header or a row label from runs of page text printed where they govern the cell."""

    stream: _Stream
    chars: list[Char]
    reserved: list[bool]
    layout: _Layout
    rows: list[list[Cell]]

    def cell(self, i: int, j: int, cell: Cell, *, header: bool) -> Cell:
        eligible = cell.match != "exact" and j in self.layout.bands and (header or i in self.layout.rows)
        if not eligible or not _label(cell.transcribed, header=header):
            return cell
        pieces = self.compose(i, j, cell.transcribed, header=header)
        if pieces is None:
            return cell
        picked = [sorted({self.stream.owner[k] for k in range(*p)}) for p in pieces]
        text = " ".join(_text(self.chars, ks) for ks in picked)
        bbox = union([self.chars[k].bbox for ks in picked for k in ks])
        return cell.model_copy(update={"text": text, "bbox": bbox, "match": "exact"})

    def compose(self, i: int, j: int, raw: str, *, header: bool) -> list[_Piece] | None:
        """The cell's words as runs of page text, each the longest run printed where it governs the cell, top to
        bottom: over the column, above the body for a header, and at or above its row for a row label. None when a
        word is not there."""
        words = [w for w in map(_despace, norm(raw).split()) if w]
        if not words:
            return None
        pieces: list[_Piece] = []
        n = 0
        while n < len(words):
            prev = self.box(pieces[-1]) if pieces else None
            for size in range(len(words) - n, 0, -1):
                found = _occurrences(self.stream, "".join(words[n : n + size]))
                spans = [s for s in found if not self.taken(s) and self.one_line(s)]
                fits = [s for s in spans if self.fits(self.box(s), prev, i, j, header=header)]
                if fits:
                    pieces.append(min(fits, key=lambda s: self.distance(self.box(s), prev, i, j, header=header)))
                    n += size
                    break
            else:
                return None
        return pieces if header or self.governs(pieces, i, j) else None

    def fits(self, box: Box, prev: Box | None, i: int, j: int, *, header: bool) -> bool:
        y = (box[1] + box[3]) / 2
        if not self.layout.over(j, box) or (prev is not None and y < prev[1]):
            return False
        if header:
            return box[3] <= self.layout.top + 0.5
        return box[1] >= self.layout.top - 0.5 and y <= self.layout.rows[i][1]

    def distance(self, box: Box, prev: Box | None, i: int, j: int, *, header: bool) -> float:
        band = self.layout.bands[j]
        dx = abs((box[0] + box[2]) / 2 - (band[0] + band[1]) / 2)
        y = (box[1] + box[3]) / 2
        if prev is not None:
            return dx + abs(y - (prev[1] + prev[3]) / 2)
        return dx if header else dx + abs(sum(self.layout.rows[i]) / 2 - y)

    def governs(self, pieces: list[_Piece], i: int, j: int) -> bool:
        """A stub label printed above governs this row when the column prints nothing else on this row, and nothing
        on the rows in between but what their own cells say. A misread label never borrows a neighbour's."""
        mine = {self.stream.owner[k] for p in pieces for k in range(*p)}
        if any(k not in mine for k in self.printed(i, j)):
            return False
        above = [self.box(p)[3] for p in pieces if self.box(p)[3] < self.layout.rows[i][0]]
        if not above:
            return True
        for r, (a, b) in self.layout.rows.items():
            if max(above) < (a + b) / 2 < self.layout.rows[i][0]:
                own = "".join(_despace(self.chars[k].text) for k in self.printed(r, j))
                if own not in _despace(self.rows[r][j].transcribed):
                    return False
        return True

    def printed(self, i: int, j: int) -> list[int]:
        y0, y1 = self.layout.rows[i]
        return [k for k, c in enumerate(self.chars)
                if y0 <= (c.bbox[1] + c.bbox[3]) / 2 <= y1 and self.layout.over(j, c.bbox)]  # fmt: skip

    def taken(self, span: _Piece) -> bool:
        return any(self.reserved[span[0] : span[1]])

    def one_line(self, span: _Piece) -> bool:
        """Printed as one run: left to right on one line, no gap wider than the line is high. The stream's reading
        order can jump between headers, so a run of it is not always one."""
        ks = sorted({self.stream.owner[k] for k in range(*span)})
        for a, b in pairwise(ks):
            x, y = self.chars[a].bbox, self.chars[b].bbox
            ahead = x[0] <= y[0] and y[0] - x[2] <= max(x[3] - x[1], y[3] - y[1])
            if not (ahead and same_line(x, y)):
                return False
        return True

    def box(self, span: _Piece) -> Box:
        return union([self.chars[self.stream.owner[k]].bbox for k in range(*span)])


def _occurrences(stream: _Stream, key: str) -> list[_Piece]:
    out: list[_Piece] = []
    pos = stream.text.find(key)
    while pos != -1:
        if _aligned(stream, pos, pos + len(key)):
            out.append((pos, pos + len(key)))
        pos = stream.text.find(key, pos + 1)
    return out


def mark_table_blocks(doc: Document) -> list[Block]:
    """Blocks mostly inside a grounded table become kind "table", so the paper view shows the grid instead. A block
    an earlier grounding marked goes back to "text" when no grounded table covers it now."""
    boxes: dict[int, list[Box]] = {}
    for t in doc.tables:
        if t.status in ("grounded", "partial"):
            boxes.setdefault(t.page, []).append(t.bbox)
    out: list[Block] = []
    for b in doc.blocks:
        kind = b.kind
        if kind in ("text", "table"):
            kind = "table" if any(_overlap(b.bbox, box) > 0.5 for box in boxes.get(b.page, [])) else "text"
        out.append(b if kind == b.kind else b.model_copy(update={"kind": kind}))
    return out


NOTE_SHARE = 0.8
"""Share of a block's words the transcribed notes must hold for the block to count as the table's footnote."""


def attach_notes(doc: Document) -> list[Table]:
    """Each transcribed table with its footnote blocks: the text blocks in its region, other than the caption and the
    grid, whose words are the transcribed notes'. The transcription only finds them; the text layer is what is cited."""
    out: list[Table] = []
    for t in doc.tables:
        if t.notes is None or t.status not in ("grounded", "partial"):
            out.append(t.model_copy(update={"notes_anchors": []}))
            continue
        said = set(_words(t.notes))
        blocks = [b for b in doc.blocks if b.page == t.page and b.kind == "text" and b.anchor != t.caption_anchor]
        anchors = [b.anchor for b in blocks if center_in(b.bbox, t.region) and _within(_words(b.text), said)]
        out.append(t.model_copy(update={"notes_anchors": anchors}))
    return out


def _words(text: str) -> list[str]:
    return [m.group() for m in re.finditer(r"\w+", norm(text).lower())]


def _within(words: list[str], said: set[str]) -> bool:
    return bool(words) and sum(w in said for w in words) >= NOTE_SHARE * len(words)


def _find(
    stream: _Stream,
    used: list[bool],
    key: str,
    cursor: int,
    row_y: list[float] | None,
    col_x: list[float] | None,
    chars: list[Char],
    threshold: float,
) -> tuple[int, int, CellMatch] | None:
    exact = [(start, end) for start, end in _occurrences(stream, key) if not any(used[start:end])]
    if exact:
        start, end = min(exact, key=lambda c: _cost(stream, chars, c, cursor, row_y, col_x))
        return start, end, "exact"
    if len(key) < 3:
        return None
    best: tuple[float, int, int] | None = None
    for start in range(len(stream.text)):
        if not stream.starts[start] or used[start]:
            continue
        for length in range(max(1, len(key) - 2), len(key) + 3):
            end = start + length
            if end > len(stream.text) or any(used[start:end]):
                break
            ratio = SequenceMatcher(None, key, stream.text[start:end]).ratio()
            if ratio < threshold:
                continue
            score = ratio - _cost(stream, chars, (start, end), cursor, row_y, col_x) / 1000
            if best is None or score > best[0]:
                best = (score, start, end)
    if best is None:
        return None
    return best[1], best[2], "fuzzy"


def _cost(
    stream: _Stream,
    chars: list[Char],
    span: tuple[int, int],
    cursor: int,
    row_y: list[float] | None,
    col_x: list[float] | None,
) -> float:
    box = union([chars[stream.owner[k]].bbox for k in range(*span)])
    cost = 0.0 if span[0] >= cursor else OUT_OF_ORDER
    if row_y is not None:
        cost += abs((box[1] + box[3]) / 2 - median(row_y))
    if col_x is not None:
        cost += 0.3 * abs((box[0] + box[2]) / 2 - median(col_x))
    return cost + (span[0] - cursor) * 0.001 if span[0] >= cursor else cost


def _aligned(stream: _Stream, start: int, end: int) -> bool:
    text = stream.text
    left = stream.starts[start] or not text[start].isalnum() or not text[start - 1].isalnum()
    right = end >= len(text) or stream.starts[end] or not text[end].isalnum() or not text[end - 1].isalnum()
    return left and right


def _stream(chars: list[Char]) -> _Stream:
    text: list[str] = []
    owner: list[int] = []
    starts: list[bool] = []
    for i, c in enumerate(chars):
        for n, ch in enumerate(_despace(c.text)):
            text.append(ch)
            owner.append(i)
            starts.append(c.boundary and n == 0)
    if starts:
        starts[0] = True
    return _Stream(text="".join(text), owner=owner, starts=starts)


def _despace(text: str) -> str:
    return "".join(norm(text).split())


def _overlap(b: Box, box: Box) -> float:
    w = max(0.0, min(b[2], box[2]) - max(b[0], box[0]))
    h = max(0.0, min(b[3], box[3]) - max(b[1], box[1]))
    area = max(1e-6, (b[2] - b[0]) * (b[3] - b[1]))
    return w * h / area
