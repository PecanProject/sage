"""Latitude and longitude: `reported_text` keeps the source's notation; the signed decimal is derived from it here."""

import re
from typing import Literal, NamedTuple

from sage.domain.normalize import norm
from sage.ir.schema import QuantityValue

Axis = Literal["lat", "lon"]

CONVERTED_UNITS = "decimal degrees"
CONVERSION_FORMULA = "degrees + minutes/60 + seconds/3600; negative for S or W or a leading minus sign"
CONVERSION_RATIONALE = (
    "reported_text gives the coordinate in degree/minute/second notation or signs it with a hemisphere letter, so "
    "code derives the signed decimal degrees from it"
)

_HEMISPHERES: dict[Axis, str] = {"lat": "NS", "lon": "EW"}
_LIMITS: dict[Axis, float] = {"lat": 90, "lon": 180}
_AXIS_NAMES: dict[Axis, str] = {"lat": "latitude", "lon": "longitude"}

# Mapped before NFKC, which would turn the masculine ordinal into "o" and split the double prime.
_MARKS = str.maketrans({0x02DA: "°", 0x00BA: "°", 0x2032: "'", 0x02B9: "'", 0x00B4: "'", 0x2033: '"', 0x02BA: '"'})
_LABEL = re.compile(r"^(?:lat(?:itude)?|lon(?:g(?:itude)?)?)\.?\s*[:=]?\s*", re.IGNORECASE)
_WORDS = re.compile(r"\b(north|south|east|west)\b", re.IGNORECASE)
_COORDINATE = re.compile(
    r"""(?P<pre>[NSEW])?\s*
    (?P<sign>[-+])?\s*
    (?P<deg>\d{1,3}(?:\.\d+)?)\s*(?P<mark>°)?\s*
    (?:(?P<min>\d{1,2}(?:\.\d+)?)\s*'(?!')\s*)?
    (?:(?P<sec>\d{1,2}(?:\.\d+)?)\s*(?:"|'')\s*)?
    (?P<post>[NSEW])?""",
    re.VERBOSE | re.IGNORECASE,
)


class _Coordinate(NamedTuple):
    decimal: float
    written: float
    hemisphere: str | None
    sexagesimal: bool
    degree_mark: bool


def _parse(text: str) -> _Coordinate | None:
    """One coordinate as written, or None for anything else: a range, two coordinates, prose around it, minutes or
    seconds of 60 or more, a hemisphere both before and after, a sign its hemisphere contradicts. Never a guess."""
    text = _LABEL.sub("", norm(text.translate(_MARKS)))
    text = _WORDS.sub(lambda m: m[1][0].upper(), text)
    match = _COORDINATE.fullmatch(text)
    if match is None or (match["pre"] and match["post"]):
        return None
    if (match["sec"] and not match["min"]) or ("." in match["deg"] and match["min"]):
        return None
    minutes = float(match["min"] or 0)
    seconds = float(match["sec"] or 0)
    if minutes >= 60 or seconds >= 60:
        return None
    hemisphere = (match["pre"] or match["post"] or "").upper() or None
    if match["sign"] and hemisphere is not None and (match["sign"] == "-") != (hemisphere in ("S", "W")):
        return None
    magnitude = float(match["deg"]) + minutes / 60 + seconds / 3600
    negative = match["sign"] == "-" or hemisphere in ("S", "W")
    return _Coordinate(
        decimal=round(-magnitude if negative else magnitude, 6),
        written=-float(match["deg"]) if match["sign"] == "-" else float(match["deg"]),
        hemisphere=hemisphere,
        sexagesimal=bool(match["min"]),
        degree_mark=bool(match["mark"]),
    )


def _axis_problem(coordinate: _Coordinate, axis: Axis) -> str | None:
    hemisphere = coordinate.hemisphere
    if hemisphere is not None and hemisphere not in _HEMISPHERES[axis]:
        other: Axis = "lon" if axis == "lat" else "lat"
        return f"hemisphere {hemisphere} is a {_AXIS_NAMES[other]}, not a {_AXIS_NAMES[axis]}"
    if abs(coordinate.decimal) > _LIMITS[axis]:
        return f"|{coordinate.decimal}| exceeds {_LIMITS[axis]:g} degrees"
    return None


def coordinate_problem(text: str, axis: Axis) -> str | None:
    """Why `text` is not one value of `axis`, or None when it is."""
    coordinate = _parse(text)
    if coordinate is None:
        return f"{text!r} is not one coordinate (degrees, optional minutes and seconds, optional N/S/E/W)"
    return _axis_problem(coordinate, axis)


def recompute_coordinate(q: QuantityValue, axis: Axis) -> QuantityValue:
    """Derive the decimal from `reported_text`, overwriting whatever the model computed. A conversion is recorded
    only when the decimal is not the number written (sexagesimal text, or a hemisphere letter carrying the sign).
    When the text is not one coordinate, any number the model worked out is dropped; `coordinate_problem` says why."""
    coordinate = _parse(q.reported_text)
    if coordinate is None or _axis_problem(coordinate, axis) is not None:
        dropped = dict.fromkeys(
            (
                "reported_numeric_value",
                "converted_value",
                "converted_units",
                "conversion_formula",
                "conversion_rationale",
            )
        )
        return QuantityValue.model_validate(q.model_dump() | dropped)
    converted = coordinate.sexagesimal or coordinate.decimal != coordinate.written
    return QuantityValue.model_validate(
        q.model_dump()
        | {
            "reported_numeric_value": None if coordinate.sexagesimal else coordinate.written,
            "reported_units": "°" if coordinate.degree_mark else q.reported_units,
            "converted_value": coordinate.decimal if converted else None,
            "converted_units": CONVERTED_UNITS if converted else None,
            "conversion_formula": CONVERSION_FORMULA if converted else None,
            "conversion_rationale": CONVERSION_RATIONALE if converted else None,
        }
    )
