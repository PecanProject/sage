"""Content hashes that detect a running service with stale schema/validator code and record what produced a run."""

from __future__ import annotations

import hashlib
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PIPELINE_DIR.parent

# The files that decide whether a record is valid or what the running service does with it.
_SCHEMA_FILES = ["ir_schema.py", "validators.py", "ir_service.py", "reconstruction.py"]


def schema_fingerprint() -> str:
    """Hash of the schema/validation source actually on disk right now."""
    h = hashlib.sha256()
    for name in _SCHEMA_FILES:
        h.update((PIPELINE_DIR / name).read_bytes())
    return h.hexdigest()[:16]


def config_fingerprint(path: Path) -> str:
    """Hash of an arbitrary config file (e.g. the root opencode.json), for
    recording exactly which config produced a given run -- not itself
    validated against anything, just captured for after-the-fact comparison
    across runs."""
    path = Path(path)
    if not path.is_file():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
