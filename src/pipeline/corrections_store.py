"""`pipeline/corrections_store.py` -- the scientist review/correction log.

Mirrors `store.py`'s append-only JSONL pattern exactly (one file per paper,
never rewritten, never deleted), kept as a separate root from `ir-store/`
on purpose: the original extraction (`ir-store/`) must remain immutable,
and a correction is a *statement about* a record, not a replacement of it.

    ORIGINAL EXTRACTION (ir-store/<paper_id>.jsonl, untouched)
              +
    CORRECTION LOG (corrections/<paper_id>.jsonl, this module)
              v
    EFFECTIVE REVIEWED VALUE (computed by the caller, e.g. streamlit_app)

This module only ever appends and reads; it has no opinion about what an
"effective value" is -- that's a UI/service-layer concern (see
streamlit_app/api_client.py), consistent with keeping business logic out
of the storage layer, same as store.py/run_store.py/results_store.py.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_CORRECTIONS_ROOT = Path(os.environ.get("IR_CORRECTIONS_ROOT", "corrections"))

# The only actions this store knows about -- matches the review actions
# specified for the Streamlit UI. Enforced here, server-side, same
# discipline as store.py's status enforcement note.
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
    if root is None:
        root = _corrections_root()
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{paper_id}.jsonl"


def append_correction(
    paper_id: str,
    entity_type: str,
    record_id: str,
    action: str,
    field_name: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    reviewer: str = "scientist",
    root: Optional[Path] = None,
) -> dict[str, Any]:
    """Record one review action. `field_name` is None for a record-level
    action (e.g. Approve on the whole record); set for a field-level one
    (Correct Value, Relocate Evidence, Confirm Unresolved). Never mutates
    or validates against ir-store -- the caller (streamlit_app/api_client)
    decides whether a correction needs to be re-run through deterministic
    validation before it's trusted."""
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
    path = _corrections_path(paper_id, root)
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def read_all(paper_id: str, root: Optional[Path] = None) -> list[dict[str, Any]]:
    path = _corrections_path(paper_id, root)
    if not path.exists():
        return []
    entries = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def read_for_record(
    paper_id: str, entity_type: str, record_id: str, field_name: Optional[str] = None, root: Optional[Path] = None
) -> list[dict[str, Any]]:
    """All corrections for one record, in chronological order (oldest
    first) -- the caller applies them in order to compute the effective
    state. When `field_name` is given, also includes record-level entries
    (field_name=None) so a record-level Approve still shows up for a
    field-level query."""
    return [
        e for e in read_all(paper_id, root)
        if e["entity_type"] == entity_type and e["record_id"] == record_id
        and (field_name is None or e["field_name"] in (None, field_name))
    ]


def latest_action_for_field(
    paper_id: str, entity_type: str, record_id: str, field_name: Optional[str], root: Optional[Path] = None
) -> Optional[dict[str, Any]]:
    """The most recent correction entry that applies to this exact field
    (or the whole record, when field_name is None) -- "latest wins", same
    convention as store.py's latest_status."""
    matches = [
        e for e in read_all(paper_id, root)
        if e["entity_type"] == entity_type and e["record_id"] == record_id and e["field_name"] == field_name
    ]
    return matches[-1] if matches else None
