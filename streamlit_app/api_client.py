from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import sage_paths  # wires sys.path to src/ and src/docproc/

from pipeline import corrections_store, results_store
from pipeline.ir_schema import ENTITY_MODELS
from pipeline.validators import _value_supported_by_text, _load_rendered_blocks

ENTITY_TYPES: list[str] = list(ENTITY_MODELS.keys())  # canonical order, straight from the real schema

REVIEW_ACTIONS = sorted(corrections_store.VALID_ACTIONS)


def is_extracted(paper_id: str) -> bool:
    from pipeline import results_store
    from pipeline.ir_schema import ENTITY_MODELS

    for entity_type in ENTITY_MODELS:
        for result in results_store.load_any_entity_results(paper_id, entity_type):
            if result.get("status") in ("ready", "unresolved"):
                return True
    return False


def has_error_records(paper_id: str) -> bool:
    from pipeline import results_store
    from pipeline.ir_schema import ENTITY_MODELS

    for entity_type in ENTITY_MODELS:
        for result in results_store.load_any_entity_results(paper_id, entity_type):
            if result.get("status") == "error":
                return True
    return False


def list_papers() -> list[dict]:
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
    rows = []
    for paper_id in results_store.list_paper_ids():
        if not is_extracted(paper_id):
            continue
        rows.append({
            "paper_id": paper_id,
            "has_pdf": sage_paths.has_pdf(paper_id),
            "has_errors": has_error_records(paper_id),
            "summary": review_summary(paper_id),
        })
    return rows


def upload_pdfs(files: list[tuple[str, bytes]]) -> list[str]:
    sage_paths.PDF_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    for filename, content in files:
        paper_id = Path(filename).stem
        dest = sage_paths.PDF_DIR / f"{paper_id}.pdf"
        dest.write_bytes(content)
        saved.append(paper_id)
    return saved


def delete_paper(paper_id: str) -> list[str]:
    return sage_paths.delete_paper_source(paper_id)


def delete_extracted_records(paper_id: str) -> list[str]:
    import shutil
    from pipeline import results_store

    paper_dir = results_store.paper_dir(paper_id)
    if not paper_dir.is_dir():
        return []
    shutil.rmtree(paper_dir)
    return [str(paper_dir)]


def rename_stored_paper(old_paper_id: str, new_paper_id: str) -> tuple[bool, str]:
    return sage_paths.rename_paper_source(old_paper_id, new_paper_id)


def rename_extracted_paper(old_paper_id: str, new_paper_id: str) -> tuple[bool, str]:
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
    import marker_pipeline

    return marker_pipeline.process_all_papers(timeout=timeout)


def run_full_paper_processing(model: Optional[str] = None, timeout: int = 1800):
    import marker_pipeline

    yield from marker_pipeline.process_all_papers_full(model=model, timeout=timeout)


def run_pipeline_for_paper(paper_id: str, model: Optional[str] = None, timeout: int = 1800):
    import marker_pipeline

    yield from marker_pipeline.process_single_paper_full(paper_id, model=model, timeout=timeout)


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


def _normalize_field(paper_id: str, entity_type: str, record_id: str, field_name: str, raw: Any) -> dict:
    is_extracted_field = isinstance(raw, dict) and "provenance_label" in raw

    latest = corrections_store.latest_action_for_field(paper_id, entity_type, record_id, field_name)
    record_level = corrections_store.latest_action_for_field(paper_id, entity_type, record_id, None)

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


def get_review_data(paper_id: str) -> dict[str, list[dict]]:
    data: dict[str, list[dict]] = {}
    for entity_type in ENTITY_TYPES:
        raw_results = results_store.load_any_entity_results(paper_id, entity_type)

        records = []
        for raw_result in raw_results:
            status = raw_result.get("status", "not_extracted")
            payload = raw_result.get("payload") or {}
            fields = {
                field_name: _normalize_field(paper_id, entity_type, raw_result["record_id"], field_name, value)
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


def review_summary(paper_id: str) -> dict:
    data = get_review_data(paper_id)
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
            for field in record["fields"].values():
                if field["kind"] != "extracted_field":
                    continue
                total_fields += 1
                if field["review_status"] in ("approved", "corrected", "relinked", "confirmed_unresolved"):
                    reviewed += 1
    remaining = total_fields - reviewed
    return {
        "total_fields": total_fields, "reviewed": reviewed, "unresolved": unresolved,
        "blocked": blocked, "remaining": remaining,
    }


def list_record_ids(paper_id: str, entity_type: str) -> list[str]:
    data = get_review_data(paper_id)
    return [r["record_id"] for r in data.get(entity_type, []) if r["status"] == "ready"]


def _revalidate_locators(paper_id: str, new_value: Any, locators: list[dict]) -> list[dict]:
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
    field_name: Optional[str] = None, payload: Optional[dict] = None,
) -> dict:
    payload = dict(payload or {})
    if action == "correct_value" and field_name:
        record = next((r for r in get_review_data(paper_id).get(entity_type, []) if r["record_id"] == record_id), None)
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
    )


def get_corrections(paper_id: str, entity_type: str, record_id: str, field_name: Optional[str] = None) -> list[dict]:
    return corrections_store.read_for_record(paper_id, entity_type, record_id, field_name)


def get_block_text(paper_id: str, block_anchor: str) -> Optional[str]:
    if not block_anchor:
        return None
    anchor = block_anchor.strip("[]")
    try:
        blocks = _load_rendered_blocks(paper_id)
    except FileNotFoundError:
        return None
    text = blocks.get(anchor)
    return text.strip() if text else None
