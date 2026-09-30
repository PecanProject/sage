"""Phase A (multi-record Variable) focused tests for
`api_client.get_review_data` -- must return both Variable records when
several exist, while every other (single-record) entity type keeps
working exactly as before. Isolated from real data via IR_RESULTS_ROOT
pointed at a tmp_path per test, matching marker_pipeline's own tests.
"""

from __future__ import annotations

import json

import api_client
from pipeline import results_store


def _write_single_result(results_root, paper_id: str, entity_type: str, record_id: str, status: str, payload=None):
    path = results_root / paper_id / f"{entity_type}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "paper_id": paper_id, "entity_type": entity_type, "record_id": record_id,
        "status": status, "payload": payload,
    }))


def _write_variable_result(results_root, paper_id: str, record_id: str, status: str, name_value: str):
    path = results_root / paper_id / "Variable" / f"{record_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"id": record_id, "name": {"value": name_value, "provenance_label": "EXTRACTED", "source": {
        "source_document_id": paper_id, "page_number": 1, "locators": [{"kind": "text", "block_anchor": "b:0001"}],
    }}}
    path.write_text(json.dumps({
        "paper_id": paper_id, "entity_type": "Variable", "record_id": record_id,
        "status": status, "payload": payload,
    }))


def test_get_review_data_returns_both_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path))
    monkeypatch.setenv("IR_CORRECTIONS_ROOT", str(tmp_path / "corrections"))
    _write_variable_result(tmp_path, "pecan", "pecan_variable_lai", "ready", "Leaf area index")
    _write_variable_result(tmp_path, "pecan", "pecan_variable_soc", "ready", "Soil organic carbon")

    data = api_client.get_review_data("pecan")

    assert len(data["Variable"]) == 2
    record_ids = {r["record_id"] for r in data["Variable"]}
    assert record_ids == {"pecan_variable_lai", "pecan_variable_soc"}
    names = {r["fields"]["name"]["effective_value"] for r in data["Variable"]}
    assert names == {"Leaf area index", "Soil organic carbon"}


def test_get_review_data_no_variables_returns_empty_list(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path))
    monkeypatch.setenv("IR_CORRECTIONS_ROOT", str(tmp_path / "corrections"))
    data = api_client.get_review_data("pecan")
    assert data["Variable"] == []


def _set_paper_dirs(monkeypatch, tmp_path):
    # Isolate ALL THREE directories delete_paper_source touches -- leaving
    # any one of them pointed at the real project tree risks deleting real
    # data (confirmed the hard way: an earlier version of this helper only
    # patched PDF_DIR/PAPER_DIR, and a delete_paper() call in a test here
    # deleted the real project's src/docproc/marker_json/pecan/ cache).
    import sage_paths

    pdf_dir = tmp_path / "docproc" / "paper"
    marker_dir = tmp_path / "docproc" / "marker_json"
    paper_dir = tmp_path / "paper"
    pdf_dir.mkdir(parents=True)
    marker_dir.mkdir(parents=True)
    paper_dir.mkdir(parents=True)
    monkeypatch.setattr(sage_paths, "PDF_DIR", pdf_dir)
    monkeypatch.setattr(sage_paths, "MARKER_JSON_DIR", marker_dir)
    monkeypatch.setattr(sage_paths, "PAPER_DIR", paper_dir)
    return pdf_dir, paper_dir


def test_list_papers_distinguishes_processed_from_extracted(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path / "results"))
    monkeypatch.setenv("IR_CORRECTIONS_ROOT", str(tmp_path / "corrections"))
    pdf_dir, paper_dir = _set_paper_dirs(monkeypatch, tmp_path)

    # "uploaded" -- PDF only, never processed.
    (pdf_dir / "uploaded.pdf").write_bytes(b"%PDF-fake")

    # "processed" -- Marker/document prep done, but never extracted.
    (pdf_dir / "processed.pdf").write_bytes(b"%PDF-fake")
    (paper_dir / "processed").mkdir()
    (paper_dir / "processed" / "content.md").write_text("x")
    (paper_dir / "processed" / "provenance.json").write_text("{}")

    # "extracted" -- processed AND has a real committed extraction.
    (pdf_dir / "extracted.pdf").write_bytes(b"%PDF-fake")
    (paper_dir / "extracted").mkdir()
    (paper_dir / "extracted" / "content.md").write_text("x")
    (paper_dir / "extracted" / "provenance.json").write_text("{}")
    _write_single_result(tmp_path / "results", "extracted", "Citation", "extracted", "ready")

    rows = {r["paper_id"]: r for r in api_client.list_papers()}

    assert rows["uploaded"]["processed"] is False
    assert rows["uploaded"]["extracted"] is False

    assert rows["processed"]["processed"] is True
    assert rows["processed"]["extracted"] is False

    assert rows["extracted"]["processed"] is True
    assert rows["extracted"]["extracted"] is True
    assert rows["extracted"]["summary"] is not None


def test_is_extracted_true_despite_unrelated_stray_errors(tmp_path, monkeypatch):
    # Regression: a real run (Oceologia-1998) with 89 of 92 records
    # ready/unresolved and only 2 unrelated stray errors (a different
    # Species candidate, a different Method candidate) must still count as
    # extracted/reviewable -- it must NOT be hidden from the Extracted
    # Papers list just because something unrelated also errored.
    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path / "results"))
    _write_single_result(tmp_path / "results", "mixed", "Citation", "mixed", "ready")
    _write_single_result(tmp_path / "results", "mixed", "Method", "mixed_method_bad", "error")

    assert api_client.is_extracted("mixed") is True


def test_is_extracted_false_when_nothing_ever_reached_ready_or_unresolved(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path / "results"))
    _write_single_result(tmp_path / "results", "all_errors", "Citation", "all_errors", "error")

    assert api_client.is_extracted("all_errors") is False


def test_is_extracted_false_when_nothing_extracted_at_all(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path / "results"))
    assert api_client.is_extracted("never_touched") is False


def test_delete_paper_removes_from_library(tmp_path, monkeypatch):
    pdf_dir, paper_dir = _set_paper_dirs(monkeypatch, tmp_path)
    (pdf_dir / "pecan.pdf").write_bytes(b"%PDF-fake")

    removed = api_client.delete_paper("pecan")

    assert not (pdf_dir / "pecan.pdf").exists()
    assert removed == [str(pdf_dir / "pecan.pdf")]


def test_delete_extracted_records_clears_results_but_keeps_ir_store(tmp_path, monkeypatch):
    from pipeline import results_store, store

    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path / "results"))
    monkeypatch.setenv("IR_STORE_ROOT", str(tmp_path / "ir-store"))
    _write_single_result(tmp_path / "results", "pecan", "Citation", "pecan", "ready")
    store.append_record(
        paper_id="pecan", entity_type="Citation", record_id="pecan", status="ready",
        payload={"id": "pecan"}, extra={"run_id": "run1"},
    )

    assert api_client.is_extracted("pecan") is True

    removed = api_client.delete_extracted_records("pecan")

    assert removed == [str(results_store.paper_dir("pecan"))]
    assert not results_store.paper_dir("pecan").exists()
    assert api_client.is_extracted("pecan") is False
    # ir-store's own commit ledger is never touched by this action.
    assert len(store.read_all("pecan")) == 1


def test_delete_extracted_records_is_a_noop_when_nothing_extracted(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path / "results"))
    assert api_client.delete_extracted_records("never_extracted") == []


def test_list_extracted_papers_independent_of_stored_pdf_presence(tmp_path, monkeypatch):
    # Regression for a real, confirmed bug: Extracted Papers used to be
    # derived from list_papers() (PDF presence), so deleting a paper's PDF
    # made it vanish from Extracted Papers too, even with real results
    # still on disk.
    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path / "results"))
    _set_paper_dirs(monkeypatch, tmp_path)  # empty PDF_DIR -- no stored PDFs at all
    _write_single_result(tmp_path / "results", "pecan", "Citation", "pecan", "ready")

    rows = api_client.list_extracted_papers()

    assert len(rows) == 1
    assert rows[0]["paper_id"] == "pecan"
    assert rows[0]["has_pdf"] is False  # confirmed: no PDF, still listed


def test_list_extracted_papers_omits_results_dirs_with_nothing_ready_or_unresolved(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path / "results"))
    _write_single_result(tmp_path / "results", "all_errors", "Citation", "all_errors", "error")
    assert api_client.list_extracted_papers() == []


def test_rename_extracted_paper_moves_results_and_ir_store(tmp_path, monkeypatch):
    from pipeline import results_store, store

    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path / "results"))
    monkeypatch.setenv("IR_STORE_ROOT", str(tmp_path / "ir-store"))
    _write_single_result(tmp_path / "results", "pecan", "Citation", "pecan", "ready")
    store.append_record(
        paper_id="pecan", entity_type="Citation", record_id="pecan", status="ready",
        payload={"id": "pecan"}, extra={"run_id": "run1"},
    )

    ok, message = api_client.rename_extracted_paper("pecan", "Daren-1997-Canopy")

    assert ok is True
    assert not results_store.paper_dir("pecan").exists()
    assert results_store.paper_dir("Daren-1997-Canopy").exists()
    assert len(store.read_all("Daren-1997-Canopy")) == 1
    assert store.read_all("pecan") == []


def test_rename_extracted_paper_refuses_when_destination_taken(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path / "results"))
    _write_single_result(tmp_path / "results", "pecan", "Citation", "pecan", "ready")
    _write_single_result(tmp_path / "results", "other_paper", "Citation", "other_paper", "ready")

    ok, message = api_client.rename_extracted_paper("pecan", "other_paper")

    assert ok is False
    assert "already exist" in message


def test_rename_stored_paper_delegates_to_sage_paths(tmp_path, monkeypatch):
    pdf_dir, paper_dir = _set_paper_dirs(monkeypatch, tmp_path)
    (pdf_dir / "pecan.pdf").write_bytes(b"%PDF-fake")

    ok, message = api_client.rename_stored_paper("pecan", "Daren-1997-Canopy")

    assert ok is True
    assert (pdf_dir / "Daren-1997-Canopy.pdf").exists()


def test_get_review_data_existing_single_record_entities_unchanged(tmp_path, monkeypatch):
    # Citation (and every other single-record entity type) must keep
    # returning a 0-or-1-length list, read from the OLD flat-file
    # convention, completely unaffected by Variable's new storage shape.
    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path))
    monkeypatch.setenv("IR_CORRECTIONS_ROOT", str(tmp_path / "corrections"))
    _write_single_result(tmp_path, "pecan", "Citation", "pecan", "ready", payload={
        "id": "pecan",
        "author": {"value": "A. Author", "provenance_label": "EXTRACTED", "source": {
            "source_document_id": "pecan", "page_number": 1, "locators": [{"kind": "text", "block_anchor": "b:0001"}],
        }},
    })

    data = api_client.get_review_data("pecan")

    assert len(data["Citation"]) == 1
    assert data["Citation"][0]["record_id"] == "pecan"
    assert data["Citation"][0]["fields"]["author"]["effective_value"] == "A. Author"
    # Untouched: results_store.load_entity_result (the single-file API)
    # still reads exactly what was written, independent of Variable's
    # directory-based convention.
    assert results_store.load_entity_result("pecan", "Citation")["record_id"] == "pecan"


def test_get_review_data_single_record_entity_not_extracted_is_empty_list(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path))
    monkeypatch.setenv("IR_CORRECTIONS_ROOT", str(tmp_path / "corrections"))
    data = api_client.get_review_data("pecan")
    assert data["Citation"] == []
