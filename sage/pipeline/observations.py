"""Observations from the agent's groups in `records/observations/*.yaml`. A table group says which cells are values and
what each row, column or cell is (treatment, time, depth…); code reads every cell's number, dispersion, letters and
unit, and decides from the headers, the caption, the table's notes and the cited text whether each claim is stated,
derived or inferred. Nothing is matched by guessing: the agent names the treatment of every value.

    - table: p7.t1
      variable: grain_yield_Mg_ha
      site: arlington
      method: combine_harvest
      scope: aggregated_mean
      over: [cultivar]
      scope_at: [p5.b9]
      n: [4, p2.b6]
      rows:    {2: {treatment: N0}, 3: {treatment: N60}}
      columns: {1: {time: {at: p7.t1.r1.c1}}, 2: {time: {at: p7.t1.r1.c2}}}
      cells:   {p7.t1.r4.c1: {treatment: N120, time: {at: p7.t1.r1.c1}}}
      skip:    [p7.t1.r3.c2]
    - table: p9.t2
      irrelevant: ANOVA table, no experiment values
    - text: true
      variable: n2o_flux
      values:
        n2o-july-N120: {value: [12.4 g m-2, p8.b2], treatment: N120, time: [July 2019, p8.b2]}
"""

import re
from dataclasses import dataclass, field
from typing import Any

from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from sage.domain.cell_values import CellValue, parse_cell
from sage.domain.normalize import contains_loose, squash
from sage.domain.stats import (
    dispersion,
    dispersion_statistic,
    letters_meaning,
    replicate_statement,
    scope_reason,
    scope_statement,
)
from sage.domain.units import find_unit, is_unit, normalize_unit
from sage.ir.draft import TableDecision
from sage.ir.schema import (
    DateRange,
    DepthRange,
    EffectScope,
    Entity,
    Evidence,
    Field,
    Observation,
    QuantityValue,
    StatisticalSummary,
    Variable,
)
from sage.parse.document import Cell, Table
from sage.pipeline.shapes import Located, Reader, coerce, is_null, plain

UNIT_IN_LABEL = re.compile(r"\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*$")
CELL = re.compile(r"^(p\d+\.t\d+)\.r(\d+)\.c(\d+)$")

REFS = ("variable", "site", "method", "crop", "species", "citation", "treatment")
"""Keys that name another record by id."""
CLAIMS = ("time", "depth", "n", "units", "statistic", "letters", "notes", "replicate")
"""Keys that are claims about the value, written as field shapes (or, for time, depth, units, statistic and letters in
a table, as bare text code looks for in the table's own text)."""
SCOPE = ("scope", "over", "scope_at")
"""What the values are means of. A group says it for all its values; a row, column, cell or text value may say it for
its own, such as a table's Mean row averaged over a factor the other rows fix."""
GROUP = (*REFS, *CLAIMS, *SCOPE, "raw")
TABLE_GROUP = (*GROUP, "table", "irrelevant", "rows", "columns", "cells", "skip")
TEXT_GROUP = (*GROUP, "text", "values")
PER_VALUE = (*REFS, *CLAIMS, *SCOPE)


@dataclass
class Built:
    observations: list[tuple[Observation, Located]] = field(default_factory=list)
    tables: list[tuple[TableDecision, Located]] = field(default_factory=list)


@dataclass
class _Group:
    """What a group says once for all its values."""

    keys: dict[str, Node]
    scope: EffectScope | None
    over: list[str]
    scope_at: list[Evidence]
    raw: bool | None


def build(reader: Reader, root: Node, records: dict[str, Entity], citation: str | None) -> Built:
    """`citation` is the paper's one Citation, which a group may leave out."""
    out = Built()
    if not isinstance(root, SequenceNode):
        reader.issue(
            root, "an observations file is a list of groups, each starting with '- table: …' or '- text: true'"
        )
        return out
    for node in root.value:
        entries = reader.entries(node, "a group")
        keys = {k: v for k, _, v in entries}
        if "table" in keys:
            _table_group(reader, node, keys, records, citation, out)
        elif "text" in keys:
            _text_group(reader, node, keys, records, citation, out)
        elif entries:
            reader.issue(node, "start a group with 'table: <table anchor>' or 'text: true'")
    return out


def _table_group(
    reader: Reader, node: Node, keys: dict[str, Node], records: dict[str, Entity], citation: str | None, out: Built
) -> None:
    if not _known(reader, keys, TABLE_GROUP, "a table group"):
        return
    anchor = _text(keys["table"])
    table = reader.doc.table(anchor)
    if table is None:
        known = ", ".join(t.anchor for t in reader.doc.tables) or "none"
        reader.issue(keys["table"], f"no table '{anchor}' in this paper; tables: {known}")
        return
    if "irrelevant" in keys:
        reason = _text(keys["irrelevant"])
        others = sorted(k for k in keys if k not in ("table", "irrelevant"))
        if others:
            reader.issue(node, f"an irrelevant table has no values, so drop {', '.join(others)}")
        decision = TableDecision(table_id=anchor, status="irrelevant", reason=reason or "irrelevant")
        out.tables.append((decision, reader.at(node)))
        return
    if not table.rows:
        reader.issue(
            keys["table"],
            f"{table.label} isn't transcribed yet: write tables/{anchor}.csv from tables/{anchor}.png first",
        )
        return
    group = _group(reader, keys)
    if group is None:
        return
    rows = _indexed(reader, keys.get("rows"), "rows")
    columns = _indexed(reader, keys.get("columns"), "columns")
    cells = _cells(reader, keys.get("cells"), anchor)
    skip = {_text(n) for n in _items(keys.get("skip"))}
    picked: dict[str, tuple[list[MappingNode], Located]] = {}
    for r, (row_node, row_at) in rows.items():
        for c, (col_node, _) in columns.items():
            picked[f"{anchor}.r{r}.c{c}"] = ([row_node, col_node], row_at)
    for a, (cell_node, cell_at) in cells.items():
        layers, _ = picked.get(a, ([], cell_at))
        picked[a] = ([*layers, cell_node], cell_at)
    ids: list[str] = []
    mapped: set[int] = set()
    for a, (layers, at) in picked.items():
        if a in skip or a.rsplit(".", 1)[0] in skip:
            continue
        obs = _table_value(reader, table, a, group, layers, records, citation, explicit=a in cells, at=at)
        if obs is not None:
            out.observations.append((obs, at))
            ids.append(obs.id)
            if (m := CELL.match(a)) is not None:
                mapped.add(int(m[2]))
    if not picked:
        reader.issue(node, f"say which cells of {anchor} are values: rows  x  columns, or cells")
    decision = TableDecision(
        table_id=anchor, status="expanded", observation_ids=ids, rows=sorted(mapped), skipped=sorted(skip)
    )
    out.tables.append((decision, reader.at(node)))


def _text_group(
    reader: Reader, node: Node, keys: dict[str, Node], records: dict[str, Entity], citation: str | None, out: Built
) -> None:
    if not _known(reader, keys, TEXT_GROUP, "a text group"):
        return
    group = _group(reader, keys)
    values = keys.get("values")
    if values is None:
        reader.issue(node, "a text group lists its values: values: {<id>: {value: [12.4 g m-2, p8.b2], …}}")
    if group is None or values is None:
        return
    for oid, key_node, body in reader.entries(values, "values"):
        entries = {k: v for k, _, v in reader.entries(body, oid)}
        if not _known(reader, entries, (*PER_VALUE, "value"), oid):
            continue
        if "value" not in entries:
            reader.issue(key_node, f"{oid} needs value: [12.4 g m-2, p8.b2]")
            continue
        at = reader.at(key_node)
        obs = _text_value(reader, oid, group, entries, records, citation, at)
        if obs is not None:
            out.observations.append((obs, at))


def _group(reader: Reader, keys: dict[str, Node]) -> _Group | None:
    scope_text = _text(keys["scope"]) if "scope" in keys else None
    if scope_text is not None and scope_text not in ("treatment_mean", "aggregated_mean"):
        reader.issue(keys["scope"], "scope is treatment_mean or aggregated_mean")
        return None
    over = [_text(n) for n in _items(keys.get("over"))]
    scope_at: list[Evidence] = []
    for n in _items(keys.get("scope_at")):
        resolved = reader.doc.resolve(_text(n))
        if resolved is None:
            reader.issue(n, f"no anchor '{_text(n)}' in this paper; copy one from paper.md")
            return None
        scope_at.append(Evidence(anchor=resolved.anchor, quote=resolved.text))
    raw: bool | None = None
    if "raw" in keys:
        said = coerce(_text(keys["raw"]), bool, coordinate=False)
        if not isinstance(said, bool):
            reader.issue(keys["raw"], "raw is true or false")
            return None
        raw = said
    scope: EffectScope | None = (
        None if scope_text is None else "aggregated_mean" if scope_text == "aggregated_mean" else "treatment_mean"
    )
    return _Group(keys=keys, scope=scope, over=over, scope_at=scope_at, raw=raw)


@dataclass
class _Value:
    """One value's keys after group, row, column and cell are merged, later winning."""

    nodes: dict[str, Node]

    def ref(self, key: str) -> str | None:
        node = self.nodes.get(key)
        value = plain(node) if node is not None else None
        return value if isinstance(value, str) and value else None


def _scoped(reader: Reader, group: _Group, v: _Value) -> _Group | None:
    """The group as this value sees it: with the scope, over and scope_at its row, column or cell sets."""
    own = {k: n for k, n in v.nodes.items() if k in SCOPE}
    if all(group.keys.get(k) is n for k, n in own.items()):
        return group
    return _group(reader, {**group.keys, **own})


def _merged(group: _Group, layers: list[MappingNode], reader: Reader) -> _Value:
    nodes = {k: v for k, v in group.keys.items() if k in PER_VALUE}
    for layer in layers:
        for k, _, v in reader.entries(layer, "a cell"):
            if k not in PER_VALUE:
                reader.issue(layer, f"'{k}' can't be set per row, column or cell; it is one of {', '.join(PER_VALUE)}")
                continue
            nodes[k] = v
    return _Value(nodes)


def _table_value(
    reader: Reader,
    table: Table,
    anchor: str,
    group: _Group,
    layers: list[MappingNode],
    records: dict[str, Entity],
    citation: str | None,
    *,
    explicit: bool,
    at: Located,
) -> Observation | None:
    m = CELL.match(anchor)
    cell = _cell(table, anchor)
    if m is None or cell is None:
        reader.issue_at(at, f"{anchor} isn't a cell of {table.anchor}; its rows are 0..{len(table.rows) - 1}")
        return None
    row, col = int(m[2]), int(m[3])
    if row < table.header_rows:
        reader.issue_at(at, f"{anchor} is in a header row (header_rows is {table.header_rows}), not a value")
        return None
    parsed = parse_cell(cell.text)
    if parsed.missing or not cell.text.strip():
        if explicit:
            reader.issue_at(at, f"{anchor} is empty ('{cell.text}'); leave it out")
        return None
    if parsed.numeric is None:
        reader.issue_at(at, f"{anchor} '{cell.text}' holds no number; leave it out, or fix tables/{table.anchor}.csv")
        return None
    v = _merged(group, layers, reader)
    scoped = _scoped(reader, group, v)
    if scoped is None:
        return None
    group = scoped
    heads = _heads(reader, table, col)
    head_ev = [_ev(h) for h in heads]
    caption = [Evidence(anchor=table.caption_anchor, quote=table.caption)] if table.caption.strip() else []
    notes = [Evidence(anchor=r.anchor, quote=r.text) for a in table.notes_anchors if (r := reader.doc.resolve(a))]
    sources = [*head_ev, *caption, *notes, *group.scope_at]
    here = _ev(cell)
    labels = [c for c in table.rows[row][:col] if c.text.strip()]
    return _observation(
        reader,
        "obs-" + anchor.replace(".", "-"),
        v,
        group,
        records,
        citation,
        at,
        value=None,
        parsed=parsed,
        here=here,
        sources=sources,
        unit_places=[*reversed(heads), *labels],
        head_ev=head_ev[-1:],
    )


def _heads(reader: Reader, table: Table, col: int) -> list[Cell]:
    """The header cells over a value's column. A table carried on from an earlier page prints no header of its own:
    the first page's header governs its columns, and a header the agent copied onto it, which the page doesn't print,
    is left out."""
    own = [r[col] for r in table.rows[: table.header_rows] if col < len(r) and r[col].text.strip()]
    head = reader.doc.table(table.continues) if table.continues is not None else None
    if head is None:
        return own
    first = [r[col] for r in head.rows[: head.header_rows] if col < len(r) and r[col].text.strip()]
    return [*first, *(c for c in own if c.match != "none")]


def _text_value(
    reader: Reader,
    oid: str,
    group: _Group,
    entries: dict[str, Node],
    records: dict[str, Entity],
    citation: str | None,
    at: Located,
) -> Observation | None:
    raw = reader.field(entries["value"], QuantityValue, where=f"{oid}.value")
    if raw is None:
        return None
    value = Field[QuantityValue].model_validate(raw)
    v = _Value({**{k: n for k, n in group.keys.items() if k in PER_VALUE}, **entries})
    scoped = _scoped(reader, group, v)
    if scoped is None:
        return None
    group = scoped
    text = value.value.reported_text if value.value is not None else ""
    here = value.evidence[0] if value.evidence else Evidence(anchor="", quote=text)
    obs = _observation(
        reader,
        oid,
        v,
        group,
        records,
        citation,
        at,
        value=value,
        parsed=parse_cell(text),
        here=here,
        sources=[*value.evidence, *group.scope_at],
        unit_places=[],
        head_ev=[],
    )
    return obs if obs is None or value.evidence else _uncited(obs)


UNCITED = ("variable_name", "statistical_encoding", "is_raw_replicate_level", "notes")
"""The fields code reads off a value's text. When the value cites no anchor, none of them has one either."""


def _uncited(obs: Observation) -> Observation:
    """An inferred value's facts are inferred too: each field read off its text drops the empty anchor it was given
    and, left with nothing cited, becomes inferred with the value's own note, rather than citing an anchor that isn't
    there."""
    why = f"read off the value, which cites no anchor: {obs.value.note or 'no note'}"
    update: dict[str, object] = {}
    for name in UNCITED:
        f = getattr(obs, name)
        if f is None or all(e.anchor for e in f.evidence):
            continue
        cited = [e for e in f.evidence if e.anchor]
        update[name] = f.model_copy(
            update={"evidence": cited} if cited else {"evidence": [], "basis": "inferred", "note": f.note or why}
        )
    return obs.model_copy(update=update)


def _observation(
    reader: Reader,
    oid: str,
    v: _Value,
    group: _Group,
    records: dict[str, Entity],
    citation: str | None,
    at: Located,
    *,
    value: Field[QuantityValue] | None,
    parsed: CellValue,
    here: Evidence,
    sources: list[Evidence],
    unit_places: list[Cell],
    head_ev: list[Evidence],
) -> Observation | None:
    vid = v.ref("variable")
    treatment = v.ref("treatment")
    site = v.ref("site")
    citation = v.ref("citation") or citation
    if treatment is None or site is None or citation is None:
        missing = [k for k, val in (("treatment", treatment), ("site", site), ("citation", citation)) if val is None]
        reader.issue_at(at, f"{oid}: say its {' and '.join(missing)} (on the group, row or cell)")
        return None
    variable = records.get(vid) if vid is not None else None
    var = variable if isinstance(variable, Variable) else None
    if value is not None and value.value is not None and not value.value.reported_units:
        units, unit_ev = _units(reader, v, var, [], sources)
        if units:
            q = value.value.model_copy(update={"reported_units": units})
            value = value.model_copy(update={"value": q, "evidence": [*value.evidence, *unit_ev]})
    if value is None:
        units, unit_ev = _units(reader, v, var, unit_places, sources)
        value = Field[QuantityValue](
            value=QuantityValue(reported_text=here.quote, reported_numeric_value=parsed.numeric, reported_units=units),
            basis="stated",
            evidence=[here, *unit_ev],
        )
    named = var.name.value if var is not None else None
    name = named if named is not None and named.strip() else vid if vid is not None else oid
    label = next((_ev(c) for c in unit_places if contains_loose(c.text, name)), None)
    name_ev = [label] if label is not None else (var.name.evidence if var is not None else []) or head_ev or [here]
    scope, aggregated = _scope(group, sources)
    replicate = _valid(Field[str], _claim(reader, v, "replicate", str, oid))
    try:
        return Observation(
            id=oid,
            citation_id=citation,
            site_id=site,
            treatment_id=treatment,
            species_id=v.ref("species"),
            crop_id=v.ref("crop"),
            method_id=v.ref("method"),
            variable_id=vid,
            variable_name=Field[str](value=name, basis="derived", evidence=name_ev),
            value=value,
            statistical_encoding=_statistic(reader, v, parsed, here, sources, oid),
            reported_effect_scope=scope,
            aggregated_over_factors=aggregated,
            n=_valid(Field[int], _claim(reader, v, "n", int, oid)),
            temporal_info=_time(reader, v, sources, oid),
            depth=_depth(reader, v, sources, oid),
            replicate_id=replicate,
            is_raw_replicate_level=_replicate_level(group, parsed, here, replicate),
            notes=_valid(Field[str], _claim(reader, v, "notes", str, oid)) or _letters(v, parsed, here, sources),
        )
    except ValueError as e:
        reader.issue_at(at, f"{oid}: {e}")
        return None


def _units(
    reader: Reader, v: _Value, var: Variable | None, places: list[Cell], sources: list[Evidence]
) -> tuple[str, list[Evidence]]:
    """The unit: as the group, row or cell gives it; else the "(…)" of the value column's header or the row's label;
    else the Variable's units, cited where a header prints them."""
    node = v.nodes.get("units")
    if node is not None:
        if isinstance(node, ScalarNode):
            return str(node.value), _unit_said(reader, str(node.value), sources)
        raw = reader.field(node, str, where="units")
        if raw is not None:
            f = Field[str].model_validate(raw)
            return f.value or "", f.evidence
    for c in places:
        m = UNIT_IN_LABEL.search(c.text)
        if m is not None and is_unit(m.group(1)):
            return str(m.group(1)).strip(), [_ev(c)]
    if var is not None and var.units is not None and var.units.value is not None:
        unit = var.units.value
        named = [e for e in sources if squash(normalize_unit(unit)) in squash(normalize_unit(e.quote))]
        return unit, named[:1] or var.units.evidence
    return "", []


def _unit_said(reader: Reader, unit: str, sources: list[Evidence]) -> list[Evidence]:
    """Where the cited text prints a unit the group gives as plain text: the words in a quote, or else in the whole
    block a quote comes from, since a value from the text cites only its number."""
    for e in sources:
        if contains_loose(e.quote, unit):
            return [e]
    for e in sources:
        block = reader.doc.resolve(e.anchor)
        printed = find_unit(block.text, unit) if block is not None else None
        if printed is not None:
            return [Evidence(anchor=e.anchor, quote=printed)]
    return []


def _statistic(
    reader: Reader, v: _Value, parsed: CellValue, here: Evidence, sources: list[Evidence], oid: str
) -> Field[StatisticalSummary] | None:
    node = v.nodes.get("statistic")
    if node is not None and not isinstance(node, ScalarNode):
        raw = reader.field(node, StatisticalSummary, where=f"{oid}.statistic")
        return Field[StatisticalSummary].model_validate(raw) if raw is not None else None
    spread = dispersion(parsed)
    if spread is None:
        return None
    named = dispersion_statistic(parsed, sources)
    if named is not None:
        name, said = named
        return Field[StatisticalSummary](
            value=StatisticalSummary(statistic_name=name, statistic_value=spread),
            basis="stated" if contains_loose(said.quote, name) else "derived",
            evidence=[here, said],
        )
    if isinstance(node, ScalarNode) and node.value.strip():
        return Field[StatisticalSummary](
            value=StatisticalSummary(statistic_name=node.value.strip(), statistic_value=spread),
            basis="inferred",
            evidence=[here],
            note="named in the observations file; no header, caption or cited note states it",
        )
    return Field[StatisticalSummary](
        value=None, basis="stated", note=f"'{here.quote}' carries a dispersion that no header, caption or note names"
    )


def _letters(v: _Value, parsed: CellValue, here: Evidence, sources: list[Evidence]) -> Field[str] | None:
    if parsed.letters is None:
        return None
    group = f"letter group '{parsed.letters}'"
    meaning = letters_meaning(sources)
    if meaning is not None:
        return Field[str](value=f"{group}: {meaning.quote}", basis="derived", evidence=[here, meaning])
    node = v.nodes.get("letters")
    if isinstance(node, ScalarNode) and node.value.strip():
        return Field[str](
            value=f"{group} ({node.value.strip()})",
            basis="inferred",
            evidence=[here],
            note="what the letters mean is from the observations file; no caption, header or cited note states it",
        )
    return Field[str](value=group, basis="derived", evidence=[here])


def _scope(group: _Group, sources: list[Evidence]) -> tuple[Field[EffectScope], Field[list[str]]]:
    scope: EffectScope = group.scope or "treatment_mean"
    factors = group.over
    said = scope_statement(scope, factors, sources)
    if said is None and not group.scope_at:
        note = f"nothing says what these values are means of; {scope_reason(scope, factors, sources)}"
        if group.scope is None:
            note += ". Say it with scope and scope_at on the group"
        null_scope = Field[EffectScope](value=None, basis="stated", note=note)
        return null_scope, Field[list[str]](value=None, basis="stated", note=note)
    if said is not None:
        evidence, basis, note = [said], "derived", None
    else:
        evidence, basis = group.scope_at, "inferred"
        note = f"{scope} is from the observations file; {scope_reason(scope, factors, evidence)}"
    claimed = Field[EffectScope](value=scope, basis=basis, evidence=evidence, note=note)
    if scope == "treatment_mean":
        return claimed, Field[list[str]](value=[], basis="derived", evidence=evidence)
    if factors:
        return claimed, Field[list[str]](value=factors, basis=basis, evidence=evidence, note=note)
    return claimed, Field[list[str]](value=None, basis="stated", note="over: say which factors it is averaged over")


def _replicate_level(group: _Group, parsed: CellValue, here: Evidence, replicate: Field[str] | None) -> Field[bool]:
    if dispersion(parsed) is not None or parsed.letters is not None:
        return Field[bool](value=False, basis="derived", evidence=[here])
    if replicate is not None and replicate.value is not None and replicate.evidence:
        return Field[bool](value=True, basis="derived", evidence=replicate.evidence)  # as replicate_level reads it
    said = group.scope_at
    unsaid = "nothing cited says whether these are replicate values or means"
    if not said:
        if group.raw is None:
            return Field[bool](value=None, basis="stated", note=unsaid)
        return Field[bool](
            value=group.raw, basis="inferred", note="raw is set in the observations file; nothing is cited"
        )
    # With raw left out, the values are means only when the cited text says so; nothing assumes it.
    raw = bool(group.raw)
    if replicate_statement(raw, said) is not None:
        return Field[bool](value=raw, basis="derived", evidence=said)
    kind, words = ("replicate values", "'individual plot' or 'replicate'") if raw else ("means", "'mean' or 'average'")
    quoted = "; ".join(f"{e.anchor} '{e.quote[:100]}'" for e in said)
    if group.raw is None:
        return Field[bool](
            value=None,
            basis="stated",
            note=f"{unsaid}: the value has no ± or letters, and code looked for {words} in the cited text: {quoted}",
        )
    return Field[bool](
        value=raw,
        basis="inferred",
        evidence=said,
        note=f"that these are {kind} is from the observations file; the value has no ± or letters, and code looked "
        f"for {words} in the cited text: {quoted}",
    )


def _time(reader: Reader, v: _Value, sources: list[Evidence], oid: str) -> Field[DateRange]:
    got = _valid(Field[DateRange], _given(reader, v, "time", DateRange, sources, oid))
    return got if got is not None else Field[DateRange](value=None, basis="stated", note="no time is given for it")


def _depth(reader: Reader, v: _Value, sources: list[Evidence], oid: str) -> Field[DepthRange] | None:
    return _valid(Field[DepthRange], _given(reader, v, "depth", DepthRange, sources, oid))


def _given(
    reader: Reader, v: _Value, key: str, kind: type[DateRange | DepthRange], sources: list[Evidence], oid: str
) -> dict[str, object] | None:
    """A time or depth: a field shape as written, or bare text, which is stated when the table's own text (headers,
    caption, notes, scope_at) holds it and inferred otherwise."""
    node = v.nodes.get(key)
    if node is None or is_null(node):
        return None
    if isinstance(node, ScalarNode):
        text = str(node.value).strip()
        said = next((e for e in sources if contains_loose(e.quote, text)), None)
        if said is not None:
            return {"value": {"reported_text": text}, "basis": "stated", "evidence": [said.model_dump()]}
        note = f"'{text}' is from the observations file; no header, caption or cited note holds it"
        return {"value": {"reported_text": text}, "basis": "inferred", "note": note}
    return reader.field(node, kind, where=f"{oid}.{key}")


def _claim(reader: Reader, v: _Value, key: str, kind: type, oid: str) -> dict[str, object] | None:
    node = v.nodes.get(key)
    return reader.field(node, kind, where=f"{oid}.{key}") if node is not None else None


def _valid[F: Field[Any]](cls: type[F], raw: dict[str, object] | None) -> F | None:
    return cls.model_validate(raw) if raw is not None else None


def _known(reader: Reader, keys: dict[str, Node], allowed: tuple[str, ...], what: str) -> bool:
    unknown = sorted(set(keys) - set(allowed))
    for k in unknown:
        reader.issue(keys[k], f"{what} has no key '{k}'; its keys are {', '.join(allowed)}")
    return not unknown


def _indexed(reader: Reader, node: Node | None, what: str) -> dict[int, tuple[MappingNode, Located]]:
    out: dict[int, tuple[MappingNode, Located]] = {}
    if node is None:
        return out
    for key, key_node, body in reader.entries(node, what):
        if not key.isdigit():
            reader.issue(key_node, f"{what} are keyed by index, e.g. 2: {{treatment: N0}}")
            continue
        if not isinstance(body, MappingNode):
            reader.issue(body, f"{what} {key}: write what it shares as a mapping, e.g. {{treatment: N0}}")
            continue
        out[int(key)] = (body, reader.at(key_node))
    return out


def _cells(reader: Reader, node: Node | None, table: str) -> dict[str, tuple[MappingNode, Located]]:
    out: dict[str, tuple[MappingNode, Located]] = {}
    if node is None:
        return out
    for key, key_node, body in reader.entries(node, "cells"):
        if not key.startswith(f"{table}.r") or CELL.match(key) is None:
            reader.issue(key_node, f"cells are keyed by cell anchor, e.g. {table}.r2.c1")
            continue
        if not isinstance(body, MappingNode):
            reader.issue(body, f"{key}: write what it is as a mapping, e.g. {{treatment: N0}}")
            continue
        out[key] = (body, reader.at(key_node))
    return out


def _items(node: Node | None) -> list[Node]:
    if node is None:
        return []
    return list(node.value) if isinstance(node, SequenceNode) else [node]


def _text(node: Node) -> str:
    return str(node.value).strip() if isinstance(node, ScalarNode) else ""


def _cell(table: Table, anchor: str) -> Cell | None:
    return next((c for row in table.rows for c in row if c.anchor == anchor), None)


def _ev(c: Cell) -> Evidence:
    return Evidence(anchor=c.anchor, quote=c.text)
