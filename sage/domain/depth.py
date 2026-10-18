"""A soil sampling depth as printed, read into centimetres. The text decides, never a guess."""

import re

from sage.domain.normalize import norm
from sage.domain.units import convert
from sage.ir.schema import DepthRange

_NUM = r"\d+(?:\.\d+)?"
_UNIT = r"(?:mm|cm|m|in(?:ch(?:es)?)?\.?|ft|feet)"
_UNITS = {"inch": "in", "inches": "in", "in.": "in", "feet": "ft"}
_INTERVAL = re.compile(
    rf"(?<![\d.])(?P<top>{_NUM})\s*(?P<unit1>{_UNIT})?\s*(?:-|to)\s*(?P<bottom>{_NUM})[\s-]*(?P<unit>{_UNIT})(?![a-z])",
    re.IGNORECASE,
)
_TO = re.compile(
    rf"\b(?:to|top|upper|surface|first)\s+(?:a\s+depth\s+of\s+)?(?P<bottom>{_NUM})[\s-]*(?P<unit>{_UNIT})(?![a-z])",
    re.IGNORECASE,
)
"""A layer from the surface down: "to a depth of 30 cm", "the top 15 cm", "surface 10 cm"."""
_AT = re.compile(rf"(?<![\d.])(?P<bottom>{_NUM})[\s-]*(?P<unit>{_UNIT})(?![a-z])", re.IGNORECASE)
"""One depth: "at 30 cm"."""
_OPEN_BEFORE = re.compile(
    r"(?:\b(?:below|beneath|under|over|above|beyond|(?:deeper|shallower|greater|more|less) than)(?:\s+the)?"
    r"|[<>≤≥])\s*$",
    re.IGNORECASE,
)
_OPEN_AFTER = re.compile(r"^\s*(?:\+|(?:or|and) (?:more|deeper|below|beyond)\b|downwards?\b)", re.IGNORECASE)
"""Words that make one depth the end of an open layer ("below 30 cm", "> 30 cm", "30 cm and below")."""


def _cm(value: str, unit: str) -> float | None:
    unit = unit.lower()
    return convert(float(value), _UNITS.get(unit, unit.rstrip(".")), "cm")


def parse_depth(text: str) -> tuple[float, float] | None:
    """(top, bottom) in cm from the one depth `text` states: "0-15 cm", "15 to 30 cm", "the top 10 cm" or "30 cm".
    None when it states none, holds another number besides ("0-15 and 15-30 cm"), or leaves the layer open ("below
    30 cm"), so code never reads part of a list or one end of a layer as the whole of it."""
    cleaned = norm(text)
    for pattern in (_INTERVAL, _TO, _AT):
        found = list(pattern.finditer(cleaned))
        if not found:
            continue
        m = found[0]
        before, after = cleaned[: m.start()], cleaned[m.end() :]
        if len(found) > 1 or re.search(r"\d", before + after):
            return None
        if pattern is not _INTERVAL and (_OPEN_BEFORE.search(before) or _OPEN_AFTER.search(after)):
            return None
        bottom = _cm(m["bottom"], m["unit"])
        if pattern is _INTERVAL:
            top = _cm(m["top"], m["unit1"] or m["unit"])
        else:
            top = 0.0 if pattern is _TO else bottom
        if top is None or bottom is None or top > bottom:
            return None
        return top, bottom
    return None


def recompute_depth(d: DepthRange) -> DepthRange:
    """Code wins: the centimetres come from `reported_text`, and what it can't read is dropped."""
    parsed = parse_depth(d.reported_text)
    if parsed is None:
        return DepthRange(reported_text=d.reported_text)
    return DepthRange(reported_text=d.reported_text, top_cm=parsed[0], bottom_cm=parsed[1])


def depth_problem(d: DepthRange) -> str | None:
    if parse_depth(d.reported_text) is not None:
        return None
    return (
        f"code can't read one depth from '{d.reported_text}'; put one depth or layer with its unit in reported_text, "
        "e.g. '0-15 cm' or '30 cm' (a column header's unit goes after the cell's numbers). If the paper gives only "
        "one end of the layer ('below 30 cm'), set the value null with a note"
    )
