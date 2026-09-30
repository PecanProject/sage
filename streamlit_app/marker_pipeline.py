"""
marker_pipeline.py
================
Triggers the existing, already-tested two-step conversion pipeline. Does
NOT reimplement Marker or the adapter -- it runs the real `marker` CLI as
a subprocess (real PDF parsing/layout, so it must be an external process,
not something to run inline in Streamlit) and then imports and calls the
existing, unmodified `docproc/prepare_papers.py:prepare_papers()`
function, which itself only calls the existing, unmodified
`marker_adapter.process_paper()` and `run_qc_batch.run_batch()` -- see
those modules' own docstrings.

    src/docproc/paper/<paper_id>.pdf
          -> `marker` CLI (subprocess)
          -> src/docproc/marker_json/<paper_id>/<paper_id>.json
          -> prepare_papers.prepare_papers()            [existing, unmodified]
          -> src/paper/<paper_id>/{content.md, provenance.json, qc_report.json}

`src/docproc/` is the single, canonical source of truth for both original
PDFs and raw Marker JSON -- there is no other PDF location (see
sage_paths.py's own docstring).
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

# Deliberately NOT orchestrator.DEFAULT_MODEL ("jetstream-scout/llama-4-scout"),
# which earlier real-data runs in this project established is broken --
# this is the model already used for the real, working extraction runs.
_DEFAULT_EXTRACTION_MODEL = "jetstream-gpt/gpt-oss-120b"


def run_marker(timeout: int = 1800) -> dict[str, Any]:
    """Run the real `marker` CLI over every PDF in src/docproc/paper/,
    writing raw Marker JSON to src/docproc/marker_json/. Passes
    Marker's own `--skip_existing` flag so a PDF whose Marker output
    already exists isn't reprocessed -- the existing, reliable way to
    avoid unnecessary re-runs of an expensive step, rather than this
    module inventing its own staleness heuristic for internals it doesn't
    own."""
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
    """One short, human-readable reason string from a prepare_papers()
    summary -- what the Library page actually shows on failure, instead of
    the full raw summary dict (adapter_results, per-paper QC internals,
    etc.), which is real evidence kept available but not the primary,
    always-visible message."""
    parts = []
    for failure in summary.get("adapter_failures") or []:
        error = failure.get("adapter_error") or failure.get("error") or "unknown adapter error"
        parts.append(f"{failure['paper_id']}: {error}")
    for entry in summary.get("qc", {}).get("per_paper") or []:
        if not entry.get("passed", True):
            parts.append(_describe_qc_failure(entry))
    return "; ".join(parts) if parts else "conversion failed for an unspecified reason -- see raw details"


def run_conversion() -> dict[str, Any]:
    """Call the existing, unmodified docproc.prepare_papers pipeline over
    whatever is currently in src/docproc/marker_json/."""
    import prepare_papers  # existing module; docproc/ is on sys.path via sage_paths

    summary = prepare_papers.prepare_papers(sage_paths.MARKER_JSON_DIR, sage_paths.PAPER_DIR)
    ok = not summary["adapter_failures"] and summary["qc"]["papers_failed"] == 0
    result = {"ok": ok, "step": "conversion", "summary": summary}
    if not ok:
        result["error_summary"] = _describe_conversion_failure(summary)
    return result


def run_marker_for_paper(paper_id: str, timeout: int = 1800) -> dict[str, Any]:
    """Same real `marker` CLI call as run_marker, scoped to exactly ONE
    paper's PDF. `marker`'s IN_FOLDER argument is always a directory scan
    with no single-file mode, so scoping happens via a temporary directory
    containing only a symlink to this one PDF -- output still lands in the
    real, shared src/docproc/marker_json/ (Marker names its own output
    subdirectory from the input filename, independent of the input
    directory's other contents).

    Fixes a real, confirmed bug: process_single_paper_full previously
    called plain run_marker(), which re-scans EVERY PDF in the library on
    every single "run pipeline for one paper" click -- wasteful, and it
    meant an unrelated PDF's own Marker failure could report failure for a
    request that had nothing to do with it.

    Deliberately NO --skip_existing here (unlike the batch run_marker()
    above, which keeps it -- that one still benefits from not redundantly
    reprocessing dozens of unrelated, already-converted PDFs). This call is
    always for exactly the one paper the user explicitly asked to Extract
    -- a real staleness bug otherwise: sage_paths.delete_paper_source()
    intentionally removes only the PDF, not its derived Marker JSON/
    rendered document, so if that paper_id is later reused for a genuinely
    DIFFERENT PDF, --skip_existing would see the OLD paper's leftover
    Marker output still on disk and skip reprocessing entirely, silently
    extracting the new upload from the old paper's content. Always
    reprocessing the one explicitly-selected paper here is the same
    "deliberate re-run is never silently skipped" principle the extraction
    stage already follows."""
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
    """Same real prepare_papers() call as run_conversion, scoped to exactly
    ONE paper. Neither prepare_papers() nor the run_qc_batch() it delegates
    to (both existing, unmodified, and both unconditionally batch over
    EVERY subdirectory they're handed -- confirmed by reading both) have a
    single-paper mode, so scoping happens by giving them a temporary
    marker-input directory and a temporary output directory containing
    only this one paper, then moving the real output into place at
    src/paper/<paper_id>/ afterward.

    Fixes a real, confirmed bug: run_conversion() over the real shared
    directories QCs and adapts EVERY paper in the library every time, so
    one unrelated, already-uploaded paper failing QC (confirmed live: a
    paper called "Winter cover" fails block-type-coverage QC over an
    unhandled PictureGroup block) silently made EVERY OTHER paper's
    conversion stage report failure too, regardless of whether that other
    paper's own conversion was perfectly fine."""
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
    """Run the complete pipeline: real PDFs -> raw Marker JSON -> Sage
    paper artifacts. Returns a status dict the UI renders directly; never
    raises for an expected failure (missing binary, bad PDF, QC failure)
    -- those come back as ok=False with a reason attached."""
    marker_result = run_marker(timeout=timeout)
    if not marker_result["ok"]:
        return {"ok": False, "marker": marker_result, "conversion": None}

    conversion_result = run_conversion()

    import provenance_adapter
    provenance_adapter.clear_cache()  # provenance.json may have just changed

    return {"ok": conversion_result["ok"], "marker": marker_result, "conversion": conversion_result}


def _needs_extraction(paper_id: str) -> bool:
    """True unless this paper has a genuinely completed extraction.

    orchestrator.run_record's only real, disclosed terminal outcomes are
    "ready" and "unresolved" (see orchestrator.py's own CLI exit-code check:
    `0 if result.status in ("ready", "unresolved") else 1`). "error" is a
    genuine failure, and "blocked" is never itself a completed result -- it
    is only ever a knock-on consequence of another entity's status (a
    missing/errored prerequisite, or -- for TreatmentPair specifically -- a
    disclosed, permanent limitation of a single-record-per-type run; see
    orchestrator.run_paper's docstring). So a paper counts as needing
    (re-)extraction unless at least one entity type reached "ready"/
    "unresolved" AND none reached "error" -- stale error results (e.g. from
    a since-fixed ir_service outage) must never be mistaken for a completed
    extraction, and a partially-errored run must never be treated as done
    just because some other entity type happened to succeed."""
    from pipeline import results_store
    from pipeline.ir_schema import ENTITY_MODELS

    statuses = []
    for entity_type in ENTITY_MODELS:
        # load_any_entity_results reads whichever storage convention this
        # entity_type actually uses (a single file, or -- Phase A: Variable
        # -- a directory of several) and always returns a list, 0-or-more
        # entries, so this loop never needs to know which one it is.
        for result in results_store.load_any_entity_results(paper_id, entity_type):
            statuses.append(result.get("status"))

    if not statuses:
        return True
    if any(status == "error" for status in statuses):
        return True
    return not any(status in ("ready", "unresolved") for status in statuses)


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
    [(entity_type, record_id), ...]) naming exactly which candidates did.

    Fixes a real, confirmed bug: the previous `ok = all(status in (...))`
    boolean treated ANY single "error" anywhere in a ~90-record paper the
    same as a total pipeline crash -- a real run (Winter cover,
    20260916T050510_41f112a0) with 2 isolated provider-glitch extraction
    failures out of ~90 records was reported to the user as "Extraction
    failed for Winter cover", indistinguishable from a run that produced
    nothing at all. "blocked" is unchanged -- still a normal, disclosed,
    expected outcome (e.g. TreatmentPair genuinely needing 2 Treatments
    that don't both exist), never counted as an error."""
    error_records: list[tuple[str, str]] = []
    for entity_type, r_or_list in records.items():
        for r in (r_or_list if isinstance(r_or_list, list) else [r_or_list]):
            if r.get("status") == "error":
                error_records.append((entity_type, r.get("record_id", "?")))
    outcome = "completed_with_errors" if error_records else "success"
    return outcome, error_records


def _run_extraction_for_paper(paper_id: str, model: str) -> Iterator[dict[str, Any]]:
    """Run the real, existing full-paper extraction pipeline
    (pipeline.orchestrator.run_paper) for one paper -- never a second,
    parallel extraction implementation. Health-checks ir_service first
    since orchestrator refuses to run against an unreachable or stale
    service (see orchestrator.check_health's docstring).

    A GENERATOR, not a single return -- fixes a real, confirmed UX problem:
    run_paper() is one long, blocking call (well over an hour on a real
    paper with many multi-record entities) with no progress callback of
    its own, so the Library page previously showed one single "Extracting
    entities for <paper>..." message and then nothing else until the
    ENTIRE run finished, indistinguishable from a hang. This runs
    run_paper() in a background thread and polls ITS OWN run_store
    artifacts (runs/<run_id>/records/.../final.json) on disk every couple
    of seconds, yielding one real progress event per record as it actually
    completes. Every yielded dict has `"progress": True` except the LAST
    one, which carries the real final ok/statuses/run_id -- exactly what
    every existing caller of this function already expects."""
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
    # result["records"][entity_type] is a single record_info dict for most
    # entity types, or a list of them for a multi-record type (Phase A:
    # Variable) -- normalize both into the same {entity_type: status(es)}
    # summary shape for callers/UI (still reported in full, unchanged).
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
    """Consumes _run_extraction_for_paper's progress-event generator and
    re-yields it as the same {"stage": "extraction", "status": ..., ...}
    shape process_single_paper_full/process_all_papers_full already yield
    for every other stage, ending with the final terminal event -- so both
    callers stay simple call sites rather than each re-implementing "which
    of these events is the real final one".

    Three, not two, terminal `status` values (see _classify_run_outcome):
      "done"             -- run_outcome == "success": every candidate
                             terminal without errors.
      "done_with_errors" -- run_outcome == "completed_with_errors": the
                             pipeline completed, but names EXACTLY which
                             candidates errored -- never a bare count, and
                             never presented as if the whole run crashed.
      "error"            -- run_outcome == "failed": the pipeline itself
                             could not complete (e.g. ir_service
                             unreachable, a network exception) -- the
                             ORIGINAL, more severe meaning "error" already
                             had; two isolated candidate failures no
                             longer collapse into this."""
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
    """The complete, single, user-triggered processing session for every
    PDF in src/docproc/paper/: Marker conversion (skipping PDFs already
    converted) -> Sage document preparation -> full extraction pipeline
    for whichever papers still need it -> review data availability.

    A generator, not a single return, so the UI can show real per-stage
    progress rather than one opaque spinner -- each yielded dict is one
    stage's outcome as it actually happens, never simulated/fake progress.
    Extraction is evaluated per-paper and is NEVER skipped just because
    Marker conversion was skipped for an already-converted PDF -- those
    are independent stages with independent staleness checks.
    """
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
    """The complete pipeline for exactly ONE user-selected paper_id --
    Marker conversion -> Sage document preparation -> full extraction --
    never every paper that happens to need it (that blanket behavior is
    process_all_papers_full, above, kept for a future explicit "process
    everything" action but no longer the only option in the UI).

    Every stage is scoped to exactly this paper_id (run_marker_for_paper /
    run_conversion_for_paper / the extraction stage's own run_id) --
    confirmed real bug this fixes: the previous whole-directory batch calls
    meant a completely unrelated, already-uploaded paper failing Marker or
    QC silently made THIS paper's run report failure too, and the
    "Extracting entities for <paper>..." stage showed no further progress
    for the entire (often 1+ hour) extraction, indistinguishable from a
    hang. Extraction always runs when explicitly requested here -- a
    deliberate, user-triggered re-run (e.g. after a code/prompt fix) is not
    silently skipped just because a PREVIOUS extraction already exists,
    unlike process_all_papers_full's "only if it still needs it" batch
    default."""
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
