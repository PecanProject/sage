from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

import sage_paths

MARKER_BIN = str(sage_paths.SAGE_ROOT / ".venv" / "bin" / "marker")
_DEFAULT_EXTRACTION_MODEL = "jetstream-gpt/gpt-oss-120b"


def run_marker(timeout: int = 1800) -> dict[str, Any]:
    source_dir = sage_paths.source_pdf_dir()
    sage_paths.MARKER_JSON_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        MARKER_BIN, str(source_dir),
        "--output_dir", str(sage_paths.MARKER_JSON_DIR),
        "--output_format", "json",
        "--workers", "1",
        "--skip_existing",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        return {"ok": False, "step": "marker", "cmd": cmd, "error": f"marker executable not found: {exc}"}
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False, "step": "marker", "cmd": cmd, "error": f"marker timed out after {timeout}s",
            "stdout": exc.stdout, "stderr": exc.stderr,
        }
    return {
        "ok": proc.returncode == 0, "step": "marker", "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-4000:], "stderr": (proc.stderr or "")[-4000:],
    }


def _describe_qc_failure(entry: dict[str, Any]) -> str:
    if entry.get("error"):
        return f"{entry['paper_id']}: {entry['error']}"
    reasons = []
    if not entry.get("round_trip_ok", True):
        counts = entry.get("round_trip_issue_counts") or {}
        detail = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
        reasons.append(f"round-trip check failed ({detail})" if detail else "round-trip check failed")
    if not entry.get("tag_leak_ok", True):
        patterns = entry.get("tag_leak_patterns_hit") or []
        reasons.append(f"tag leak detected ({', '.join(patterns)})" if patterns else "tag leak detected")
    if not entry.get("block_type_coverage_ok", True):
        types = entry.get("block_types_not_in_any_policy_set") or []
        reasons.append(f"unhandled block type(s): {', '.join(types)}" if types else "unhandled block type(s)")
    return f"{entry['paper_id']}: " + ("; ".join(reasons) if reasons else "QC failed (unspecified check)")


def _describe_conversion_failure(summary: dict[str, Any]) -> str:
    parts = []
    for failure in summary.get("adapter_failures") or []:
        error = failure.get("adapter_error") or failure.get("error") or "unknown adapter error"
        parts.append(f"{failure['paper_id']}: {error}")
    for entry in summary.get("qc", {}).get("per_paper") or []:
        if not entry.get("passed", True):
            parts.append(_describe_qc_failure(entry))
    return "; ".join(parts) if parts else "conversion failed for an unspecified reason -- see raw details"


def run_conversion() -> dict[str, Any]:
    import prepare_papers  # existing module; docproc/ is on sys.path via sage_paths

    summary = prepare_papers.prepare_papers(sage_paths.MARKER_JSON_DIR, sage_paths.PAPER_DIR)
    ok = not summary["adapter_failures"] and summary["qc"]["papers_failed"] == 0
    result = {"ok": ok, "step": "conversion", "summary": summary}
    if not ok:
        result["error_summary"] = _describe_conversion_failure(summary)
    return result


def run_marker_for_paper(paper_id: str, timeout: int = 1800) -> dict[str, Any]:
    pdf_path = sage_paths.pdf_path(paper_id)
    if pdf_path is None:
        return {"ok": False, "step": "marker", "error": f"no PDF on disk for '{paper_id}'"}

    sage_paths.MARKER_JSON_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"sage_marker_in_{paper_id}_") as tmp_dir:
        scoped_input_dir = Path(tmp_dir)
        (scoped_input_dir / pdf_path.name).symlink_to(pdf_path)
        cmd = [
            MARKER_BIN, str(scoped_input_dir),
            "--output_dir", str(sage_paths.MARKER_JSON_DIR),
            "--output_format", "json",
            "--workers", "1",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            return {"ok": False, "step": "marker", "cmd": cmd, "error": f"marker executable not found: {exc}"}
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False, "step": "marker", "cmd": cmd, "error": f"marker timed out after {timeout}s",
                "stdout": exc.stdout, "stderr": exc.stderr,
            }
    result = {
        "ok": proc.returncode == 0, "step": "marker", "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-4000:], "stderr": (proc.stderr or "")[-4000:],
    }
    if not result["ok"]:
        result["error_summary"] = (
            f"marker exited with code {proc.returncode}: {_last_meaningful_line(proc.stderr) or _last_meaningful_line(proc.stdout) or '(no output captured)'}"
        )
    return result


def _last_meaningful_line(text: Optional[str]) -> str:
    for line in reversed((text or "").splitlines()):
        if line.strip():
            return line.strip()
    return ""


def run_conversion_for_paper(paper_id: str) -> dict[str, Any]:
    import prepare_papers

    marker_dir = sage_paths.MARKER_JSON_DIR / paper_id
    if not marker_dir.is_dir():
        return {"ok": False, "step": "conversion", "error": f"no Marker output found for '{paper_id}' -- run Marker first"}

    with tempfile.TemporaryDirectory(prefix=f"sage_marker_scope_{paper_id}_") as tmp_marker_dir, \
         tempfile.TemporaryDirectory(prefix=f"sage_paper_scope_{paper_id}_") as tmp_output_dir:
        scoped_marker_root = Path(tmp_marker_dir)
        scoped_output_root = Path(tmp_output_dir)
        (scoped_marker_root / paper_id).symlink_to(marker_dir)

        summary = prepare_papers.prepare_papers(scoped_marker_root, scoped_output_root)

        produced = scoped_output_root / paper_id
        if produced.is_dir():
            real_dest = sage_paths.PAPER_DIR / paper_id
            if real_dest.exists():
                shutil.rmtree(real_dest)
            real_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(produced), str(real_dest))

    ok = not summary["adapter_failures"] and summary["qc"]["papers_failed"] == 0
    result = {"ok": ok, "step": "conversion", "summary": summary}
    if not ok:
        result["error_summary"] = _describe_conversion_failure(summary)
    return result


def process_all_papers(timeout: int = 1800) -> dict[str, Any]:
    marker_result = run_marker(timeout=timeout)
    if not marker_result["ok"]:
        return {"ok": False, "marker": marker_result, "conversion": None}

    conversion_result = run_conversion()

    import provenance_adapter
    provenance_adapter.clear_cache()  # provenance.json may have just changed

    return {"ok": conversion_result["ok"], "marker": marker_result, "conversion": conversion_result}


def _needs_extraction(paper_id: str) -> bool:
    from pipeline import results_store
    from pipeline.ir_schema import ENTITY_MODELS

    statuses = []
    for entity_type in ENTITY_MODELS:
        for result in results_store.load_any_entity_results(paper_id, entity_type):
            statuses.append(result.get("status"))

    if not statuses:
        return True
    if any(status == "error" for status in statuses):
        return True
    return not any(status in ("ready", "unresolved") for status in statuses)


def _poll_new_completions(run_id: str, seen: set[str]) -> list[str]:
    from pipeline import run_store

    messages = []
    for record_key in run_store.list_records(run_id):
        if record_key in seen:
            continue
        final_path = run_store.record_dir(run_id, record_key) / "final.json"
        if not final_path.is_file():
            continue
        seen.add(record_key)
        if record_key.endswith("__enumeration"):
            entity_type = record_key.split("__", 1)[0]
            messages.append(f"{entity_type}: enumeration complete.")
            continue
        try:
            data = run_store.load_json(final_path)
        except (OSError, ValueError):
            continue
        entity_type, _, record_id = record_key.partition("__")
        messages.append(f"{entity_type} — {record_id}: {data.get('status')}")
    return messages


def _classify_run_outcome(records: dict[str, Any]) -> tuple[str, list[tuple[str, str]]]:
    error_records: list[tuple[str, str]] = []
    for entity_type, r_or_list in records.items():
        for r in (r_or_list if isinstance(r_or_list, list) else [r_or_list]):
            if r.get("status") == "error":
                error_records.append((entity_type, r.get("record_id", "?")))
    outcome = "completed_with_errors" if error_records else "success"
    return outcome, error_records


def _run_extraction_for_paper(paper_id: str, model: str) -> Iterator[dict[str, Any]]:
    import httpx
    from pipeline import orchestrator

    ok, msg = orchestrator.check_health(orchestrator.DEFAULT_IR_SERVICE_URL)
    if not ok:
        yield {"ok": False, "step": "extraction", "error": msg}
        return

    run_id = orchestrator._new_run_id()
    outcome: dict[str, Any] = {}

    def _worker():
        try:
            with httpx.Client(base_url=orchestrator.DEFAULT_IR_SERVICE_URL, timeout=120.0) as http_client:
                client = orchestrator.IRServiceClient(http_client)
                outcome["result"] = orchestrator.run_paper(paper_id=paper_id, model=model, client=client, run_id=run_id)
        except Exception as exc:  # real network/agent failures during a live run
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    seen: set[str] = set()
    while thread.is_alive():
        for message in _poll_new_completions(run_id, seen):
            yield {"ok": True, "step": "extraction", "progress": True, "message": message}
        time.sleep(2.0)
    thread.join()
    # Catch anything that finished between the last poll and the thread exiting.
    for message in _poll_new_completions(run_id, seen):
        yield {"ok": True, "step": "extraction", "progress": True, "message": message}

    if "error" in outcome:
        yield {"ok": False, "step": "extraction", "error": outcome["error"], "run_outcome": "failed"}
        return

    result = outcome["result"]
    statuses: dict[str, Any] = {
        et: (r["status"] if not isinstance(r, list) else [x["status"] for x in r])
        for et, r in result["records"].items()
    }
    run_outcome, error_records = _classify_run_outcome(result["records"])
    yield {
        "ok": run_outcome == "success", "step": "extraction", "run_id": result["run_id"],
        "statuses": statuses, "run_outcome": run_outcome, "error_records": error_records,
    }


def _run_extraction_with_ui_progress(paper_id: str, model: str) -> Iterator[dict[str, Any]]:
    final: Optional[dict[str, Any]] = None
    for event in _run_extraction_for_paper(paper_id, model):
        if event.get("progress"):
            yield {"stage": "extraction", "status": "running", "message": event["message"]}
        else:
            final = event
    if final is None:  # defensive: the generator above always yields a final event
        final = {"ok": False, "step": "extraction", "error": "extraction generator produced no result", "run_outcome": "failed"}

    run_outcome = final.get("run_outcome") or ("success" if final["ok"] else "failed")
    if run_outcome == "failed":
        yield {
            "stage": "extraction", "status": "error", "paper_id": paper_id,
            "message": f"Extraction failed for {paper_id}: {final.get('error') or final.get('statuses')}",
            "detail": final,
        }
    elif run_outcome == "completed_with_errors":
        error_records = final.get("error_records") or []
        error_names = ", ".join(f"{et}/{rid}" for et, rid in error_records)
        yield {
            "stage": "extraction", "status": "done_with_errors", "paper_id": paper_id,
            "message": (
                f"Extraction completed for {paper_id} with {len(error_records)} error(s): {error_names}. "
                f"All other candidates completed normally (ready/unresolved)."
            ),
            "detail": final,
        }
    else:
        yield {
            "stage": "extraction", "status": "done", "paper_id": paper_id,
            "message": f"Entities extracted and stored for {paper_id}.",
            "detail": final,
        }


def process_all_papers_full(model: str | None = None, timeout: int = 1800) -> Iterator[dict[str, Any]]:
    model = model or _DEFAULT_EXTRACTION_MODEL

    yield {"stage": "marker", "status": "running", "message": "Processing PDF(s) with Marker..."}
    marker_result = run_marker(timeout=timeout)
    if not marker_result["ok"]:
        yield {
            "stage": "marker", "status": "error",
            "message": marker_result.get("error_summary") or marker_result.get("error") or "Marker processing failed.",
            "detail": marker_result,
        }
        return
    yield {"stage": "marker", "status": "done", "message": "Marker processing complete.", "detail": marker_result}

    yield {"stage": "conversion", "status": "running", "message": "Preparing document (Sage paper artifacts)..."}
    conversion_result = run_conversion()
    import provenance_adapter
    provenance_adapter.clear_cache()
    if not conversion_result["ok"]:
        yield {
            "stage": "conversion", "status": "error",
            "message": conversion_result.get("error_summary") or conversion_result.get("error") or "Document preparation failed.",
            "detail": conversion_result,
        }
        return
    yield {"stage": "conversion", "status": "done", "message": "Document preparation complete.", "detail": conversion_result}

    paper_ids = sage_paths.list_source_paper_ids()
    to_extract = [pid for pid in paper_ids if sage_paths.is_processed(pid) and _needs_extraction(pid)]

    if not to_extract:
        yield {"stage": "extraction", "status": "done", "message": "No papers need extraction (already extracted)."}
    for paper_id in to_extract:
        yield from _run_extraction_with_ui_progress(paper_id, model)

    yield {"stage": "review_data", "status": "running", "message": "Preparing review data..."}
    yield {"stage": "review_data", "status": "done", "message": "Review data ready in Scientist Review."}

    yield {"stage": "complete", "status": "done", "message": "Complete."}


def process_single_paper_full(paper_id: str, model: str | None = None, timeout: int = 1800) -> Iterator[dict[str, Any]]:
    model = model or _DEFAULT_EXTRACTION_MODEL

    yield {"stage": "marker", "status": "running", "message": f"Processing {paper_id} with Marker..."}
    marker_result = run_marker_for_paper(paper_id, timeout=timeout)
    if not marker_result["ok"]:
        yield {
            "stage": "marker", "status": "error",
            "message": marker_result.get("error_summary") or marker_result.get("error") or f"Marker processing failed for {paper_id}.",
            "detail": marker_result,
        }
        return
    yield {"stage": "marker", "status": "done", "message": "Marker processing complete.", "detail": marker_result}

    yield {"stage": "conversion", "status": "running", "message": f"Preparing document for {paper_id}..."}
    conversion_result = run_conversion_for_paper(paper_id)
    import provenance_adapter
    provenance_adapter.clear_cache()
    if not conversion_result["ok"]:
        yield {
            "stage": "conversion", "status": "error",
            "message": conversion_result.get("error_summary") or conversion_result.get("error") or f"Document preparation failed for {paper_id}.",
            "detail": conversion_result,
        }
        return
    yield {"stage": "conversion", "status": "done", "message": "Document preparation complete.", "detail": conversion_result}

    if not sage_paths.is_processed(paper_id):
        yield {
            "stage": "extraction", "status": "error", "paper_id": paper_id,
            "message": f"{paper_id} was not successfully converted (content.md/provenance.json missing) -- cannot extract.",
        }
        return

    yield from _run_extraction_with_ui_progress(paper_id, model)

    yield {"stage": "complete", "status": "done", "message": "Complete."}
