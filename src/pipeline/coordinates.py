"""
Geographic coordinates: the reported text stays the evidence, the decimal value is a documented transformation.

Protocol Section 6.2 asks for latitude/longitude; Section 11.1/11.2 say to store what the source reports and, when a
transformation is unavoidable, record the original, the formula and the rationale. Papers write coordinates as
degree-minute(-second) text ("45°42′ N", "36˚37´N", "-121˚32´W") -- none of which is a single number, so a decimal
value can never be quoted from the source. Before this module a model-computed decimal in `reported_numeric_value`
was (correctly) rejected by grounding, the field was left empty, and Site went unresolved (Philippe-2007-Six,
Kathryn-2020-Winter), blocking every Treatment and Observation of both papers.

Here the decimal is DERIVED DETERMINISTICALLY from the quoted text -- never taken from the model -- and stored in the
QuantityValue's existing conversion fields; `reported_text` keeps the literal the grounding check verifies.
"""

from __future__ import annotations

import re
from typing import Any, Optional

COORDINATE_FIELDS = {"latitude": "lat", "longitude": "lon"}
CONVERTED_UNITS = "decimal degrees"
CONVERSION_FORMULA = "degrees + minutes/60 + seconds/3600; negative for S or W (or a leading minus sign)"
CONVERSION_RATIONALE = (
    "The source reports the coordinate as text in degree/minute/second notation, not as one number; the decimal value "
    "is derived deterministically from reported_text by the pipeline (protocol Section 11.2), never quoted."
)

_CHAR_MAP = str.maketrans({
    "˚": "°", "º": "°",
    "′": "'", "´": "'", "ʹ": "'", "’": "'", "‘": "'",
    "″": '"', "“": '"', "”": '"',
    "−": "-", "–": "-", "—": "-", "‐": "-", "‑": "-",
})
_NUM = r"\d{1,3}(?:\.\d+)?"
_COORD_RE = re.compile(
    rf"""^\s*
    (?P<pre>[NSEW])?\s*
    (?P<sign>-)?\s*
    (?P<deg>{_NUM})\s*(?P<degmark>°)?\s*
    (?:(?P<min>\d{{1,2}}(?:\.\d+)?)\s*'(?!')\s*)?
    (?:(?P<sec>\d{{1,2}}(?:\.\d+)?)\s*(?:"|'')\s*)?
    (?P<post>[NSEW])?\s*$""",
    re.VERBOSE | re.IGNORECASE,
)


def parse_coordinate(text: str) -> Optional[dict[str, Any]]:
    """Parse one coordinate written as the source writes it. Returns {decimal, hemisphere, is_sexagesimal} or None
    when the text is not a single, well-formed coordinate (never a guess: a range, two coordinates, prose around it,
    minutes >= 60, both a prefix and a suffix hemisphere -- all None)."""
    if not isinstance(text, str):
        return None
    match = _COORD_RE.match(text.translate(_CHAR_MAP))
    if not match:
        return None
    pre, post = match.group("pre"), match.group("post")
    if pre and post:
        return None
    hemisphere = (pre or post or "").upper() or None
    minutes = float(match.group("min")) if match.group("min") else 0.0
    seconds = float(match.group("sec")) if match.group("sec") else 0.0
    if minutes >= 60 or seconds >= 60:
        return None
    if match.group("sec") and not match.group("min"):
        return None
    if not (match.group("degmark") or match.group("min") or hemisphere):
        return None          # a bare number is not recognisably a coordinate
    magnitude = float(match.group("deg")) + minutes / 60 + seconds / 3600
    negative = bool(match.group("sign")) or hemisphere in ("S", "W")
    return {
        "decimal": -magnitude if negative else magnitude,
        "hemisphere": hemisphere,
        "is_sexagesimal": bool(match.group("min") or match.group("sec")),
    }


def axis_problem(axis: str, parsed: dict[str, Any]) -> Optional[str]:
    """Why this parsed coordinate cannot be a value of `axis` ('lat' / 'lon'), or None."""
    hemisphere, value = parsed["hemisphere"], parsed["decimal"]
    if axis == "lat":
        if hemisphere in ("E", "W"):
            return f"hemisphere {hemisphere} is a longitude, not a latitude"
        if abs(value) > 90:
            return f"|{value}| exceeds 90 degrees"
    else:
        if hemisphere in ("N", "S"):
            return f"hemisphere {hemisphere} is a latitude, not a longitude"
        if abs(value) > 180:
            return f"|{value}| exceeds 180 degrees"
    return None


def _needs_conversion(parsed: dict[str, Any], reported_text: str) -> bool:
    """Only when the decimal is not already the number written: sexagesimal text, or a hemisphere letter carrying the
    sign ("121.5 W" is -121.5)."""
    if parsed["is_sexagesimal"]:
        return True
    written_negative = "-" in reported_text.translate(_CHAR_MAP)
    return (parsed["decimal"] < 0) != written_negative


def apply_coordinate_transformations(entity_type: str, payload: Any) -> Any:
    """For a Site payload: every latitude/longitude whose reported_text parses as one coordinate of the right axis
    gets its decimal derived here (converted_value + units + formula + rationale). A sexagesimal text has no single
    reported number, so a model-supplied `reported_numeric_value` is removed rather than trusted, and the degree sign
    the text actually carries becomes the reported unit. Anything that does not parse is left exactly as it was for the
    validators to judge. Returns the payload (mutated in place)."""
    if entity_type != "Site" or not isinstance(payload, dict):
        return payload
    for field_name, axis in COORDINATE_FIELDS.items():
        field = payload.get(field_name)
        if not isinstance(field, dict) or not isinstance(field.get("value"), dict):
            continue
        quantity = field["value"]
        reported_text = quantity.get("reported_text")
        parsed = parse_coordinate(reported_text)
        if parsed is None or axis_problem(axis, parsed):
            continue
        if not _needs_conversion(parsed, reported_text):
            continue
        if parsed["is_sexagesimal"]:
            quantity["reported_numeric_value"] = None
        if "°" in reported_text.translate(_CHAR_MAP):
            quantity["reported_units"] = "°"
        elif not quantity.get("reported_units"):
            quantity["reported_units"] = ""
        quantity["converted_value"] = round(parsed["decimal"], 6)
        quantity["converted_units"] = CONVERTED_UNITS
        quantity["conversion_formula"] = CONVERSION_FORMULA
        quantity["conversion_rationale"] = CONVERSION_RATIONALE
    return payload


_IN_TEXT_RE = re.compile(
    r"[NSEW]?\s*-?\d{1,3}(?:\.\d+)?\s*°\s*(?:\d{1,2}(?:\.\d+)?\s*'\s*)?(?:\d{1,2}(?:\.\d+)?\s*(?:\"|'')\s*)?[NSEW]?",
)


def coordinates_in_text(text: str) -> list[dict[str, Any]]:
    """Every single coordinate a block writes, parsed (degree sign required, so plain numbers never count)."""
    found = []
    for match in _IN_TEXT_RE.finditer((text or "").translate(_CHAR_MAP)):
        parsed = parse_coordinate(match.group(0).strip(" ,;()"))
        if parsed is not None:
            found.append({"text": match.group(0).strip(" ,;()"), **parsed})
    return found


def stated_equivalently(reported_text: str, block_texts: list[str]) -> bool:
    """Phase A6: is `reported_text` the same coordinate (same value AND same hemisphere) as one a cited block writes,
    only spelled differently -- "35°03' N" for the source's "35°3' N" (Paul-1998-Foliar b:0023, which left its Site
    unresolved and blocked Treatment and Observation)? A different coordinate is never equivalent."""
    parsed = parse_coordinate(reported_text)
    if parsed is None:
        return False
    for text in block_texts:
        for stated in coordinates_in_text(text):
            if abs(stated["decimal"] - parsed["decimal"]) < 1e-9 and stated["hemisphere"] == parsed["hemisphere"]:
                return True
    return False


def coordinate_issues(field_path: str, quantity: dict[str, Any]) -> list[tuple[str, str]]:
    """Validator side, for a latitude/longitude QuantityValue: (code, message) pairs. A converted value must be exactly
    what the reported text yields -- a transformed value is never accepted on the model's word -- and the text must be
    a coordinate of the field's own axis."""
    leaf = field_path.rsplit(".", 1)[-1]
    axis = COORDINATE_FIELDS.get(leaf)
    if axis is None or not isinstance(quantity, dict):
        return []
    parsed = parse_coordinate(quantity.get("reported_text"))
    issues: list[tuple[str, str]] = []
    if parsed is not None:
        problem = axis_problem(axis, parsed)
        if problem:
            issues.append(("coordinate_axis_mismatch", f"{field_path}: reported_text {quantity.get('reported_text')!r}: {problem}."))
    converted = quantity.get("converted_value")
    if converted is not None:
        if parsed is None:
            issues.append((
                "coordinate_conversion_unverifiable",
                f"{field_path}: converted_value={converted!r} but reported_text {quantity.get('reported_text')!r} is not "
                f"one parseable coordinate, so the conversion cannot be checked.",
            ))
        elif abs(float(converted) - parsed["decimal"]) > 1e-4:
            issues.append((
                "coordinate_conversion_mismatch",
                f"{field_path}: converted_value={converted!r} but reported_text {quantity.get('reported_text')!r} "
                f"converts to {round(parsed['decimal'], 6)}.",
            ))
    return issues
