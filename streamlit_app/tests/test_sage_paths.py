"""Focused tests for sage_paths.delete_paper_source -- the library-cleanup
action that removes ONLY a paper's uploaded PDF, deliberately leaving its
derived Marker JSON, rendered document, and any committed extraction
(ir-store/results/runs, which this module doesn't even know the paths of)
untouched. See that function's own docstring for why: a paper_id later
reused for a genuinely different PDF must not silently inherit stale
Marker/document output -- that staleness risk is handled separately, by
marker_pipeline.run_marker_for_paper no longer passing --skip_existing, not
by this function proactively deleting derived artifacts.
"""

from __future__ import annotations

import sage_paths


def _set_dirs(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "docproc" / "paper"
    marker_dir = tmp_path / "docproc" / "marker_json"
    paper_dir = tmp_path / "paper"
    pdf_dir.mkdir(parents=True)
    marker_dir.mkdir(parents=True)
    paper_dir.mkdir(parents=True)
    monkeypatch.setattr(sage_paths, "PDF_DIR", pdf_dir)
    monkeypatch.setattr(sage_paths, "MARKER_JSON_DIR", marker_dir)
    monkeypatch.setattr(sage_paths, "PAPER_DIR", paper_dir)
    return pdf_dir, marker_dir, paper_dir


def test_delete_paper_source_removes_only_the_pdf(tmp_path, monkeypatch):
    pdf_dir, marker_dir, paper_dir = _set_dirs(monkeypatch, tmp_path)
    (pdf_dir / "pecan.pdf").write_bytes(b"%PDF-fake")
    (marker_dir / "pecan").mkdir()
    (marker_dir / "pecan" / "pecan.json").write_text("{}")
    (paper_dir / "pecan").mkdir()
    (paper_dir / "pecan" / "content.md").write_text("x")

    removed = sage_paths.delete_paper_source("pecan")

    assert not (pdf_dir / "pecan.pdf").exists()
    # Marker JSON and the rendered document are deliberately NOT removed.
    assert (marker_dir / "pecan" / "pecan.json").exists()
    assert (paper_dir / "pecan" / "content.md").exists()
    assert removed == [str(pdf_dir / "pecan.pdf")]


def test_delete_paper_source_is_a_noop_for_unknown_paper(tmp_path, monkeypatch):
    _set_dirs(monkeypatch, tmp_path)
    removed = sage_paths.delete_paper_source("never_existed")
    assert removed == []


def test_delete_paper_source_never_touches_a_different_paper_id(tmp_path, monkeypatch):
    pdf_dir, marker_dir, paper_dir = _set_dirs(monkeypatch, tmp_path)
    (pdf_dir / "pecan.pdf").write_bytes(b"%PDF-fake")
    (pdf_dir / "other_paper.pdf").write_bytes(b"%PDF-fake")

    sage_paths.delete_paper_source("pecan")

    assert not (pdf_dir / "pecan.pdf").exists()
    assert (pdf_dir / "other_paper.pdf").exists()


def test_rename_paper_source_renames_all_present_artifacts(tmp_path, monkeypatch):
    pdf_dir, marker_dir, paper_dir = _set_dirs(monkeypatch, tmp_path)
    (pdf_dir / "pecan.pdf").write_bytes(b"%PDF-fake")
    (marker_dir / "pecan").mkdir()
    (marker_dir / "pecan" / "pecan.json").write_text("{}")
    (paper_dir / "pecan").mkdir()
    (paper_dir / "pecan" / "content.md").write_text("x")

    ok, message = sage_paths.rename_paper_source("pecan", "Daren-1997-Canopy")

    assert ok is True
    assert "Daren-1997-Canopy" in message
    assert not (pdf_dir / "pecan.pdf").exists()
    assert (pdf_dir / "Daren-1997-Canopy.pdf").exists()
    assert (marker_dir / "Daren-1997-Canopy" / "pecan.json").exists()
    assert (paper_dir / "Daren-1997-Canopy" / "content.md").exists()


def test_rename_paper_source_renames_partial_artifacts_only(tmp_path, monkeypatch):
    # Only a PDF exists (never processed) -- must not raise looking for
    # marker_json/paper directories that were never created.
    pdf_dir, marker_dir, paper_dir = _set_dirs(monkeypatch, tmp_path)
    (pdf_dir / "pecan.pdf").write_bytes(b"%PDF-fake")

    ok, _ = sage_paths.rename_paper_source("pecan", "Daren-1997-Canopy")

    assert ok is True
    assert (pdf_dir / "Daren-1997-Canopy.pdf").exists()
    assert not (marker_dir / "pecan").exists()
    assert not (marker_dir / "Daren-1997-Canopy").exists()


def test_rename_paper_source_refuses_when_destination_taken(tmp_path, monkeypatch):
    pdf_dir, marker_dir, paper_dir = _set_dirs(monkeypatch, tmp_path)
    (pdf_dir / "pecan.pdf").write_bytes(b"%PDF-fake")
    (pdf_dir / "other_paper.pdf").write_bytes(b"%PDF-fake")

    ok, message = sage_paths.rename_paper_source("pecan", "other_paper")

    assert ok is False
    assert "already exists" in message
    # Nothing was touched -- refused cleanly, not partially renamed.
    assert (pdf_dir / "pecan.pdf").exists()
    assert (pdf_dir / "other_paper.pdf").exists()


def test_rename_paper_source_refuses_empty_or_identical_name(tmp_path, monkeypatch):
    pdf_dir, marker_dir, paper_dir = _set_dirs(monkeypatch, tmp_path)
    (pdf_dir / "pecan.pdf").write_bytes(b"%PDF-fake")

    ok, _ = sage_paths.rename_paper_source("pecan", "  ")
    assert ok is False
    ok, _ = sage_paths.rename_paper_source("pecan", "pecan")
    assert ok is False
    assert (pdf_dir / "pecan.pdf").exists()
