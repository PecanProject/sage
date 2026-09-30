"""
Evidence replay corpus: what evidence the regression papers are KNOWN to contain, and whether a recorded run's
extraction actually saw it.

The corpus (`tests/replay/expectations/<paper_id>.json`) is hand-written and hand-verified against the source PDF --
never copied from any run's output. Each expectation names one piece of evidence (a site's coordinates, a management
event, a table header's units, a design statement, ...), the entity type whose extraction needs it, the content.md
block(s) holding it and the literal text that proves it is there.

Scoring a run is read-only over artifacts the pipeline already writes (`runs/<run_id>/records/**/attemptN.json`,
`results/<paper_id>/<run_id>/`) and makes no model call. Per expectation it reports, in this order:

  not_attempted  -- the entity type produced no evidence-gathering trace at all (blocked by a prerequisite, or never
                    run): retrieval was never tried, so nothing below can be judged;
  supplied       -- every literal was in a prompt the orchestrator itself built (deterministic context);
  retrieved      -- every literal reached the model: supplied, or returned by one of its read-tool calls;
  cited          -- a record of that entity type cites one of the expectation's blocks (or a cell of that table);
  captured       -- the optional `outcome` check holds on the run's final results.

The distinction the later stages need is exactly `not_attempted` vs `not retrieved` vs `retrieved but not captured`:
the first is a gate problem, the second a retrieval problem, the third an interpretation/validation problem.

Expectations whose evidence is NOT in content.md carry `status`:
  "absent"          -- the document does not state it (verified by `pattern` finding nothing in content.md or the PDF);
  "upstream_absent" -- the PDF states it but content.md does not (a Marker loss, class A): never a retrieval failure.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from pipeline import content_reader
from pipeline.results_store import _results_root as results_root
from pipeline.run_store import _runs_root as runs_root

EXPECTATIONS_DIR = Path(__file__).resolve().parents[1] / "tests" / "replay" / "expectations"
PDF_ROOT = Path(__file__).resolve().parents[1] / "docproc" / "paper"

# Stages in which a model gathers evidence (as opposed to converting or reviewing what was already gathered).
RETRIEVAL_STAGES = ("enumeration", "extraction")
TABLE_ENTITY = "Table"          # expectations about a table itself are scored against the table-classification pass
TABLE_STAGE = "table_classification"
VALID_STATUSES = ("present", "absent", "upstream_absent")


@dataclass(frozen=True)
class Expectation:
    id: str
    entity_type: str
    concept: str
    anchors: tuple[str, ...] = ()
    literals: tuple[str, ...] = ()
    status: str = "present"
    pdf_literals: tuple[str, ...] = ()
    pattern: Optional[str] = None
    outcome: Optional[dict[str, Any]] = None
    note: str = ""
    # How the literals were checked against the PDF when an automatic text search cannot do it (a scanned PDF with no
    # text layer, a table header the text layer splits across lines): e.g. "visual: rendered page 5". Empty means the
    # automatic check applies.
    pdf_verification: str = ""

    @staticmethod
    def from_dict(data: dict[str, Any], default_pdf_verification: str = "") -> "Expectation":
        return Expectation(
            id=data["id"], entity_type=data["entity_type"], concept=data["concept"],
            anchors=tuple(data.get("anchors") or ()), literals=tuple(data.get("literals") or ()),
            status=data.get("status", "present"), pdf_literals=tuple(data.get("pdf_literals") or ()),
            pattern=data.get("pattern"), outcome=data.get("outcome"), note=data.get("note", ""),
            pdf_verification=data.get("pdf_verification", default_pdf_verification),
        )


def load_expectations(paper_id: str, root: Path = EXPECTATIONS_DIR) -> list[Expectation]:
    data = json.loads((root / f"{paper_id}.json").read_text(encoding="utf-8"))
    default = data.get("pdf_verification", "")
    return [Expectation.from_dict(item, default) for item in data["expectations"]]


def corpus_papers(root: Path = EXPECTATIONS_DIR) -> list[str]:
    return sorted(path.stem for path in root.glob("*.json"))


def _collapse(text: str) -> str:
    return " ".join(str(text).split())


def _pdf_norm(text: str) -> str:
    """pdftotext breaks lines and hyphenates; compare on letters and digits only."""
    return re.sub(r"[\W_]+", "", str(text).casefold())


# --------------------------------------------------------------------------- #
# Corpus verification (content.md always; the PDF when its text is available)
# --------------------------------------------------------------------------- #

def verify_expectation(exp: Expectation, blocks: dict[str, str], full_text: str) -> list[str]:
    problems: list[str] = []
    if exp.status not in VALID_STATUSES:
        return [f"{exp.id}: unknown status {exp.status!r}"]
    if exp.status == "present":
        if not exp.anchors or not exp.literals:
            problems.append(f"{exp.id}: a present expectation needs anchors and literals")
        missing_anchors = [a for a in exp.anchors if a not in blocks]
        if missing_anchors:
            problems.append(f"{exp.id}: anchors not in content.md: {missing_anchors}")
        cited_text = _collapse(" ".join(blocks.get(a, "") for a in exp.anchors))
        for literal in exp.literals:
            if _collapse(literal) not in cited_text:
                problems.append(f"{exp.id}: literal {literal!r} is not in block(s) {list(exp.anchors)}")
    else:
        if exp.pattern and re.search(exp.pattern, full_text):
            problems.append(f"{exp.id}: status {exp.status} but pattern {exp.pattern!r} matches content.md")
        if exp.status == "upstream_absent" and not exp.pdf_literals:
            problems.append(f"{exp.id}: upstream_absent needs pdf_literals (the PDF text proving it exists)")
    return problems


def verify_pdf(exp: Expectation, pdf_text: str) -> list[str]:
    if exp.pdf_verification:
        return []
    normalized = _pdf_norm(pdf_text)
    problems = []
    for literal in exp.pdf_literals or (exp.literals if exp.status == "present" else ()):
        if _pdf_norm(literal) not in normalized:
            problems.append(f"{exp.id}: {literal!r} not found in the PDF text")
    if exp.status == "absent" and exp.pattern and re.search(exp.pattern, pdf_text):
        problems.append(f"{exp.id}: status absent but pattern {exp.pattern!r} matches the PDF text")
    return problems


def verify_corpus(paper_id: str, pdf_text: Optional[str] = None, papers_root: Optional[Path] = None) -> list[str]:
    root = papers_root or content_reader.DEFAULT_PAPERS_ROOT
    blocks = content_reader._rendered_block_texts(paper_id, root)
    full_text = content_reader._content_md_path(paper_id, root).read_text(encoding="utf-8")
    problems: list[str] = []
    seen_ids: set[str] = set()
    for exp in load_expectations(paper_id):
        if exp.id in seen_ids:
            problems.append(f"{exp.id}: duplicate id")
        seen_ids.add(exp.id)
        problems += verify_expectation(exp, blocks, full_text)
        if pdf_text is not None:
            problems += verify_pdf(exp, pdf_text)
    return problems


# --------------------------------------------------------------------------- #
# Reading a recorded run
# --------------------------------------------------------------------------- #

def _strings(node: Any) -> Iterable[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _tool_outputs(stdout: str) -> list[str]:
    """Every read-tool OUTPUT in an opencode JSON event stream, JSON-decoded so the literal text is comparable."""
    texts: list[str] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            part = json.loads(line).get("part") or {}
        except (ValueError, AttributeError):
            continue
        if part.get("type") != "tool":
            continue
        output = (part.get("state") or {}).get("output")
        if not isinstance(output, str):
            continue
        try:
            texts.extend(_strings(json.loads(output)))
        except ValueError:
            texts.append(output)
    return texts


@dataclass
class EntityTrace:
    attempted: bool = False
    prompt_text: str = ""
    seen_text: str = ""                     # prompts + every tool output
    cited: set[str] = field(default_factory=set)


def _record_dirs(run_dir: Path, entity_type: str) -> list[Path]:
    prefix = f"{TABLE_STAGE}__" if entity_type == TABLE_ENTITY else f"{entity_type}__"
    records = run_dir / "records"
    return sorted(p for p in records.glob(f"{prefix}*") if p.is_dir()) if records.is_dir() else []


def _payload_anchors(node: Any) -> set[str]:
    anchors: set[str] = set()
    if isinstance(node, dict):
        if isinstance(node.get("block_anchor"), str):
            anchors.add(node["block_anchor"])
        for key in ("anchors", "table_anchors"):
            if isinstance(node.get(key), list):
                anchors.update(a for a in node[key] if isinstance(a, str))
        for key in ("source_table_anchor", "evidence_anchor"):
            if isinstance(node.get(key), str):
                anchors.add(node[key])
        for value in node.values():
            anchors |= _payload_anchors(value)
    elif isinstance(node, list):
        for value in node:
            anchors |= _payload_anchors(value)
    return anchors


def entity_trace(run_dir: Path, entity_type: str) -> EntityTrace:
    trace = EntityTrace()
    stages = (TABLE_STAGE,) if entity_type == TABLE_ENTITY else RETRIEVAL_STAGES
    prompts: list[str] = []
    seen: list[str] = []
    for record_dir in _record_dirs(run_dir, entity_type):
        for stage in stages:
            for attempt in sorted((record_dir / stage).glob("attempt*.json")):
                try:
                    data = json.loads(attempt.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                trace.attempted = True
                prompt = data.get("prompt") or ""
                prompts.append(prompt)
                seen.append(prompt)
                seen.extend(_tool_outputs(data.get("stdout") or ""))
                if stage == "extraction" or entity_type == TABLE_ENTITY:
                    trace.cited |= _payload_anchors(data.get("parsed_json"))
        final = record_dir / "final.json"
        if final.is_file():
            try:
                data = json.loads(final.read_text(encoding="utf-8"))
            except ValueError:
                data = {}
            trace.cited |= _payload_anchors(data.get("payload") or data.get("classification"))
    trace.prompt_text = _collapse("\n".join(prompts))
    trace.seen_text = _collapse("\n".join(seen))
    return trace


def load_results(paper_id: str, run_id: str, root: Optional[Path] = None) -> dict[str, list[dict]]:
    run_results = (root or results_root()) / paper_id / run_id
    by_type: dict[str, list[dict]] = {}
    for path in sorted(run_results.glob("*.json")) + sorted(run_results.glob("*/*.json")):
        if path.name.startswith("_"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(data, dict) and "entity_type" in data:
            by_type.setdefault(data["entity_type"], []).append(data)
    return by_type


def latest_run_id(paper_id: str, root: Optional[Path] = None) -> Optional[str]:
    marker = (root or results_root()) / paper_id / "LATEST"
    return marker.read_text(encoding="utf-8").strip() if marker.is_file() else None


# --------------------------------------------------------------------------- #
# Outcome checks
# --------------------------------------------------------------------------- #

def _field_text(payload: dict, field_name: str) -> tuple[Optional[str], Optional[str]]:
    """(provenance_label, value-as-text) of one top-level payload field; (None, None) when absent."""
    value = (payload or {}).get(field_name)
    if isinstance(value, dict) and "provenance_label" in value:
        inner = value.get("value")
        return value["provenance_label"], None if inner is None else json.dumps(inner, ensure_ascii=False)
    if value is None:
        return None, None
    return "REFERENCE", json.dumps(value, ensure_ascii=False)


def _contains_any(text: Optional[str], needles: list[str]) -> bool:
    return text is not None and any(n.casefold() in text.casefold() for n in needles)


def check_outcome(outcome: dict[str, Any], records: list[dict]) -> bool:
    """`outcome` = {"field": name or [names] (any-of), optional "contains_any": [...], optional "label_in": [...] (default EXTRACTED /
    INFERRED), optional "where": {"field": name, "contains_any": [...], optional "not_contains_any": [...]},
    optional "status_in": [...] (default ["ready"])}. True when ANY record of the entity type satisfies every given
    condition. A field is only captured in a record the run actually committed: a value sitting in an unresolved or
    blocked record never counts."""
    labels = outcome.get("label_in") or ["EXTRACTED", "INFERRED"]
    statuses = outcome.get("status_in") or ["ready"]
    for record in records:
        if record.get("status") not in statuses:
            continue
        payload = record.get("payload") or {}
        where = outcome.get("where")
        if where:
            subject = _field_text(payload, where["field"])[1]
            if not _contains_any(subject, where["contains_any"]):
                continue
            if where.get("not_contains_any") and _contains_any(subject, where["not_contains_any"]):
                continue
        fields = outcome["field"] if isinstance(outcome["field"], list) else [outcome["field"]]
        for field_name in fields:      # a list of fields is any-of: the source's value may sit in either
            label, text = _field_text(payload, field_name)
            if label is None or (label != "REFERENCE" and label not in labels):
                continue
            if outcome.get("contains_any") and not _contains_any(text, outcome["contains_any"]):
                continue
            return True
    return False


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

def _expanded_anchors(anchors: Iterable[str], provenance: Optional[dict]) -> set[str]:
    """A table anchor stands for every one of its cells too (read_table_row/_cell hand out cell anchors)."""
    wanted = set(anchors)
    if provenance:
        wanted |= {a for a, meta in provenance.items() if meta.get("parent_table_anchor") in wanted}
    return wanted


def score_expectation(exp: Expectation, trace: EntityTrace, records: list[dict], provenance: Optional[dict]) -> dict:
    row: dict[str, Any] = {"id": exp.id, "entity_type": exp.entity_type, "concept": exp.concept, "status": exp.status}
    if exp.status != "present":
        row["state"] = exp.status
        row["captured"] = check_outcome(exp.outcome, records) if exp.outcome else None
        return row
    if not trace.attempted:
        reasons = sorted({str(r.get("reason"))[:160] for r in records if r.get("status") == "blocked"})
        row.update(state="not_attempted", supplied=False, retrieved=False, cited=False, captured=False,
                   blocked_reason=reasons[0] if reasons else None)
        return row
    literals = [_collapse(lit) for lit in exp.literals]
    row["supplied"] = all(lit in trace.prompt_text for lit in literals)
    row["retrieved"] = all(lit in trace.seen_text for lit in literals)
    row["cited"] = bool(_expanded_anchors(exp.anchors, provenance) & trace.cited)
    row["captured"] = check_outcome(exp.outcome, records) if exp.outcome else None
    if row["captured"]:
        row["state"] = "captured"
    elif row["retrieved"]:
        row["state"] = "retrieved_not_captured" if exp.outcome else "retrieved"
    else:
        row["state"] = "not_retrieved"
    return row


def score_run(paper_id: str, run_id: str, runs_dir: Optional[Path] = None, results_dir: Optional[Path] = None,
              papers_root: Optional[Path] = None) -> dict[str, Any]:
    run_dir = (runs_dir or runs_root()) / run_id
    provenance = content_reader._load_provenance(paper_id, papers_root or content_reader.DEFAULT_PAPERS_ROOT)
    results = load_results(paper_id, run_id, results_dir)
    traces: dict[str, EntityTrace] = {}
    rows = []
    for exp in load_expectations(paper_id):
        if exp.entity_type not in traces:
            traces[exp.entity_type] = entity_trace(run_dir, exp.entity_type)
        rows.append(score_expectation(exp, traces[exp.entity_type], results.get(exp.entity_type, []), provenance))
    return {"paper_id": paper_id, "run_id": run_id, "expectations": rows, "summary": summarize(rows)}


def summarize(rows: list[dict]) -> dict[str, Any]:
    present = [r for r in rows if r["status"] == "present"]
    attempted = [r for r in present if r["state"] != "not_attempted"]
    with_outcome = [r for r in present if r.get("captured") is not None]

    def count(key: str, pool: list[dict]) -> int:
        return sum(1 for r in pool if r.get(key))

    return {
        "present": len(present),
        "not_attempted": len(present) - len(attempted),
        "attempted": len(attempted),
        "supplied": count("supplied", attempted),
        "retrieved": count("retrieved", attempted),
        "cited": count("cited", attempted),
        "outcome_checks": len(with_outcome),
        "captured": count("captured", with_outcome),
        "absent": sum(1 for r in rows if r["status"] == "absent"),
        "upstream_absent": sum(1 for r in rows if r["status"] == "upstream_absent"),
    }


# --------------------------------------------------------------------------- #
# Packet recall (Stage 4): which verified evidence the pipeline now SUPPLIES up front
# --------------------------------------------------------------------------- #

def packet_recall(paper_id: str, papers_root: Optional[Path] = None) -> list[dict[str, Any]]:
    """For every present expectation: is its evidence in the enumeration packet of its entity type (the packet every
    candidate of that type starts from), with every literal verbatim? Deterministic, no model call. Entity types
    without a packet strategy report `packet: None`."""
    from pipeline.context_bundle import build_context_bundle

    root = papers_root or content_reader.DEFAULT_PAPERS_ROOT
    rows = []
    packets: dict[str, Any] = {}
    for exp in load_expectations(paper_id):
        if exp.status != "present":
            continue
        if exp.entity_type not in packets:
            packets[exp.entity_type] = build_context_bundle(paper_id, exp.entity_type, "enumeration", papers_root=root)
        bundle = packets[exp.entity_type]
        if bundle is None:
            rows.append({"id": exp.id, "entity_type": exp.entity_type, "packet": None})
            continue
        text = _collapse(" ".join(item.text for item in bundle.items))
        rows.append({
            "id": exp.id, "entity_type": exp.entity_type, "packet": bundle.bundle_id,
            "supplied": all(_collapse(lit) in text for lit in exp.literals),
            "anchors_in_packet": sorted(set(exp.anchors) & set(bundle.anchors)),
        })
    return rows


# --------------------------------------------------------------------------- #
# Offline revalidation of recorded conversion attempts
# --------------------------------------------------------------------------- #

def revalidate_record(paper_id: str, entity_type: str, record_dir: Path) -> list[dict[str, Any]]:
    """Every recorded conversion (and correction) attempt of one record, re-checked with the CURRENT deterministic
    path -- the pipeline's own payload post-processing (coordinate transformation) followed by exactly what the IR
    service's propose_record runs (schema construction, provenance grounding, readiness) minus the whole-graph
    dataset context. The model's output is the recorded one: this measures what the deterministic layer now does with
    it, never what a new model call would produce."""
    import copy

    from pipeline import coordinates
    from pipeline.ir_service import _construction_errors
    from pipeline.validators import readiness_issues, validate_provenance

    rows = []
    for stage in ("conversion", "correction"):
        for attempt in sorted((record_dir / stage).glob("attempt*.json")):
            data = json.loads(attempt.read_text(encoding="utf-8"))
            payload = data.get("parsed_json")
            if not isinstance(payload, dict):
                continue
            verdict_path = record_dir / f"{stage}_validation" / attempt.name
            recorded = json.loads(verdict_path.read_text(encoding="utf-8")) if verdict_path.is_file() else {}
            payload = coordinates.apply_coordinate_transformations(entity_type, copy.deepcopy(payload))
            errors = _construction_errors(entity_type, payload)
            errors += [issue.to_dict() for issue in validate_provenance(paper_id, payload)]
            readiness = [] if errors else [i.to_dict() for i in readiness_issues(entity_type, payload, record_dir.name)]
            rows.append({
                "record": record_dir.name, "stage": stage, "attempt": attempt.stem,
                "recorded_valid": recorded.get("valid"),
                "recorded_errors": [e.get("message", "")[:160] for e in recorded.get("errors", [])],
                "now_valid": not errors, "now_ready": not errors and not readiness,
                "now_errors": [str(e.get("message", ""))[:160] for e in errors],
            })
    return rows


def revalidate_run(paper_id: str, run_id: str, entity_types: Iterable[str], runs_dir: Optional[Path] = None) -> list[dict]:
    run_dir = (runs_dir or runs_root()) / run_id
    rows: list[dict] = []
    for entity_type in entity_types:
        for record_dir in _record_dirs(run_dir, entity_type):
            rows += revalidate_record(paper_id, entity_type, record_dir)
    return rows


# --------------------------------------------------------------------------- #
# Offline re-decision of recorded outcomes (Stage 2: gates and field-level settling)
# --------------------------------------------------------------------------- #

def _recorded_run_records(run_dir: Path) -> dict[str, list[dict]]:
    """this_run_records as the orchestrator held them, rebuilt from every record's final.json."""
    records: dict[str, list[dict]] = {}
    for final in sorted((run_dir / "records").glob("*/final.json")):
        key = final.parent.name
        entity_type = key.split("__", 1)[0]
        if entity_type == TABLE_STAGE:
            continue
        data = json.loads(final.read_text(encoding="utf-8"))
        info = {"entity_type": entity_type, "record_id": data.get("record_id") or key.split("__", 1)[1],
                "status": data.get("status"), "detail": data}
        if data.get("status") == "blocked":
            info["reason"] = data.get("reason")
        records.setdefault(entity_type, []).append(info)
    return records


def _still_flagged_after_correction(record_dir: Path, detail: dict) -> Optional[tuple[dict, dict]]:
    """(corrected payload, second verdict) of a record the run left unresolved because the AI validator still flagged
    it after a correction that DID pass validation -- `_finalize_unresolved` keeps neither in final.json's `payload` /
    `ai_validation`, so both are read back from the record's own stage artifacts."""
    if not any("still flagged this record as suspicious" in str(e.get("message")) for e in detail.get("last_errors") or []):
        return None
    second = record_dir / "ai_validation" / "attempt2.json"
    if not second.is_file() or not isinstance(detail.get("last_candidate_payload"), dict):
        return None
    verdict = json.loads(second.read_text(encoding="utf-8")).get("parsed_json") or {}
    return detail["last_candidate_payload"], verdict


def redecide_run(paper_id: str, run_id: str, runs_dir: Optional[Path] = None) -> dict[str, Any]:
    """What the CURRENT deterministic decisions would do with a recorded run's own outcomes, no model call:
      - settled:   each record left unresolved by an unanswered AI concern, re-decided by field-level settling
                   (demotion + the same construction/provenance/readiness checks);
      - unblocked: each entity type the run blocked on a prerequisite, re-decided by the identity-based gate over the
                   recorded (and re-settled) prerequisite records. Unblocked means "would now be ATTEMPTED" -- what it
                   would extract needs a live run."""
    import copy

    from pipeline import orchestrator
    from pipeline.ir_service import _construction_errors
    from pipeline.validators import readiness_issues, validate_provenance

    run_dir = (runs_dir or runs_root()) / run_id
    records = _recorded_run_records(run_dir)
    # A blocked multi-record entity has no records/ directory: its blocked entry lives only in the results.
    for entity_type, results in load_results(paper_id, run_id).items():
        if entity_type not in records:
            records[entity_type] = [
                {"entity_type": entity_type, "record_id": r.get("record_id"), "status": r.get("status"),
                 "reason": r.get("reason"), "detail": {}}
                for r in results if r.get("status") == "blocked"
            ]
    settled_rows = []
    for entity_type, infos in records.items():
        for info in infos:
            detail = info["detail"]
            if info["status"] != "unresolved":
                continue
            if detail.get("unresolved_by") == "ai_validation":
                payload, verdict = detail.get("payload"), orchestrator._scope_ai_concerns(detail.get("ai_validation") or {})
                if verdict.get("verdict") == "plausible":
                    # Phase A3: every concern was about a pipeline-owned field -- nothing left to settle.
                    settled_rows.append({"entity_type": entity_type, "record_id": info["record_id"], "settled": True,
                                         "now_status": "ready", "withdrawn": [], "open_concerns": [],
                                         "errors": [], "scoped_out": [i.get("field") for i in verdict.get("scoped_out", [])]})
                    info["status"] = "ready"
                    info["detail"] = {**detail, "ai_validation": verdict}
                    continue
            else:
                flagged = _still_flagged_after_correction(run_dir / "records" / f"{entity_type}__{info['record_id']}", detail)
                if flagged is None:
                    continue
                payload, verdict = flagged
                verdict = orchestrator._scope_ai_concerns(verdict)
                if verdict.get("verdict") == "plausible":
                    settled_rows.append({"entity_type": entity_type, "record_id": info["record_id"], "settled": True,
                                         "now_status": "ready", "withdrawn": [], "open_concerns": [], "errors": [],
                                         "scoped_out": [i.get("field") for i in verdict.get("scoped_out", [])]})
                    info["status"] = "ready"
                    info["detail"] = {**detail, "payload": payload, "ai_validation": verdict}
                    continue
            demotion = orchestrator._demote_concerned_fields(entity_type, copy.deepcopy(payload), verdict)
            row = {"entity_type": entity_type, "record_id": info["record_id"], "settled": False}
            if demotion is not None:
                demoted, demotions, open_concerns = demotion
                errors = _construction_errors(entity_type, demoted) + [i.to_dict() for i in validate_provenance(paper_id, demoted)]
                ready = not errors and not readiness_issues(entity_type, demoted, info["record_id"])
                row.update(settled=not errors, now_status="ready" if ready else "unresolved",
                           withdrawn=[f'{d["field"]}({"label->INFERRED" if d["rule"] == "ai_concern_label_downgraded" else "value withdrawn"})' for d in demotions], open_concerns=[c["field"] for c in open_concerns],
                           errors=[str(e.get("message"))[:140] for e in errors])
                if not errors:
                    # as committed live: ready, or unresolved WITH its (valid) payload via _finalize_not_ready
                    info["status"] = "ready" if ready else "unresolved"
                    info["detail"] = {**detail, "payload": demoted, "ai_validation": verdict}
            settled_rows.append(row)
    # Records committed READY that the current readiness rules would refuse (Phase A5), and Crops whose species link
    # their own evidence does not support (Phase A2).
    now_refused = []
    for entity_type, infos in records.items():
        for info in infos:
            if info["status"] != "ready":
                continue
            payload = (info["detail"] or {}).get("payload") or {}
            issues = readiness_issues(entity_type, payload, info["record_id"])
            if issues:
                now_refused.append({"entity_type": entity_type, "record_id": info["record_id"], "rule": "readiness",
                                    "why": [i.code for i in issues]})
                info["status"] = "unresolved"
            if entity_type == "Crop" and payload.get("species_id"):
                seeds = [loc.get("block_anchor") for v in payload.values() if isinstance(v, dict)
                         for loc in ((v.get("source") or {}).get("locators") or []) if loc.get("block_anchor")]
                ids, decision = orchestrator._evidenced_species(paper_id, type("C", (), {"anchors": seeds or []})(), records)
                if payload["species_id"] not in ids:
                    now_refused.append({"entity_type": entity_type, "record_id": info["record_id"], "rule": "species_link",
                                        "why": [decision.get("message") or f"linked to {payload['species_id']}"]})
                    info["status"] = "unresolved"
    unblocked_rows = []
    for entity_type, infos in records.items():
        for info in infos:
            if info["status"] != "blocked":
                continue
            _refs, reason = orchestrator._resolve_known_refs(entity_type, records)
            unblocked_rows.append({"entity_type": entity_type, "was": str(info.get("reason"))[:120],
                                   "now_blocked": reason is not None, "now_reason": reason,
                                   "notes": orchestrator._prerequisite_notes(entity_type, records) if reason is None else []})
    return {"paper_id": paper_id, "run_id": run_id, "settled": settled_rows, "unblocked": unblocked_rows,
            "now_refused": now_refused}


# --------------------------------------------------------------------------- #
# Observation replay (Phase D): compose each table cell's experimental context from a recorded run
# --------------------------------------------------------------------------- #

def observation_replay(paper_id: str, run_id: str, runs_dir: Optional[Path] = None) -> dict[str, Any]:
    """From a recorded run's table classifications and records, with the CURRENT deterministic path and no model call:
    every table cell's experimental context (method from the map's deterministic tiers, aggregation from design.py,
    statistics from the cell), the cells blocked by representation, and which Observations the run committed READY
    that the Phase D relationship checks would now send back."""
    import collections
    import types

    from pipeline import document_map, evidence_index, method_map, orchestrator, design as design_mod
    from pipeline.raw_schema import TableClassification

    run_dir = (runs_dir or runs_root()) / run_id
    records = _recorded_run_records(run_dir)
    classifications = {}
    for final in sorted((run_dir / "records").glob("table_classification__*/final.json")):
        data = json.loads(final.read_text(encoding="utf-8")).get("classification")
        if data:
            classifications[final.parent.name.split("__", 1)[1]] = TableClassification.model_validate(data)
    index = evidence_index.build_evidence_index(document_map.build_document_map(paper_id))
    summary = design_mod.design_summary(classifications, index)
    methods_ready = [{**r, "slug": orchestrator._candidate_slug_from_record_id(paper_id, "Method", r["record_id"])}
                     for r in records.get("Method", []) if r["status"] == "ready"]
    methods = method_map.method_evidence(methods_ready)
    entries = method_map.collect_variables(classifications.values())
    links = method_map.build_map(entries, methods, index, None, None) if len(methods) > 1 else {}
    blocked: list[dict] = []
    counts: collections.Counter = collections.Counter()
    examples = []
    for classification in classifications.values():
        for candidate in orchestrator._table_classification_to_candidates(classification, {}, links, blocked):
            known = {}
            method_slug = (candidate.linked_candidates or {}).get("method_id")
            if len(methods_ready) == 1:
                known["method_id"] = methods_ready[0]["record_id"]
            context, _ = orchestrator._observation_experimental_context(paper_id, candidate, known, records, links, summary)
            counts["cells"] += 1
            counts[f"method_{context['method']['status']}"] += 1
            counts[f"scope_{context['aggregation']['reported_effect_scope']}"] += 1
            stats = context.get("statistics") or {}
            counts["statistic_named" if stats.get("statistic_name") else "statistic_unnamed_or_none"] += 1
            if len(examples) < 3:
                examples.append({"cell": context["cell"], "method": context["method"].get("record_id"),
                                 "aggregation": context["aggregation"], "statistic": stats.get("statistic_name")})
    # READY Observations of the run vs the relationship checks (their own recorded candidate context + composition)
    now_rejected = []
    for info in records.get("Observation", []):
        if info["status"] != "ready":
            continue
        candidate_ctx = None
        attempt = run_dir / "records" / f"Observation__{info['record_id']}" / "extraction" / "attempt1.json"
        payload = (info["detail"] or {}).get("payload") or {}
        for classification in classifications.values():
            for candidate in orchestrator._table_classification_to_candidates(classification, {}, links):
                if info["record_id"].endswith("_" + candidate.candidate_id):
                    candidate_ctx, _ = orchestrator._observation_experimental_context(
                        paper_id, candidate, {k: payload.get(k) for k in ("method_id", "treatment_id", "variable_id", "site_id")},
                        records, links, summary)
        if candidate_ctx is None:
            continue
        errors = orchestrator._observation_relationship_errors("Observation", payload, {"experimental_context": candidate_ctx})
        if errors:
            now_rejected.append({"record_id": info["record_id"], "errors": [e["message"][:160] for e in errors]})
    return {"paper_id": paper_id, "run_id": run_id, "counts": dict(counts), "blocked_by_representation": len(blocked),
            "method_map": {s: sum(1 for l in links.values() if l.status == s) for s in ("linked", "ambiguous", "none")},
            "conflicts": summary["conflicts"], "examples": examples, "ready_now_rejected": now_rejected}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _pdf_text(paper_id: str) -> Optional[str]:
    import shutil
    import subprocess

    pdf = PDF_ROOT / f"{paper_id}.pdf"
    if not pdf.is_file() or not shutil.which("pdftotext"):
        return None
    return subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True, check=True).stdout


def _format_report(report: dict[str, Any]) -> str:
    lines = [f"== {report['paper_id']}  run={report['run_id']}"]
    for row in report["expectations"]:
        flags = "".join(
            ("S" if row.get("supplied") else "-", "R" if row.get("retrieved") else "-", "C" if row.get("cited") else "-")
        ) if row["status"] == "present" and row["state"] != "not_attempted" else "   "
        captured = {True: "cap", False: "MISS", None: "   "}[row.get("captured")]
        lines.append(f"  {row['state']:<24} {flags} {captured:<4} {row['entity_type']:<11} {row['id']}")
        if row.get("blocked_reason"):
            lines.append(f"      blocked: {row['blocked_reason']}")
    s = report["summary"]
    lines.append(
        f"  summary: present={s['present']} not_attempted={s['not_attempted']} attempted={s['attempted']} "
        f"supplied={s['supplied']} retrieved={s['retrieved']} cited={s['cited']} "
        f"captured={s['captured']}/{s['outcome_checks']} absent={s['absent']} upstream_absent={s['upstream_absent']}"
    )
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Score recorded runs against the evidence replay corpus.")
    parser.add_argument("--paper", action="append", help="paper id (repeatable); default: every corpus paper")
    parser.add_argument("--run", help="run id to score (default: the paper's LATEST)")
    parser.add_argument("--verify", action="store_true", help="only verify the corpus against content.md and the PDF")
    parser.add_argument("--json", type=Path, help="also write the full report as JSON to this path")
    parser.add_argument("--observations", action="store_true",
                        help="compose each table cell's experimental context from a recorded run (Phase D), no model call")
    parser.add_argument("--packets", action="store_true",
                        help="report which verified evidence each entity type's enumeration packet supplies up front")
    parser.add_argument("--redecide", action="store_true",
                        help="re-decide recorded unresolved/blocked outcomes with the current gates and settling")
    parser.add_argument("--revalidate", metavar="ENTITY", action="append",
                        help="re-check this entity type's recorded conversion attempts with the current validators")
    args = parser.parse_args(argv)

    papers = args.paper or corpus_papers()
    if args.observations:
        for paper_id in papers:
            run_id = args.run or latest_run_id(paper_id)
            if not run_id:
                continue
            report = observation_replay(paper_id, run_id)
            print(f"== {paper_id} run={run_id}")
            print(f"  cells: {report['counts']}")
            print(f"  blocked_by_representation: {report['blocked_by_representation']}   method map: {report['method_map']}")
            print(f"  conflicts: {[(c['claim_a'], c['claim_b'], c['source_b']) for c in report['conflicts']]}")
            for row in report["ready_now_rejected"]:
                print(f"  READY now rejected: {row['record_id']}: {row['errors']}")
        return 0
    if args.packets:
        covered = supplied = 0
        for paper_id in papers:
            rows = [r for r in packet_recall(paper_id) if r["packet"]]
            covered += len(rows)
            supplied += sum(r["supplied"] for r in rows)
            print(f"{paper_id}: {sum(r['supplied'] for r in rows)}/{len(rows)} supplied by the packets")
            for row in rows:
                if not row["supplied"]:
                    print(f"  missing  {row['entity_type']:<11} {row['id']}")
        print(f"TOTAL {supplied}/{covered}")
        return 0
    if args.redecide:
        for paper_id in papers:
            run_id = args.run or latest_run_id(paper_id)
            if not run_id:
                continue
            report = redecide_run(paper_id, run_id)
            print(f"== {paper_id} run={run_id}")
            for row in report["settled"]:
                outcome = row.get("now_status", "unchanged (not settleable field by field)")
                print(f"  settle   {row['entity_type']:<11} {row['record_id'][-48:]:<48} -> {outcome}"
                      + (f"  scoped_out={row['scoped_out']}" if row.get("scoped_out") else "")
                      + (f"  demoted={row['withdrawn']}" if row.get("withdrawn") else "")
                      + (f"  open={row['open_concerns']}" if row.get("open_concerns") else ""))
                for message in row.get("errors") or []:
                    print(f"      error: {message}")
            for row in report["now_refused"]:
                print(f"  refuse   {row['entity_type']:<11} {row['record_id'][-48:]:<48} ({row['rule']}) {str(row['why'])[:150]}")
            for row in report["unblocked"]:
                state = f"still blocked: {row['now_reason'][:110]}" if row["now_blocked"] else "would now be ATTEMPTED"
                print(f"  gate     {row['entity_type']:<11} {state}")
                for note in row["notes"]:
                    print(f"      via non-ready {note['prerequisite']} {note['record_id'][-40:]} open={note['open_fields']}")
        return 0
    if args.revalidate:
        for paper_id in papers:
            run_id = args.run or latest_run_id(paper_id)
            if not run_id:
                continue
            for row in revalidate_run(paper_id, run_id, args.revalidate):
                change = "" if row["recorded_valid"] == row["now_valid"] else "  <-- CHANGED"
                print(f"{paper_id} {row['record'][-58:]:<58} {row['stage']}/{row['attempt']}: "
                      f"recorded valid={row['recorded_valid']} now valid={row['now_valid']} ready={row['now_ready']}{change}")
                for message in row["now_errors"]:
                    print(f"      now: {message}")
        return 0
    if args.verify:
        failed = False
        for paper_id in papers:
            pdf_text = _pdf_text(paper_id)
            problems = verify_corpus(paper_id, pdf_text)
            note = "" if pdf_text is not None else " (PDF text unavailable: content.md only)"
            print(f"{paper_id}: {'OK' if not problems else f'{len(problems)} problem(s)'}{note}")
            for problem in problems:
                print(f"  - {problem}")
            failed |= bool(problems)
        return 1 if failed else 0

    reports = []
    for paper_id in papers:
        run_id = args.run or latest_run_id(paper_id)
        if not run_id:
            print(f"{paper_id}: no run to score")
            continue
        report = score_run(paper_id, run_id)
        reports.append(report)
        print(_format_report(report))
    if args.json:
        args.json.write_text(json.dumps(reports, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
