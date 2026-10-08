"""Scientist review log: append-only JSONL per paper, separate from ir-store so the original extraction is never
modified. Computing the effective reviewed value is the caller's job (streamlit_app/api_client.py)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

from pipeline.store import jsonl_path, append_jsonl, read_jsonl

DEFAULT_CORRECTIONS_ROOT = Path(os.environ.get("IR_CORRECTIONS_ROOT", "corrections"))

VALID_ACTIONS = {
    "approve",
    "correct_value",
    "relocate_evidence",
    "relink_entity",
    "confirm_unresolved",
    "note",
}


def _corrections_root() -> Path:
    return Path(os.environ.get("IR_CORRECTIONS_ROOT", str(DEFAULT_CORRECTIONS_ROOT)))


def _corrections_path(paper_id: str, root: Optional[Path] = None) -> Path:
    return jsonl_path(root or _corrections_root(), paper_id)


def _applies_to_run(entry: dict[str, Any], run_id: Optional[str]) -> bool:
    """Record ids repeat across runs, so a correction applies only to its own run; an entry without run_id applies
    to every run."""
    return run_id is None or entry.get("run_id") in (None, run_id)


def append_correction(
    paper_id: str,
    entity_type: str,
    record_id: str,
    action: str,
    field_name: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    reviewer: str = "scientist",
    root: Optional[Path] = None,
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Record one review action; field_name is None for a record-level action."""
    if action not in VALID_ACTIONS:
        raise ValueError(f"Unknown correction action {action!r}; must be one of {sorted(VALID_ACTIONS)}")

    entry = {
        "ts": time.time(),
        "paper_id": paper_id,
        "entity_type": entity_type,
        "record_id": record_id,
        "field_name": field_name,
        "action": action,
        "payload": payload or {},
        "reviewer": reviewer,
    }
    if run_id is not None:
        entry["run_id"] = run_id
    append_jsonl(_corrections_path(paper_id, root), entry)
    return entry


def read_all(paper_id: str, root: Optional[Path] = None) -> list[dict[str, Any]]:
    return read_jsonl(_corrections_path(paper_id, root))


def latest_action_for_field(
    paper_id: str, entity_type: str, record_id: str, field_name: Optional[str], root: Optional[Path] = None,
    run_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """The most recent correction entry that applies to this exact field
    (or the whole record, when field_name is None) -- "latest wins"."""
    matches = [
        e for e in read_all(paper_id, root)
        if e["entity_type"] == entity_type and e["record_id"] == record_id and e["field_name"] == field_name
        and _applies_to_run(e, run_id)
    ]
    return matches[-1] if matches else None
