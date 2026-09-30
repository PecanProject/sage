"""`pipeline/results_store.py` -- unified, per-paper finalized IR output.

Sits one layer above `pipeline.store` (the append-only `ir-store/`, the
authoritative record of every commit/flag ever made) and `pipeline.run_store`
(per-run, per-attempt artifacts under `runs/`): this module persists the
*derived*, deterministic "current best known state" for one paper, computed
by `orchestrator.finalize_paper` from `ir-store`'s latest-entry-per-record-key
state.

This is never itself a source of truth and never written to directly by
`propose_record`/`commit_record`/`flag_unresolved` -- `ir-store` remains
that. `results/<paper_id>/result.json` is a regenerable snapshot: deleting
it and re-running `orchestrator.py finalize` reproduces the same content
(aside from the `finalized_at` timestamp) from `ir-store` alone, so it is
never at risk of drifting out of sync in a way that matters -- it can
always be thrown away and rebuilt.

Run isolation (this module's per-run layout)
--------------------------------------------
`orchestrator.run_paper` writes its per-entity results under
`results/<paper_id>/<run_id>/`, so two runs can never overwrite or merge each
other's outputs (before this, `save_multi_entity_results` cleared and
rewrote a shared `results/<paper_id>/<Entity>/` directory, last writer wins).
A `results/<paper_id>/LATEST` pointer file names the most recent run that
COMPLETED; it is written atomically and only on completion, so a crashed or
still-running run never changes what readers (the review UI, `finalize`)
see. Readers that pass no `run_id` resolve `LATEST`; when a paper has no
`LATEST` (results written before this layout existed) they fall back to the
legacy flat `results/<paper_id>/<Entity>...` files, which stay readable.
Calls that write with no `run_id` keep the legacy flat behavior exactly (used
by unit tests and any pre-existing script).

`result.json` (finalize's paper-level aggregate, built from ir-store) is not
run-scoped and stays at `results/<paper_id>/result.json`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

DEFAULT_RESULTS_ROOT = Path(os.environ.get("IR_RESULTS_ROOT", "results"))

LATEST_FILENAME = "LATEST"
RUN_MARKER_FILENAME = "_run.json"


def _results_root() -> Path:
    """Resolve the results root from the current environment at call time
    (same pattern as `store._store_root()` / `run_store._runs_root()`)."""
    return Path(os.environ.get("IR_RESULTS_ROOT", str(DEFAULT_RESULTS_ROOT)))


def paper_dir(paper_id: str) -> Path:
    return _results_root() / paper_id


def list_paper_ids() -> list[str]:
    """Every paper_id with a results/<paper_id>/ directory -- the
    independent source of truth for "which papers have extracted records",
    deliberately never cross-referenced against whether a source PDF still
    exists (a real, confirmed UI bug: a paper's Extracted Papers row used
    to vanish entirely if its PDF was deleted from the library, even
    though its real, reviewable results were still sitting here
    untouched)."""
    root = _results_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def rename_paper_dir(old_paper_id: str, new_paper_id: str) -> bool:
    """Renames the results/<paper_id>/ DIRECTORY only -- moves the whole
    derived snapshot to its new name; never rewrites file contents inside
    it. Caller is responsible for checking the destination doesn't already
    exist first (see api_client.rename_extracted_paper). Returns True if
    there was something to rename, False if old_paper_id has no results
    directory at all."""
    old_dir = paper_dir(old_paper_id)
    if not old_dir.is_dir():
        return False
    old_dir.rename(paper_dir(new_paper_id))
    return True


def result_path(paper_id: str) -> Path:
    return paper_dir(paper_id) / "result.json"


def save_result(paper_id: str, data: Any) -> Path:
    path = result_path(paper_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str, sort_keys=False), encoding="utf-8")
    return path


def load_result(paper_id: str) -> Any:
    return json.loads(result_path(paper_id).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Run scoping: results/<paper_id>/<run_id>/ + results/<paper_id>/LATEST
# ---------------------------------------------------------------------------


def run_results_dir(paper_id: str, run_id: str) -> Path:
    return paper_dir(paper_id) / run_id


def latest_run_id(paper_id: str) -> Optional[str]:
    """The most recent run that COMPLETED for this paper, or None when no
    run-scoped results exist (legacy flat layout / never run)."""
    path = paper_dir(paper_id) / LATEST_FILENAME
    if not path.is_file():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def set_latest(paper_id: str, run_id: str) -> Path:
    """Atomically point LATEST at `run_id` (write to a temp file in the
    same directory, then `os.replace`) -- a reader never observes a partial
    pointer. Callers write this ONLY after a run completed."""
    directory = paper_dir(paper_id)
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f".{LATEST_FILENAME}.{os.getpid()}.tmp"
    tmp.write_text(run_id, encoding="utf-8")
    os.replace(tmp, directory / LATEST_FILENAME)
    return directory / LATEST_FILENAME


def mark_run_results(paper_id: str, run_id: str, data: Optional[dict] = None) -> Path:
    """Write the small marker file that identifies a directory as a run's
    results directory (so `list_run_ids` never mistakes an entity directory
    for a run)."""
    directory = run_results_dir(paper_id, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / RUN_MARKER_FILENAME
    path.write_text(json.dumps({"run_id": run_id, "paper_id": paper_id, **(data or {})}, indent=2), encoding="utf-8")
    return path


def list_run_ids(paper_id: str) -> list[str]:
    directory = paper_dir(paper_id)
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.is_dir() and (p / RUN_MARKER_FILENAME).is_file())


def _read_base(paper_id: str, run_id: Optional[str]) -> Path:
    """Directory READERS look in: the given run's, else LATEST's, else the
    legacy flat directory."""
    if run_id is not None:
        return run_results_dir(paper_id, run_id)
    latest = latest_run_id(paper_id)
    return run_results_dir(paper_id, latest) if latest else paper_dir(paper_id)


def _write_base(paper_id: str, run_id: Optional[str]) -> Path:
    """Directory WRITERS use: the given run's, else the legacy flat
    directory (unchanged behavior for callers that pass no run_id)."""
    return run_results_dir(paper_id, run_id) if run_id is not None else paper_dir(paper_id)


# ---------------------------------------------------------------------------
# Per-entity-type result files (results/<paper_id>[/<run_id>]/<EntityType>.json)
# -- the `orchestrator.run_paper` output. Additive to, and independent of,
# `save_result`/`load_result` above (`finalize`'s single combined-file
# output): both can coexist under the same paper_id without collision,
# since they write different filenames.
# ---------------------------------------------------------------------------


def entity_result_path(paper_id: str, entity_type: str, run_id: Optional[str] = None) -> Path:
    return _read_base(paper_id, run_id) / f"{entity_type}.json"


def save_entity_result(paper_id: str, entity_type: str, data: Any, run_id: Optional[str] = None) -> Path:
    path = _write_base(paper_id, run_id) / f"{entity_type}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str, sort_keys=False), encoding="utf-8")
    return path


def load_entity_result(paper_id: str, entity_type: str, run_id: Optional[str] = None) -> Any:
    return json.loads(entity_result_path(paper_id, entity_type, run_id).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Multi-record result storage (Phase A: Variable; Phase B: + Treatment;
# Phase C: + Observation) --
# results/<paper_id>[/<run_id>]/<EntityType>/<record_id>.json, one file per record.
#
# Deliberately a DIFFERENT path shape from entity_result_path's single
# `<EntityType>.json` file above (a directory, no extension, vs a file with
# one) so the two conventions can never collide on disk for the same
# entity_type. An entity_type uses exactly one convention at a time, listed
# in MULTI_RECORD_ENTITY_TYPES; every other entity_type keeps using the
# single-file convention untouched.
# ---------------------------------------------------------------------------

MULTI_RECORD_ENTITY_TYPES: set[str] = {
    "Variable", "Treatment", "Observation",  # Phase A/B/C
    # Universal Multi-Record pass: the calibration/validation protocol and
    # datapackage schema both establish these as genuinely "zero or more per
    # paper", not "exactly one" -- e.g. methods.csv's own primary key is
    # "method name" (protocol Section 6.4: "give EACH DISTINCT method a
    # stable method_id"), confirmed as a real (not hypothetical) gap against
    # Oceologia-1998: the single Method record it was forced into captures
    # only the paper's 15N tracer method, while Observation records for
    # unrelated measurements (fine root mass, extractable NH4-N, etc.) were
    # all still forced to reference that same, inapplicable method_id.
    "Site", "Species", "Method", "Crop", "Management", "Study", "TreatmentPair", "Coverage",
}


def entity_dir(paper_id: str, entity_type: str, run_id: Optional[str] = None) -> Path:
    return _read_base(paper_id, run_id) / entity_type


def multi_entity_result_path(paper_id: str, entity_type: str, record_id: str, run_id: Optional[str] = None) -> Path:
    return entity_dir(paper_id, entity_type, run_id) / f"{record_id}.json"


def save_multi_entity_results(
    paper_id: str, entity_type: str, records: list[Any], run_id: Optional[str] = None,
) -> list[Path]:
    """Overwrite ALL stored records for this (paper, entity_type[, run]) with
    exactly the given list -- one file per record_id. The directory is
    cleared first, not just added to: a run that finds fewer real
    candidates than a previous run must not leave a stale, no-longer-real
    record_id file behind (same "built ONLY from what this call itself
    produced" principle `run_paper`'s own docstring already states for the
    single-record case). With a `run_id` that directory belongs to that run
    alone, so clearing it can never touch another run's results."""
    directory = _write_base(paper_id, run_id) / entity_type
    if directory.is_dir():
        for existing in directory.glob("*.json"):
            existing.unlink()
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for record in records:
        path = directory / f"{record['record_id']}.json"
        path.write_text(json.dumps(record, indent=2, default=str, sort_keys=False), encoding="utf-8")
        paths.append(path)
    return paths


def load_multi_entity_results(paper_id: str, entity_type: str, run_id: Optional[str] = None) -> list[Any]:
    """Every stored record for this (paper, entity_type), in a stable
    (record_id-sorted) order. An empty list -- never an exception -- is a
    valid, normal outcome (no real candidates found/ready), unlike
    `load_entity_result`'s FileNotFoundError for the single-record case."""
    directory = entity_dir(paper_id, entity_type, run_id)
    if not directory.is_dir():
        return []
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))]


def load_any_entity_results(paper_id: str, entity_type: str, run_id: Optional[str] = None) -> list[Any]:
    """The single, canonical way for a caller that just wants "every result
    we have for this (paper, entity_type)" to read it without needing to
    know which storage convention this particular entity_type currently
    uses -- always a list, 0-or-1 entries for a single-record type, 0-or-more
    for a multi-record one. Reads the given run's results, else the latest
    completed run's, else the legacy flat layout."""
    if entity_type in MULTI_RECORD_ENTITY_TYPES:
        return load_multi_entity_results(paper_id, entity_type, run_id)
    try:
        return [load_entity_result(paper_id, entity_type, run_id)]
    except FileNotFoundError:
        return []
