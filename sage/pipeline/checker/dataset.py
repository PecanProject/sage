"""Dataset level: references resolve to the right entity type, no observation is written twice, and a value's site and
what it is a mean of agree with the treatment it belongs to."""

import re
from collections import defaultdict
from collections.abc import Collection
from decimal import ROUND_HALF_UP, Decimal

from sage.domain.normalize import contains, norm, numbers_in, read_number, squash
from sage.domain.stats import PLACES
from sage.domain.units import normalize_unit
from sage.ir.draft import Draft, Overwrite
from sage.ir.schema import (
    FIELD_REFERENCES,
    REFERENCE_FIELDS,
    Entity,
    Field,
    Method,
    Observation,
    Site,
    Species,
    Study,
    Treatment,
    Variable,
    fields_of,
)
from sage.pipeline.checker.issues import Issue, blocking, warning


def check_dataset(draft: Draft, uncompiled: Collection[str] = ()) -> list[Issue]:
    """`uncompiled` holds ids whose records failed to compile: a reference to one is that record's problem, reported at
    its own lines, so it is left out here."""
    records = draft.records
    checks: list[tuple[str, str, str, str]] = []
    for rid, record in records.items():
        pointers = REFERENCE_FIELDS.get(record.type, {}) | FIELD_REFERENCES.get(record.type, {})
        for attr, wanted in pointers.items():
            value = getattr(record, attr)
            if isinstance(value, Field):
                value = value.value
            refs = value if isinstance(value, list) else [value]
            checks += [(rid, attr, ref, wanted) for ref in refs if isinstance(ref, str) and ref not in uncompiled]
    issues = [i for c in checks if (i := _ref_issue(*c, records)) is not None]
    issues += _duplicate_observations(records)
    issues += _two_values(records)
    issues += _same_value_other_variable(records, {o for d in draft.tables.values() for o in d.observation_ids})
    issues += _duplicate_names(records)
    issues += _site_against_treatment(records)
    issues += _pooled_sites(records)
    issues += _scope_against_treatment(records)
    return issues


def recompute_dataset(draft: Draft) -> list[Overwrite]:
    """References code can work out from the whole draft: with exactly one Study, every Treatment belongs to it.
    Updates the draft in place and returns the replaced values."""
    studies = [r for r in draft.records.values() if isinstance(r, Study)]
    if len(studies) != 1:
        return []
    study = studies[0]
    evidence = [e for f in fields_of(study).values() for e in f.evidence]
    if not evidence:
        return []
    derived = Field[str](value=study.id, basis="derived", evidence=evidence, note="the paper describes one Study")
    overwrites: list[Overwrite] = []
    for rid, r in list(draft.records.items()):
        if not isinstance(r, Treatment) or r.study_id == derived:
            continue
        if r.study_id.value not in (None, study.id):
            why = "the paper describes one Study, so every Treatment belongs to it"
            overwrites.append(
                Overwrite(record_id=rid, field="study_id", before=r.study_id.value, after=study.id, why=why)
            )
        draft.records[rid] = r.model_copy(update={"study_id": derived})
    return overwrites


def _ref_issue(rid: str, attr: str, ref: str, wanted: str, records: dict[str, Entity]) -> Issue | None:
    target = records.get(ref)
    if target is None:
        known = [r for r, rec in records.items() if rec.type == wanted][:8]
        hint = (
            f"known {wanted} ids: {', '.join(known)}"
            if known
            else f"no {wanted} is written yet; write it in records/ first"
        )
        return blocking("dataset", f"{rid}.{attr}", f"points at '{ref}' but no record has that id; {hint}")
    if target.type != wanted:
        return blocking("dataset", f"{rid}.{attr}", f"points at '{ref}', which is a {target.type}, not a {wanted}")
    return None


def _duplicate_observations(records: dict[str, Entity]) -> list[Issue]:
    seen: dict[tuple[object, ...], str] = {}
    issues: list[Issue] = []
    for rid, r in records.items():
        if not isinstance(r, Observation) or r.value.value is None:
            continue
        key = (
            r.treatment_id,
            r.variable_id,
            r.value.value.reported_text,
            r.temporal_info.value.reported_text if r.temporal_info.value else None,
            r.site_id,
            r.depth.value.reported_text if r.depth is not None and r.depth.value is not None else None,
            r.replicate_id.value if r.replicate_id is not None else None,
        )
        if key in seen:
            why = "same treatment, variable, time, depth, replicate and value"
            issues.append(blocking("dataset", rid, f"duplicates {seen[key]} ({why}); remove one"))
        else:
            seen[key] = rid
    return issues


def _two_values(records: dict[str, Entity]) -> list[Issue]:
    """Two values for one Treatment that nothing else tells apart: the same Variable and name, site, time, depth,
    method, crop, species, replicate, replicate level, scope and unit. Each level a table reports needs a Treatment of
    its own, so this is most often a level with no Treatment, written onto another level's."""
    seen: dict[tuple[object, ...], tuple[str, str]] = {}
    issues: list[Issue] = []
    for rid, o in records.items():
        if not isinstance(o, Observation) or o.value.value is None:
            continue
        depth = o.depth.value if o.depth is not None else None
        over = o.aggregated_over_factors.value if o.aggregated_over_factors is not None else None
        key = (
            o.treatment_id,
            o.variable_id,
            squash(o.variable_name.value or "").lower(),
            o.site_id,
            _when(o),
            squash(depth.reported_text).lower() if depth is not None else None,
            o.method_id,
            o.crop_id,
            o.species_id,
            o.replicate_id.value if o.replicate_id is not None else None,
            o.is_raw_replicate_level.value,
            o.reported_effect_scope.value,
            tuple(sorted(_key(f) for f in over or [])),
            squash(normalize_unit(o.value.value.reported_units)).lower(),
        )
        text = o.value.value.reported_text
        first, first_text = seen.setdefault(key, (rid, text))
        if squash(first_text) != squash(text):
            t = o.temporal_info.value
            when = f"time '{t.reported_text}'" if t is not None else "no time"
            issues.append(
                blocking(
                    "dataset",
                    rid,
                    f"'{text}' and {first}'s '{first_text}' are two values for {o.treatment_id} with the same "
                    f"variable, site, depth and {when}. When they differ in time, give each its own; otherwise give "
                    "each level the table reports its own Treatment, with factor_levels naming it, and point the "
                    "value at it; tell replicate values apart with replicate",
                )
            )
    return issues


def _same_value_other_variable(records: dict[str, Entity], from_tables: set[str]) -> list[Issue]:
    """One value written twice: values for the same treatment, site, time and unit that agree at the coarser of their
    two printed precisions, under different Variables (a table's "Aboveground N" and the text's "plant N") or at
    different precisions. The warning lands on the text value, since a table's value wins over the text's."""
    issues: list[Issue] = []
    seen: dict[tuple[object, ...], list[tuple[str, Observation]]] = defaultdict(list)
    for rid, o in records.items():
        if not isinstance(o, Observation) or o.value.value is None or o.value.value.reported_numeric_value is None:
            continue
        key = (o.treatment_id, o.site_id, _when(o), squash(normalize_unit(o.value.value.reported_units)).lower())
        twin = next(((i, x) for i, x in seen[key] if _restated(o, x)), None)
        seen[key].append((rid, o))
        if twin is None:
            continue
        (here, mine), (there, theirs) = ((rid, o), twin) if rid not in from_tables else (twin, (rid, o))
        issues.append(
            warning(
                "dataset",
                here,
                f"{_shown(mine)} under {mine.variable_id} matches {there} ({_shown(theirs)}, {theirs.variable_id}) for "
                "the same treatment and time; if both report one quantity, keep the table's value and one Variable",
            )
        )
    return issues


def _restated(a: Observation, b: Observation) -> bool:
    """One value written twice in different forms: under another Variable, or at another precision ("11.5" in the
    text, "12" in the table). The same Variable and the same text is a duplicate, which blocks elsewhere."""
    qa, qb = a.value.value, b.value.value
    same_text = qa is not None and qb is not None and squash(qa.reported_text) == squash(qb.reported_text)
    return _agree(a, b) and (a.variable_id != b.variable_id or not same_text)


def _when(o: Observation) -> tuple[object, ...]:
    t = o.temporal_info.value
    if t is None:
        return ()
    if t.relative_timing_days is not None:
        return ("days", t.relative_timing_days)
    if t.earliest is not None:
        return ("dates", t.earliest, t.latest)
    return ("text", squash(t.reported_text).lower())


def _decimals(text: str, value: float) -> int:
    """Decimal places the value is printed with in its text."""
    for token in numbers_in(text):
        read = read_number(token)
        if read is not None and abs(read[0]) == abs(value):
            return read[1]
    return 0


def _agree(a: Observation, b: Observation) -> bool:
    qa, qb = a.value.value, b.value.value
    if qa is None or qb is None or qa.reported_numeric_value is None or qb.reported_numeric_value is None:
        return False
    places = min(
        _decimals(qa.reported_text, qa.reported_numeric_value), _decimals(qb.reported_text, qb.reported_numeric_value)
    )
    step = Decimal(1).scaleb(-places)

    def rounded(v: float) -> Decimal:
        return Decimal(str(v)).quantize(step, rounding=ROUND_HALF_UP)

    return rounded(qa.reported_numeric_value) == rounded(qb.reported_numeric_value)


def _shown(o: Observation) -> str:
    q = o.value.value
    if q is None:
        return "no value"
    return q.reported_text if contains(q.reported_text, q.reported_units) else f"{q.reported_text} {q.reported_units}"


def _site_against_treatment(records: dict[str, Entity]) -> list[Issue]:
    """A Treatment is scoped to one Site, so a value for it was measured there."""
    issues: list[Issue] = []
    for rid, o in records.items():
        if not isinstance(o, Observation):
            continue
        t = records.get(o.treatment_id)
        if isinstance(t, Treatment) and t.site_id != o.site_id:
            issues.append(
                blocking(
                    "dataset",
                    f"{rid}.site_id",
                    f"is '{o.site_id}', but its treatment {o.treatment_id} is at '{t.site_id}'; fix site or treatment. "
                    "A treatment run at several sites is one Treatment per site",
                )
            )
    return issues


def _pooled_sites(records: dict[str, Entity]) -> list[Issue]:
    """A value averaged over sites belongs to no one of them: it sits on a Site that stands for the sites it pools, with
    Treatments of its own. A Site other values are measured at is one site."""
    observations = {rid: o for rid, o in records.items() if isinstance(o, Observation)}
    pooled = {rid for rid, o in observations.items() if _over_places(o)}
    single = {o.site_id for rid, o in observations.items() if rid not in pooled}
    return [
        blocking(
            "dataset",
            f"{rid}.site_id",
            f"this value is averaged over sites, but '{o.site_id}' is one site other values are measured at. Write a "
            "Site for the sites it pools, named for them ('Bristol and South Shore, pooled'), with Treatments of its "
            "own, and put the value there",
        )
        for rid, o in observations.items()
        if rid in pooled and o.site_id in single
    ]


def _over_places(o: Observation) -> bool:
    agg = o.aggregated_over_factors
    factors = agg.value if agg is not None and agg.value is not None else []
    return o.reported_effect_scope.value == "aggregated_mean" and any(_key(f) in PLACES for f in factors)


def _scope_against_treatment(records: dict[str, Entity]) -> list[Issue]:
    """A value averaged over a factor can't belong to a Treatment that fixes that factor, and the factor must be one the
    Study's Treatments vary: averaging one treatment over time or samples is still its mean. A treatment mean for a
    Treatment that leaves out a factor its Study's other Treatments fix is most likely averaged over that factor,
    unless the value's own text names a level of it (a column header "Arlington")."""
    treatments = {rid: r for rid, r in records.items() if isinstance(r, Treatment)}
    by_study: dict[str | None, dict[str, str]] = defaultdict(dict)
    levels: dict[str, set[str]] = defaultdict(set)
    for t in treatments.values():
        by_study[t.study_id.value].update({_key(f): f for f in t.factor_levels})
        for f, level in t.factor_levels.items():
            levels[_key(f)].add(level)
    issues: list[Issue] = []
    for rid, o in records.items():
        if not isinstance(o, Observation) or (t := treatments.get(o.treatment_id)) is None:
            continue
        fixed = {_key(f): f"{f}={level}" for f, level in t.factor_levels.items()}
        factors = by_study[t.study_id.value]
        agg = o.aggregated_over_factors
        if o.reported_effect_scope.value == "aggregated_mean" and agg is not None and agg.value is not None:
            clash = [fixed[_key(f)] for f in agg.value if _key(f) in fixed]
            unknown = [f for f in agg.value if _key(f) not in factors and _key(f) not in PLACES]
            if unknown:
                named = ", ".join(factors.values()) or "none"
                issues.append(
                    blocking(
                        "dataset",
                        f"{rid}.aggregated_over_factors",
                        f"{', '.join(unknown)} is not a factor this Study's Treatments vary (they vary: {named}). A "
                        "mean over harvests, dates, samples or years of one treatment is a treatment_mean, with the "
                        "time it covers; aggregated_mean pools over treatment factors, or over sites (name it site)",
                    )
                )
            elif clash:
                issues.append(
                    blocking(
                        "dataset",
                        f"{rid}.aggregated_over_factors",
                        f"{o.treatment_id} fixes {', '.join(clash)}, so its values are not averaged over that factor; "
                        "set treatment to the Treatment it belongs to, or drop the factor from over",
                    )
                )
        elif o.reported_effect_scope.value == "treatment_mean":
            said = _own_text(o)
            left_out = [
                name
                for key, name in factors.items()
                if key not in fixed and not any(_names(said, level) for level in levels[key])
            ]
            if left_out:
                names = ", ".join(left_out)
                issues.append(
                    blocking(
                        "dataset",
                        f"{rid}.reported_effect_scope",
                        f"{o.treatment_id} fixes only {', '.join(t.factor_levels) or 'no factor'}, while other "
                        f"Treatments in its Study fix {names}, so a mean of it is averaged over {names} (a Mean row, a "
                        f"main effect). Set scope: aggregated_mean and over: [{names}] on its row, column or cell (or "
                        "the group), with scope_at pointing at what says it: the row's label, the caption. If the "
                        f"value belongs to no level of {names} (measured before it was applied), give its Treatment "
                        "that factor with the level none",
                    )
                )
    return issues


def _key(factor: str) -> str:
    return squash(factor).lower()


def _own_text(o: Observation) -> str:
    """The Observation's variable name and every quote it cites, such as a column header that names a location."""
    quotes = [e.quote for f in fields_of(o).values() for e in f.evidence]
    return norm(" ".join([o.variable_name.value or "", *quotes]))


def _names(text: str, level: str) -> bool:
    """Whether `text` names a factor level, by its leading words ("Arlington" for "Arlington, WI")."""
    head = norm(level).split(",")[0].strip()
    return bool(head) and re.search(rf"(?<!\w){re.escape(head)}(?!\w)", text, re.IGNORECASE) is not None


def _duplicate_names(records: dict[str, Entity]) -> list[Issue]:
    """Two records of one type with the same name. A Treatment belongs to its site and study, so the same treatment
    run at two sites is two Treatments by design, not a duplicate."""
    by_name: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for rid, r in records.items():
        if isinstance(r, Species | Site | Treatment | Variable | Method):
            value = (r.scientific_name if isinstance(r, Species) else r.name).value
            if value is None:
                continue
            where = (r.site_id, str(r.study_id.value)) if isinstance(r, Treatment) else ()
            by_name[(r.type, value.strip().lower(), *where)].append(rid)
    return [
        warning("dataset", rid, f"same {kind} name as {ids[0]} ('{name}'); merge them unless they really differ")
        for (kind, name, *_), ids in by_name.items()
        for rid in ids[1:]
    ]
