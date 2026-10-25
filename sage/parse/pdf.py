"""The only module that reads a PDF: pdftext for the text layer, pypdfium2 for pages and images."""

import io
import math
import re
import threading
from collections import Counter
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from statistics import median

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from pdftext.extraction import dictionary_output
from pydantic import BaseModel, TypeAdapter

from sage.domain.normalize import norm
from sage.parse.document import Block, BlockKind, Box, Document, Page, Table, rounded, union
from sage.parse.layout import (
    Span,
    blank,
    center_in,
    from_upright,
    is_bold,
    is_caption,
    layout,
    rotation_of,
    to_upright,
)

TABLE_CAPTION = re.compile(r"^\s*Table\s*(\d+|[IVX]+)\b", re.IGNORECASE)
REFERENCES = {"references", "literaturecited", "bibliography", "referencescited", "citedliterature", "literature"}
MARGIN = 0.07
JUNK_TITLE = re.compile(
    r"^(untitled|unbekannt|microsoft word|doi:|\d)|\.(docx?|pdf|tex)$|^[\w-]*\d{4,}[\w-]*$", re.IGNORECASE
)
SOFT_HYPHEN, MINUS = "\u00ad", "\u2212"
_CONTROL = {**dict.fromkeys(c for c in range(32) if chr(c) not in "\t\n\r"), 0x02: SOFT_HYPHEN}
"""C0 control characters leave the text layer here. U+0002 that `_resolve` did not place is a soft hyphen, which
quote matching ignores; the rest are dropped."""
EXPONENT = 0.8
"""Height ratio under which a U+0002 is set smaller than the character before it."""
RAISED = 0.25
"""Share of the line height a U+0002's bottom must sit above the character before it to be raised off the line."""
COVER_SHEET = re.compile(
    r"this article was downloaded by|please scroll down for article|your use of the jstor archive|"
    r"jstor is a not-for-profit|this content downloaded from",
    re.IGNORECASE,
)


class PdfError(Exception):
    pass


_PDFIUM = threading.RLock()
"""PDFium is not thread-safe, and pdftext calls it too. The API serves requests on a thread pool, so two at once
corrupt its heap and kill the process. Every call into either library holds this lock."""


@dataclass(frozen=True)
class Char:
    text: str
    bbox: Box
    boundary: bool
    """True when the character starts a span or follows whitespace in the text layer."""
    space: bool = False
    """True when the text layer writes a whitespace character before this one."""


class _RawChar(BaseModel):
    char: str
    bbox: list[float]


class _RawFont(BaseModel):
    name: str | None = None
    flags: int | None = None
    weight: float | None = None


class _RawSpan(BaseModel):
    text: str
    bbox: list[float]
    rotation: float | None = None
    font: _RawFont = _RawFont()
    chars: list[_RawChar]


class _RawLine(BaseModel):
    spans: list[_RawSpan]


class _RawBlock(BaseModel):
    bbox: list[float]
    lines: list[_RawLine]


class _RawPage(BaseModel):
    page: int
    width: float
    height: float
    blocks: list[_RawBlock]


_raw_pages = TypeAdapter(list[_RawPage])
_metadata = TypeAdapter(dict[str, str])


def _extract(data: bytes, *, page_range: list[int] | None = None) -> list[_RawPage]:
    with _PDFIUM:
        raw = dictionary_output(data, page_range=page_range, keep_chars=True, disable_links=True)
    pages = _raw_pages.validate_python(raw)
    for p in pages:
        _resolve(p)
    return pages


def _resolve(page: _RawPage) -> None:
    """pdftext writes U+0002 for two glyphs, told apart by the character's own box against the one before it. Set
    smaller and raised off the line, it is an exponent's minus ("Mg ha" then a raised U+0002 and "1" is "Mg ha-1").
    Otherwise it is the soft hyphen at a line break ("pro\u0002 cessing")."""
    for block in page.blocks:
        prev: Box | None = None
        for line in block.lines:
            for span in line.spans:
                rot = rotation_of(span.rotation)
                for ch in span.chars:
                    box = to_upright(rounded(ch.bbox), rot, page.width, page.height)
                    if ch.char == "\x02":
                        ch.char = MINUS if prev is not None and _superscript(box, prev) else SOFT_HYPHEN
                    if ch.char.strip():
                        prev = box


def _superscript(box: Box, prev: Box) -> bool:
    height = prev[3] - prev[1]
    return box[3] - box[1] < EXPONENT * height and prev[3] - box[3] > RAISED * height


@contextmanager
def _open(data: bytes) -> Generator[pdfium.PdfDocument]:
    """The document, under the lock until it and its pages are closed, so no finalizer frees it on another thread."""
    with _PDFIUM:
        try:
            pdf = pdfium.PdfDocument(data)
        except pdfium.PdfiumError as e:
            raise PdfError(f"not a readable PDF: {e}") from e
        try:
            yield pdf
        finally:
            pdf.close()


def page_count(data: bytes) -> int:
    with _open(data) as pdf:
        return len(pdf)


def title(data: bytes, doc: Document) -> str | None:
    """The metadata title when it reads like one, else the largest text near the top of the first content page."""
    with _open(data) as pdf:
        meta = _metadata.validate_python(pdf.get_metadata_dict(skip_empty=True)).get("Title", "").strip()
    if len(meta.split()) >= 4 and not JUNK_TITLE.search(meta):
        return meta
    for page in doc.pages[:3]:
        blocks = [b for b in doc.blocks if b.page == page.number and b.kind == "text"]
        if not blocks:
            continue
        top = [b for b in blocks if b.bbox[1] < page.height * 0.5 and 3 <= len(b.text.split()) <= 40]
        if top:
            return max(top, key=lambda b: (round(b.size, 1), -b.bbox[1])).text
    return meta or None


def parse(data: bytes, *, paper_id: str, sha256: str) -> Document:
    count = page_count(data)
    pages: list[Page] = []
    blocks: list[Block] = []
    for p in _extract(data):
        number = p.page + 1
        spans = [_span(s) for b in p.blocks for line in b.lines for s in line.spans]
        laid = layout(spans, width=p.width, height=p.height)
        pages.append(Page(number=number, width=p.width, height=p.height, has_text=bool(laid)))
        for n, lb in enumerate(laid, start=1):
            kind: BlockKind = "caption" if is_caption(lb.text, bold=lb.bold_start) else "text"
            blocks.append(
                Block(
                    anchor=f"p{number}.b{n}",
                    page=number,
                    bbox=rounded(lb.bbox),
                    text=lb.text,
                    kind=kind,
                    size=round(lb.line_height, 2),
                    rotation=lb.rotation,
                )
            )
    if len(pages) != count:
        raise PdfError("pdftext and pdfium disagree on the page count")
    by_number = {p.number: p for p in pages}
    blocks = _classify(blocks, by_number)
    tables = _table_stubs(blocks, by_number)
    return Document(paper_id=paper_id, sha256=sha256, pages=pages, blocks=blocks, tables=tables)


def pages_chars(data: bytes, pages: Sequence[int]) -> dict[int, list[Char]]:
    out: dict[int, list[Char]] = {}
    for p in _extract(data, page_range=[n - 1 for n in sorted(set(pages))]):
        chars = out[p.page + 1] = []
        for block in p.blocks:
            space = False
            for line in block.lines:
                for span in line.spans:
                    first = True
                    for ch in span.chars:
                        text = ch.char.translate(_CONTROL)
                        if not text.strip():
                            first, space = True, space or blank(text)
                            continue
                        chars.append(Char(text=text, bbox=rounded(ch.bbox), boundary=first, space=space))
                        first = space = False
    return out


def render(data: bytes, page: int, *, bbox: Box | None = None, scale: float = 2.0, text_rotation: int = 0) -> bytes:
    """A PNG of the page, or of `bbox` on it, at `scale` x 72 dpi, turned so text of `text_rotation` reads upright.
    The part of `bbox` on the page is drawn, and never less than one pixel."""
    with _open(data) as pdf:
        pg = pdf[page - 1]
        width, height = pg.get_size()
        crop = (0.0, 0.0, 0.0, 0.0)
        if bbox is not None:
            w, h = (height, width) if text_rotation in (90, 270) else (width, height)
            crop = _crop(to_upright(bbox, text_rotation, width, height), w, h, scale)
        bitmap = pg.render(scale=scale, crop=crop, rotation=(360 - text_rotation) % 360)
        image = bitmap.to_pil().copy()
        bitmap.close()
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _crop(box: Box, width: float, height: float, scale: float) -> tuple[float, float, float, float]:
    """PDFium's `(left, bottom, right, top)` crop for an upright `box`, in whole pixels of the bitmap. PDFium crops
    after it turns the page, and rounds the page and each crop up to whole pixels, so a box off the page or under a
    pixel wide would leave it nothing to draw."""
    w, h = math.ceil(width * scale), math.ceil(height * scale)
    left = min(max(math.floor(box[0] * scale), 0), w - 1)
    top = min(max(math.floor(box[1] * scale), 0), h - 1)
    right = max(min(math.ceil(box[2] * scale), w), left + 1)
    bottom = max(min(math.ceil(box[3] * scale), h), top + 1)

    def edge(px: int) -> float:
        """Half a pixel under the edge, so PDFium's rounding up lands on it."""
        return max(0.0, (px - 0.5) / scale)

    return edge(left), edge(h - bottom), edge(w - right), edge(top)


def _span(s: _RawSpan) -> Span:
    f = s.font
    bold = is_bold(f.weight or 0, f.flags or 0, f.name or "")
    chars = tuple((c.char.translate(_CONTROL), rounded(c.bbox)) for c in s.chars)
    text = s.text.translate(_CONTROL)
    return Span(text=text, bbox=rounded(s.bbox), bold=bold, rotation=rotation_of(s.rotation), chars=chars)


def _classify(blocks: list[Block], pages: dict[int, Page]) -> list[Block]:
    """Mark running heads, footers, page numbers and a download cover sheet on page 1, and everything after the
    References heading. A running head repeats in the margin at about the same height on several pages; a figure label
    that repeats does not count."""
    seen: dict[tuple[str, int], set[int]] = {}
    for b in blocks:
        if _in_margin(b, pages[b.page]) and len(b.text) < 160:
            seen.setdefault(_furniture_key(b), set()).add(b.page)
    repeats = max(2, round(len(pages) * 0.2))
    cover = {1} if any(b.page == 1 and COVER_SHEET.search(b.text) for b in blocks) and len(pages) > 1 else set()
    in_references = False
    out: list[Block] = []
    for b in blocks:
        kind: BlockKind = b.kind
        if b.kind == "text" and len(b.text) < 40 and re.sub(r"[\W\d_]+", "", b.text).lower() in REFERENCES:
            in_references = True
        if b.page in cover:
            kind = "furniture"
        elif in_references and kind == "text":
            kind = "references"
        elif _in_margin(b, pages[b.page]) and (
            len(seen.get(_furniture_key(b), set())) >= repeats
            or re.fullmatch(r"\d{1,4}", b.text.strip()) is not None
            or COVER_SHEET.search(b.text) is not None
        ):
            kind = "furniture"
        out.append(b.model_copy(update={"kind": kind}))
    return out


def _furniture_key(b: Block) -> tuple[str, int]:
    return re.sub(r"\d+", "#", norm(b.text).lower()), round(b.bbox[1] / 20)


def _in_margin(b: Block, page: Page) -> bool:
    if b.rotation != 0:
        return False
    return b.bbox[3] < page.height * MARGIN * 1.6 or b.bbox[1] > page.height * (1 - MARGIN)


def _table_stubs(blocks: list[Block], pages: dict[int, Page]) -> list[Table]:
    """One pending Table per "Table N" caption. Worked out in the caption's upright frame, so a landscape table on
    a portrait page works too. The region runs from the caption down its column to the next caption, the first
    paragraph of body text, or the bottom margin."""
    body = _body_size(blocks)
    tables: list[Table] = []
    per_page: Counter[int] = Counter()
    for b in blocks:
        if b.kind != "caption" or not (m := TABLE_CAPTION.match(b.text)):
            continue
        page = pages[b.page]
        rot = b.rotation
        w, h = (page.height, page.width) if rot in (90, 270) else (page.width, page.height)

        def up(box: Box, rot: int = rot, page: Page = page) -> Box:
            return to_upright(box, rot, page.width, page.height)

        cap = up(b.bbox)
        x0, x1 = _column(cap, w)
        stops = [
            up(o.bbox)[1]
            for o in blocks
            if o.page == b.page
            and o.rotation == rot
            and o.anchor != b.anchor
            and up(o.bbox)[1] > cap[3]
            and up(o.bbox)[0] < x1
            and up(o.bbox)[2] > x0
            and _stops_region(o, body)
        ]
        bottom = min([*stops, h * (1 - MARGIN)])
        per_page[b.page] += 1
        region = from_upright((x0, max(0.0, cap[1] - 2), x1, bottom), rot, page.width, page.height)
        tables.append(
            Table(
                anchor=f"p{b.page}.t{per_page[b.page]}",
                page=b.page,
                label=f"Table {m.group(1)}",
                caption_anchor=b.anchor,
                caption=b.text,
                region=rounded(region),
                rotation=rot,
            )
        )
    return tables


def _body_size(blocks: list[Block]) -> float:
    """The line height of the paper's body text."""
    return median([b.size for b in blocks if b.kind == "text" and len(b.text) > 200] or [10.0])


def _stops_region(b: Block, body: float) -> bool:
    """A table's or figure's region ends at another caption or at a paragraph of body text."""
    return b.kind == "caption" or (b.kind == "text" and len(b.text) > 300 and b.size >= 0.95 * body)


def _column(cap: Box, width: float) -> tuple[float, float]:
    mid = width / 2
    if cap[2] < mid + 10 and cap[2] - cap[0] < width * 0.5:
        return 0.0, mid
    if cap[0] > mid - 10:
        return mid, width
    return 0.0, width


def render_pages(data: bytes, pages: Iterable[int], *, scale: float, write: Callable[[int, bytes], None]) -> None:
    """Each page as a PNG at `scale` x 72 dpi, handed to `write(page, png)` as soon as it is drawn, so a long paper is
    never in memory whole. The PDF is opened for each page, so other requests reach PDFium between pages."""
    for n in pages:
        with _open(data) as pdf:
            pg = pdf[n - 1]
            bitmap = pg.render(scale=scale)
            out = io.BytesIO()
            bitmap.to_pil().save(out, format="PNG")
            bitmap.close()
            pg.close()
        write(n, out.getvalue())


FIGURE_CAPTION = re.compile(r"^\s*(?:Fig\.?|Figure)\s*(\d+|[IVX]+)\b", re.IGNORECASE)
FIGURE_REACH = 72.0
"""Points from a figure caption to the nearest drawing of its figure, at most."""
FIGURE_JOIN = 36.0
"""Points between two drawings of one figure, at most."""
FIGURE_MIN = 36.0
"""Points a figure's drawings must span both ways, so a rule or a logo is never taken for a figure."""
FORM_DEPTH = 4
"""How deep to look into nested Form XObjects for a figure's images and drawings."""


@dataclass(frozen=True)
class Figure:
    anchor: str
    """`p{page}.f{n}`: the `n`th figure caption on the page, counted like a table's `t`."""
    page: int
    region: Box
    rotation: int = 0


def figures(data: bytes, doc: Document) -> list[Figure]:
    """The figure of each "Figure N" or "Fig. N" caption, where its drawings can be found. A figure is the images and
    vector drawings closest to the caption: beside a narrow caption set in the margin, else above it in its column
    (where most journals put a figure), else below it, never past the body text and captions around it, with the
    short text among them such as axis labels. A drawing belongs to one figure. A caption with no drawings there has
    no Figure, so a figure is never guessed."""
    captions = [b for b in doc.blocks if b.kind == "caption" and FIGURE_CAPTION.match(b.text)]
    if not captions:
        return []
    pages = {p.number: p for p in doc.pages}
    drawn = _drawings(data, sorted({b.page for b in captions}))
    body = _body_size(doc.blocks)
    per_page: Counter[int] = Counter()
    numbered = []
    for b in captions:
        per_page[b.page] += 1
        numbered.append((f"p{b.page}.f{per_page[b.page]}", b))
    found: dict[str, Figure] = {}
    # Side captions first, so a figure beside its caption is never claimed by the one under it.
    for anchor, b in sorted(numbered, key=lambda n: not _side_caption(n[1], pages[n[1].page])):
        region = _figure_region(b, doc.blocks, drawn[b.page], pages[b.page], body)
        if region is None:
            continue
        drawn[b.page] = [g for g in drawn[b.page] if not center_in(g, region)]
        found[anchor] = Figure(anchor=anchor, page=b.page, region=rounded(region), rotation=b.rotation)
    return [found[a] for a, _ in numbered if a in found]


def _side_caption(b: Block, page: Page) -> bool:
    """A caption of several lines set in a narrow margin column, beside its figure."""
    width = page.height if b.rotation in (90, 270) else page.width
    box = to_upright(b.bbox, b.rotation, page.width, page.height)
    return box[2] - box[0] < 0.3 * width and box[3] - box[1] > 3 * max(b.size, 1.0)


def _figure_region(cap_block: Block, blocks: list[Block], drawn: list[Box], page: Page, body: float) -> Box | None:
    """Worked out in the caption's upright frame, like a table's region."""
    rot = cap_block.rotation
    w, h = (page.height, page.width) if rot in (90, 270) else (page.width, page.height)

    def up(box: Box) -> Box:
        return to_upright(box, rot, page.width, page.height)

    cap = up(cap_block.bbox)
    near = [
        (up(o.bbox), o)
        for o in blocks
        if o.page == cap_block.page and o.rotation == rot and o.anchor != cap_block.anchor
    ]
    stops = [box for box, o in near if _stops_region(o, body)]

    def bounds(x0: float, x1: float, y: float) -> tuple[float, float]:
        """The top and bottom of the free space around height `y` between x0 and x1."""
        across = [box for box in stops if box[0] < x1 and box[2] > x0]
        top = max([box[3] for box in across if box[3] <= y] + [h * MARGIN])
        bottom = min([box[1] for box in across if box[1] >= y] + [h * (1 - MARGIN)])
        return top, bottom

    x0, x1 = _column(cap, w)
    top, _ = bounds(x0, x1, cap[1])
    _, bottom = bounds(x0, x1, cap[3])
    side_top, side_bottom = bounds(cap[2], w, cap[1])
    tries = [
        ((x0, top, x1, cap[1]), lambda g: max(0.0, cap[1] - g[3]) <= FIGURE_REACH),
        ((x0, cap[3], x1, bottom), lambda g: max(0.0, g[1] - cap[3]) <= FIGURE_REACH),
    ]
    beside = (
        (cap[2], side_top, w, side_bottom),
        lambda g: g[0] - cap[2] <= FIGURE_REACH and g[1] < cap[3] and g[3] > cap[1],
    )
    if _side_caption(cap_block, page):
        tries.insert(0, beside)
    for gap, seed in tries:
        region = _cluster([g for g in (up(g) for g in drawn) if _within(g, gap)], seed)
        if region is None:
            continue
        labels = [
            box
            for box, o in near
            if o.kind == "text" and center_in(box, gap) and box[3] > region[1] and box[1] < region[3]
        ]
        return from_upright(union([region, *labels]), rot, page.width, page.height)
    return None


def _within(box: Box, gap: Box) -> bool:
    """Most of the box is inside the gap."""
    w = min(box[2], gap[2]) - max(box[0], gap[0])
    h = min(box[3], gap[3]) - max(box[1], gap[1])
    area = max(box[2] - box[0], 1.0) * max(box[3] - box[1], 1.0)
    return w >= 0 and h >= 0 and max(w, 1.0) * max(h, 1.0) >= 0.8 * area


def _cluster(drawn: list[Box], seed: Callable[[Box], bool]) -> Box | None:
    """The drawings that reach the caption (`seed`), and those that reach them in turn, as one box."""
    seeds = [g for g in drawn if seed(g)]
    if not seeds:
        return None
    region = union(seeds)
    rest = [g for g in drawn if not seed(g)]
    grew = True
    while grew:
        grew = False
        for g in list(rest):
            if max(0.0, g[0] - region[2], region[0] - g[2], g[1] - region[3], region[1] - g[3]) <= FIGURE_JOIN:
                region = union([region, g])
                rest.remove(g)
                grew = True
    if region[2] - region[0] < FIGURE_MIN or region[3] - region[1] < FIGURE_MIN:
        return None
    return region


def _drawings(data: bytes, pages: Sequence[int]) -> dict[int, list[Box]]:
    """The boxes of each page's images and vector paths, in the text layer's frame, nested Form XObjects included. A
    page-sized background is left out."""
    out: dict[int, list[Box]] = {}
    with _open(data) as pdf:
        for n in pages:
            pg = pdf[n - 1]
            frame = pg.get_bbox()
            rotation = pg.get_rotation()
            boxes = [_layer_box(r, frame, rotation) for r in _objects(pg, None, None, 0)]
            width, height = math.ceil(abs(frame[2] - frame[0])), math.ceil(abs(frame[3] - frame[1]))
            out[n] = [
                b
                for b in boxes
                if (b[2] - b[0] > 0.5 or b[3] - b[1] > 0.5)
                and not (b[2] - b[0] >= 0.9 * width and b[3] - b[1] >= 0.9 * height)
            ]
            pg.close()
    return out


def _objects(
    page: pdfium.PdfPage, form: pdfium.PdfObject | None, matrix: pdfium.PdfMatrix | None, depth: int
) -> Iterator[tuple[float, float, float, float]]:
    """PDF-space rects. An object inside a form has its bounds in the form's space, which the forms' matrices map to
    the page's."""
    for o in page.get_objects(max_depth=0, form=form):
        if o.type == pdfium_c.FPDF_PAGEOBJ_FORM:
            if depth < FORM_DEPTH:
                inner = o.get_matrix()
                yield from _objects(page, o, inner if matrix is None else inner.multiply(matrix), depth + 1)
        elif o.type in (pdfium_c.FPDF_PAGEOBJ_IMAGE, pdfium_c.FPDF_PAGEOBJ_PATH):
            rect = o.get_bounds()
            left, bottom, right, top = rect if matrix is None else matrix.on_rect(*rect)
            yield (float(left), float(bottom), float(right), float(top))


def _layer_box(rect: Sequence[float], frame: Sequence[float], rotation: int) -> Box:
    """A PDF-space rect in the frame pdftext gives characters: from the page box's top left, turned with the page."""
    x_start, y_start, x_end, y_end = frame
    width, height = math.ceil(abs(x_end - x_start)), math.ceil(abs(y_end - y_start))
    left, bottom, right, top = rect
    x0, x1 = left - x_start, right - x_start
    y0, y1 = height - (top - y_start), height - (bottom - y_start)
    match rotation:
        case 90:
            x0, y0, x1, y1 = height - y1, x0, height - y0, x1
        case 180:
            x0, y0, x1, y1 = width - x1, height - y1, width - x0, height - y0
        case 270:
            x0, y0, x1, y1 = y0, width - x1, y1, width - x0
    return rounded((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
