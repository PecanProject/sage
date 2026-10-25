"""What the scientist sees: a plain label per record, the review queue order, and reviews folded back in."""

from pydantic import BaseModel

from sage.ir.schema import (
    Citation,
    Crop,
    Entity,
    Field,
    Management,
    Method,
    Observation,
    QuantityValue,
    Site,
    Species,
    Study,
    Treatment,
    TreatmentPair,
    Variable,
)
from sage.ir.settled import Settled, SettledRecord, Status
from sage.store.models import Review

QUEUE_RANK: dict[Status, int] = {"unresolved": 0, "flagged": 1, "verified": 2}
TYPE_ORDER = [
    "Citation",
    "Study",
    "Site",
    "Species",
    "Crop",
    "Treatment",
    "Management",
    "Method",
    "Variable",
    "Observation",
    "TreatmentPair",
    "Coverage",
]


class QueueItem(BaseModel):
    record_id: str
    record_type: str
    status: Status
    label: str
    reason: str | None = None
    """The first field that is not verified, and why."""
    review: Review | None = None


def standing(reviews: list[Review], settled: Settled, ancestors: list[Settled]) -> list[Review]:
    kept: dict[str, set[str]] = {settled.run_id: {r.record.id for r in settled.records}}
    for a in ancestors:
        before = {r.record.id: r.record for r in a.records}
        kept[a.run_id] = {r.record.id for r in settled.records if before.get(r.record.id) == r.record}
    return [r for r in reviews if r.record_id in kept.get(r.run_id, set())]


def latest_reviews(reviews: list[Review]) -> dict[str, Review]:
    return {r.record_id: r for r in sorted(reviews, key=lambda r: r.ts)}


def queue(settled: Settled, reviews: list[Review]) -> list[QueueItem]:
    """Every record: unresolved first, then flagged, then verified, each by type. Reviewed items stay in the queue
    with their latest review, so the scientist can revisit them."""
    last = latest_reviews(reviews)
    names = _names(settled)
    ordered = sorted(settled.records, key=lambda r: (QUEUE_RANK[r.status], _type_rank(r.record.type)))
    return [
        QueueItem(
            record_id=r.record.id,
            record_type=r.record.type,
            status=r.status,
            label=label(r.record, names),
            reason=reason(r),
            review=last.get(r.record.id),
        )
        for r in ordered
    ]


def _type_rank(t: str) -> int:
    return TYPE_ORDER.index(t) if t in TYPE_ORDER else len(TYPE_ORDER)


def reason(r: SettledRecord) -> str | None:
    """The first field that is not verified, and why."""
    for f in r.fields:
        if f.status != "verified" and f.cause != "absent":
            return f"{f.field}: {f.reason or f.cause or f.status}"
    return r.issues[0] if r.issues else None


def labels(settled: Settled) -> dict[str, str]:
    names = _names(settled)
    return {r.record.id: label(r.record, names) for r in settled.records}


def _names(settled: Settled) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in settled.records:
        rec = r.record
        if isinstance(rec, Treatment | Variable | Method | Site) and rec.name.value is not None:
            out[rec.id] = rec.name.value
    return out


def label(rec: Entity, names: dict[str, str]) -> str:
    """One line a scientist can read without knowing the schema."""
    match rec:
        case Observation():
            value = rec.value.value
            shown = _quantity(value) if value is not None else "no value"
            when = rec.temporal_info.value
            parts = [
                rec.variable_name.value or names.get(rec.variable_id or "", "?"),
                names.get(rec.treatment_id, rec.treatment_id),
                shown,
            ]
            if when is not None:
                parts.append(when.reported_text)
            return " · ".join(parts)
        case Citation():
            return " · ".join(str(v) for v in (rec.author.value, rec.year.value, rec.title.value) if v is not None)
        case Study():
            return rec.design.value if rec.design is not None and rec.design.value is not None else "Study"
        case Site():
            return rec.name.value or "Site"
        case Species():
            return rec.scientific_name.value or "Species"
        case Crop():
            return " · ".join(v for v in (_value(rec.common_name), _value(rec.cultivar)) if v is not None) or "Crop"
        case Method() | Variable():
            return rec.name.value or rec.type
        case Treatment():
            levels = ", ".join(f"{k}: {v}" for k, v in rec.factor_levels.items())
            return (rec.name.value or rec.id) + (f" ({levels})" if levels else "")
        case Management():
            parts = [rec.event_type.value or "event"]
            if rec.date.value is not None:
                parts.append(rec.date.value.reported_text)
            if rec.amount is not None and rec.amount.value is not None:
                parts.append(_quantity(rec.amount.value))
            return " · ".join(parts)
        case TreatmentPair():
            return rec.comparison_label.value or "Comparison"
        case _:
            return rec.type


def _value(f: Field[str] | None) -> str | None:
    return f.value if f is not None else None


def _quantity(q: QuantityValue) -> str:
    if not q.reported_units or q.reported_units in q.reported_text:
        return q.reported_text
    return f"{q.reported_text} {q.reported_units}"
