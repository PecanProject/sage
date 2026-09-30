"""Parse a table cell into mean, dispersion or interval, and mean-separation letters. The statistic is named only
when the table's header, caption or notes state it; otherwise the dispersion stays unnamed."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

_NUM = r"[-−–]?\d+(?:[.,]\d+)?"
_CELL_RE = re.compile(
    rf"^\s*\$?\s*(?P<mean>{_NUM})\s*"
    rf"(?:(?:±|\\pm|\+/-|\+-)\s*\{{?\s*\}}?\s*(?P<disp>{_NUM})\s*)?"
    rf"(?:\[\s*(?P<low>{_NUM})\s*,\s*(?P<high>{_NUM})\s*\]\s*)?"
    rf"(?:\(\s*(?P<paren>{_NUM})\s*\)\s*)?"
    r"\$?\s*(?P<letters>[A-Za-z]{1,4})?\s*$"
)


@dataclass
class CellValue:
    text: str
    mean_text: str
    mean: float
    dispersion: Optional[float] = None
    interval: Optional[tuple[float, float]] = None
    parenthetical: Optional[float] = None
    letters: Optional[str] = None


_UNIT_TOKENS = frozenset("mm cm m km g kg mg ha l ml mol pa kpa d h s".split())


def _num(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    return float(text.replace("−", "-").replace("–", "-").replace(",", "."))


def parse_cell(text: str) -> Optional[CellValue]:
    """One cell's parts, or None when it is not a single reported value (a range, a sentence, several numbers)."""
    if not isinstance(text, str):
        return None
    cleaned = re.sub(r"\\mathrm\{[^}]*\}|~|\$|\{|\}", " ", text).replace("\\pm", "±")
    match = _CELL_RE.match(" ".join(cleaned.split()))
    if not match:
        return None
    letters = match.group("letters")
    if letters and letters.lower() in _UNIT_TOKENS:
        letters = None   # "88±22 mm": a unit, not a mean-separation letter
    return CellValue(
        text=text.strip(), mean_text=match.group("mean"), mean=_num(match.group("mean")),
        dispersion=_num(match.group("disp")),
        interval=(_num(match.group("low")), _num(match.group("high"))) if match.group("low") else None,
        parenthetical=_num(match.group("paren")), letters=letters,
    )


_STATISTIC_NAMES = (
    ("SE", re.compile(r"±\s*s\.?e\.?(?:m\.?)?\b|\bs\.?e\.?m?\b|standard errors?", re.I)),
    ("SD", re.compile(r"±\s*s\.?d\.?\b|\bs\.?d\.?\b|standard deviations?", re.I)),
    ("95% CI", re.compile(r"95\s*%\s*(?:confidence (?:intervals?|limits?)|c\.?i\.?)", re.I)),
    ("CI", re.compile(r"confidence (?:intervals?|limits?)", re.I)),
)
_LETTERS_MEANING_RE = re.compile(r"(?:different|same) letters?[^.]{0,120}(?:significant|differ)", re.I)


@dataclass
class StatisticBasis:
    name: Optional[str]            # SE | SD | 95% CI | CI | None (unnamed)
    source_anchor: Optional[str]   # where the table states it
    source_text: Optional[str]
    letters_meaning: Optional[str] = None
    letters_anchor: Optional[str] = None


def statistic_basis(sources: Iterable[tuple[str, str]], cell: CellValue) -> StatisticBasis:
    """What the cell's dispersion/interval is, from the (anchor, text) of the table's header, caption and notes -- the
    first explicit statement wins; an interval pairs only with an interval name, a "±" only with SE/SD."""
    sources = list(sources)
    name = anchor = text = None
    wanted = ("95% CI", "CI") if cell.interval else ("SE", "SD") if cell.dispersion is not None or cell.parenthetical is not None else ()
    for statistic, pattern in _STATISTIC_NAMES:
        if statistic not in wanted:
            continue
        for src_anchor, src_text in sources:
            match = pattern.search(src_text or "")
            if match:
                name, anchor, text = statistic, src_anchor, match.group(0)
                break
        if name:
            break
    letters_meaning = letters_anchor = None
    if cell.letters:
        for src_anchor, src_text in sources:
            match = _LETTERS_MEANING_RE.search(src_text or "")
            if match:
                letters_meaning, letters_anchor = match.group(0), src_anchor
                break
    return StatisticBasis(name, anchor, text, letters_meaning, letters_anchor)
