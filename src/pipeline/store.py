"""ir-store: append-only JSONL of committed and unresolved records; the last line for a record is its status."""

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


def jsonl_path(root: Path, paper_id: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{paper_id}.jsonl"


def append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def _store_path(paper_id: str, root: Optional[Path] = None) -> Path:
    return jsonl_path(root or _store_root(), paper_id)


def append_record(
    paper_id: str,
    entity_type: str,
    record_id: str,
    status: str,
    payload: dict[str, Any],
    extra: Optional[dict[str, Any]] = None,
    root: Optional[Path] = None,
) -> dict[str, Any]:
    """status ('ready' or 'unresolved') is enforced by the caller, ir_service.commit_record."""
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
    append_jsonl(_store_path(paper_id, root), entry)
    return entry


def read_all(paper_id: str, root: Optional[Path] = None) -> list[dict[str, Any]]:
    return read_jsonl(_store_path(paper_id, root))


def has_paper(paper_id: str, root: Optional[Path] = None) -> bool:
    """True when an ir-store file exists for this paper_id."""
    return _store_path(paper_id, root).is_file()


def rename_paper(old_paper_id: str, new_paper_id: str, root: Optional[Path] = None) -> bool:
    """Rename the store file only (line contents keep the old paper_id); False if there is none. The caller checks
    the destination is free."""
    old_path = _store_path(old_paper_id, root)
    if not old_path.is_file():
        return False
    old_path.rename(_store_path(new_paper_id, root))
    return True
