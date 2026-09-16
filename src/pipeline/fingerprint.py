from __future__ import annotations

import hashlib
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PIPELINE_DIR.parent

# The files whose behavior actually determines whether a record is valid.
# Deliberately narrow -- this is a staleness check, not a full source hash.
_SCHEMA_FILES = ["ir_schema.py", "validators.py"]


def schema_fingerprint() -> str:
    """Hash of the schema/validation source actually on disk right now."""
    h = hashlib.sha256()
    for name in _SCHEMA_FILES:
        h.update((PIPELINE_DIR / name).read_bytes())
    return h.hexdigest()[:16]


def config_fingerprint(path: Path) -> str:
    path = Path(path)
    if not path.is_file():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
