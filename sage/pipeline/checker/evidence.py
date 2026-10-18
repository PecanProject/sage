"""Evidence level: anchors exist, quotes are verbatim, and the stated value (its number, unit, statistic, date or words)
is in the quotes."""

from difflib import SequenceMatcher

from sage.domain.normalize import contains, contains_loose, count_in, norm, number_in, numbers_in, squash
from sage.domain.stats import replicate_level, scope_reason, scope_statement
from sage.domain.units import normalize_unit, number_text
from sage.ir.schema import (
    FIELD_REFERENCES,
    DateRange,
    DepthRange,
    Entity,
    Field,
    Observation,
    QuantityValue,
    StatisticalSummary,
    fields_of,
)
from sage.parse.document import ANCHOR, Document
from sage.pipeline.checker.issues import Issue, blocking, warning

UNITLESS = {"", "-", "unitless", "dimensionless", "none", "n/a", "ratio", "index", "count", "number"}


def check_evidence(record: Entity, doc: Document) -> list[Issue]:
    rid = record.id
    issues: list[Issue] = []
    for name, f in fields_of(record).items():
        target = f"{rid}.{name}"
        issues += _field_shape(target, f)
        quotes: list[str] = []
        for ev in f.evidence:
            resolved = doc.resolve(ev.anchor)
            if resolved is None:
                hint = (
                    "that anchor doesn't exist in this paper; copy one from the paper view"
                    if ANCHOR.match(ev.anchor)
                    else "anchors look like p3.b4 (block) or p7.t1.r2.c3 (table cell)"
                )
                issues.append(blocking("evidence", target, f"anchor '{ev.anchor}' not found; {hint}"))
                continue
            if not ev.quote.strip():
                issues.append(blocking("evidence", target, f"the quote for {ev.anchor} is empty; copy the words there"))
                continue
            if not contains(resolved.text, ev.quote):
                issues.append(
                    blocking(
                        "evidence",
                        target,
                        f"quote '{_short(ev.quote)}' not found in {ev.anchor}; the text there is "
                        f"'{_closest(resolved.text, ev.quote)}'. Copy the quote from it, or cite the anchor that "
                        "holds the words",
                    )
                )
                continue
            quotes.append(ev.quote)
        if f.value is not None and f.basis == "stated" and quotes:
            issues += _stated_content(target, f.value, quotes, words=_holds_words(record, name, f))
    if isinstance(record, Observation):
        issues += _observation_scope(record)
    return issues


def _observation_scope(o: Observation) -> list[Issue]:
    """What a value is a mean of is a claim like any other: a stated or derived scope, factor list or replicate level
    needs a quote that says it. Code derives the replicate level and the empty factor list where the text says them,
    so what is left here is the agent's own claim."""
    issues: list[Issue] = []
    scope, agg = o.reported_effect_scope, o.aggregated_over_factors
    factors = agg.value if agg is not None and agg.value is not None else []
    claimed = scope.basis != "inferred" and bool(scope.evidence)
    if claimed and scope.value is not None and scope_statement(scope.value, factors, scope.evidence) is None:
        reason = scope_reason(scope.value, factors, scope.evidence)
        issues.append(
            blocking(
                "evidence",
                f"{o.id}.reported_effect_scope",
                f"no quote says these are {scope.value} values: {reason}. Cite the text that says what they are means "
                "of, or use basis inferred and say why in note",
            )
        )
        if scope.value == "treatment_mean" and agg is not None:
            issues.append(
                blocking(
                    "evidence",
                    f"{o.id}.aggregated_over_factors",
                    "[] follows from reported_effect_scope, which no quote supports; fix that field",
                )
            )
    pooled = factors if agg is not None and agg.basis != "inferred" and agg.evidence else []
    if pooled and agg is not None and scope_statement("aggregated_mean", pooled, agg.evidence) is None:
        reason = scope_reason("aggregated_mean", pooled, agg.evidence)
        issues.append(
            blocking(
                "evidence",
                f"{o.id}.aggregated_over_factors",
                f"no quote says the values are averaged over {', '.join(pooled)}: {reason}. Cite that text, or use "
                "basis inferred",
            )
        )
    level = o.is_raw_replicate_level
    if level.value is not None and level.basis != "inferred" and o.value.value is not None:
        cited = [e for name, f in fields_of(o).items() if name != "value" for e in f.evidence]
        found = replicate_level(o.value.value, o.value.evidence, cited)
        if found is None or found.raw != level.value:
            kind = "individual replicate values" if level.value else "means"
            issues.append(
                blocking(
                    "evidence",
                    f"{o.id}.is_raw_replicate_level",
                    f"nothing cited says these are {kind}: code looks for a ± or letters in the value, 'individual "
                    "plot' or 'replicate' wording, 'mean' or 'average', or a P value in the value's quote. Cite that "
                    "text, or use basis inferred and say why in note",
                )
            )
    return issues


def _holds_words(record: Entity, name: str, f: Field[object]) -> bool:
    """A free-text Field[str], such as a name or a cultivar; not a Literal like an effect scope, nor an id."""
    declared = type(f).__pydantic_generic_metadata__["args"]
    return declared == (str,) and name not in FIELD_REFERENCES.get(record.type, {})


def _field_shape(target: str, f: Field[object]) -> list[Issue]:
    if f.value is None:
        if f.note is None or not f.note.strip():
            return [blocking("evidence", target, "value is null; say in note why the paper doesn't give it")]
        return []
    if f.basis in ("stated", "derived") and not f.evidence:
        return [blocking("evidence", target, f"basis is {f.basis} but there is no evidence; cite anchor + quote")]
    if f.basis == "inferred" and (f.note is None or not f.note.strip()):
        return [blocking("evidence", target, "basis is inferred; say in note what it is inferred from")]
    return []


def _stated_content(target: str, value: object, quotes: list[str], *, words: bool) -> list[Issue]:
    if isinstance(value, bool):
        return []
    joined = " ".join(quotes)
    issues: list[Issue] = []
    if isinstance(value, str):
        if words and not any(contains_loose(q, value) for q in quotes):
            issues.append(
                blocking(
                    "evidence",
                    target,
                    f"'{_short(value)}' isn't in any quote of this field. If it is your own wording (a summary, a "
                    "name or a note), keep it and set basis derived, citing the sentences it sums up; don't swap in a "
                    "shorter span that says less. If the paper prints it, quote the words as printed",
                )
            )
        return issues
    if isinstance(value, int):
        if not count_in(joined, value):
            issues.append(_missing_number(target, value))
    elif isinstance(value, float):
        if not number_in(joined, value):
            issues.append(_missing_number(target, value))
    elif isinstance(value, QuantityValue):
        issues.extend(_missing_number(target, n) for n in _claimed_numbers(value) if not number_in(joined, n))
        unit = value.reported_units.strip()
        if unit.lower() not in UNITLESS and not _unit_in(joined, unit):
            issues.append(
                blocking(
                    "evidence",
                    target,
                    f"the unit '{unit}' isn't in any quote; also cite the anchor that states it "
                    "(a column header, the variable label or the methods sentence)",
                )
            )
    elif isinstance(value, DateRange | DepthRange):
        what = "date" if isinstance(value, DateRange) else "depth"
        if not any(contains_loose(q, value.reported_text) for q in quotes):
            issues.append(
                blocking(
                    "evidence",
                    target,
                    f"the {what} '{_short(value.reported_text)}' isn't in any quote of this field; put it in "
                    "reported_text exactly as printed and quote the text that holds it",
                )
            )
    elif isinstance(value, StatisticalSummary):
        if isinstance(value.statistic_value, float) and not number_in(joined, value.statistic_value):
            issues.append(_missing_number(target, value.statistic_value))
        name = value.statistic_name.split("(")[0].strip()
        if name and not contains_loose(joined, name):
            issues.append(
                warning("evidence", target, f"'{name}' isn't in the quotes; cite where the paper names the statistic")
            )
    return issues


def _claimed_numbers(q: QuantityValue) -> list[float]:
    """The numbers a quantity states: its one number, or, when it has none ("<0.5", "250 to 300"), every number its
    text writes outside the unit."""
    if q.reported_numeric_value is not None:
        return [q.reported_numeric_value]
    out: list[float] = []
    for token in numbers_in(number_text(q)):
        try:
            out.append(float(token.replace(",", ".")))
        except ValueError:
            continue
    return out


def _missing_number(target: str, value: float) -> Issue:
    shown = int(value) if float(value).is_integer() else value
    return blocking(
        "evidence", target, f"the number {shown} isn't in any quote of this field; quote the text that states it"
    )


def _unit_in(text: str, unit: str) -> bool:
    if contains(text, unit):
        return True
    want = squash(normalize_unit(unit))
    return bool(want) and want in squash(normalize_unit(text))


def _short(text: str, limit: int = 80) -> str:
    text = norm(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _closest(text: str, quote: str, width: int = 120) -> str:
    """The stretch of `text` most like `quote`, so the model can copy the right words."""
    text = norm(text)
    if len(text) <= width:
        return text
    m = SequenceMatcher(None, text, norm(quote), autojunk=False).find_longest_match()
    start = max(0, m.a - (width - m.size) // 2)
    return ("…" if start else "") + text[start : start + width] + ("…" if start + width < len(text) else "")
