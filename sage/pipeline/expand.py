"""expand_table: the agent says what a table's columns mean; code turns every value cell into an Observation."""

import re
from dataclasses import dataclass, field

from sage.domain.cell_values import CellValue, parse_cell
from sage.domain.depth import parse_depth
from sage.domain.normalize import contains, contains_loose, squash
from sage.domain.stats import (
    dispersion,
    dispersion_statistic,
    letters_meaning,
    replicate_statement,
    scope_reason,
    scope_statement,
)
from sage.domain.units import normalize_unit
from sage.ir.draft import Column, ColumnRole, Draft, TableRoles
from sage.ir.schema import (
    DateRange,
    DepthRange,
    EffectScope,
    Evidence,
    Field,
    Observation,
    QuantityValue,
    StatisticalSummary,
    Treatment,
    Variable,
)
from sage.parse.document import Cell, Table

UNIT_IN_LABEL = re.compile(r"\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*$")
STATISTIC_ROW = re.compile(
    r"^\s*(p[\s-]*values?|p|lsd|hsd|se|sem|sd|cv|f|df|r2|contrast)\b[\s\w().%=<>-]*$", re.IGNORECASE
)
SUMMARY_ROW = re.compile(
    r"^\s*(grand\s+|overall\s+)?(mean|means|average|total|pooled)(\s+(of|across|over)\b.*)?[\s:*†‡a-z]?$",
    re.IGNORECASE,
)
"""A treatment cell that names no treatment, only a pooling: "Mean", "Overall mean", "Average across N rates".
A variable called "Total fruit" is not one, so only treatment and factor cells are tested."""
BARE_NUMBER = re.compile(r"^\s*[-+]?\d+(?:[.,]\d+)?\s*$")
DEPTH_UNIT = re.compile(r"(?<![a-z])(mm|cm|m|in|inches|ft)(?![a-z])", re.IGNORECASE)


@dataclass
class Expansion:
    observations: list[Observation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    empty_cells: int = 0
    skipped_rows: list[int] = field(default_factory=list)


@dataclass
class _Table:
    table: Table
    notes: list[Evidence]
    roles: TableRoles
    header: int
    by_role: dict[ColumnRole, list[Column]]
    treatments: dict[str, Treatment]
    variables: dict[str, Variable]

    def columns(self, role: ColumnRole) -> list[Column]:
        return self.by_role.get(role, [])


@dataclass
class _Error:
    message: str


@dataclass
class _Var:
    record: Variable
    label: Cell | None


def expand(table: Table, roles: TableRoles, draft: Draft, notes: list[Evidence]) -> Expansion:
    """`notes` are the table's footnote blocks, cited for what the values are, beside the headers and the caption."""
    out = Expansion()
    by_role: dict[ColumnRole, list[Column]] = {}
    for c in roles.columns:
        by_role.setdefault(c.role, []).append(c)
    width = max((len(r) for r in table.rows), default=0)
    bad_cols = [c.col for c in roles.columns if c.col >= width or c.col < 0]
    if bad_cols:
        out.errors.append(f"columns {bad_cols} don't exist; the table has columns 0..{width - 1}")
        return out
    if "value" not in by_role:
        out.errors.append("no column has role=value, so there is nothing to expand")
        return out
    header = roles.header_rows if roles.header_rows is not None else table.header_rows
    if not 0 <= header < len(table.rows):
        out.errors.append(
            f"header_rows is {header}, but the table has {len(table.rows)} rows, header rows first; set header_rows "
            "to the number of header rows"
        )
        return out
    t = _Table(
        table=table,
        notes=notes,
        roles=roles,
        header=header,
        by_role=by_role,
        treatments={rid: r for rid, r in draft.records.items() if isinstance(r, Treatment)},
        variables={rid: r for rid, r in draft.records.items() if isinstance(r, Variable)},
    )
    out.errors += _roles_problems(t)
    if out.errors:
        return out
    label_cols = [c.col for c in roles.columns if c.role in ("treatment", "factor", "variable")]
    treatment_cols = [c.col for c in roles.columns if c.role in ("treatment", "factor")]
    for i in range(t.header, len(table.rows)):
        row = table.rows[i]
        labels = [row[c].text for c in label_cols if c < len(row)]
        if i in roles.skip_rows or any(STATISTIC_ROW.match(text) for text in labels):
            out.skipped_rows.append(i)
            continue
        summary = next((row[c].text for c in treatment_cols if c < len(row) and SUMMARY_ROW.match(row[c].text)), None)
        for vcol in t.columns("value"):
            if vcol.col >= len(row):
                continue
            cell = row[vcol.col]
            parsed = parse_cell(cell.text)
            if parsed.missing or not cell.text.strip():
                out.empty_cells += 1
                continue
            if parsed.numeric is None:
                out.errors.append(f"{cell.anchor} '{cell.text}' holds no number; add row {i} to skip_rows or fix roles")
                continue
            if summary is not None:
                out.errors.append(
                    f"{cell.anchor} is in a '{summary}' row, pooled over the treatments themselves; add row {i} to "
                    "skip_rows and mark it unresolved (schema_limitation) if it matters"
                )
                continue
            treatment = _treatment(t, row, vcol)
            variable = _variable(t, row, vcol)
            if isinstance(treatment, _Error) or isinstance(variable, _Error):
                out.errors += [r.message for r in (treatment, variable) if isinstance(r, _Error)]
                continue
            out.observations.append(_observation(t, cell, vcol, treatment, variable, parsed, row))
    return out


def _roles_problems(t: _Table) -> list[str]:
    """Roles that contradict the table, said once per column rather than once per cell."""
    varied = {squash(c.factor).lower(): c.col for c in t.columns("factor") if c.factor is not None}
    varied |= {squash(f).lower(): c.col for c in t.columns("value") for f in c.factor_levels}
    problems = [
        f"aggregated_over_factors names '{factor}', but column {col} gives its level, so the values are not averaged "
        "over it; drop it from aggregated_over_factors"
        for factor in t.roles.aggregated_over_factors
        if (col := varied.get(squash(factor).lower())) is not None
    ]
    body = t.table.rows[t.header :]
    if t.roles.n is None and not t.columns("n"):
        spread = next(
            (
                r[c.col].text
                for r in body
                for c in t.columns("value")
                if c.col < len(r) and dispersion(parse_cell(r[c.col].text)) is not None
            ),
            None,
        )
        if spread is not None:
            problems.append(
                f"cells carry a statistic (e.g. '{spread}'), so set roles.n to the replicates each value summarises, "
                "citing where the paper says it ('four replicates'), or to null with a note if it never does"
            )
    if t.roles.time_meaning is None:
        for c in t.columns("time"):
            bare = next((r[c.col].text for r in body if c.col < len(r) and BARE_NUMBER.match(r[c.col].text)), None)
            if bare is not None:
                head = " / ".join(h.text for h in _heads(t, c.col)) or "no header"
                problems.append(
                    f"column {c.col} (header '{head}') holds bare numbers such as '{bare}'; set roles.time_meaning to "
                    "what they count (e.g. 'days after planting'), taken from the header, caption or footnote"
                )
    return problems


def _treatment(t: _Table, row: list[Cell], vcol: Column) -> str | _Error:
    """The Treatment a value cell belongs to: the row's treatment name, the value column's treatment_id, or the levels
    the factor columns give, plus the levels the value column itself fixes (`Column.factor_levels`)."""
    wanted: dict[str, str] = {}
    for c in t.columns("treatment"):
        text = row[c.col].text
        named = t.roles.treatment_by_label.get(text)
        hits = [rid for rid, r in t.treatments.items() if r.name.value is not None and _same(r.name.value, text)]
        if named is None and len(hits) != 1:
            return _Error(
                f"{row[c.col].anchor} '{text}' matches {len(hits)} Treatments by name; add it to treatment_by_label"
            )
        tid = named if named is not None else hits[0]
        if not vcol.factor_levels:
            return tid
        base = t.treatments.get(tid)
        if base is None:
            return _Error(f"treatment_by_label names '{tid}', which isn't written yet")
        wanted.update(base.factor_levels)
        break
    if vcol.treatment_id is not None:
        base = t.treatments.get(vcol.treatment_id)
        if base is None:
            return _Error(f"column {vcol.col} names treatment '{vcol.treatment_id}', which isn't written yet")
        if not t.columns("factor") and not vcol.factor_levels:
            return vcol.treatment_id
        wanted.update(base.factor_levels)
    wanted.update({c.factor: row[c.col].text for c in t.columns("factor") if c.factor is not None})
    wanted.update(vcol.factor_levels)
    if not wanted:
        return _Error("no treatment column, factor column or value-column treatment_id says whose values these are")
    hits = [rid for rid, r in t.treatments.items() if _levels_match(r.factor_levels, wanted)]
    if len(hits) == 1:
        return hits[0]
    levels = ", ".join(f"{k}={v}" for k, v in wanted.items())
    return _Error(f"{row[vcol.col].anchor}: {len(hits)} Treatments have factor_levels {{{levels}}}; need exactly one")


def _variable(t: _Table, row: list[Cell], vcol: Column) -> _Var | _Error:
    if vcol.variable_id is not None:
        v = t.variables.get(vcol.variable_id)
        if v is None:
            return _Error(f"column {vcol.col} names variable '{vcol.variable_id}', which isn't written yet")
        return _Var(v, None)
    for c in t.columns("variable"):
        label = row[c.col]
        vid = t.roles.variable_by_label.get(label.text)
        if vid is not None:
            v = t.variables.get(vid)
            return _Var(v, label) if v is not None else _Error(f"variable_by_label names '{vid}', not written yet")
        hits = [v for v in t.variables.values() if v.name.value is not None and _same_label(v.name.value, label.text)]
        if len(hits) == 1:
            return _Var(hits[0], label)
        return _Error(f"{label.anchor} '{label.text}' matches {len(hits)} Variables; add it to variable_by_label")
    return _Error(f"value column {vcol.col} has no variable_id and no column has role=variable")


def _observation(
    t: _Table, cell: Cell, vcol: Column, treatment_id: str, var: _Var, parsed: CellValue, row: list[Cell]
) -> Observation:
    roles = t.roles
    heads = _heads(t, vcol.col)
    here = Evidence(anchor=cell.anchor, quote=cell.text)
    label_ev = [Evidence(anchor=var.label.anchor, quote=var.label.text)] if var.label is not None else []
    head_ev = [Evidence(anchor=heads[-1].anchor, quote=heads[-1].text)] if heads else []
    units, unit_ev = _units(t, row, var, head_ev)
    table = t.table
    caption = [Evidence(anchor=table.caption_anchor, quote=table.caption)] if table.caption.strip() else []
    sources = [*(Evidence(anchor=h.anchor, quote=h.text) for h in heads), *caption, *t.notes, *roles.scope_evidence]
    statistic = _statistic(roles, cell, parsed, here, sources)
    letters = _letters(roles, parsed, here, sources)
    scope, aggregated = _scope(roles, sources)
    return Observation(
        id="obs-" + cell.anchor.replace(".", "-"),
        citation_id=roles.citation_id,
        site_id=roles.site_id,
        treatment_id=treatment_id,
        species_id=roles.species_id,
        crop_id=roles.crop_id,
        method_id=roles.method_by_variable.get(var.record.id, roles.method_id),
        variable_id=var.record.id,
        variable_name=Field[str](
            value=var.label.text if var.label is not None else (var.record.name.value or var.record.id),
            basis="stated" if var.label is not None else "derived",
            evidence=label_ev or head_ev or [here],
        ),
        value=Field[QuantityValue](
            value=QuantityValue(reported_text=cell.text, reported_numeric_value=parsed.numeric, reported_units=units),
            basis="stated",
            evidence=[here, *unit_ev],
        ),
        statistical_encoding=statistic,
        reported_effect_scope=scope,
        aggregated_over_factors=aggregated,
        n=_n(t, row),
        temporal_info=_time(t, row, vcol, head_ev, sources),
        depth=_depth(t, row, vcol, sources),
        is_raw_replicate_level=_replicate_level(roles, parsed, here),
        notes=letters,
    )


def _statistic(
    roles: TableRoles, cell: Cell, parsed: CellValue, here: Evidence, sources: list[Evidence]
) -> Field[StatisticalSummary] | None:
    spread = dispersion(parsed)
    if spread is None:
        return None
    named = dispersion_statistic(parsed, sources)
    if named is not None:
        name, said = named
        return Field[StatisticalSummary](
            value=StatisticalSummary(statistic_name=name, statistic_value=spread),
            basis="stated" if contains(said.quote, name) else "derived",
            evidence=[here, said],
        )
    if roles.statistic is not None:
        return Field[StatisticalSummary](
            value=StatisticalSummary(statistic_name=roles.statistic, statistic_value=spread),
            basis="inferred",
            evidence=[here],
            note="named from the table roles; no header, caption or cited note states it",
        )
    return Field[StatisticalSummary](
        value=None, basis="stated", note=f"'{cell.text}' carries a dispersion that no header, caption or note names"
    )


def _letters(roles: TableRoles, parsed: CellValue, here: Evidence, sources: list[Evidence]) -> Field[str] | None:
    if parsed.letters is None:
        return None
    group = f"letter group '{parsed.letters}'"
    meaning = letters_meaning(sources)
    if meaning is not None:
        return Field[str](value=f"{group}: {meaning.quote}", basis="derived", evidence=[here, meaning])
    if roles.letters is not None:
        return Field[str](
            value=f"{group} ({roles.letters})",
            basis="inferred",
            evidence=[here],
            note="what the letters mean is from the table roles; no caption, header or cited note states it",
        )
    return Field[str](value=group, basis="derived", evidence=[here])


def _scope(roles: TableRoles, sources: list[Evidence]) -> tuple[Field[EffectScope], Field[list[str]]]:
    factors = roles.aggregated_over_factors
    said = scope_statement(roles.effect_scope, factors, sources)
    if said is None and not roles.scope_evidence:
        note = f"nothing says what these values are means of; {scope_reason(roles.effect_scope, factors, sources)}"
        return Field[EffectScope](value=None, basis="stated", note=note), Field[list[str]](
            value=None, basis="stated", note=note
        )
    if said is not None:
        evidence, basis, note = [said], "derived", None
    else:
        evidence, basis = roles.scope_evidence, "inferred"
        note = f"{roles.effect_scope} is from the table roles; {scope_reason(roles.effect_scope, factors, evidence)}"
    scope = Field[EffectScope](value=roles.effect_scope, basis=basis, evidence=evidence, note=note)
    if roles.effect_scope == "treatment_mean":
        return scope, Field[list[str]](value=[], basis="derived", evidence=evidence)
    if factors:
        return scope, Field[list[str]](value=factors, basis=basis, evidence=evidence, note=note)
    return scope, Field[list[str]](value=None, basis="stated", note="the cited text doesn't say which factors")


def _replicate_level(roles: TableRoles, parsed: CellValue, here: Evidence) -> Field[bool]:
    if dispersion(parsed) is not None or parsed.letters is not None:
        return Field[bool](value=False, basis="derived", evidence=[here])
    said = roles.scope_evidence
    if not said:
        return Field[bool](
            value=None, basis="stated", note="nothing cited says whether these are replicate values or means"
        )
    raw = roles.is_raw_replicate_level
    if replicate_statement(raw, said) is not None:
        return Field[bool](value=raw, basis="derived", evidence=said)
    kind, words = ("replicate values", "'individual plot' or 'replicate'") if raw else ("means", "'mean' or 'average'")
    quoted = "; ".join(f"{e.anchor} '{e.quote[:100]}'" for e in said)
    return Field[bool](
        value=raw,
        basis="inferred",
        evidence=said,
        note=f"that these are {kind} is from the table roles; the cell has no ± or letters, and code looked for "
        f"{words} in the cited text: {quoted}",
    )


def _time(
    t: _Table, row: list[Cell], vcol: Column, head_ev: list[Evidence], sources: list[Evidence]
) -> Field[DateRange]:
    """Text only; `recompute_date` reads the dates from it when the Observation is stored."""
    meaning = t.roles.time_meaning
    for c in t.columns("time"):
        cell = row[c.col]
        cited = [Evidence(anchor=x.anchor, quote=x.text) for x in (cell, *_heads(t, c.col))]
        if meaning is None:
            return Field[DateRange](value=DateRange(reported_text=cell.text), basis="stated", evidence=cited)
        value = DateRange(reported_text=f"{cell.text} {meaning}".strip())
        said = next((e for e in [*cited, *sources] if contains_loose(e.quote, meaning)), None)
        if said is not None:
            return Field[DateRange](value=value, basis="derived", evidence=cited if said in cited else [*cited, said])
        return Field[DateRange](
            value=value,
            basis="inferred",
            evidence=cited,
            note=f"'{meaning}' is from the table roles; no header, caption or cited note says it",
        )
    if vcol.time is not None:
        value = DateRange(reported_text=vcol.time)
        said = next((e for e in sources if contains_loose(e.quote, vcol.time)), None)
        if said is not None:
            return Field[DateRange](value=value, basis="stated", evidence=[said])
        return Field[DateRange](
            value=value,
            basis="inferred",
            evidence=head_ev,
            note=f"'{vcol.time}' is from the table roles; no header, caption or cited note says it",
        )
    if t.roles.default_time is not None:
        value = DateRange(reported_text=t.roles.default_time)
        said = next((e for e in sources if contains_loose(e.quote, t.roles.default_time)), None)
        if said is not None:
            return Field[DateRange](value=value, basis="stated", evidence=[said])
        return Field[DateRange](
            value=value,
            basis="inferred",
            note=f"the table gives no time; '{t.roles.default_time}' is from the table roles, and no header, caption, "
            "footnote or cited note holds it",
        )
    return Field[DateRange](value=None, basis="stated", note="the table gives no time")


def _n(t: _Table, row: list[Cell]) -> Field[int] | None:
    for c in t.columns("n"):
        cell = row[c.col]
        cited = [Evidence(anchor=x.anchor, quote=x.text) for x in (cell, *_heads(t, c.col))]
        count = parse_cell(cell.text).numeric
        if count is None or not count.is_integer():
            return Field[int](value=None, basis="stated", note=f"the n column holds '{cell.text}', not a count")
        return Field[int](value=int(count), basis="stated", evidence=cited)
    return t.roles.n


def _depth(t: _Table, row: list[Cell], vcol: Column, sources: list[Evidence]) -> Field[DepthRange] | None:
    """Text only; `recompute_depth` reads the centimetres when the Observation is stored. A depth cell of bare
    numbers takes the unit its header prints ("Depth (cm)")."""
    for c in t.columns("depth"):
        cell = row[c.col]
        heads = _heads(t, c.col)
        cited = [Evidence(anchor=x.anchor, quote=x.text) for x in (cell, *heads)]
        if parse_depth(cell.text) is not None:
            return Field[DepthRange](value=DepthRange(reported_text=cell.text), basis="stated", evidence=cited[:1])
        unit = next((m[1] for h in heads if (m := DEPTH_UNIT.search(h.text)) is not None), None)
        if unit is None:
            return Field[DepthRange](
                value=None, basis="stated", note=f"the depth cell '{cell.text}' and its header give no unit"
            )
        return Field[DepthRange](
            value=DepthRange(reported_text=f"{cell.text.strip()} {unit}"), basis="derived", evidence=cited
        )
    for given in (vcol.depth, t.roles.default_depth):
        if given is None:
            continue
        said = next((e for e in sources if contains_loose(e.quote, given)), None)
        if said is not None:
            return Field[DepthRange](value=DepthRange(reported_text=given), basis="stated", evidence=[said])
        return Field[DepthRange](
            value=DepthRange(reported_text=given),
            basis="inferred",
            note=f"'{given}' is from the table roles; no header, caption or cited note holds it",
        )
    return None


def _units(t: _Table, row: list[Cell], var: _Var, head_ev: list[Evidence]) -> tuple[str, list[Evidence]]:
    for c in t.columns("unit"):
        cell = row[c.col]
        if cell.text.strip():
            return cell.text.strip(), [Evidence(anchor=cell.anchor, quote=cell.text)]
    if var.label is not None and (m := UNIT_IN_LABEL.search(var.label.text)) is not None:
        return str(m.group(1)).strip(), [Evidence(anchor=var.label.anchor, quote=var.label.text)]
    units = var.record.units
    if units is not None and units.value is not None:
        named = [e for e in head_ev if squash(normalize_unit(units.value)) in squash(normalize_unit(e.quote))]
        return units.value, named or units.evidence
    return "", []


def _heads(t: _Table, col: int) -> list[Cell]:
    return [r[col] for r in t.table.rows[: t.header] if col < len(r) and r[col].text.strip()]


def _same(a: str, b: str) -> bool:
    return squash(a).lower() == squash(b).lower()


def _same_label(name: str, label: str) -> bool:
    bare = UNIT_IN_LABEL.sub("", label)
    return _same(name, label) or _same(name, bare) or _same(UNIT_IN_LABEL.sub("", name), bare)


def _levels_match(have: dict[str, str], wanted: dict[str, str]) -> bool:
    def key(d: dict[str, str]) -> dict[str, str]:
        return {squash(k).lower(): squash(v).lower() for k, v in d.items()}

    return key(have) == key(wanted)
