"""
ir-store persistence — Playbook Section 7: `ir-store/<paper_id>.jsonl,
committed + unresolved records, append-only audit trail`.

Append-only, deliberately: every commit_record/flag_unresolved call adds a
line, never rewrites or deletes one. A record's current status is "whatever
the last line for that record_key says" — the history itself is the audit
trail Section 6's tool surface exists to support (a reviewer can see every
attempt, not just the final one).
"""

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
    """status must be 'ready' or 'unresolved' -- enforced by the caller
    (ir_service.commit_record), never trusted from this layer alone."""
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
    """True when an ir-store JSONL file already exists for this paper_id --
    used by callers (e.g. api_client.rename_extracted_paper) to check a
    rename destination is free before renaming anything."""
    return _store_path(paper_id, root).is_file()


def rename_paper(old_paper_id: str, new_paper_id: str, root: Optional[Path] = None) -> bool:
    """Renames the ir-store JSONL FILE only -- old_paper_id.jsonl ->
    new_paper_id.jsonl. Never rewrites the file's own content: every
    existing line's own "paper_id" field still says the OLD id, and this
    module's append-only invariant ("never rewrite or delete a LINE") is
    still honored -- only the file's name changes. Safe to do: nothing in
    this codebase reads a loaded entry's own "paper_id" field back out
    after `read_all()` and compares it to anything (confirmed by
    inspection), so a pure file rename is sufficient for the store to be
    found under its new name going forward. Caller is responsible for
    checking the destination doesn't already exist first (see
    api_client.rename_extracted_paper). Returns True if there was
    something to rename, False if old_paper_id has no store file at all."""
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
