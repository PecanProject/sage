from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_STORE_ROOT = Path(os.environ.get("IR_STORE_ROOT", "ir-store"))


def _store_root() -> Path:
    """Resolve the store root from the current environment at call time."""
    return Path(os.environ.get("IR_STORE_ROOT", str(DEFAULT_STORE_ROOT)))


def _store_path(paper_id: str, root: Optional[Path] = None) -> Path:
    if root is None:
        root = _store_root()
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{paper_id}.jsonl"


def append_record(
    paper_id: str,
    entity_type: str,
    record_id: str,
    status: str,
    payload: dict[str, Any],
    extra: Optional[dict[str, Any]] = None,
    root: Optional[Path] = None,
) -> dict[str, Any]:
    entry = {
        "ts": time.time(),
        "paper_id": paper_id,
        "entity_type": entity_type,
        "record_id": record_id,
        "status": status,
        "payload": payload,
    }
    if extra:
        entry.update(extra)
    path = _store_path(paper_id, root)
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def read_all(paper_id: str, root: Optional[Path] = None) -> list[dict[str, Any]]:
    path = _store_path(paper_id, root)
    if not path.exists():
        return []
    entries = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def has_paper(paper_id: str, root: Optional[Path] = None) -> bool:
    return _store_path(paper_id, root).is_file()


def rename_paper(old_paper_id: str, new_paper_id: str, root: Optional[Path] = None) -> bool:
    old_path = _store_path(old_paper_id, root)
    if not old_path.is_file():
        return False
    old_path.rename(_store_path(new_paper_id, root))
    return True


def latest_status(paper_id: str, entity_type: str, record_id: str, root: Optional[Path] = None) -> Optional[str]:
    entries = read_all(paper_id, root)
    for entry in reversed(entries):
        if entry["entity_type"] == entity_type and entry["record_id"] == record_id:
            return entry["status"]
    return None
