"""Tests for provenance_adapter.py -- resolving block_anchor -> real PDF
page + normalized polygon. Uses the real pecan.pdf as a valid-PDF fixture
(borrowed only for its real page dimensions), with controlled fake
provenance.json content per test for precise, isolated assertions. Also
exercises the real, unmodified pecan artifacts directly for the exact
proof case from the investigation (LLM page_number vs. real page_id).
"""

from __future__ import annotations

import json

import pytest

import provenance_adapter as pa
import sage_paths

REAL_PECAN_PDF = sage_paths.pdf_path("pecan")
# Real project PDFs are mutable by design (the library page's own Delete
# action removes a PDF; a re-upload can legitimately land under a different
# paper_id/filename) -- confirmed directly: "pecan.pdf" is no longer present
# on disk after real use of the app. Skip cleanly with a clear reason
# instead of failing confusingly on `None` when this file this whole module
# borrows isn't there, rather than asserting it must always exist.
_MISSING_PECAN_REASON = (
    "real docproc/paper/pecan.pdf not present on disk -- this test module borrows it only "
    "for known-valid PDF page geometry; nothing to borrow if it's been renamed/removed "
    "(e.g. via the library page's own Delete action)."
)


@pytest.fixture(autouse=True)
def _clear_adapter_cache():
    pa.clear_cache()
    yield
    pa.clear_cache()


@pytest.fixture()
def fake_paper(tmp_path, monkeypatch):
    """Isolated paper_id backed by the real pecan PDF (for valid, known
    612x792 page geometry) and a provenance.json this fixture controls."""
    if REAL_PECAN_PDF is None:
        pytest.skip(_MISSING_PECAN_REASON)
    paper_id = "fixture_paper"
    provenance_file = tmp_path / "provenance.json"

    def _provenance_path(pid):
        return provenance_file if pid == paper_id else sage_paths.provenance_path(pid)

    def _pdf_path(pid):
        return REAL_PECAN_PDF if pid == paper_id else sage_paths.pdf_path(pid)

    monkeypatch.setattr(sage_paths, "provenance_path", _provenance_path)
    monkeypatch.setattr(sage_paths, "pdf_path", _pdf_path)

    def write(entries: dict):
        provenance_file.write_text(json.dumps(entries), encoding="utf-8")
        pa.clear_cache()

    return paper_id, write


def test_resolves_bbox_anchor_to_normalized_top_left_polygon(fake_paper):
    paper_id, write = fake_paper
    # A known real page size for pecan.pdf is 612x792 (US Letter, verified
    # against the actual file during the architecture investigation).
    write({"b:0001": {"block_type": "Text", "page_id": "page_0", "bbox": [61.2, 79.2, 306.0, 158.4]}})
    result = pa.resolve_anchor(paper_id, "b:0001")
    assert result is not None
    assert result["page"] == 1  # page_id page_0 -> 1-indexed page 1
    assert result["polygon"] == [[0.1, 0.1], [0.5, 0.1], [0.5, 0.2], [0.1, 0.2]]


def test_prefers_polygon_over_bbox_when_both_present(fake_paper):
    paper_id, write = fake_paper
    write({
        "b:0001": {
            "block_type": "Text", "page_id": "page_0",
            "bbox": [0, 0, 612, 792],  # would normalize to the whole page
            "polygon": [[61.2, 79.2], [122.4, 79.2], [122.4, 158.4], [61.2, 158.4]],
        }
    })
    result = pa.resolve_anchor(paper_id, "b:0001")
    assert result["polygon"] == [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2], [0.1, 0.2]]


def test_page_id_is_zero_indexed_converted_to_one_indexed(fake_paper):
    paper_id, write = fake_paper
    write({"b:0050": {"block_type": "SectionHeader", "page_id": "page_4", "bbox": [0, 0, 61.2, 79.2]}})
    result = pa.resolve_anchor(paper_id, "b:0050")
    assert result["page"] == 5


def test_missing_anchor_returns_none_not_error(fake_paper):
    paper_id, write = fake_paper
    write({"b:0001": {"block_type": "Text", "page_id": "page_0", "bbox": [0, 0, 61.2, 79.2]}})
    assert pa.resolve_anchor(paper_id, "b:9999") is None


def test_missing_provenance_file_returns_none(fake_paper):
    paper_id, _write = fake_paper  # never call write() -- file doesn't exist
    assert pa.resolve_anchor(paper_id, "b:0001") is None


def test_missing_pdf_returns_none(tmp_path, monkeypatch):
    paper_id = "no_pdf_paper"
    provenance_file = tmp_path / "provenance.json"
    provenance_file.write_text(json.dumps({"b:0001": {"page_id": "page_0", "bbox": [0, 0, 10, 10]}}))
    monkeypatch.setattr(sage_paths, "provenance_path", lambda pid: provenance_file)
    monkeypatch.setattr(sage_paths, "pdf_path", lambda pid: None)
    pa.clear_cache()
    assert pa.resolve_anchor(paper_id, "b:0001") is None


def test_page_id_out_of_pdf_range_returns_none(fake_paper):
    paper_id, write = fake_paper
    write({"b:0001": {"block_type": "Text", "page_id": "page_9999", "bbox": [0, 0, 10, 10]}})
    assert pa.resolve_anchor(paper_id, "b:0001") is None


def test_malformed_page_id_returns_none(fake_paper):
    paper_id, write = fake_paper
    write({"b:0001": {"block_type": "Text", "page_id": "not-a-page-id", "bbox": [0, 0, 10, 10]}})
    assert pa.resolve_anchor(paper_id, "b:0001") is None


def test_entry_with_no_geometry_returns_none(fake_paper):
    paper_id, write = fake_paper
    write({"b:0001": {"block_type": "Text", "page_id": "page_0"}})  # no bbox, no polygon
    assert pa.resolve_anchor(paper_id, "b:0001") is None


def test_resolve_locators_supports_multiple_locators(fake_paper):
    paper_id, write = fake_paper
    write({
        "b:0001": {"block_type": "Text", "page_id": "page_0", "bbox": [0, 0, 61.2, 79.2]},
        "b:0002": {"block_type": "Text", "page_id": "page_1", "bbox": [0, 0, 61.2, 79.2]},
    })
    source = {"locators": [{"kind": "text", "block_anchor": "b:0001"}, {"kind": "text", "block_anchor": "b:0002"}]}
    resolved = pa.resolve_locators(paper_id, source)
    assert len(resolved) == 2
    assert [r["page"] for r in resolved] == [1, 2]


def test_resolve_locators_omits_unresolvable_without_raising(fake_paper):
    paper_id, write = fake_paper
    write({"b:0001": {"block_type": "Text", "page_id": "page_0", "bbox": [0, 0, 61.2, 79.2]}})
    source = {"locators": [{"block_anchor": "b:0001"}, {"block_anchor": "b:9999"}]}
    resolved = pa.resolve_locators(paper_id, source)
    assert len(resolved) == 1
    assert resolved[0]["block_anchor"] == "b:0001"


def test_resolve_locators_handles_empty_and_none_source():
    assert pa.resolve_locators("anything", None) == []
    assert pa.resolve_locators("anything", {}) == []
    assert pa.resolve_locators("anything", {"locators": []}) == []


def test_bracketed_anchor_is_stripped(fake_paper):
    paper_id, write = fake_paper
    write({"b:0001": {"block_type": "Text", "page_id": "page_0", "bbox": [0, 0, 61.2, 79.2]}})
    assert pa.resolve_anchor(paper_id, "[b:0001]") is not None


# --------------------------------------------------------------------- #
# Real, unmodified pecan artifacts -- the exact proof case from the
# architecture investigation.
# --------------------------------------------------------------------- #


@pytest.mark.skipif(REAL_PECAN_PDF is None, reason=_MISSING_PECAN_REASON)
def test_real_pecan_title_anchor_resolves_to_real_page_two():
    result = pa.resolve_anchor("pecan", "b:0006")
    assert result is not None
    assert result["page"] == 2
    assert all(0.0 <= c <= 1.0 for point in result["polygon"] for c in point)


@pytest.mark.skipif(REAL_PECAN_PDF is None, reason=_MISSING_PECAN_REASON)
def test_real_pecan_llm_reported_page_number_does_not_match_ground_truth():
    # Documents, with real data, exactly why source.page_number must never
    # be used for navigation.
    citation = json.load(open(sage_paths.SRC_DIR / "results" / "pecan" / "Citation.json"))
    title_source = citation["payload"]["title"]["source"]
    assert title_source["page_number"] == 1  # LLM-reported
    resolved = pa.resolve_locators("pecan", title_source)
    assert resolved[0]["page"] == 2  # real, ground-truth page
