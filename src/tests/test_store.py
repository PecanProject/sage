"""Focused tests for pipeline.store.rename_paper -- the one new function on
this module. store.py otherwise has no dedicated test file; its append/read
behavior is exercised indirectly through test_orchestrator.py's real
run_record()/ir_service integration tests.
"""

from __future__ import annotations

from pipeline import store


def test_rename_paper_moves_the_jsonl_file_without_rewriting_content(tmp_path):
    root = tmp_path / "ir-store"
    store.append_record(
        paper_id="pecan", entity_type="Citation", record_id="pecan", status="ready",
        payload={"id": "pecan"}, root=root,
    )

    moved = store.rename_paper("pecan", "Daren-1997-Canopy", root=root)

    assert moved is True
    assert not (root / "pecan.jsonl").exists()
    assert (root / "Daren-1997-Canopy.jsonl").exists()
    entries = store.read_all("Daren-1997-Canopy", root=root)
    assert len(entries) == 1
    # Content is untouched -- the embedded paper_id still says the OLD id,
    # by design (append-only: never rewrite a line, only the file's name
    # changes; nothing in the pipeline reads this field back out anyway).
    assert entries[0]["paper_id"] == "pecan"


def test_rename_paper_is_a_noop_when_nothing_to_rename(tmp_path):
    root = tmp_path / "ir-store"
    assert store.rename_paper("never_existed", "new_name", root=root) is False


def test_has_paper_reflects_real_file_presence(tmp_path):
    root = tmp_path / "ir-store"
    assert store.has_paper("pecan", root=root) is False
    store.append_record(
        paper_id="pecan", entity_type="Citation", record_id="pecan", status="ready",
        payload={"id": "pecan"}, root=root,
    )
    assert store.has_paper("pecan", root=root) is True
