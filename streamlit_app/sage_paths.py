"""
sage_paths.py
================
Single, centralized place for every filesystem path this UI touches, and
the one spot that wires `sys.path` so `from pipeline import ...` works from
inside streamlit_app (a sibling directory of src/, not a package under it).
Nothing else in streamlit_app should build these paths by hand.

Canonical convention (single source of truth -- no fallback location):
    - Original/source PDFs:  src/docproc/paper/<paper_id>.pdf
    - Raw Marker JSON:       src/docproc/marker_json/<paper_id>/<paper_id>.json
    - Converted artifacts:   src/paper/<paper_id>/{content.md, provenance.json, qc_report.json}
    - Sage pipeline code:    src/pipeline/*  (ir_schema, validators, store, results_store, ...)
    - docproc adapter code:  src/docproc/{marker_adapter.py, prepare_papers.py, run_qc_batch.py, qc_gate.py}

`data/all_papers/` is NOT used. It was the originally documented PDF
location but was found empty on disk during implementation, while the real
PDF bytes lived under `docproc/paper/`; that fallback has since been
promoted to the one, single, documented convention -- see this module's
git history / the session that made this change for the reasoning.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

SAGE_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = SAGE_ROOT / "src"

PAPER_DIR = SRC_DIR / "paper"
DOCPROC_DIR = SRC_DIR / "docproc"
PDF_DIR = DOCPROC_DIR / "paper"
MARKER_JSON_DIR = DOCPROC_DIR / "marker_json"
CORRECTIONS_DIR = SRC_DIR / "corrections"

for _p in (str(SRC_DIR), str(DOCPROC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# pipeline.store / run_store / results_store / corrections_store all
# resolve their root directories relative to whatever the CURRENT WORKING
# DIRECTORY happens to be when their env var isn't set (see each module's
# own `_..._root()` helper) -- correct for CLI use from src/, wrong for a
# Streamlit process that may be launched from anywhere. Pin them here,
# once, centrally, to real absolute paths under src/. `setdefault` so an
# operator's own explicit override (e.g. in a deployment) still wins.
os.environ.setdefault("IR_PAPERS_ROOT", str(PAPER_DIR))
os.environ.setdefault("IR_STORE_ROOT", str(SRC_DIR / "ir-store"))
os.environ.setdefault("IR_RUNS_ROOT", str(SRC_DIR / "runs"))
os.environ.setdefault("IR_RESULTS_ROOT", str(SRC_DIR / "results"))
os.environ.setdefault("IR_CORRECTIONS_ROOT", str(CORRECTIONS_DIR))


def pdf_path(paper_id: str) -> Optional[Path]:
    candidate = PDF_DIR / f"{paper_id}.pdf"
    return candidate if candidate.is_file() else None


def has_pdf(paper_id: str) -> bool:
    return pdf_path(paper_id) is not None


def content_md_path(paper_id: str) -> Path:
    return PAPER_DIR / paper_id / "content.md"


def provenance_path(paper_id: str) -> Path:
    return PAPER_DIR / paper_id / "provenance.json"


def qc_report_path(paper_id: str) -> Path:
    return PAPER_DIR / paper_id / "qc_report.json"


def is_processed(paper_id: str) -> bool:
    """A paper is 'processed' when both the rendered content and its
    positional provenance exist -- the two artifacts the rest of this
    module (and the extraction pipeline) actually depend on."""
    return content_md_path(paper_id).is_file() and provenance_path(paper_id).is_file()


def source_pdf_dir() -> Path:
    """The single directory to hand to the `marker` CLI as its input
    folder, and where uploaded PDFs are saved."""
    return PDF_DIR


def list_source_paper_ids() -> list[str]:
    """Every paper_id with an original PDF on disk, sorted -- the library
    page's source of truth for "what papers exist", independent of whether
    they've been processed yet."""
    if not PDF_DIR.is_dir():
        return []
    return sorted(p.stem for p in PDF_DIR.glob("*.pdf"))


def delete_paper_source(paper_id: str) -> list[str]:
    """Remove ONLY this paper's uploaded PDF from the library -- not the
    derived Marker JSON or rendered document (paper/<paper_id>/). Those are
    intentionally left alone: if a paper_id is later reused for a genuinely
    different PDF, a fresh Extract run regenerates both from scratch anyway
    (marker_pipeline.run_marker_for_paper no longer passes --skip_existing,
    specifically so a deliberate, user-triggered Extract always reprocesses
    the current PDF on disk rather than ever trusting leftover output from
    whatever was there before -- see that function's own docstring), so
    there's nothing to proactively clean up here, and it avoids destroying
    a paper's intermediate artifacts for no reason while it's still mid-use
    elsewhere. `ir-store/`, `results/`, and `runs/` -- the committed
    extraction ledger -- were never touched by this function and still
    aren't. Returns the paths actually removed (usually just the one PDF,
    or empty if nothing was there)."""
    removed: list[str] = []
    pdf = PDF_DIR / f"{paper_id}.pdf"
    if pdf.is_file():
        pdf.unlink()
        removed.append(str(pdf))
    return removed


def rename_paper_source(old_paper_id: str, new_paper_id: str) -> tuple[bool, str]:
    """Renames this paper's uploaded PDF and, if present, its derived
    Marker JSON and rendered document -- keeps all three staging artifacts
    consistent under the new name (rather than orphaning already-done
    Marker/conversion work, which a bare PDF rename alone would do).

    Checks every destination is free FIRST, before renaming anything, so a
    name collision never leaves a half-renamed paper split across two ids
    (`Path.rename` would otherwise silently overwrite an existing file, or
    raise partway through a multi-step rename with no clean way back).
    Returns (True, message) on success or (False, reason) if refused --
    never raises for an ordinary "name taken" case."""
    old_paper_id = old_paper_id.strip()
    new_paper_id = new_paper_id.strip()
    if not new_paper_id:
        return False, "New name cannot be empty."
    if old_paper_id == new_paper_id:
        return False, "New name is the same as the current name."

    old_pdf = PDF_DIR / f"{old_paper_id}.pdf"
    new_pdf = PDF_DIR / f"{new_paper_id}.pdf"
    if new_pdf.exists():
        return False, f"A stored paper named '{new_paper_id}' already exists."
    old_marker = MARKER_JSON_DIR / old_paper_id
    new_marker = MARKER_JSON_DIR / new_paper_id
    if new_marker.exists():
        return False, f"Marker output for '{new_paper_id}' already exists."
    old_paper = PAPER_DIR / old_paper_id
    new_paper = PAPER_DIR / new_paper_id
    if new_paper.exists():
        return False, f"A rendered document for '{new_paper_id}' already exists."

    if old_pdf.is_file():
        old_pdf.rename(new_pdf)
    if old_marker.is_dir():
        old_marker.rename(new_marker)
    if old_paper.is_dir():
        old_paper.rename(new_paper)
    return True, f"Renamed '{old_paper_id}' to '{new_paper_id}'."
