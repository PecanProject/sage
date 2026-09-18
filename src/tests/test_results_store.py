"""Tests for pipeline/results_store.py -- the unified per-paper finalized
result written by orchestrator.finalize_paper."""

from pathlib import Path

import pytest

from pipeline import results_store


@pytest.fixture()
def isolated_results_root(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path))
    return tmp_path


def test_save_and_load_result_round_trip(isolated_results_root):
    data = {"paper_id": "pecan", "entities": {}}
    path = results_store.save_result("pecan", data)
    assert path == isolated_results_root / "pecan" / "result.json"
    assert path.exists()
    assert results_store.load_result("pecan") == data


def test_save_result_overwrites_previous(isolated_results_root):
    results_store.save_result("pecan", {"v": 1})
    results_store.save_result("pecan", {"v": 2})
    assert results_store.load_result("pecan") == {"v": 2}


def test_separate_papers_do_not_collide(isolated_results_root):
    results_store.save_result("pecan", {"paper_id": "pecan"})
    results_store.save_result("culti-mixtures", {"paper_id": "culti-mixtures"})
    assert results_store.load_result("pecan")["paper_id"] == "pecan"
    assert results_store.load_result("culti-mixtures")["paper_id"] == "culti-mixtures"


def test_save_and_load_entity_result_round_trip(isolated_results_root):
    path = results_store.save_entity_result("pecan", "Citation", {"entity_type": "Citation", "status": "ready"})
    assert path == isolated_results_root / "pecan" / "Citation.json"
    assert results_store.load_entity_result("pecan", "Citation") == {"entity_type": "Citation", "status": "ready"}


def test_entity_result_files_coexist_with_combined_result_file(isolated_results_root):
    # finalize's single result.json and run_paper's per-entity files must
    # not collide -- different filenames in the same paper directory.
    results_store.save_result("pecan", {"combined": True})
    results_store.save_entity_result("pecan", "Site", {"entity_type": "Site"})
    assert results_store.load_result("pecan") == {"combined": True}
    assert results_store.load_entity_result("pecan", "Site") == {"entity_type": "Site"}


def test_entity_result_files_are_independent_per_type(isolated_results_root):
    results_store.save_entity_result("pecan", "Citation", {"v": 1})
    results_store.save_entity_result("pecan", "Site", {"v": 2})
    assert results_store.load_entity_result("pecan", "Citation") == {"v": 1}
    assert results_store.load_entity_result("pecan", "Site") == {"v": 2}


# --------------------------------------------------------------------- #
# Phase A: multi-record storage (Variable only) --
# results/<paper_id>/<EntityType>/<record_id>.json
# --------------------------------------------------------------------- #


def test_variable_is_registered_as_multi_record():
    assert "Variable" in results_store.MULTI_RECORD_ENTITY_TYPES


def test_save_and_load_multi_entity_results_round_trip(isolated_results_root):
    records = [
        {"entity_type": "Variable", "record_id": "pecan_variable_lai", "status": "ready"},
        {"entity_type": "Variable", "record_id": "pecan_variable_soc", "status": "ready"},
    ]
    paths = results_store.save_multi_entity_results("pecan", "Variable", records)
    assert len(paths) == 2
    assert all(p.parent == isolated_results_root / "pecan" / "Variable" for p in paths)

    loaded = results_store.load_multi_entity_results("pecan", "Variable")
    assert {r["record_id"] for r in loaded} == {"pecan_variable_lai", "pecan_variable_soc"}


def test_multi_entity_results_cannot_silently_overwrite_siblings(isolated_results_root):
    # Each record gets its OWN file (named by record_id) -- saving one must
    # never touch another's file.
    results_store.save_multi_entity_results("pecan", "Variable", [
        {"entity_type": "Variable", "record_id": "pecan_variable_lai", "status": "ready", "payload": {"v": 1}},
    ])
    results_store.save_multi_entity_results("pecan", "Variable", [
        {"entity_type": "Variable", "record_id": "pecan_variable_lai", "status": "ready", "payload": {"v": 1}},
        {"entity_type": "Variable", "record_id": "pecan_variable_soc", "status": "ready", "payload": {"v": 2}},
    ])
    loaded = {r["record_id"]: r for r in results_store.load_multi_entity_results("pecan", "Variable")}
    assert loaded["pecan_variable_lai"]["payload"] == {"v": 1}
    assert loaded["pecan_variable_soc"]["payload"] == {"v": 2}


def test_save_multi_entity_results_clears_stale_records_from_a_prior_run(isolated_results_root):
    # A run that finds FEWER real candidates than a previous run must not
    # leave a stale, no-longer-real record_id file behind (same "built ONLY
    # from what this call itself produced" principle as the single-record
    # path).
    results_store.save_multi_entity_results("pecan", "Variable", [
        {"entity_type": "Variable", "record_id": "pecan_variable_lai", "status": "ready"},
        {"entity_type": "Variable", "record_id": "pecan_variable_soc", "status": "ready"},
    ])
    results_store.save_multi_entity_results("pecan", "Variable", [
        {"entity_type": "Variable", "record_id": "pecan_variable_lai", "status": "ready"},
    ])
    loaded = results_store.load_multi_entity_results("pecan", "Variable")
    assert [r["record_id"] for r in loaded] == ["pecan_variable_lai"]


def test_load_multi_entity_results_empty_when_never_saved(isolated_results_root):
    assert results_store.load_multi_entity_results("pecan", "Variable") == []


def test_load_any_entity_results_dispatches_correctly(isolated_results_root):
    # Single-record type: 0 or 1 entries, same as load_entity_result.
    assert results_store.load_any_entity_results("pecan", "Citation") == []
    results_store.save_entity_result("pecan", "Citation", {"entity_type": "Citation", "record_id": "pecan", "status": "ready"})
    assert results_store.load_any_entity_results("pecan", "Citation") == [
        {"entity_type": "Citation", "record_id": "pecan", "status": "ready"}
    ]

    # Multi-record type: however many are actually stored.
    results_store.save_multi_entity_results("pecan", "Variable", [
        {"entity_type": "Variable", "record_id": "pecan_variable_lai", "status": "ready"},
        {"entity_type": "Variable", "record_id": "pecan_variable_soc", "status": "unresolved"},
    ])
    loaded = results_store.load_any_entity_results("pecan", "Variable")
    assert len(loaded) == 2


def test_multi_record_directory_does_not_collide_with_single_record_file(isolated_results_root):
    # Different path shapes (Variable.json vs Variable/) -- both existing
    # data conventions must be able to coexist without confusion if a
    # caller mistakenly used the old API for a multi-record type, though
    # normal use never mixes them for the same entity_type.
    results_store.save_entity_result("pecan", "OtherLegacy", {"v": "single"})
    results_store.save_multi_entity_results("pecan", "Variable", [
        {"entity_type": "Variable", "record_id": "pecan_variable_lai", "status": "ready"},
    ])
    assert results_store.load_entity_result("pecan", "OtherLegacy") == {"v": "single"}
    assert len(results_store.load_multi_entity_results("pecan", "Variable")) == 1


def test_list_paper_ids_returns_every_paper_with_a_results_dir(isolated_results_root):
    assert results_store.list_paper_ids() == []
    results_store.save_entity_result("pecan", "Citation", {"entity_type": "Citation", "record_id": "pecan", "status": "ready"})
    results_store.save_entity_result("Oceologia-1998", "Citation", {"entity_type": "Citation", "record_id": "Oceologia-1998", "status": "ready"})
    assert results_store.list_paper_ids() == ["Oceologia-1998", "pecan"]


def test_rename_paper_dir_moves_the_whole_snapshot(isolated_results_root):
    results_store.save_entity_result("pecan", "Citation", {"entity_type": "Citation", "record_id": "pecan", "status": "ready"})
    results_store.save_multi_entity_results("pecan", "Variable", [
        {"entity_type": "Variable", "record_id": "pecan_variable_lai", "status": "ready"},
    ])

    moved = results_store.rename_paper_dir("pecan", "Daren-1997-Canopy")

    assert moved is True
    assert results_store.list_paper_ids() == ["Daren-1997-Canopy"]
    assert results_store.load_entity_result("Daren-1997-Canopy", "Citation") == {
        "entity_type": "Citation", "record_id": "pecan", "status": "ready",
    }
    assert len(results_store.load_multi_entity_results("Daren-1997-Canopy", "Variable")) == 1


def test_rename_paper_dir_is_a_noop_when_nothing_to_rename(isolated_results_root):
    assert results_store.rename_paper_dir("never_existed", "new_name") is False
