"""`apply_reconstruction` kinds (date mapping, stat encoding, factorial expansion): pure transforms to IR-shaped
fragments; the result is still a candidate that must pass `propose_record`."""

from __future__ import annotations

import calendar
import re
from datetime import date
from typing import Any, Optional

_MONTH_RANGE_RE = re.compile(
    r"(?P<y1>\d{4})-(?P<m1>\d{2})(?:-(?P<d1>\d{2}))?\s*(?:to|-|–|—)\s*(?P<y2>\d{4})-(?P<m2>\d{2})(?:-(?P<d2>\d{2}))?"
)
_SINGLE_DATE_RE = re.compile(r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})")

# Written dates ("9 June 1993", "June 9, 1993", "15-24 June 1981", "June 1993"). Every pattern
# REQUIRES a four-digit year: a day and month with no year ("9 June") is never completed with a
# guessed year -- the caller supplies the year from where the source states it.
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_DAY = r"(?P<{n}>\d{{1,2}})(?:st|nd|rd|th)?"
_WRITTEN_PATTERNS = [
    ("range_months", re.compile(rf"\b{_DAY.format(n='d1')}\s+(?P<m1>{_MONTH})\s*(?:to|[-–—])\s*{_DAY.format(n='d2')}\s+(?P<m2>{_MONTH}),?\s+(?P<y>\d{{4}})\b", re.I)),
    ("range_days", re.compile(rf"\b{_DAY.format(n='d1')}\s*[-–—]\s*{_DAY.format(n='d2')}\s+(?P<m1>{_MONTH}),?\s+(?P<y>\d{{4}})\b", re.I)),
    ("day_month_year", re.compile(rf"\b{_DAY.format(n='d1')}\s+(?P<m1>{_MONTH}),?\s+(?P<y>\d{{4}})\b", re.I)),
    ("month_day_year", re.compile(rf"\b(?P<m1>{_MONTH})\s+{_DAY.format(n='d1')},?\s+(?P<y>\d{{4}})\b", re.I)),
    ("month_year", re.compile(rf"\b(?P<m1>{_MONTH}),?\s+(?P<y>\d{{4}})\b", re.I)),
]


def _month_number(name: str) -> int:
    return _MONTHS[name.lower().rstrip(".")[:3]]


def _month_end(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _parse_written_dates(text: str) -> Optional[tuple[date, date]]:
    """(earliest, latest) for a text holding exactly ONE written date or date range with a year, else
    None -- several separate dates ("9 June (vegetative), 19 July (elongating)") are ambiguous and are
    never collapsed into one. A month with no day spans the whole month (protocol Section 10.1)."""
    found = []
    for name, pattern in _WRITTEN_PATTERNS:
        for m in pattern.finditer(text):
            found.append((m.start(), m.end(), name, m))
    # a longer/earlier pattern swallows any shorter one inside its own span (e.g. "June 9, 1993" contains "June 9")
    found.sort(key=lambda f: (f[0], -(f[1] - f[0])))
    spans: list[tuple[int, int, str, Any]] = []
    for f in found:
        if not any(f[0] >= s[0] and f[1] <= s[1] for s in spans):
            spans.append(f)
    if len(spans) != 1:
        return None
    _, _, name, m = spans[0]
    year = int(m.group("y"))
    try:
        if name == "range_months":
            m1, m2 = _month_number(m.group("m1")), _month_number(m.group("m2"))
            return date(year, m1, int(m.group("d1"))), date(year, m2, int(m.group("d2")))
        if name == "range_days":
            month = _month_number(m.group("m1"))
            return date(year, month, int(m.group("d1"))), date(year, month, int(m.group("d2")))
        if name in ("day_month_year", "month_day_year"):
            d = date(year, _month_number(m.group("m1")), int(m.group("d1")))
            return d, d
        month = _month_number(m.group("m1"))
        return date(year, month, 1), date(year, month, _month_end(year, month))
    except ValueError:
        return None


_RELATIVE_TERMS = {
    "before planting": "before_planting",
    "after harvest": "after_harvest",
    "at planting": "at_planting",
    "at harvest": "at_harvest",
    "pre-planting": "before_planting",
    "post-harvest": "after_harvest",
}


def reconstruct_date_mapping(reported_text: str) -> dict[str, Any]:
    """Map a raw reported date/date-range string onto DateRange's shape.
    Deterministic parsing only -- never guesses a date that isn't literally
    parseable from the string; anything it can't parse is returned with
    earliest/latest/relative_timing all None so the caller (propose_record,
    ultimately construction-time validation) surfaces it as needing
    UNRESOLVED with a reason, not a fabricated guess."""
    text = reported_text.strip()
    lowered = text.lower()

    for phrase, token in _RELATIVE_TERMS.items():
        if phrase in lowered:
            return {
                "earliest": None,
                "latest": None,
                "reported_text": text,
                "relative_timing": token,
                "relative_timing_days": None,
            }

    m = _MONTH_RANGE_RE.search(text)
    if m:
        d1 = int(m.group("d1")) if m.group("d1") else 1
        try:
            # no end day given -> the last day of that month
            d2 = int(m.group("d2")) if m.group("d2") else _month_end(int(m.group("y2")), int(m.group("m2")))
            earliest = date(int(m.group("y1")), int(m.group("m1")), d1)
            latest = date(int(m.group("y2")), int(m.group("m2")), d2)
        except ValueError:
            earliest = latest = None
        return {
            "earliest": earliest.isoformat() if earliest else None,
            "latest": latest.isoformat() if latest else None,
            "reported_text": text,
            "relative_timing": None,
            "relative_timing_days": None,
        }

    m = _SINGLE_DATE_RE.search(text)
    if m:
        try:
            d = date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
            return {
                "earliest": d.isoformat(),
                "latest": d.isoformat(),
                "reported_text": text,
                "relative_timing": None,
                "relative_timing_days": None,
            }
        except ValueError:
            pass

    written = _parse_written_dates(text)
    if written is not None:
        return {
            "earliest": written[0].isoformat(),
            "latest": written[1].isoformat(),
            "reported_text": text,
            "relative_timing": None,
            "relative_timing_days": None,
        }

    return {
        "earliest": None,
        "latest": None,
        "reported_text": text,
        "relative_timing": None,
        "relative_timing_days": None,
    }


_STAT_ALIASES = {
    "std err": "SE",
    "stderr": "SE",
    "se": "SE",
    "std dev": "SD",
    "stdev": "SD",
    "sd": "SD",
    "95% ci": "95%CI",
    "95%ci": "95%CI",
    "n": "n",
    "mse": "MSE",
    "lsd": "LSD",
    "msd": "MSD",
}


def reconstruct_stat_encoding(raw_stats: dict[str, Any]) -> dict[str, Any]:
    """{label: value} -> StatisticalSummary entries; labels are alias-normalised (not the BETYdb vocabulary) and
    unrecognised labels are kept verbatim."""
    entries = []
    for raw_label, value in raw_stats.items():
        norm = _STAT_ALIASES.get(raw_label.strip().lower(), raw_label.strip())
        entries.append({"statistic_name": norm, "statistic_value": value})
    return {"entries": entries}


def reconstruct_factorial_expansion(
    factor_levels: dict[str, list[str]],
    aggregated: bool,
    aggregated_factor_names: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Expand a factorial design table into per-cell reported_effect_scope /
    aggregated_over_factors payload fragments, following IR spec Table 16
    exactly:
      - treatment_mean cells -> aggregated_over_factors = EXTRACTED, []
      - aggregated_mean cells -> aggregated_over_factors = EXTRACTED with the
        collapsed factor name(s), or left for the caller to mark UNRESOLVED
        with a reason if the factor(s) collapsed aren't stated.

    `factor_levels` is e.g. {"nitrogen_rate": ["0", "100", "200"], "tillage":
    ["conventional", "no-till"]} -- used only to enumerate the treatment-mean
    cells; this function does not itself decide which specific cells exist
    in the source table (that's an extraction fact, not a reconstruction
    rule) -- it returns the shape for however many cells the caller asks
    the aggregate view to represent.
    """
    if not aggregated:
        return {
            "reported_effect_scope": "treatment_mean",
            "aggregated_over_factors": {"provenance_label": "EXTRACTED", "value": []},
        }
    if not aggregated_factor_names:
        return {
            "reported_effect_scope": "aggregated_mean",
            "aggregated_over_factors": None,  # caller must fill UNRESOLVED + reason
            "note": "aggregated_mean requires aggregated_over_factors -- caller must supply factor "
            "names as EXTRACTED or mark UNRESOLVED with a reason; never leave absent.",
        }
    return {
        "reported_effect_scope": "aggregated_mean",
        "aggregated_over_factors": {
            "provenance_label": "EXTRACTED",
            "value": aggregated_factor_names,
        },
    }


RECONSTRUCTION_KINDS = {
    "date_mapping": reconstruct_date_mapping,
    "stat_encoding": reconstruct_stat_encoding,
    "factorial_expansion": reconstruct_factorial_expansion,
}
