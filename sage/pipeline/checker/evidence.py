"""Evidence level: anchors exist, pinned quotes are verbatim, and the stated value (its number, unit, statistic, date or
words) is in the cited text."""

from difflib import SequenceMatcher

from sage.domain.normalize import contains, contains_loose, count_in, norm, number_in, numbers_in, read_number, squash
from sage.domain.stats import named_replicate, replicate_level, scope_reason, scope_statement
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

UNITLESS = {"-", "unitless", "dimensionless", "none", "n/a", "ratio", "index", "count", "number"}


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
                    "copy one from paper.md"
                    if ANCHOR.match(ev.anchor)
                    else "anchors look like p3.b4 (block) or p7.t1.r2.c3 (table cell)"
                )
                issues.append(blocking("evidence", target, f"there is no anchor '{ev.anchor}' in this paper; {hint}"))
                continue
            if not ev.quote.strip():
                issues.append(
                    blocking(
                        "evidence",
                        target,
                        f"the pinned quote for {ev.anchor} is empty; pin the exact words, "
                        f'{{{ev.anchor}: "the words"}}, or cite plain {ev.anchor}',
                    )
                )
                continue
            if not contains(resolved.text, ev.quote):
                issues.append(
                    blocking(
                        "evidence",
                        target,
                        f"the pinned quote '{_short(ev.quote)}' isn't in {ev.anchor}; the text there is "
                        f"'{_closest(resolved.text, ev.quote)}'. Pin words from it, or cite the anchor that holds "
                        "the words",
                    )
                )
                continue
            quotes.append(ev.quote)
        if f.value is not None and f.basis == "stated" and quotes:
            issues += _stated_content(target, f.value, quotes, words=_holds_words(record, name, f))
    if isinstance(record, Observation):
        issues += _observation_scope(record) + _observation_unit(record)
    return issues


def _observation_unit(o: Observation) -> list[Issue]:
    """A measured value has a unit, or says it has none: an empty unit is one nobody gave."""
    q = o.value.value
    if q is None or q.reported_units.strip():
        return []
    return [
        blocking(
            "evidence",
            f"{o.id}.value",
            "the value has no unit; give the units as printed (units: [unit, anchor] on the group, the row or the "
            "Variable), or units: unitless for a count, ratio or index",
        )
    ]


def _observation_scope(o: Observation) -> list[Issue]:
    """What a value is a mean of is a claim like any other: a stated or derived scope, factor list or replicate level
    needs a quote that says it. Code derives the replicate level and the empty factor list where the text says them,
    so what is left here is the agent's own claim."""
    issues: list[Issue] = []
    scope, agg = o.reported_effect_scope, o.aggregated_over_factors
    factors = agg.value if agg is not None and agg.value is not None else []
    level = o.is_raw_replicate_level
    one_replicate = level.value is True and level.basis == "derived"  # a single replicate's value is of one treatment
    claimed = scope.basis != "inferred" and bool(scope.evidence) and not one_replicate
    if claimed and scope.value is not None and scope_statement(scope.value, factors, scope.evidence) is None:
        reason = scope_reason(scope.value, factors, scope.evidence)
        issues.append(
            blocking(
                "evidence",
                f"{o.id}.reported_effect_scope",
                f"nothing cited says these are {scope.value} values: {reason}. Add the anchor that says what they are "
                "means of to scope_at on the group",
            )
        )
        if scope.value == "treatment_mean" and agg is not None:
            issues.append(
                blocking(
                    "evidence",
                    f"{o.id}.aggregated_over_factors",
                    "[] follows from scope (reported_effect_scope), which nothing cited supports; fix scope first",
                )
            )
    pooled = factors if agg is not None and agg.basis != "inferred" and agg.evidence else []
    if pooled and agg is not None and scope_statement("aggregated_mean", pooled, agg.evidence) is None:
        reason = scope_reason("aggregated_mean", pooled, agg.evidence)
        issues.append(
            blocking(
                "evidence",
                f"{o.id}.aggregated_over_factors",
                f"nothing cited says the values are averaged over {', '.join(pooled)}: {reason}. Add the anchor that "
                "says it to scope_at on the group",
            )
        )
    if level.value is not None and level.basis != "inferred" and o.value.value is not None:
        cited = [e for name, f in fields_of(o).items() if name != "value" for e in f.evidence]
        found = replicate_level(o.value.value, o.value.evidence, cited, named_replicate(o))
        if found is None or found.raw != level.value:
            kind = "individual replicate values" if level.value else "means"
            issues.append(
                blocking(
                    "evidence",
                    f"{o.id}.is_raw_replicate_level",
                    f"nothing cited says these are {kind}: code looks for a ± or letters in the value, 'individual "
                    "plot' or 'replicate' wording, 'mean' or 'average', or a P value in the cited text. Add the anchor "
                    "that says it to scope_at on the group",
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
            return [
                blocking("evidence", target, "the value is null with no note; write [null, note: what was looked for]")
            ]
        return []
    if f.basis in ("stated", "derived") and not f.evidence:
        return [
            blocking(
                "evidence",
                target,
                f"this {f.basis} value cites no anchor; write [value, anchor] citing the text that holds it, or "
                "[value, note: why] if it is your judgement",
            )
        ]
    if f.basis == "inferred" and (f.note is None or not f.note.strip()):
        return [blocking("evidence", target, "the note is empty; write [value, note: what it is inferred from]")]
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
                    f"'{_short(value)}' isn't in the cited text. If it is your own wording (a summary or a name), "
                    "keep it and add a note, [value, anchor, note: how], citing the sentences it sums up; don't swap "
                    "in a shorter span that says less. If the paper prints it, write it as printed and cite the anchor "
                    "that holds it",
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
        issues.extend(_missing_number(target, n) for n in _claimed_numbers(value) if not _written(joined, n))
        unit = value.reported_units.strip()
        if unit and unit.lower() not in UNITLESS and not _unit_in(joined, unit):
            issues.append(
                blocking(
                    "evidence",
                    target,
                    f"the unit '{unit}' isn't in the cited text; also cite the anchor that states it (a column "
                    "header, the variable label or the methods sentence), or give the group units: [unit, anchor]",
                )
            )
    elif isinstance(value, DateRange | DepthRange):
        what = "date" if isinstance(value, DateRange) else "depth"
        if not any(contains_loose(q, value.reported_text) for q in quotes):
            issues.append(
                blocking(
                    "evidence",
                    target,
                    f"the {what} '{_short(value.reported_text)}' isn't in the cited text; write it exactly as the "
                    "paper prints it and cite the anchor that holds it",
                )
            )
    elif isinstance(value, StatisticalSummary):
        if isinstance(value.statistic_value, float) and not number_in(joined, value.statistic_value):
            issues.append(_missing_number(target, value.statistic_value))
        name = value.statistic_name.split("(")[0].strip()
        if name and not contains_loose(joined, name):
            issues.append(
                warning(
                    "evidence", target, f"'{name}' isn't in the cited text; cite the anchor where the paper names it"
                )
            )
    return issues


def _claimed_numbers(q: QuantityValue) -> list[float | str]:
    """The numbers a quantity states: its one number, or, when it has none ("<0.5", "250 to 300"), every number its
    text writes outside the unit. A token that is no single number, such as a date ("15.05.1999"), is kept as
    written."""
    if q.reported_numeric_value is not None:
        return [q.reported_numeric_value]
    tokens = numbers_in(number_text(q))
    return [read[0] if (read := read_number(token)) is not None else token for token in tokens]


def _written(text: str, claimed: float | str) -> bool:
    """A number at the precision `text` prints it, or a token that is no single number as printed."""
    return number_in(text, claimed) if isinstance(claimed, float) else claimed in numbers_in(text)


def _missing_number(target: str, value: float | str) -> Issue:
    shown = int(value) if isinstance(value, float) and value.is_integer() else value
    return blocking("evidence", target, f"the number {shown} isn't in the cited text; cite the anchor that states it")


def _unit_in(text: str, unit: str) -> bool:
    if contains(text, unit):
        return True
    want = squash(normalize_unit(unit))
    return bool(want) and want in squash(normalize_unit(text))


def _short(text: str, limit: int = 80) -> str:
    text = norm(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _closest(text: str, quote: str, width: int = 120) -> str:
    """The stretch of `text` most like `quote`, so the agent can pin the right words."""
    text = norm(text)
    if len(text) <= width:
        return text
    m = SequenceMatcher(None, text, norm(quote), autojunk=False).find_longest_match()
    start = max(0, m.a - (width - m.size) // 2)
    return ("…" if start else "") + text[start : start + width] + ("…" if start + width < len(text) else "")
