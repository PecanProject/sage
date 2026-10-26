"""The agent's `records/` files compiled into the Draft the checker reads. Every value and every checker issue keeps
the `file:line` it came from, so `sage extract check` can point the agent at the line to fix.

    records/citation.yaml studies.yaml sites.yaml species.yaml crops.yaml methods.yaml variables.yaml
            treatments.yaml pairs.yaml managements.yaml coverage.yaml      id: {attribute: …}
    records/observations/*.yaml                                            groups (see observations.py)
    records/unresolved.yaml                                                target: {cause, reason}

Scientific attributes use the field shapes in shapes.py; ids, references and `factor_levels` are plain YAML."""

import re
from dataclasses import dataclass, field
from typing import get_args

import yaml
from pydantic import ValidationError
from yaml.nodes import MappingNode, Node

from sage.ir.draft import Draft, Mark, Overwrite
from sage.ir.files import FileIssue
from sage.ir.schema import Entity, Model, RecordAdapter
from sage.parse.document import Document
from sage.pipeline import observations
from sage.pipeline.checker.dataset import recompute_dataset
from sage.pipeline.checker.record import recompute
from sage.pipeline.shapes import Located, Reader, field_kind, plain

ENTITY_FILES: dict[str, str] = {
    "citation.yaml": "Citation",
    "studies.yaml": "Study",
    "sites.yaml": "Site",
    "species.yaml": "Species",
    "crops.yaml": "Crop",
    "methods.yaml": "Method",
    "variables.yaml": "Variable",
    "treatments.yaml": "Treatment",
    "pairs.yaml": "TreatmentPair",
    "managements.yaml": "Management",
    "coverage.yaml": "Coverage",
}
MODELS: dict[str, type[Model]] = {cls.__name__: cls for cls in get_args(Entity.__value__)}
RECORD_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
RECORDS = "records/"
OBSERVATIONS = "records/observations/"
UNRESOLVED = "records/unresolved.yaml"


@dataclass
class Compiled:
    draft: Draft
    marks: list[Mark] = field(default_factory=list)
    """The agent's unresolved marks from unresolved.yaml, not yet checked against the draft."""
    where: dict[str, Located] = field(default_factory=dict)
    """Record ids, `record_id.attribute` targets and table anchors, to the line that wrote them."""
    mark_at: dict[str, Located] = field(default_factory=dict)
    """Each mark's line in unresolved.yaml."""
    issues: list[FileIssue] = field(default_factory=list)
    """What isn't a record at all: YAML that doesn't parse, a wrong shape, a value of the wrong type."""
    unreadable: bool = False
    """Some file isn't YAML, so nothing it holds was read."""
    uncompiled: set[str] = field(default_factory=set)
    """Ids of records written but left out for a problem in their own lines, which those lines report."""

    def locate(self, target: str) -> Located | None:
        """The line a checker target comes from: the attribute, else its record, else its table."""
        return self.where.get(target) or self.where.get(target.split(".", 1)[0]) or self.mark_at.get(target)


@dataclass
class _Body:
    entity: str
    rid: str
    path: str
    key: Node
    attrs: dict[str, tuple[Node, Node]]


def compile_records(texts: dict[str, str], doc: Document) -> Compiled:
    """`texts` maps paths relative to the paper folder ("records/sites.yaml") to their text."""
    out = Compiled(draft=Draft())
    roots: dict[str, Node] = {}
    for path in sorted(texts):
        if not path.endswith(".yaml"):
            out.issues.append(FileIssue(path=path, line=1, message="records are .yaml files; move or delete this one"))
            continue
        try:
            node = yaml.compose(texts[path], Loader=yaml.BaseLoader)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            line = mark.line + 1 if mark is not None else 1
            out.issues.append(FileIssue(path=path, line=line, message=f"not valid YAML: {_yaml_problem(e)}"))
            out.unreadable = True
            continue
        if node is not None:
            roots[path] = node
    bodies = _bodies(roots, doc, out)
    citations = [b.rid for b in bodies if b.entity == "Citation"]
    only = citations[0] if len(citations) == 1 else None
    studies = [b.rid for b in bodies if b.entity == "Study"]
    records: dict[str, Entity] = {}
    for body in bodies:
        record = _record(body, doc, only, studies, out)
        if record is not None:
            records[record.id] = record
        else:
            out.uncompiled.add(body.rid)
    for path, root in roots.items():
        if path.startswith(OBSERVATIONS):
            _observations(Reader(path, doc), root, records, only, out)
    overwrites: list[Overwrite] = []
    for rid, record in records.items():
        fixed, changed = recompute(record)
        records[rid] = fixed
        overwrites += changed
    out.draft.records = records
    out.draft.overwrites = overwrites + recompute_dataset(out.draft)
    if UNRESOLVED in roots:
        _marks(Reader(UNRESOLVED, doc), roots[UNRESOLVED], out)
    known = {*ENTITY_FILES, "unresolved.yaml"}
    for path in roots:
        name = path.removeprefix(RECORDS)
        if not path.startswith(OBSERVATIONS) and name not in known:
            out.issues.append(
                FileIssue(
                    path=path,
                    line=1,
                    message=f"SAGE doesn't read this file; records are {', '.join(sorted(known))}, observations/*.yaml",
                )
            )
    return out


def _bodies(roots: dict[str, Node], doc: Document, out: Compiled) -> list[_Body]:
    """Every record body in the entity files, ids checked for shape and uniqueness."""
    bodies: list[_Body] = []
    seen: dict[str, Located] = {}
    for path, root in roots.items():
        entity = ENTITY_FILES.get(path.removeprefix(RECORDS))
        if entity is None:
            continue
        reader = Reader(path, doc)
        for rid, key, value in reader.entries(root, path.removeprefix(RECORDS)):
            if not RECORD_ID.match(rid):
                reader.issue(key, f"id '{rid}' must be letters, digits, '-' or '_' (no dots or spaces)")
                continue
            if rid in seen:
                first = seen[rid]
                reader.issue(key, f"id '{rid}' is already used at {first.path}:{first.line}; ids are unique")
                continue
            seen[rid] = reader.at(key)
            if not isinstance(value, MappingNode):
                reader.issue(value, f"{rid}: write its attributes as a mapping, e.g. name: [Arlington, p2.b3]")
                continue
            attrs = {k: (kn, v) for k, kn, v in reader.entries(value, rid)}
            bodies.append(_Body(entity=entity, rid=rid, path=path, key=key, attrs=attrs))
        out.issues += reader.issues
    return bodies


def _record(body: _Body, doc: Document, citation: str | None, studies: list[str], out: Compiled) -> Entity | None:
    model = MODELS[body.entity]
    reader = Reader(body.path, doc)
    out.where[body.rid] = reader.at(body.key)
    data: dict[str, object] = {"type": body.entity, "id": body.rid}
    for attr, (key, node) in body.attrs.items():
        out.where[f"{body.rid}.{attr}"] = reader.at(key)
        if attr in ("id", "type") or attr not in model.model_fields:
            known = ", ".join(n for n in model.model_fields if n not in ("id", "type"))
            reader.issue(key, f"{body.rid}: a {body.entity} has no '{attr}'; its attributes are {known}")
            continue
        kind = field_kind(model, attr)
        if kind is None:
            data[attr] = plain(node)
            continue
        coordinate = body.entity == "Site" and attr in ("latitude", "longitude")
        value = reader.field(node, kind, where=f"{body.rid}.{attr}", coordinate=coordinate)
        if value is not None:
            data[attr] = value
    if body.entity == "Treatment" and "study_id" not in data and len(studies) > 1:
        reader.issue(
            body.key,
            f"{body.rid}: study_id is required, since the paper has several Studies ({', '.join(studies)}); write "
            "study_id: [<study id>, anchor] citing the text that places it, or [null, note: what you looked for]",
        )
    _defaults(model, data, citation)
    out.issues += reader.issues
    if reader.issues:
        return None
    try:
        return RecordAdapter.validate_python(data)
    except ValidationError as e:
        for err in e.errors(include_input=False):
            loc = [str(p) for p in err["loc"][1:]]
            attr = loc[0] if loc else ""
            line = out.where.get(f"{body.rid}.{attr}") or out.where[body.rid]
            message = err["msg"].removeprefix("Value error, ")
            if err["type"] == "missing" and field_kind(model, attr) is not None:
                message = f"{attr} is required: write [value, anchor], or [null, note: what you looked for]"
            elif err["type"] == "missing":
                message = f"{attr} is required"
            elif attr:
                message = f"{'.'.join(loc)}: {message}"
            out.issues.append(FileIssue(path=line.path, line=line.line, message=f"{body.rid}: {message}"))
        return None


def _defaults(model: type[Model], data: dict[str, object], citation: str | None) -> None:
    """What the agent may leave out: the one Citation, and a Treatment's Study, which code fills in when the paper
    describes one Study (`recompute_dataset`)."""
    if citation is not None:
        if "citation_id" in model.model_fields and "citation_id" not in data:
            data["citation_id"] = citation
        if "citation_ids" in model.model_fields and "citation_ids" not in data:
            data["citation_ids"] = [citation]
    if model.__name__ == "Treatment" and "study_id" not in data:
        data["study_id"] = {
            "value": None,
            "basis": "stated",
            "note": "the Study is left out; code fills it in when the paper describes one Study",
        }


def _observations(reader: Reader, root: Node, records: dict[str, Entity], citation: str | None, out: Compiled) -> None:
    built = observations.build(reader, root, records, citation)
    for obs, at in built.observations:
        if obs.id in records:
            first = out.where.get(obs.id)
            where = f" at {first.path}:{first.line}" if first is not None else ""
            reader.issue_at(at, f"id '{obs.id}' is already used{where}; ids are unique")
            continue
        records[obs.id] = obs
        out.where[obs.id] = at
    for decision, at in built.tables:
        if decision.table_id in out.draft.tables:
            first = out.where[decision.table_id]
            reader.issue_at(
                at, f"{decision.table_id} is already mapped at {first.path}:{first.line}; one group per table"
            )
            continue
        out.draft.tables[decision.table_id] = decision
        out.where[decision.table_id] = at
    out.issues += reader.issues


def _marks(reader: Reader, root: Node, out: Compiled) -> None:
    for target, key, body in reader.entries(root, "unresolved.yaml"):
        values = plain(body)
        if not isinstance(values, dict):
            reader.issue(body, f"{target}: write {{cause: absent, reason: why}}")
            continue
        try:
            mark = Mark.model_validate({"target": target, **values})
        except ValidationError as e:
            problem = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors(include_input=False)
            )
            reader.issue(body, f"{target}: {problem}")
            continue
        out.marks.append(mark)
        out.mark_at[target] = reader.at(key)
    out.issues += reader.issues


def _yaml_problem(e: yaml.YAMLError) -> str:
    problem = getattr(e, "problem", None)
    context = getattr(e, "context", None)
    return ", ".join(str(p) for p in (context, problem) if p) or str(e).splitlines()[0]
