"""
`apply_reconstruction` kinds — Playbook Section 6: "date mapping,
stat-encoding conversion, factorial/aggregate-summary expansion (follows IR
spec Table 16 exactly for reported_effect_scope/aggregated_over_factors)".

These are pure transforms: given a raw shape an extraction agent pulled out
of a paper (e.g. "harvested 2007-06 to 2007-08", or a table row with mean/SE/
n columns, or a factorial ANOVA table), produce the corresponding IR-shaped
payload fragment. They do NOT talk to the store and do NOT run whole-graph
validation — `propose_record`/`commit_record` remain the only path that does,
per the "LLM proposes, deterministic Python validates" rule (Playbook
instruction this sprint). A reconstruction result is still just a candidate
payload the agent must then run through `propose_record`.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

_MONTH_RANGE_RE = re.compile(
    r"(?P<y1>\d{4})-(?P<m1>\d{2})(?:-(?P<d1>\d{2}))?\s*(?:to|-|–|—)\s*(?P<y2>\d{4})-(?P<m2>\d{2})(?:-(?P<d2>\d{2}))?"
)
_SINGLE_DATE_RE = re.compile(r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})")

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
        d2 = int(m.group("d2")) if m.group("d2") else 28
        try:
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
    """Convert a raw {label: value} mapping (as an agent would read off a
    table's column headers/row) into StatisticalSummary entries. Labels are
    normalized against a small alias table for IR-layer consistency, but
    this is NOT the BETYdb closed vocabulary -- that mapping happens at
    materialization (Playbook Section 5 / IR spec Section 6.5), never here.
    Unrecognized labels are kept verbatim rather than dropped (P1: never
    silently drop information)."""
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
