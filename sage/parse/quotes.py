"""Where a quote sits on the page: one box per line of its block that the quoted words cover, so the paper can mark
the words a value came from instead of the whole block.

The quote is matched against the block's characters exactly as `normalize.contains` matches it against the block's
text: spacing ignored, a word hyphenated across a line break joined, case kept. So a quote gets line boxes only where
the quote check would find it verbatim, and any other draws as its whole block."""

from collections.abc import Iterable
from dataclasses import dataclass
from statistics import median

from sage.domain.normalize import needles, norm
from sage.parse import pdf
from sage.parse.document import Box, Document, rounded, union
from sage.parse.layout import Rotation, center_in, from_upright, same_line, to_upright

LOCATOR_VERSION = "1"
"""Bump when the boxes a quote gets change, so cached quote boxes are worked out again."""

SMALL = 0.75
TALL = 1.6
"""Characters shorter than SMALL or taller than TALL times the block's median height are set off the line: a
superscript or subscript, or a drop cap."""

QuoteLines = dict[str, dict[str, list[Box]]]
"""Anchor, then quote, then the boxes of the lines the quote covers, in reading order."""


@dataclass(frozen=True)
class _Glyph:
    text: str
    """The character as the quote check reads it: normalised, with no whitespace. Often empty (a soft hyphen)."""
    box: Box
    """In the block's upright frame."""
    line: int
    order: int
    """Its place in the text layer, whose order within a line is right even where OCR boxes overlap."""
    space: bool
    """The text layer writes a space before it."""


@dataclass(frozen=True)
class _Reading:
    """The block's characters in one order, run together the way `squash` runs the block's text together."""

    glyphs: list[_Glyph]
    text: str
    owner: list[int]
    """Index into `glyphs` for each character of `text`."""


def locate(data: bytes, doc: Document, cites: Iterable[tuple[str, str]]) -> QuoteLines:
    """The line boxes of every `(anchor, quote)` cited in a text block. Table cells already have a box of their own
    and are left out, and so is a quote that can't be found in its block."""
    blocks = {b.anchor: b for b in doc.blocks}
    wanted = sorted({(a, q) for a, q in cites if a in blocks and q.strip()})
    if not wanted:
        return {}
    chars = pdf.pages_chars(data, sorted({blocks[a].page for a, _ in wanted}))
    sizes = {p.number: (p.width, p.height) for p in doc.pages}
    readings: dict[str, list[_Reading]] = {}
    out: QuoteLines = {}
    for anchor, quote in wanted:
        block = blocks[anchor]
        width, height = sizes[block.page]
        if anchor not in readings:
            inside = [c for c in chars[block.page] if center_in(c.bbox, block.bbox)]
            readings[anchor] = _readings(_lines(inside, block.rotation, width, height))
        lines = next((found for r in readings[anchor] if (found := _find(r, quote))), [])
        if lines:
            out.setdefault(anchor, {})[quote] = [rounded(from_upright(b, block.rotation, width, height)) for b in lines]
    return out


def _lines(chars: list[pdf.Char], rotation: Rotation, width: float, height: float) -> list[list[_Glyph]]:
    """The block's characters by line, top to bottom, in the upright frame.

    Lines are banded from characters of body height. A smaller one (a superscript, a subscript) then joins the line
    it overlaps most, and a taller one (a drop cap) the first line it reaches, so neither splits a line in two."""
    up = sorted(
        ((i, c, to_upright(c.bbox, rotation, width, height)) for i, c in enumerate(chars)), key=lambda t: _mid(t[2])
    )
    if not up:
        return []
    body = median(_height(b) for _, _, b in up)
    lines: list[list[_Glyph]] = []
    bands: list[Box] = []
    odd: list[tuple[int, pdf.Char, Box]] = []
    for i, c, b in up:
        if not SMALL * body <= _height(b) <= TALL * body:
            odd.append((i, c, b))
            continue
        if not bands or not same_line(bands[-1], b):
            bands.append(b)
            lines.append([])
        bands[-1] = union([bands[-1], b])
        lines[-1].append(_glyph(i, c, b, len(lines) - 1))
    for i, c, b in odd:
        overlaps = [_overlap(b, band) for band in bands]
        if not overlaps or max(overlaps) <= 0:
            continue
        n = overlaps.index(max(overlaps)) if _height(b) < body else next(k for k, o in enumerate(overlaps) if o > 0)
        lines[n].append(_glyph(i, c, b, n))
    return lines


def _glyph(order: int, c: pdf.Char, box: Box, line: int) -> _Glyph:
    return _Glyph(text="".join(norm(c.text).split()), box=box, line=line, order=order, space=c.space)


def _readings(lines: list[list[_Glyph]]) -> list[_Reading]:
    """Each line in the text layer's order, then each left to right by the characters' centres. The first is right
    for OCR, whose boxes overlap their neighbours; the second places a superscript the text layer wrote apart."""
    by_layer = [g for line in lines for g in sorted(line, key=lambda g: g.order)]
    by_place = [g for line in lines for g in sorted(line, key=lambda g: g.box[0] + g.box[2])]
    first = _reading(by_layer)
    second = _reading(by_place)
    return [first] if second.text == first.text else [first, second]


def _reading(glyphs: list[_Glyph]) -> _Reading:
    text: list[str] = []
    owner: list[int] = []
    for i, g in enumerate(glyphs):
        if g.text == "-" and _joins(glyphs, i):
            continue
        for ch in g.text:
            text.append(ch)
            owner.append(i)
    return _Reading(glyphs=glyphs, text="".join(text), owner=owner)


def _find(r: _Reading, quote: str) -> list[Box]:
    for needle in needles(quote):
        at = r.text.find(needle)
        if at >= 0:
            by_line: dict[int, list[Box]] = {}
            for k in sorted({r.owner[j] for j in range(at, at + len(needle))}):
                by_line.setdefault(r.glyphs[k].line, []).append(r.glyphs[k].box)
            return [union(boxes) for _, boxes in sorted(by_line.items())]
    return []


def _joins(glyphs: list[_Glyph], i: int) -> bool:
    """A hyphen right after a letter, with a break before the letter after it, which the quote check drops
    ("pro- cessing"). One with a space before it is a dash ("Table 5 - Growth", an en dash in print) and stays."""
    if glyphs[i].space:
        return False
    before = next((glyphs[k].text for k in range(i - 1, -1, -1) if glyphs[k].text), "")
    k = next((k for k in range(i + 1, len(glyphs)) if glyphs[k].text), None)
    if k is None or not before[-1:].isalnum() or not glyphs[k].text[:1].isalnum():
        return False
    return glyphs[k].space or glyphs[k].line != glyphs[i].line


def _mid(b: Box) -> float:
    return (b[1] + b[3]) / 2


def _height(b: Box) -> float:
    return b[3] - b[1]


def _overlap(b: Box, band: Box) -> float:
    return min(b[3], band[3]) - max(b[1], band[1])
