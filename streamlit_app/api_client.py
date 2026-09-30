"""
api_client.py
================
This module is the ONLY place the Streamlit UI talks to for data --
unchanged promise from the original skeleton. It now reads REAL Sage
artifacts (src/results/, src/paper/, src/pipeline/corrections_store)
instead of mock_data.py. UI components (app.py, components/*) must keep
calling only functions in this file -- never import pipeline.* or
mock_data directly.

No extraction/business logic lives here beyond simple read-shaping and
delegating to existing pipeline modules (results_store, corrections_store,
validators) -- this file never re-implements Sage's schema or validation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import sage_paths  # wires sys.path to src/ and src/docproc/

from pipeline import corrections_store, results_store
from pipeline.ir_schema import ENTITY_MODELS
from pipeline.validators import _value_supported_by_text, _load_rendered_blocks

ENTITY_TYPES: list[str] = list(ENTITY_MODELS.keys())  # canonical order, straight from the real schema

REVIEW_ACTIONS = sorted(corrections_store.VALID_ACTIONS)


# ---------------------------------------------------------------------------
# Paper library
# ---------------------------------------------------------------------------

def _run_status_counts(paper_id: str, run_id: Optional[str]) -> dict[str, int]:
    """{status: record count} over every entity type of ONE results set (a run id, `results_store.LEGACY_RUN`, or
    None for the default LATEST/legacy resolution)."""
    counts: dict[str, int] = {}
    for entity_type in ENTITY_TYPES:
        for result in results_store.load_any_entity_results(paper_id, entity_type, run_id):
            status = result.get("status")
            counts[status] = counts.get(status, 0) + 1
    return counts


def _is_reviewable(counts: dict[str, int]) -> bool:
    return bool(counts.get("ready") or counts.get("unresolved"))


def _candidate_runs(paper_id: str) -> list[str]:
    """Every results set this paper has, in the order they are preferred for review: the LATEST completed run, then
    the other runs newest first, then the legacy flat layout (results written before per-run directories)."""
    latest = results_store.latest_run_id(paper_id)
    runs = results_store.list_run_ids_newest_first(paper_id)
    ordered = ([latest] if latest in runs else []) + [r for r in runs if r != latest]
    if results_store.has_legacy_results(paper_id):
        ordered.append(results_store.LEGACY_RUN)
    return ordered


def list_result_runs(paper_id: str) -> list[dict]:
    """One entry per results set of this paper (see `_candidate_runs` for the order): its run id, a label, its status
    counts, whether it has reviewable records, and whether it is the LATEST completed run."""
    latest = results_store.latest_run_id(paper_id)
    rows = []
    for run_id in _candidate_runs(paper_id):
        counts = _run_status_counts(paper_id, run_id)
        label = "earlier results (before per-run folders)" if run_id == results_store.LEGACY_RUN else run_id
        if run_id == latest:
            label += " (latest)"
        rows.append({
            "run_id": run_id, "label": label, "counts": counts,
            "reviewable": _is_reviewable(counts), "is_latest": run_id == latest,
        })
    return rows


def default_review_run(paper_id: str) -> Optional[str]:
    """The results set the review opens by default: the LATEST run when it has reviewable (ready/unresolved) records,
    otherwise the newest run that has, so a failed latest run (e.g. Citation error -> everything blocked) never hides
    real, earlier results. When no run has any, the first candidate (so its failure can still be inspected), or None."""
    candidates = _candidate_runs(paper_id)
    for run_id in candidates:
        if _is_reviewable(_run_status_counts(paper_id, run_id)):
            return run_id
    return candidates[0] if candidates else None


def is_extracted(paper_id: str) -> bool:
    """True when ANY results set of this paper (any run, or the legacy flat layout) has at least one real record to
    review (ready/unresolved) -- distinct from `processed` (Marker/document preparation done), which says nothing about
    whether the extraction pipeline itself has ever been run.

    Deliberately NOT `marker_pipeline._needs_extraction` inverted: that function answers a different question ("should
    the batch pipeline re-run extraction for this paper"), and its "any single error anywhere means treat the WHOLE
    paper as not extracted" rule is right for that. Whether a paper has data worth reviewing is a much lower bar -- ANY
    real ready/unresolved record, regardless of unrelated errors elsewhere (real case: Oceologia-1998, 89 of 92 records
    ready/unresolved and 2 stray errors). Nor is it only the LATEST run: a latest run that failed at Citation (everything
    else blocked) must not hide an earlier run's real results (real case: Oceologia-1998 and Paul-1998-Foliar, whose
    latest runs failed while their earlier flat-layout results hold 81 and 39 ready records)."""
    return any(_is_reviewable(_run_status_counts(paper_id, run_id)) for run_id in _candidate_runs(paper_id))


def has_error_records(paper_id: str, run_id: Optional[str] = None) -> bool:
    """True when the given results set (default: `default_review_run`) includes at least one entity/record that ended
    in "error" -- read from real, on-disk state, so the library's "with errors" status survives a restart."""
    run_id = run_id if run_id is not None else default_review_run(paper_id)
    return bool(_run_status_counts(paper_id, run_id).get("error"))


def list_papers() -> list[dict]:
    """The Stored Papers listing -- one row per paper_id discovered from
    real PDFs on disk (sage_paths.list_source_paper_ids()), each cross-
    referenced against its own processed/extracted/error state. `processed`
    and `extracted` are deliberately separate signals: a paper can be
    processed (Marker + document preparation done) without ever having
    been extracted yet.

    Deliberately independent of list_extracted_papers() below -- a real,
    confirmed bug this fixes: this function used to be the ONLY listing,
    so deleting a paper's PDF made it disappear from BOTH Stored Papers
    AND Extracted Papers, even when real, reviewable results/<paper_id>/
    data was still sitting on disk untouched. Each section now enumerates
    from its own real source of truth (PDFs here; results/ there)."""
    rows = []
    for paper_id in sage_paths.list_source_paper_ids():
        processed = sage_paths.is_processed(paper_id)
        extracted = is_extracted(paper_id) if processed else False
        has_errors = has_error_records(paper_id) if extracted else False
        summary = review_summary(paper_id) if extracted else None
        rows.append({
            "paper_id": paper_id,
            "has_pdf": sage_paths.has_pdf(paper_id),
            "processed": processed,
            "extracted": extracted,
            "has_errors": has_errors,
            "summary": summary,
        })
    return rows


def list_extracted_papers() -> list[dict]:
    """The Extracted Papers listing -- one row for EVERY paper_id with a results/<paper_id>/ directory on disk
    (results_store.list_paper_ids()), independent of whether that paper's PDF is still in the library (a real,
    confirmed bug: deleting a PDF used to hide its results) and of whether its latest run succeeded (a real,
    confirmed bug: a latest run that failed at Citation hid the paper entirely, earlier real results included).

    Each row names the results set the review opens (`review_run`, see `default_review_run`), whether that is the
    LATEST run, and -- when no results set has anything to review -- `reviewable=False` with `failure` saying why (the
    first error reason recorded), so every extracted paper stays visible and inspectable."""
    rows = []
    for paper_id in results_store.list_paper_ids():
        runs = _candidate_runs(paper_id)
        review_run = default_review_run(paper_id)
        counts = _run_status_counts(paper_id, review_run) if review_run is not None else {}
        reviewable = _is_reviewable(counts)
        rows.append({
            "paper_id": paper_id,
            "has_pdf": sage_paths.has_pdf(paper_id),
            "reviewable": reviewable,
            "has_errors": bool(counts.get("error")),
            "review_run": review_run,
            # True also when the paper has no LATEST pointer (legacy-only results): nothing newer exists to fall back from.
            "review_run_is_latest": results_store.latest_run_id(paper_id) in (None, review_run),
            "run_count": len(runs),
            "counts": counts,
            "failure": None if reviewable else _first_error_reason(paper_id, review_run),
            "summary": review_summary(paper_id, review_run) if reviewable else None,
        })
    return rows


def _first_error_reason(paper_id: str, run_id: Optional[str]) -> Optional[str]:
    """The reason of the first entity result that ended in "error" in this results set, in entity order (the one that
    blocked the others, e.g. Citation), or None."""
    if run_id is None:
        return None
    for entity_type in ENTITY_TYPES:
        for result in results_store.load_any_entity_results(paper_id, entity_type, run_id):
            if result.get("status") == "error":
                return f"{entity_type}: {result.get('reason') or 'no reason recorded'}"
    return None


def upload_pdfs(files: list[tuple[str, bytes]]) -> list[str]:
    """Save uploaded PDFs into the documented convention (src/docproc/paper/).
    Returns the paper_ids saved. Does not process them -- that's a separate,
    explicit action (run_marker_processing)."""
    sage_paths.PDF_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    for filename, content in files:
        paper_id = Path(filename).stem
        dest = sage_paths.PDF_DIR / f"{paper_id}.pdf"
        dest.write_bytes(content)
        saved.append(paper_id)
    return saved


def delete_paper(paper_id: str) -> list[str]:
    """Remove ONLY a paper's uploaded PDF from the library (see
    sage_paths.delete_paper_source's own docstring for exactly what this
    does and does NOT touch, and why -- neither the derived Marker JSON/
    rendered document nor any prior committed extraction in
    ir-store/results/runs is ever deleted by this action). Returns the
    paths actually removed, for the UI to report."""
    return sage_paths.delete_paper_source(paper_id)


def delete_extracted_records(paper_id: str) -> list[str]:
    """Remove this paper's DERIVED, regenerable results/<paper_id>/
    snapshot -- the exact thing is_extracted()/get_review_data() read, so
    this makes the paper stop appearing as "Extracted" (it goes back to
    "Processed — not yet extracted", or vanishes entirely from the library
    if the PDF was also removed) and lets it be re-run from scratch.

    Deliberately does NOT touch ir-store/<paper_id>.jsonl, the append-only
    ledger every commit/flag ever made is recorded in (see pipeline.store's
    own "immutable, append-only" docstring) -- that history is never
    deleted by any action in this UI. "Delete records" here means clearing
    the current reviewable snapshot, not erasing the paper's extraction
    history; a future orchestrator.run_paper() call for the same paper_id
    still appends fresh entries to ir-store exactly as it always has, and
    results_store.save_multi_entity_results/save_entity_result rebuild
    results/<paper_id>/ from that the next time extraction runs.

    Also does not touch runs/ (the per-attempt raw artifact log) -- that's
    debug/audit history, not reviewable data, and isn't paper-scoped by
    directory name in a way that's safe to bulk-delete from here.

    Returns the paths actually removed (empty if nothing was there)."""
    import shutil
    from pipeline import results_store

    paper_dir = results_store.paper_dir(paper_id)
    if not paper_dir.is_dir():
        return []
    shutil.rmtree(paper_dir)
    return [str(paper_dir)]


def rename_stored_paper(old_paper_id: str, new_paper_id: str) -> tuple[bool, str]:
    """Renames a paper in the Stored Papers library -- its PDF and, if
    present, its derived Marker JSON and rendered document (see
    sage_paths.rename_paper_source's own docstring). Does NOT rename any
    extracted records for this paper_id (see rename_extracted_paper below,
    a deliberately separate action -- the two listings are independent, so
    a paper may have one without the other). Returns (True, message) on
    success or (False, reason) if refused (e.g. the new name is taken)."""
    return sage_paths.rename_paper_source(old_paper_id, new_paper_id)


def rename_extracted_paper(old_paper_id: str, new_paper_id: str) -> tuple[bool, str]:
    """Renames a paper's extracted records -- moves results/<paper_id>/ to
    the new name and renames the ir-store/<paper_id>.jsonl file to match
    (never rewrites either's actual content -- see
    results_store.rename_paper_dir / pipeline.store.rename_paper's own
    docstrings for exactly what that does and doesn't touch, and why it's
    safe: nothing in this codebase reads a record's OWN embedded paper_id
    field back out and compares it to the file/directory it was found in).
    Checks both destinations are free before renaming either, so a name
    collision never leaves a paper half-renamed. Returns (True, message) on
    success or (False, reason) if refused."""
    from pipeline import store

    old_paper_id = old_paper_id.strip()
    new_paper_id = new_paper_id.strip()
    if not new_paper_id:
        return False, "New name cannot be empty."
    if old_paper_id == new_paper_id:
        return False, "New name is the same as the current name."
    if results_store.paper_dir(new_paper_id).exists():
        return False, f"Extracted records for '{new_paper_id}' already exist."
    if store.has_paper(new_paper_id):
        return False, f"An ir-store entry for '{new_paper_id}' already exists."

    results_moved = results_store.rename_paper_dir(old_paper_id, new_paper_id)
    store.rename_paper(old_paper_id, new_paper_id)  # ir-store entry, if any -- alongside results/
    if not results_moved:
        return False, f"No extracted records found for '{old_paper_id}'."
    return True, f"Renamed extracted records for '{old_paper_id}' to '{new_paper_id}'."


def get_pdf_bytes(paper_id: str) -> Optional[bytes]:
    path = sage_paths.pdf_path(paper_id)
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def run_marker_processing(timeout: int = 1800) -> dict[str, Any]:
    """Trigger the real, existing Marker + adapter pipeline. See
    marker_pipeline.py -- this never reimplements Marker or the adapter."""
    import marker_pipeline

    return marker_pipeline.process_all_papers(timeout=timeout)


def run_full_paper_processing(model: Optional[str] = None, timeout: int = 1800):
    """The complete, single, user-triggered processing session: Marker
    conversion -> Sage document preparation -> full extraction pipeline
    (pipeline.orchestrator.run_paper) for every paper that still needs it
    -> review data availability. A generator of per-stage progress dicts
    (see marker_pipeline.process_all_papers_full) so the Library page can
    show real stage-by-stage status rather than one opaque spinner."""
    import marker_pipeline

    yield from marker_pipeline.process_all_papers_full(model=model, timeout=timeout)


def run_pipeline_for_paper(paper_id: str, model: Optional[str] = None, timeout: int = 1800):
    """The complete processing session SCOPED TO ONE user-selected paper --
    Marker conversion -> Sage document preparation -> extraction for
    exactly this paper_id, never every paper that happens to need it. A
    generator of per-stage progress dicts, same shape as
    run_full_paper_processing, so the Library page renders both the same
    way (see marker_pipeline.process_single_paper_full)."""
    import marker_pipeline

    yield from marker_pipeline.process_single_paper_full(paper_id, model=model, timeout=timeout)


# ---------------------------------------------------------------------------
# Real record data, normalized for the review workspace
# ---------------------------------------------------------------------------

def _correction_effect(latest: Optional[dict], record_level: Optional[dict], raw_value: Any) -> dict:
    effective_value = raw_value
    review_status = "pending"
    if record_level and record_level["action"] == "approve" and latest is None:
        review_status = "approved"
    if latest:
        action = latest["action"]
        if action == "correct_value":
            effective_value = latest["payload"].get("new_value", raw_value)
            review_status = "corrected"
        elif action == "relink_entity":
            effective_value = latest["payload"].get("new_target_id", raw_value)
            review_status = "relinked"
        elif action == "relocate_evidence":
            review_status = "relocated"
        elif action == "confirm_unresolved":
            review_status = "confirmed_unresolved"
        elif action == "approve":
            review_status = "approved"
        elif action == "note":
            review_status = "pending"
    return {"effective_value": effective_value, "review_status": review_status, "latest_correction": latest}


def _normalize_field(
    paper_id: str, entity_type: str, record_id: str, field_name: str, raw: Any, run_id: Optional[str] = None,
) -> dict:
    """One entry of the record's `fields` dict. `raw` is either a bare
    reference value (str/int/bool -- ir_schema's ExtractedReference /
    dataset_id-style fields) or a full ExtractedField dict
    ({value, provenance_label, source, ...})."""
    is_extracted_field = isinstance(raw, dict) and "provenance_label" in raw

    latest = corrections_store.latest_action_for_field(paper_id, entity_type, record_id, field_name, run_id=run_id)
    record_level = corrections_store.latest_action_for_field(paper_id, entity_type, record_id, None, run_id=run_id)

    if not is_extracted_field:
        effect = _correction_effect(latest, record_level, raw)
        return {
            "kind": "reference",
            "value": raw,
            "provenance_label": None,
            "source": None,
            "confidence": None,
            "unresolved_reason": None,
            **effect,
        }

    effect = _correction_effect(latest, record_level, raw.get("value"))
    return {
        "kind": "extracted_field",
        "value": raw.get("value"),
        "provenance_label": raw.get("provenance_label"),
        "source": raw.get("source"),
        "confidence": raw.get("confidence"),
        "unresolved_reason": raw.get("unresolved_reason"),
        **effect,
    }


def get_review_data(paper_id: str, run_id: Optional[str] = None) -> dict[str, list[dict]]:
    """Every one of the 12 entity types for this paper, each holding
    whatever real result(s) exist for it -- 'not_extracted' when nothing
    does yet, never fabricated. Most entity types store at most one record
    (`results/<paper_id>/<Entity>.json`); a multi-record type (Phase A:
    Variable) can hold several (`results/<paper_id>/<Entity>/<record_id>.json`)
    -- `results_store.load_any_entity_results` reads whichever convention
    this entity_type actually uses, so this function itself never needs to
    know or care which one that is. Field values reflect corrections
    already applied (effective_value), while `value` always keeps the
    ORIGINAL extraction untouched, per the immutability requirement.

    `run_id` selects the results set (a run id, or `results_store.LEGACY_RUN`); by default the one
    `default_review_run` picks, so a failed latest run never hides earlier, real results."""
    if run_id is None:
        run_id = default_review_run(paper_id)
    data: dict[str, list[dict]] = {}
    for entity_type in ENTITY_TYPES:
        raw_results = results_store.load_any_entity_results(paper_id, entity_type, run_id)

        records = []
        for raw_result in raw_results:
            status = raw_result.get("status", "not_extracted")
            payload = raw_result.get("payload") or {}
            fields = {
                field_name: _normalize_field(
                    paper_id, entity_type, raw_result["record_id"], field_name, value, run_id=raw_result.get("run_id"),
                )
                for field_name, value in payload.items()
            } if payload else {}

            records.append({
                "record_id": raw_result.get("record_id"),
                "status": status,
                "run_id": raw_result.get("run_id"),
                "ai_validation": raw_result.get("ai_validation"),
                "reason": raw_result.get("reason"),
                "fields": fields,
            })
        data[entity_type] = records
    return data


def is_link_field(field_name: str) -> bool:
    """`id`, `*_id` and `*_ids`: the record's own key and its references to other records (citation_id, site_id,
    treatment_id, method_id, ...). They are pipeline-generated structure, not claims made by the paper, so the review
    UI neither shows nor asks for approval of them, and they never count toward review progress."""
    return field_name == "id" or field_name.endswith("_id") or field_name.endswith("_ids")


def reviewable_fields(record: dict) -> dict[str, dict]:
    """The fields of a record the reviewer is shown, in their original order: everything except link fields, and
    except plain (no-provenance) fields that hold nothing -- an optional field the extractor left empty has nothing
    to approve, correct or trace to a source."""
    shown: dict[str, dict] = {}
    for name, field in (record.get("fields") or {}).items():
        if is_link_field(name):
            continue
        if field["kind"] != "extracted_field" and field["effective_value"] in (None, ""):
            continue
        shown[name] = field
    return shown


def review_summary(paper_id: str, run_id: Optional[str] = None) -> dict:
    """Compact counts for the paper overview line -- e.g.
    '47 fields | 31 reviewed | 9 unresolved | 4 blocked | 3 remaining'.
    Deliberately just counts, no charts. Link fields are not counted (see `is_link_field`)."""
    data = get_review_data(paper_id, run_id)
    total_fields = 0
    reviewed = 0
    unresolved = 0
    blocked = 0
    for records in data.values():
        for record in records:
            if record["status"] == "unresolved":
                unresolved += 1
            elif record["status"] == "blocked":
                blocked += 1
            for field_name, field in record["fields"].items():
                if field["kind"] != "extracted_field" or is_link_field(field_name):
                    continue
                total_fields += 1
                if field["review_status"] in ("approved", "corrected", "relinked", "confirmed_unresolved"):
                    reviewed += 1
    remaining = total_fields - reviewed
    return {
        "total_fields": total_fields, "reviewed": reviewed, "unresolved": unresolved,
        "blocked": blocked, "remaining": remaining,
    }


# ---------------------------------------------------------------------------
# Entity pickers for "Relink Entity" (existing records, never raw id typing)
# ---------------------------------------------------------------------------

def list_record_ids(paper_id: str, entity_type: str, run_id: Optional[str] = None) -> list[str]:
    data = get_review_data(paper_id, run_id)
    return [r["record_id"] for r in data.get(entity_type, []) if r["status"] == "ready"]


# ---------------------------------------------------------------------------
# Review actions -- append to the correction log, never mutate results_store/ir-store
# ---------------------------------------------------------------------------

def _revalidate_locators(paper_id: str, new_value: Any, locators: list[dict]) -> list[dict]:
    """Re-run the SAME deterministic anchor/value check propose_record uses
    (pipeline.validators._value_supported_by_text against the real
    content.md block text) against a corrected value -- reusing the
    existing check, not a second implementation of it."""
    try:
        blocks = _load_rendered_blocks(paper_id)
    except FileNotFoundError as exc:
        return [{"severity": "error", "message": str(exc)}]
    issues = []
    for locator in locators:
        anchor = (locator.get("block_anchor") or "").strip("[]")
        if not anchor:
            continue
        block_text = blocks.get(anchor)
        if block_text is None:
            issues.append({"severity": "error", "message": f"locator block {anchor} does not exist in content.md"})
        elif not _value_supported_by_text(new_value, block_text):
            issues.append({
                "severity": "error",
                "message": f"value {new_value!r} is not supported by block {anchor}'s text",
            })
    return issues


def submit_correction(
    paper_id: str, entity_type: str, record_id: str, action: str,
    field_name: Optional[str] = None, payload: Optional[dict] = None, run_id: Optional[str] = None,
) -> dict:
    """Record one review action. For `correct_value`, re-runs deterministic
    provenance validation against the field's (possibly updated) locators
    before recording -- the validation OUTCOME is stored alongside the
    correction, but the correction is still recorded even if validation
    fails, so a scientist's action is never silently dropped; the UI is
    responsible for surfacing a failed re-validation clearly.

    `run_id` is the results set being reviewed (default: `default_review_run`). The correction is tagged with the
    reviewed RECORD's own run id -- the same id `get_review_data` reads corrections back with -- so it applies to
    exactly that run's record and never to another run's record of the same id."""
    payload = dict(payload or {})
    record = next((r for r in get_review_data(paper_id, run_id).get(entity_type, []) if r["record_id"] == record_id), None)
    if action == "correct_value" and field_name:
        field = (record or {}).get("fields", {}).get(field_name, {})
        source = field.get("source") or {}
        locators = source.get("locators") or []
        if "locators" in payload:
            locators = payload["locators"]
        issues = _revalidate_locators(paper_id, payload.get("new_value"), locators)
        payload["revalidation_issues"] = issues

    return corrections_store.append_correction(
        paper_id=paper_id, entity_type=entity_type, record_id=record_id,
        action=action, field_name=field_name, payload=payload,
        run_id=_correction_run_id(paper_id, record, run_id),
    )


def _correction_run_id(paper_id: str, record: Optional[dict], run_id: Optional[str]) -> Optional[str]:
    """The run a correction belongs to: the reviewed record's own run id; else the reviewed run (never the legacy
    sentinel, which is not a run); else the LATEST run."""
    if record and record.get("run_id"):
        return record["run_id"]
    if run_id is not None and run_id != results_store.LEGACY_RUN:
        return run_id
    return results_store.latest_run_id(paper_id)


def get_corrections(
    paper_id: str, entity_type: str, record_id: str, field_name: Optional[str] = None, run_id: Optional[str] = None,
) -> list[dict]:
    record = next((r for r in get_review_data(paper_id, run_id).get(entity_type, []) if r["record_id"] == record_id), None)
    return corrections_store.read_for_record(
        paper_id, entity_type, record_id, field_name, run_id=_correction_run_id(paper_id, record, run_id),
    )


def get_block_text(paper_id: str, block_anchor: str) -> Optional[str]:
    """The real rendered text of one content.md block -- the actual
    "why did Sage extract this" evidence for a field's locator, read via
    the same _load_rendered_blocks() validators.py already uses to check a
    correction's re-validation (see submit_correction/_revalidate_locators
    above), not a second implementation of anchor->text resolution. Returns
    None (never a fabricated excerpt) when the block or paper's content.md
    can't be found."""
    if not block_anchor:
        return None
    anchor = block_anchor.strip("[]")
    try:
        blocks = _load_rendered_blocks(paper_id)
    except FileNotFoundError:
        return None
    text = blocks.get(anchor)
    return text.strip() if text else None
