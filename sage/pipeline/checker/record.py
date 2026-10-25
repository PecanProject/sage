"""Record level: required content per entity, and derived values recomputed by code from the stated inputs."""

from sage.domain.coordinates import Axis, coordinate_problem, recompute_coordinate
from sage.domain.dates import date_problem, date_warning, recompute_date
from sage.domain.depth import depth_problem, recompute_depth
from sage.domain.management import event_type
from sage.domain.stats import has_dispersion, replicate_level
from sage.domain.units import conversion_problem, recompute_quantity
from sage.ir.draft import Overwrite
from sage.ir.schema import (
    Citation,
    DateRange,
    DepthRange,
    Entity,
    Field,
    Management,
    Model,
    Observation,
    QuantityValue,
    Site,
    fields_of,
)
from sage.pipeline.checker.issues import Issue, blocking, warning


def check_record(record: Entity) -> list[Issue]:
    rid = record.id
    issues: list[Issue] = []
    match record:
        case Observation():
            if record.value.value is None:
                issues.append(
                    blocking(
                        "record",
                        f"{rid}.value",
                        "an Observation needs a value; remove it, or skip its cell, if there is none",
                    )
                )
            if record.variable_id is None:
                issues.append(
                    blocking("record", f"{rid}.variable_id", "set variable to the id of the Variable this measures")
                )
            if record.method_id is None:
                issues.append(
                    blocking("record", f"{rid}.method_id", "set method to the id of the Method that measured this")
                )
            stat = record.statistical_encoding
            if stat is not None and stat.value is not None and record.n is None:
                issues.append(
                    blocking(
                        "record",
                        f"{rid}.n",
                        "a value with a statistic needs n, the replicates it summarises: write n: [4, anchor] citing "
                        "where the methods or the table give it ('four replicates', 'n = 4'), or n: [null, note: what "
                        "was looked for]",
                    )
                )
        case Site():
            if record.name.value is None:
                issues.append(blocking("record", f"{rid}.name", "a Site needs a name, e.g. the farm or station"))
        case Management():
            if record.event_type.value is None:
                issues.append(
                    blocking("record", f"{rid}.event_type", "write what happened, e.g. event_type: [planting, p3.b4]")
                )
        case Citation():
            issues += [
                warning("record", f"{rid}.{name}", "the citation is incomplete; cite the anchor that gives this")
                for name, f in (("author", record.author), ("year", record.year), ("title", record.title))
                if f.value is None
            ]
    fields = fields_of(record)
    if fields and all(f.value is None for f in fields.values()):
        issues.append(
            blocking("record", rid, "every field is null; write the values the paper gives, or delete the record")
        )
    for name, f in fields.items():
        if (problem := _derivation_problem(record, name, f)) is not None:
            issues.append(blocking("record", f"{rid}.{name}", problem))
        elif isinstance(f.value, DateRange) and (note := date_warning(f.value)) is not None:
            issues.append(warning("record", f"{rid}.{name}", note))
    return issues


def _derived(value: object) -> bool:
    if isinstance(value, QuantityValue):
        return value.converted_value is not None
    if isinstance(value, DateRange):
        return value.earliest is not None or value.relative_timing is not None
    if isinstance(value, DepthRange):
        return value.top_cm is not None
    return False


def _replaced(before: Model, after: Model) -> bool:
    """Whether code changed a value that was set, rather than only filling in what was left out."""
    return any(v is not None and getattr(after, k) != v for k, v in before)


def _axis(record: Entity, name: str) -> Axis | None:
    if isinstance(record, Site) and name in ("latitude", "longitude"):
        return "lat" if name == "latitude" else "lon"
    return None


def _derivation_problem(record: Entity, name: str, f: Field[object]) -> str | None:
    """What code couldn't derive from the stated text, so the agent fixes the text or its evidence."""
    value = f.value
    if isinstance(value, QuantityValue):
        axis = _axis(record, name)
        if axis is None:
            return conversion_problem(value)
        problem = coordinate_problem(value.reported_text, axis)
        if problem is not None:
            return f"{problem}; write just this coordinate as the value, as the paper prints it, so code derives it"
    elif isinstance(value, DateRange):
        return date_problem(value, [e.quote for e in f.evidence])
    elif isinstance(value, DepthRange):
        return depth_problem(value)
    return None


def _observation_judgements(o: Observation) -> tuple[dict[str, object], list[Overwrite]]:
    """What code reads from an Observation's own text, whatever the agent judged: whether it is a mean, the n of 1 of
    a single replicate's value, and the empty factor list of a treatment mean. A null statistic the value shows no
    sign of is left out, as an optional value the paper never prints."""
    update: dict[str, object] = {}
    overwrites: list[Overwrite] = []
    quantity = o.value.value
    if quantity is not None:
        cited = [e for name, f in fields_of(o).items() if name != "value" for e in f.evidence]
        found = replicate_level(quantity, o.value.evidence, cited)
        if found is not None:
            before = o.is_raw_replicate_level.value
            if before is not None and before != found.raw:
                why = "code read whether this is a mean from the value's text and its quotes"
                overwrites.append(
                    Overwrite(record_id=o.id, field="is_raw_replicate_level", before=before, after=found.raw, why=why)
                )
            update["is_raw_replicate_level"] = Field[bool](value=found.raw, basis="derived", evidence=found.evidence)
        stat = o.statistical_encoding
        if stat is not None and stat.value is None and not has_dispersion(quantity, o.value.evidence):
            update["statistical_encoding"] = None
    raw = update.get("is_raw_replicate_level", o.is_raw_replicate_level)
    if isinstance(raw, Field) and raw.value is True and (o.n is None or o.n.value != 1):
        if o.n is not None and o.n.value is not None:
            why = "a single replicate's value has n = 1"
            overwrites.append(Overwrite(record_id=o.id, field="n", before=o.n.value, after=1, why=why))
        inferred = raw.basis == "inferred"
        update["n"] = Field[int](
            value=1,
            basis="inferred" if inferred else "derived",
            evidence=raw.evidence,
            note="follows from is_raw_replicate_level, which is inferred" if inferred else None,
        )
    scope = o.reported_effect_scope
    if scope.value == "treatment_mean":
        inferred = scope.basis == "inferred"
        update["aggregated_over_factors"] = Field[list[str]](
            value=[],
            basis="inferred" if inferred else "derived",
            evidence=scope.evidence,
            note="follows from reported_effect_scope, which is inferred" if inferred else None,
        )
    return update, overwrites


def _management_judgements(m: Management) -> tuple[dict[str, object], list[Overwrite]]:
    """The event type code names from the vocabulary word the quote holds."""
    said = m.event_type.value
    named = event_type(said, m.event_type.evidence) if said is not None else None
    if named is None:
        return {}, []
    overwrites = []
    if said != named.name:
        why = "code named the event type from the word for it in the quote"
        overwrites.append(Overwrite(record_id=m.id, field="event_type", before=said, after=named.name, why=why))
    return {"event_type": Field[str](value=named.name, basis="derived", evidence=[named.evidence])}, overwrites


def recompute[R: Entity](record: R) -> tuple[R, list[Overwrite]]:
    """Recompute derived values by code: coordinates, dates, unit conversions, and what an Observation or a Management
    says of itself. Model arithmetic never survives: what code can't derive is dropped, and the record level says
    why."""
    judged: dict[str, object] = {}
    overwrites: list[Overwrite] = []
    if isinstance(record, Observation):
        judged, overwrites = _observation_judgements(record)
    elif isinstance(record, Management):
        judged, overwrites = _management_judgements(record)
    if judged:
        record = record.model_copy(update=judged)
    updates: dict[str, Field[object]] = {}
    for name, f in fields_of(record).items():
        value = f.value
        new: object = value
        why = ""
        if isinstance(value, QuantityValue):
            axis = _axis(record, name)
            if axis is not None:
                new = recompute_coordinate(value, axis)
                why = "coordinates converted by code from the reported text"
            else:
                new = recompute_quantity(value)
                why = "number and conversion recomputed by code from the reported text"
        elif isinstance(value, DateRange):
            new = recompute_date(value, [e.quote for e in f.evidence])
            why = "dates recomputed by code from the reported text and the year its quotes state"
        elif isinstance(value, DepthRange):
            new = recompute_depth(value)
            why = "depth read by code from the reported text"
        if new != value and isinstance(new, Model):
            updates[name] = f.model_copy(update={"value": new})
            if not isinstance(value, Model) or not _replaced(value, new):
                continue
            if _derived(value) and not _derived(new):
                why = "code could not derive this from the reported text, so the model's value was dropped"
            overwrites.append(
                Overwrite(
                    record_id=record.id,
                    field=name,
                    before=value.model_dump(mode="json"),
                    after=new.model_dump(mode="json"),
                    why=why,
                )
            )
    if not updates:
        return record, overwrites
    return record.model_copy(update=updates), overwrites
