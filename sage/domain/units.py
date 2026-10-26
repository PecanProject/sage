"""Unit spelling and conversion for agronomy quantities, so derived values are computed by code, never the model."""

import math
import re
from collections import Counter

from sage.domain.cell_values import parse_cell
from sage.domain.normalize import norm
from sage.ir.schema import QuantityValue

type Dimensions = dict[str, int]

# Factor to SI per base unit, and its dimensions. Tokens that are not units ("N", "DM", "plants") are qualifiers:
# they must match on both sides, so "kg N ha-1" converts to "lb N ac-1" but never to "kg ha-1".
_BASE: dict[str, tuple[float, Dimensions]] = {
    "μg": (1e-9, {"mass": 1}),
    "mg": (1e-6, {"mass": 1}),
    "g": (1e-3, {"mass": 1}),
    "kg": (1.0, {"mass": 1}),
    "Mg": (1e3, {"mass": 1}),
    "t": (1e3, {"mass": 1}),
    "lb": (0.45359237, {"mass": 1}),
    "μm": (1e-6, {"length": 1}),
    "mm": (1e-3, {"length": 1}),
    "cm": (1e-2, {"length": 1}),
    "m": (1.0, {"length": 1}),
    "km": (1e3, {"length": 1}),
    "in": (0.0254, {"length": 1}),
    "ft": (0.3048, {"length": 1}),
    "ha": (1e4, {"length": 2}),
    "ac": (4046.8564224, {"length": 2}),
    "mL": (1e-6, {"length": 3}),
    "L": (1e-3, {"length": 3}),
    "s": (1.0, {"time": 1}),
    "min": (60.0, {"time": 1}),
    "h": (3600.0, {"time": 1}),
    "d": (86400.0, {"time": 1}),
    "%": (1e-2, {}),
    "‰": (1e-3, {}),
    "ppm": (1e-6, {}),
    "fraction": (1.0, {}),
}
_SYNONYMS = {
    "acre": "ac",
    "acres": "ac",
    "hectare": "ha",
    "hectares": "ha",
    "lbs": "lb",
    "pound": "lb",
    "pounds": "lb",
    "tonne": "t",
    "tonnes": "t",
    "percent": "%",
    "pct": "%",
    "day": "d",
    "days": "d",
    "hr": "h",
    "hour": "h",
    "hours": "h",
    "year": "yr",
    "years": "yr",
    "degc": "°C",
    "degf": "°F",
    "inch": "in",
    "inches": "in",
    "meter": "m",
    "metre": "m",
}
_CASED = {"l": "L", "ml": "mL"}
"""Matched on the exact spelling, since "ML" is megalitres, not millilitres."""
_TEMPERATURE = {"°C", "°F", "K"}
# Mapped before NFKC, which would turn the masculine ordinal into "o".
_DEGREE_SIGNS = str.maketrans({"\u00ba": "°", "\u02da": "°"})
_LOST_MINUS = re.compile(r"(?<=[A-Za-z])2\s+(?=[1-9]\b)")
"""Some journals' fonts put a superscript minus in the text layer as "2": "kg ha2 1" is kg ha⁻¹."""


def _token(raw: str) -> tuple[str, int]:
    """("m", 2) from "m2" and ("ha", -1) from "ha-1"; "P2O5" and "CO2" stay whole, since only a unit has a power."""
    stem = raw.rstrip("0123456789")
    power = raw[len(stem) :]
    base = stem.removesuffix("-")
    if len(power) != 1 or not base or (base == stem and base not in _BASE):
        return _synonym(raw), 1
    return _synonym(base), -int(power) if base != stem else int(power)


def _synonym(word: str) -> str:
    return _CASED.get(word) or _SYNONYMS.get(word.lower(), word)


def normalize_unit(text: str) -> str:
    """One spelling per unit: "g/m2", "g m⁻²" and "g m -2" are all "g m-2"; "Mg per ha" is "Mg ha-1"."""
    text = norm(text.translate(_DEGREE_SIGNS)).strip("()[]").strip()
    text = _LOST_MINUS.sub("-", text)
    text = re.sub(r"\^|\{|\}", "", text)
    text = re.sub(r"\s*[·⋅*]\s*", " ", text)
    text = re.sub(r"°\s+([CF])\b", r"°\1", text)
    text = re.sub(r"\s+per\s+", "/", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=[^\d\s])\s*-\s*(?=\d)", "-", text)
    tokens = []
    for i, part in enumerate(text.split("/")):
        for raw in part.split():
            base, exp = _token(raw)
            exp = -exp if i > 0 else exp
            tokens.append(base if exp == 1 else f"{base}{exp}")
    return " ".join(tokens)


def find_unit(text: str, unit: str) -> str | None:
    """The words in `text` that print `unit`, however they spell it ("kg ha2 1" for "kg ha-1"), so evidence can quote
    them; None when the text doesn't state it."""
    want = normalize_unit(unit)
    if not want:
        return None
    words = text.split()
    size = len(want.split())
    for width in range(size, size + 3):
        for i in range(len(words) - width + 1):
            stretch = " ".join(words[i : i + width]).strip(".,;:()[]")
            if normalize_unit(stretch) == want:
                return stretch
    return None


def is_unit(text: str) -> bool:
    """Whether text reads as a unit ("Mg ha-1", "%", "plants m-2"), not a name in brackets ("Greece", "DM"): at least
    one token is a known unit or carries a power."""
    for raw in normalize_unit(text).split():
        base, exp = _token(raw)
        if base in _BASE or base in _TEMPERATURE or base == "yr" or exp != 1:
            return True
    return False


def _decompose(unit: str) -> tuple[float, Counter[str], Counter[str]]:
    factor, dimensions, qualifiers = 1.0, Counter[str](), Counter[str]()
    for raw in normalize_unit(unit).split():
        base, exp = _token(raw)
        if base in _BASE:
            base_factor, base_dimensions = _BASE[base]
            factor *= math.pow(base_factor, exp)
            for dimension, power in base_dimensions.items():
                dimensions[dimension] += power * exp
        else:
            qualifiers[f"{base}{exp}"] += 1
    return factor, dimensions, qualifiers


def _temperature(value: float, from_unit: str, to_unit: str) -> float:
    kelvin = {"°C": value + 273.15, "°F": (value - 32) * 5 / 9 + 273.15, "K": value}[from_unit]
    return {"°C": kelvin - 273.15, "°F": (kelvin - 273.15) * 9 / 5 + 32, "K": kelvin}[to_unit]


def _factor(from_unit: str, to_unit: str) -> float | None:
    from_factor, from_dimensions, from_qualifiers = _decompose(from_unit)
    to_factor, to_dimensions, to_qualifiers = _decompose(to_unit)
    if from_dimensions != to_dimensions or from_qualifiers != to_qualifiers:
        return None
    return from_factor / to_factor


def _tidy(value: float) -> float:
    return float(f"{value:.12g}")


def convert(value: float, from_unit: str, to_unit: str) -> float | None:
    """`value` in `to_unit`, or None when the two are not the same kind of quantity or a unit is unknown."""
    source, target = normalize_unit(from_unit), normalize_unit(to_unit)
    if source in _TEMPERATURE or target in _TEMPERATURE:
        if source in _TEMPERATURE and target in _TEMPERATURE:
            return _tidy(_temperature(value, source, target))
        return None
    factor = _factor(source, target)
    return None if factor is None else _tidy(value * factor)


def conversion_formula(from_unit: str, to_unit: str) -> str | None:
    source, target = normalize_unit(from_unit), normalize_unit(to_unit)
    if source in _TEMPERATURE and target in _TEMPERATURE:
        offset = _temperature(0, source, target)
        scale = _temperature(1, source, target) - offset
        return f"[{target}] = [{source}] * {scale:.12g} + {offset:.12g}"
    factor = _factor(source, target)
    return None if factor is None else f"[{target}] = [{source}] * {factor:.12g}"


def number_text(q: QuantityValue) -> str:
    """`reported_text` without its unit as `reported_units` spells it, so the unit's digits ("m-2") aren't numbers."""
    unit = q.reported_units.strip()
    return q.reported_text.replace(unit, " ") if unit else q.reported_text


def reported_number(q: QuantityValue) -> float | None:
    """The one exact number `reported_text` holds, or None: a range, a "<" or ">" bound, several numbers or prose."""
    for text in (q.reported_text, number_text(q)):
        cell = parse_cell(text)
        if cell.numeric is not None:
            return cell.numeric if cell.qualifier is None else None
    return None


def recompute_quantity(q: QuantityValue) -> QuantityValue:
    """Code wins on the arithmetic: `reported_numeric_value` is re-read from `reported_text`, and dropped when the
    text holds no one exact number. A conversion to `converted_units` is computed by code. A value code cannot compute
    is dropped, so the model's arithmetic never stands; `conversion_problem` tells the agent why."""
    value = reported_number(q)
    update: dict[str, float | str | None] = {"reported_numeric_value": value}
    if q.converted_units is not None:
        converted = None if value is None else convert(value, q.reported_units, q.converted_units)
        formula = conversion_formula(q.reported_units, q.converted_units)
        if converted is not None and formula is not None:
            update["converted_value"] = converted
            update["conversion_formula"] = formula
            update["conversion_rationale"] = q.conversion_rationale or (
                f"{q.reported_units} converted to {q.converted_units} by code"
            )
        else:
            update["converted_value"] = update["conversion_formula"] = None
    return QuantityValue.model_validate(q.model_dump() | update)


def conversion_problem(q: QuantityValue) -> str | None:
    """Why code can't convert `q` to its `converted_units`, or None when it can or no conversion is asked for."""
    if q.converted_units is None:
        return None
    if q.reported_numeric_value is None:
        return (
            f"'{q.reported_text}' doesn't hold one number, so code can't convert it; remove converted_units, or "
            "write a value with one number"
        )
    if convert(q.reported_numeric_value, q.reported_units, q.converted_units) is None:
        return (
            f"code can't convert '{q.reported_units}' to '{q.converted_units}': they measure different things or a "
            "unit is one code doesn't know. Fix the units spelling, {text: …, units: kg ha-1}, or remove "
            "converted_units"
        )
    return None
