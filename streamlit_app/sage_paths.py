
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
    return content_md_path(paper_id).is_file() and provenance_path(paper_id).is_file()


def source_pdf_dir() -> Path:
    return PDF_DIR


def list_source_paper_ids() -> list[str]:
    if not PDF_DIR.is_dir():
        return []
    return sorted(p.stem for p in PDF_DIR.glob("*.pdf"))


def delete_paper_source(paper_id: str) -> list[str]:
    removed: list[str] = []
    pdf = PDF_DIR / f"{paper_id}.pdf"
    if pdf.is_file():
        pdf.unlink()
        removed.append(str(pdf))
    return removed


def rename_paper_source(old_paper_id: str, new_paper_id: str) -> tuple[bool, str]:
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
