"""One table cell split into its number, qualifier, dispersion and mean-separation letters."""

import re
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict

from sage.domain.normalize import norm

Qualifier = Literal["<", ">", "≤", "≥"]


class CellValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    numeric: float | None = None
    qualifier: Qualifier | None = None
    plus_minus: float | None = None
    parenthetical: float | None = None
    interval: tuple[float, float] | None = None
    interval_text: str | None = None
    letters: str | None = None
    missing: bool = False


_NUM = r"[-+]?(?:[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?|[.,]\d+)(?![eE][-+]?\d)"
"""A number as printed. One in exponent notation ("1.2e3") is not read, rather than read as its mantissa."""
_THOUSANDS = re.compile(r"[-+]?[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?")
_CELL = re.compile(
    rf"(?P<qual><=|>=|[<>≤≥])?\s*(?P<num>{_NUM})"
    rf"\s*(?:(?:±|\+/-|\+-)\s*(?P<pm>{_NUM}))?"
    rf"\s*(?P<iv>[\[(]\s*(?P<low>{_NUM})(?:,\s+|\s*;\s*|\s*-\s*|\s+to\s+)(?P<high>{_NUM})\s*[\])])?"
    rf"\s*(?:\(\s*(?P<paren>{_NUM})\s*\))?"
    r"\s*(?P<tail>.*)"
)
_QUALIFIERS: dict[str, Qualifier] = {"<": "<", ">": ">", "≤": "≤", "≥": "≥", "<=": "≤", ">=": "≥"}

_LATEX = re.compile(r"\\(?:mathrm|text|textrm)\{([^}]*)\}")
_LATEX_SUPERSCRIPT = re.compile(r"\^\{([^}]*)\}|\^")
_LATEX_NOISE = re.compile(r"[${}~]")
# Superscript digits right after a number are footnote calls; NFKC would otherwise glue them onto the number.
_SUPERSCRIPT_CALL = re.compile(r"(?<=[\d.])[\u00b9\u00b2\u00b3\u2070\u2074-\u2079]+")
_FOOTNOTE_MARKS = re.compile(r"[†‡§¶‖#]+")

_MISSING = frozenset({"", "-", "--", "---", "nd", "n.d.", "na", "n.a.", "n/a", "ns", "n.s.", "...", "…"})
_SIGNIFICANCE = re.compile(r"\s*(?P<sig>\*{1,3}|(?<![a-z])n\.?s\.?)$", re.IGNORECASE)
_LETTERS = re.compile(r"(?:^|\s)(?P<letters>[a-z]{1,4}|[A-Z]{1,4})$")
_UNIT_WORDS = frozenset(
    {"mm", "cm", "km", "ha", "mg", "kg", "ml", "mol", "kpa", "mpa", "ppm", "lb", "ac", "in", "ft", "yr", "min"}
)
_UNIT_PART = r"(?:[%‰]|°\s?[CF]?|[A-Za-z\u03bc][A-Za-z\u03bc.]*(?:-?\d)?)"
_UNIT_TAIL = re.compile(rf"{_UNIT_PART}(?:\s*/?\s*{_UNIT_PART})*")


def _number(token: str) -> float:
    """A numeric token as `_NUM` matches it: thousands commas ("1,234") dropped, a decimal comma ("12,5") read."""
    if _THOUSANDS.fullmatch(token):
        return float(token.replace(",", ""))
    return float(token.replace(",", "."))


def _clean(text: str) -> str:
    text = _LATEX.sub(r"\1", text).replace("\\pm", "±").replace("\\%", "%")
    text = _LATEX_SUPERSCRIPT.sub(r"\1", text)
    text = _FOOTNOTE_MARKS.sub("", _SUPERSCRIPT_CALL.sub("", text))
    return norm(_LATEX_NOISE.sub(" ", text))


class _Tail(NamedTuple):
    unit: str | None
    letters: str | None


def _split_tail(tail: str) -> _Tail | None:
    """What follows the numbers, or None when it is something other than a unit, letters and stars."""
    letters = None
    if match := _SIGNIFICANCE.search(tail):
        tail = tail[: match.start()].strip()
    if (match := _LETTERS.search(tail)) and match["letters"].lower() not in _UNIT_WORDS:
        letters = match["letters"]
        tail = tail[: match.start()].strip()
    if not tail:
        return _Tail(None, letters)
    if _UNIT_TAIL.fullmatch(tail):
        return _Tail(tail, letters)
    return None


def parse_cell(text: str) -> CellValue:
    """Every part a single reported value carries. Text that is not one value (a range, a sentence, several numbers)
    comes back with `numeric` None and `missing` False, so the caller can tell it from a stated blank."""
    cleaned = _clean(text)
    if cleaned.lower() in _MISSING:
        return CellValue(missing=True)
    match = _CELL.fullmatch(cleaned)
    tail = _split_tail(cleaned if match is None else match["tail"].strip())
    if match is None or tail is None:
        if match is None and tail is not None and tail.unit is None:
            return CellValue(letters=tail.letters)
        return CellValue()
    qual, low, high, pm, paren = match.group("qual", "low", "high", "pm", "paren")
    return CellValue(
        numeric=_number(match["num"]),
        qualifier=None if qual is None else _QUALIFIERS[qual],
        plus_minus=None if pm is None else _number(pm),
        parenthetical=None if paren is None else _number(paren),
        interval=None if low is None or high is None else (_number(low), _number(high)),
        interval_text=match["iv"],
        letters=tail.letters,
    )
