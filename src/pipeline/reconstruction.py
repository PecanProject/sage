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
