"""`pipeline/fingerprint.py` -- cheap content hashes used to detect drift
between what's on disk and what a long-running process actually has loaded,
and to record exactly which schema/config version produced a given run.

This exists because of a concrete, previously-observed failure: a
long-running `uvicorn pipeline.ir_service:app` process kept an old, more
permissive copy of `ir_schema.py` in memory after the file on disk was
edited to make `Citation.persistent_identifier` a required `ExtractedField`.
A record with a bare `null` for that field was validated and committed
against the stale in-memory schema, and now sits in `ir-store/` failing
re-validation against the current code. `schema_fingerprint()` gives both
`ir_service` (via `/health`) and `orchestrator.check_health` a cheap way to
notice that gap before it happens again, instead of after.
"""

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
    """Hash of an arbitrary config file (e.g. the root opencode.json), for
    recording exactly which config produced a given run -- not itself
    validated against anything, just captured for after-the-fact comparison
    across runs."""
    path = Path(path)
    if not path.is_file():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
