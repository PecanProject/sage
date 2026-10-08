"""
marker_pipeline.py
================
Runs one paper through the `marker` CLI (a subprocess), docproc's prepare_papers(), and the extraction pipeline.

    src/docproc/paper/<paper_id>.pdf
          -> `marker` CLI (subprocess)
          -> src/docproc/marker_json/<paper_id>/<paper_id>.json
          -> prepare_papers.prepare_papers()
          -> src/paper/<paper_id>/{content.md, provenance.json, qc_report.json}
"""

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

# A UI-launched run uses the same run config as the CLI (src/eval_config.json) unless a model is passed explicitly.


def _describe_qc_failure(entry: dict[str, Any]) -> str:
    """One short, human sentence for a single failing run_qc_batch
    per-paper entry -- which specific check(s) failed and the concrete
    reason, instead of the full nested QC report (round_trip_issue_counts,
    tag_leak_patterns_hit, etc.) every failure carries."""
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
    """One short reason string from a prepare_papers() summary, for the Library page."""
    parts = []
    for failure in summary.get("adapter_failures") or []:
        error = failure.get("adapter_error") or failure.get("error") or "unknown adapter error"
        parts.append(f"{failure['paper_id']}: {error}")
    for entry in summary.get("qc", {}).get("per_paper") or []:
        if not entry.get("passed", True):
            parts.append(_describe_qc_failure(entry))
    return "; ".join(parts) if parts else "conversion failed for an unspecified reason -- see raw details"


def run_marker_for_paper(paper_id: str, timeout: int = 1800) -> dict[str, Any]:
    """Run the `marker` CLI on one paper's PDF, via a temporary directory holding only a symlink to it (Marker has no
    single-file mode); output lands in src/docproc/marker_json/. No --skip_existing: a reused paper_id must never
    pick up an old PDF's output."""
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
    """The last non-blank line of a subprocess's stdout/stderr -- usually
    the actual error message a tqdm-heavy CLI like `marker` ends on, used
    for the one-line failure summary instead of dumping the full captured
    output (still kept, in full, under "stdout"/"stderr" for anyone who
    needs it)."""
    for line in reversed((text or "").splitlines()):
        if line.strip():
            return line.strip()
    return ""


def run_conversion_for_paper(paper_id: str) -> dict[str, Any]:
    """Run prepare_papers() for one paper through temporary input/output directories (it has no single-paper mode),
    then move the output to src/paper/<paper_id>/, so another paper's QC failure never affects this one."""
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


def preflight() -> tuple[bool, str]:
    """Everything an extraction run needs from its environment, checked before anything starts. Checks, in order: the `opencode` executable exists and runs (`run_config.check_opencode`), and the
    IR service is reachable and running the current schema/validator code (`orchestrator.check_health`).
    (True, summary) or (False, the first failure's reason). Never starts a run and never writes results."""
    from pipeline import orchestrator, run_config

    ok, opencode_msg = run_config.check_opencode()
    if not ok:
        return False, f"Pre-flight failed: {opencode_msg}"
    ok, health_msg = orchestrator.check_health(orchestrator.DEFAULT_IR_SERVICE_URL)
    if not ok:
        return False, f"Pre-flight failed: {health_msg}"
    return True, f"opencode: {opencode_msg}; {health_msg}"


def _preflight_event() -> Optional[dict[str, Any]]:
    """The UI's pre-flight stage: None when the environment is ready, else the terminal error event to show."""
    ok, message = preflight()
    if ok:
        return None
    return {"stage": "preflight", "status": "error", "message": message}


def _poll_new_completions(run_id: str, seen: set[str]) -> list[str]:
    """Every record_key under runs/<run_id>/records/ that has a final.json
    now but didn't the last time this was called -- the same real, on-disk
    signal a human watching the run directory with `ls`/`find` would see.
    Mutates `seen` in place so the caller can call this repeatedly across
    a poll loop without re-reporting the same completion twice."""
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
    """Classifies a COMPLETED orchestrator.run_paper() result -- never the
    catastrophic "couldn't even run" case (that's "failed", handled
    separately where the actual exception is caught). Returns
    ("success", []) when nothing errored, or ("completed_with_errors",
    [(entity_type, record_id), ...]) naming exactly which candidates did. "blocked" is never an error."""
    error_records: list[tuple[str, str]] = []
    for entity_type, r_or_list in records.items():
        for r in (r_or_list if isinstance(r_or_list, list) else [r_or_list]):
            if r.get("status") == "error":
                error_records.append((entity_type, r.get("record_id", "?")))
    outcome = "completed_with_errors" if error_records else "success"
    return outcome, error_records


def _run_extraction_for_paper(paper_id: str, model: Optional[str]) -> Iterator[dict[str, Any]]:
    """Run the real, existing full-paper extraction pipeline
    (pipeline.orchestrator.run_paper) for one paper -- never a second,
    parallel extraction implementation. Health-checks ir_service first
    since orchestrator refuses to run against an unreachable or stale
    service. Runs run_paper() in a background thread and yields a progress event per completed record (polled from
    runs/<run_id>/records/); the last event carries the final ok/statuses/run_id."""
    import httpx
    from pipeline import orchestrator

    # Re-checked right here (not only at the start of the session): Marker may have run for many minutes since.
    ok, msg = preflight()
    if not ok:
        yield {"ok": False, "step": "extraction", "error": msg, "run_outcome": "failed"}
        return

    # One active run per paper: refuse up front (run_paper enforces the same lock).
    from pipeline import run_lock

    holder = run_lock.active_holder(paper_id)
    if holder:
        yield {
            "ok": False, "step": "extraction", "run_outcome": "failed",
            "error": str(run_lock.RunAlreadyActive(paper_id, holder)),
        }
        return

    from pipeline import run_config

    try:
        cfg, manifest_extra, invoke = orchestrator.prepare_run(model_override=model)
    except (run_config.RunConfigError, run_config.ModelUnavailable) as exc:
        yield {"ok": False, "step": "extraction", "run_outcome": "failed", "error": f"run configuration error: {exc}"}
        return

    run_id = orchestrator._new_run_id()
    outcome: dict[str, Any] = {}

    def _worker():
        try:
            with httpx.Client(
                base_url=orchestrator.DEFAULT_IR_SERVICE_URL, timeout=cfg.ir_service_timeout_seconds,
            ) as http_client:
                client = orchestrator.IRServiceClient(http_client)
                outcome["result"] = orchestrator.run_paper(
                    paper_id=paper_id, model=cfg.model_ref, client=client, invoke=invoke, run_id=run_id,
                    manifest_extra=manifest_extra,
                )
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
    # A single record_info dict, or a list for a multi-record type: normalise to {entity_type: status(es)}.
    statuses: dict[str, Any] = {
        et: (r["status"] if not isinstance(r, list) else [x["status"] for x in r])
        for et, r in result["records"].items()
    }
    run_outcome, error_records = _classify_run_outcome(result["records"])
    yield {
        "ok": run_outcome == "success", "step": "extraction", "run_id": result["run_id"],
        "statuses": statuses, "run_outcome": run_outcome, "error_records": error_records,
    }


def _run_extraction_with_ui_progress(paper_id: str, model: Optional[str]) -> Iterator[dict[str, Any]]:
    """Re-yield _run_extraction_for_paper's events in the {"stage": "extraction", "status": ...} shape the other stages
    use, ending with the final event."""
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


def process_single_paper_full(paper_id: str, model: str | None = None, timeout: int = 1800) -> Iterator[dict[str, Any]]:
    """The complete pipeline for one paper -- Marker conversion -> document preparation -> extraction -- as a
    generator of per-stage progress events. Extraction always runs, even if earlier results exist."""
    failed = _preflight_event()
    if failed:
        yield failed
        return
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
