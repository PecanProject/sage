"""`pipeline/run_store.py` -- permanent, per-run artifact capture for the
Extraction -> Conversion -> deterministic validation -> AI Validator
pipeline (`orchestrator.py`).

Every attempt at every stage is written here, success or failure, before
`orchestrator.py` decides what to do next. This is the direct fix for
"results sometimes don't appear where expected" and "unclear failures" --
nothing is ever discarded on a failed attempt; a record that never reaches
`ready` still leaves its full extraction/conversion/critique trail on disk
under its `run_id`.

Layout:
    runs/<run_id>/manifest.json
    runs/<run_id>/records/<entity_type>__<record_id>/extraction/attemptN.json
    runs/<run_id>/records/<entity_type>__<record_id>/conversion/attemptN.json
    runs/<run_id>/records/<entity_type>__<record_id>/conversion_validation/attemptN.json
    runs/<run_id>/records/<entity_type>__<record_id>/ai_validation/attemptN.json
    runs/<run_id>/records/<entity_type>__<record_id>/final.json
    runs/<run_id>/records/<entity_type>__<record_id>/record_manifest.json

`ir-store/<paper_id>.jsonl` (`pipeline/store.py`) remains the separate,
unchanged, append-only FINAL authoritative store -- this module never writes
there. It exists one layer earlier: everything that led up to (or failed to
reach) a store entry.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_RUNS_ROOT = Path(os.environ.get("IR_RUNS_ROOT", "runs"))


def _runs_root() -> Path:
    """Resolve the runs root from the current environment at call time (same
    pattern as `store._store_root()` / `content_reader._papers_root()`)."""
    return Path(os.environ.get("IR_RUNS_ROOT", str(DEFAULT_RUNS_ROOT)))


def run_dir(run_id: str) -> Path:
    return _runs_root() / run_id


def record_dir(run_id: str, record_key: str) -> Path:
    return run_dir(run_id) / "records" / record_key


def _write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str, sort_keys=False), encoding="utf-8")
    return path


def save_stage_attempt(run_id: str, record_key: str, stage: str, attempt: int, data: Any) -> Path:
    """`stage` is one of "extraction", "conversion", "conversion_validation",
    "ai_validation" -- one file per attempt, never overwritten."""
    path = record_dir(run_id, record_key) / stage / f"attempt{attempt}.json"
    return _write_json(path, data)


def save_final(run_id: str, record_key: str, data: Any) -> Path:
    return _write_json(record_dir(run_id, record_key) / "final.json", data)


def save_record_manifest(run_id: str, record_key: str, data: Any) -> Path:
    return _write_json(record_dir(run_id, record_key) / "record_manifest.json", data)


def save_run_manifest(run_id: str, data: Any) -> Path:
    return _write_json(run_dir(run_id) / "manifest.json", data)


def load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_run_manifest(run_id: str) -> Any:
    return load_json(run_dir(run_id) / "manifest.json")


def list_records(run_id: str) -> list[str]:
    records_root = run_dir(run_id) / "records"
    if not records_root.is_dir():
        return []
    return sorted(p.name for p in records_root.iterdir() if p.is_dir())


def list_runs() -> list[str]:
    root = _runs_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())
