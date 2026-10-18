"""Page layout from text spans: spans join into line segments, a recursive XY-cut orders them into regions, and
regions split into blocks at captions and headings. pdftext's own line grouping merges both columns of a two-column
page, so the layout is ours, built from its spans and their characters alone."""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from itertools import pairwise
from statistics import median

from sage.parse.document import Box, union

FORCE_BOLD = 1 << 18
BOLD_NAME = re.compile(r"bold|black|heavy|semibold|demi|(?<=[a-z])B$|[.\-+]B$|[.\-+]BI?$|-bi?$|BI$|-SB$", re.IGNORECASE)
CAPTION_START = re.compile(r"^\s*(Table|Fig\.?|Figure)\s*(\d+|[IVX]+)\b", re.IGNORECASE)
WORD_GAP = 0.1
"""Share of the line height a gap must exceed to read as a space. An exponent abuts its unit ("m" then "-2")."""
SMALLER = 0.85
"""Height ratio under which a run is set smaller than the text before it: a superscript or a subscript."""


Rotation = int
"""Text direction in degrees clockwise from upright: 0, 90, 180 or 270. 270 reads bottom to top."""


@dataclass(frozen=True)
class Span:
    text: str
    bbox: Box
    bold: bool
    rotation: Rotation = 0
    chars: tuple[tuple[str, Box], ...] = ()
    """Characters with their boxes, when known. A span is split where they show a gap of most of a line height."""

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]


@dataclass
class Segment:
    """Spans on one visual line with no wide gap between them."""

    spans: list[Span]

    @property
    def bbox(self) -> Box:
        return union([s.bbox for s in self.spans])

    @property
    def text(self) -> str:
        out = self.spans[0].text.strip()
        for prev, s in pairwise(self.spans):
            written = blank(prev.text.rstrip("\r\n")[-1:]) or blank(s.text.lstrip("\r\n")[:1])
            out += (" " if spaced(prev.bbox, prev.text.strip(), s.bbox, written=written) else "") + s.text.strip()
        return out

    @property
    def height(self) -> float:
        return median(s.height for s in self.spans)

    @property
    def bold(self) -> bool:
        return all(s.bold for s in self.spans if s.text.strip())


@dataclass(frozen=True)
class LaidBlock:
    bbox: Box
    text: str
    bold_start: bool
    """The block's first span is bold. Captions and headings usually are."""
    line_height: float
    rotation: Rotation = 0


def is_caption(text: str, *, bold: bool) -> bool:
    """A "Table 3" or "Fig. 2" label that is bold or punctuated, so "Table 1 shows" is body text."""
    label = CAPTION_START.match(text)
    return label is not None and (bold or re.match(r"\s*[.:|\u2013\u2014-]", text[label.end() :]) is not None)


def is_bold(weight: float, flags: int, name: str) -> bool:
    return weight >= 600 or bool(flags & FORCE_BOLD) or bool(BOLD_NAME.search(name))


def rotation_of(radians: float | None) -> Rotation:
    if radians is None:
        return 0
    return round(math.degrees(radians) / 90) % 4 * 90


def to_upright(box: Box, rotation: Rotation, width: float, height: float) -> Box:
    """Page coordinates to a frame where text of this rotation reads left to right, top to bottom."""
    x0, y0, x1, y1 = box
    match rotation:
        case 90:
            return (y0, width - x1, y1, width - x0)
        case 180:
            return (width - x1, height - y1, width - x0, height - y0)
        case 270:
            return (height - y1, x0, height - y0, x1)
        case _:
            return box


def from_upright(box: Box, rotation: Rotation, width: float, height: float) -> Box:
    a0, b0, a1, b1 = box
    match rotation:
        case 90:
            return (width - b1, a0, width - b0, a1)
        case 180:
            return (width - a1, height - b1, width - a0, height - b0)
        case 270:
            return (b0, height - a1, b1, height - a0)
        case _:
            return box


def layout(spans: list[Span], *, width: float, height: float) -> list[LaidBlock]:
    """Blocks in reading order. Each text direction is laid out in its own upright frame, upright text first."""
    blocks: list[LaidBlock] = []
    for rotation in (0, 270, 90, 180):
        group = [
            run
            for s in spans
            if s.rotation == rotation and s.text.strip()
            for run in _runs(s, lambda b, r=rotation: to_upright(b, r, width, height))
            if run.height > 0
        ]
        if not group:
            continue
        segments = _segments(group)
        h = median(s.height for s in segments)
        blocks += [
            replace(b, bbox=from_upright(b.bbox, rotation, width, height), rotation=rotation)
            for region in _xy_cut(segments, h)
            for b in _split_region(region, h)
        ]
    return blocks


def _runs(span: Span, up: Callable[[Box], Box]) -> list[Span]:
    """The span in the upright frame, split where two characters sit further apart than about 0.8 line heights.
    pdftext sometimes joins text across a column gutter or across table cells into one span."""
    if not span.chars:
        return [Span(span.text, up(span.bbox), span.bold, span.rotation)]
    chars = [(c, up(b)) for c, b in span.chars]
    height = median(b[3] - b[1] for c, b in chars if c.strip()) if any(c.strip() for c, _ in chars) else 0.0
    runs: list[list[tuple[str, Box]]] = [[]]
    last_end: float | None = None
    for c, b in chars:
        if c.strip() and last_end is not None and b[0] - last_end > 0.8 * height:
            runs.append([])
        runs[-1].append((c, b))
        if c.strip():
            last_end = b[2]
    out: list[Span] = []
    for run in runs:
        ink = [b for c, b in run if c.strip()]
        if not ink:
            continue
        out.append(Span("".join(c for c, _ in run), union(ink), span.bold, span.rotation))
    return out


def _segments(spans: list[Span]) -> list[Segment]:
    out: list[Segment] = []
    for span in sorted(spans, key=lambda s: (s.bbox[0], s.bbox[1])):
        best: Segment | None = None
        for seg in out:
            last = seg.spans[-1]
            gap = span.bbox[0] - last.bbox[2]
            limit = 1.1 * max(min(seg.height, span.height), 1.0)
            if -2.0 <= gap <= limit and same_line(last.bbox, span.bbox):
                best = seg
                break
        if best is None:
            out.append(Segment([span]))
        else:
            best.spans.append(span)
    return out


def same_line(a: Box, b: Box) -> bool:
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    return overlap >= 0.3 * min(a[3] - a[1], b[3] - b[1])


def center_in(b: Box, region: Box) -> bool:
    x, y = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return region[0] <= x <= region[2] and region[1] <= y <= region[3]


def blank(char: str) -> bool:
    """A space the text layer writes. pdftext ends each of its lines with a line break, and a raised run often starts a
    line of its own, so a line break is not one."""
    return char.isspace() and char not in "\r\n"


def spaced(prev: Box, prev_text: str, box: Box, *, written: bool) -> bool:
    """Whether a space goes between two runs of text: the text layer writes one there (`written`), or the page shows
    one, a new line or a gap wider than WORD_GAP of the line height. So an exponent stays on its unit ("m" then a raised
    "-2" reads "m-2"). A smaller run after a number keeps its space, so mean-separation letters and footnote calls stay
    their own token ("12.8 a"). A scanned paper's text layer can overlap its words' boxes, so a written space counts."""
    size, prev_size = box[3] - box[1], prev[3] - prev[1]
    gap = box[0] - prev[2] > WORD_GAP * max(size, prev_size)
    called = prev_text[-1:].isdigit() and size < SMALLER * prev_size
    return written or gap or called or not same_line(prev, box)


def _xy_cut(segments: list[Segment], h: float) -> list[list[Segment]]:
    """Reading-order regions. A column gutter is cut before a horizontal gap, so the left column is read top to
    bottom before the right one."""
    if len(segments) <= 1:
        return [segments]
    vertical = _best_gap([(s.bbox[0], s.bbox[2]) for s in segments], min_gap=0.9 * h)
    if vertical is not None and _both_sides_tall(segments, vertical, h):
        left = [s for s in segments if s.bbox[2] <= vertical]
        right = [s for s in segments if s.bbox[0] >= vertical]
        return _xy_cut(left, h) + _xy_cut(right, h)
    horizontal = _best_gap([(s.bbox[1], s.bbox[3]) for s in segments], min_gap=0.45 * h)
    if horizontal is not None:
        top = [s for s in segments if s.bbox[3] <= horizontal]
        bottom = [s for s in segments if s.bbox[1] >= horizontal]
        return _xy_cut(top, h) + _xy_cut(bottom, h)
    return [sorted(segments, key=lambda s: (round(s.bbox[1] / (0.5 * h)), s.bbox[0]))]


def _best_gap(intervals: list[tuple[float, float]], *, min_gap: float) -> float | None:
    """The middle of the widest empty stretch between the intervals, if one is at least min_gap wide."""
    ordered = sorted(intervals)
    reach = ordered[0][1]
    best: tuple[float, float] | None = None
    for start, end in ordered[1:]:
        if start - reach >= min_gap and (best is None or start - reach > best[1] - best[0]):
            best = (reach, start)
        reach = max(reach, end)
    return None if best is None else (best[0] + best[1]) / 2


def _both_sides_tall(segments: list[Segment], x: float, h: float) -> bool:
    """A real gutter has text running down both sides; a gap inside one short line does not count."""

    def extent(side: list[Segment]) -> float:
        return max(s.bbox[3] for s in side) - min(s.bbox[1] for s in side) if side else 0.0

    left = [s for s in segments if s.bbox[2] <= x]
    right = [s for s in segments if s.bbox[0] >= x]
    return min(extent(left), extent(right)) >= 1.5 * h or (len(left) >= 1 and len(right) >= 1 and len(segments) <= 3)


def _split_region(region: list[Segment], h: float) -> list[LaidBlock]:
    lines = _lines(region, h)
    groups: list[list[list[Segment]]] = []
    for line in lines:
        first = line[0]
        after_sentence = not groups or re.search(r"[.:;)\]]\s*$", " ".join(s.text for s in groups[-1][-1])) is not None
        starts_caption = after_sentence and is_caption(first.text, bold=first.spans[0].bold)
        heading = all(s.bold for s in line) and len(" ".join(s.text for s in line)) < 120
        prev_heading = bool(groups) and all(all(s.bold for s in ln) for ln in groups[-1])
        if not groups or starts_caption or heading != prev_heading:
            groups.append([line])
        else:
            groups[-1].append(line)
    return [_block(g) for g in groups]


def _lines(region: list[Segment], h: float) -> list[list[Segment]]:
    rows: list[list[Segment]] = []
    for seg in sorted(region, key=lambda s: (s.bbox[1], s.bbox[0])):
        if rows and same_line(rows[-1][0].bbox, seg.bbox) and abs(rows[-1][0].bbox[1] - seg.bbox[1]) < 0.6 * h:
            rows[-1].append(seg)
        else:
            rows.append([seg])
    return [sorted(r, key=lambda s: s.bbox[0]) for r in rows]


def _block(lines: list[list[Segment]]) -> LaidBlock:
    segs = [s for line in lines for s in line]
    text = " ".join(" ".join(s.text for s in line) for line in lines)
    return LaidBlock(
        bbox=union([s.bbox for s in segs]),
        text=re.sub(r"\s+", " ", text).strip(),
        bold_start=segs[0].spans[0].bold,
        line_height=median(s.height for s in segs),
    )
