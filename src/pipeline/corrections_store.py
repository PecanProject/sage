from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

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
    return [
        e for e in read_all(paper_id, root)
        if e["entity_type"] == entity_type and e["record_id"] == record_id
        and (field_name is None or e["field_name"] in (None, field_name))
    ]


def latest_action_for_field(
    paper_id: str, entity_type: str, record_id: str, field_name: Optional[str], root: Optional[Path] = None
) -> Optional[dict[str, Any]]:
    matches = [
        e for e in read_all(paper_id, root)
        if e["entity_type"] == entity_type and e["record_id"] == record_id and e["field_name"] == field_name
    ]
    return matches[-1] if matches else None
