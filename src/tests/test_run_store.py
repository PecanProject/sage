"""Tests for pipeline/run_store.py -- the permanent, per-run artifact
capture that pipeline/orchestrator.py writes to before it decides what to
do next. Focused on the persistence contract itself: every write lands at
the documented path and round-trips exactly."""

from pathlib import Path

import pytest

from pipeline import run_store


@pytest.fixture()
def isolated_runs_root(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path))
    return tmp_path


def test_save_and_load_stage_attempt(isolated_runs_root):
    path = run_store.save_stage_attempt("run1", "Citation__pecan", "extraction", 1, {"facts": ["x"]})
    assert path.exists()
    assert path == isolated_runs_root / "run1" / "records" / "Citation__pecan" / "extraction" / "attempt1.json"
    assert run_store.load_json(path) == {"facts": ["x"]}


def test_multiple_attempts_do_not_overwrite_each_other(isolated_runs_root):
    run_store.save_stage_attempt("run1", "Citation__pecan", "conversion", 1, {"n": 1})
    run_store.save_stage_attempt("run1", "Citation__pecan", "conversion", 2, {"n": 2})
    conv_dir = run_store.record_dir("run1", "Citation__pecan") / "conversion"
    assert sorted(p.name for p in conv_dir.iterdir()) == ["attempt1.json", "attempt2.json"]
    assert run_store.load_json(conv_dir / "attempt1.json") == {"n": 1}
    assert run_store.load_json(conv_dir / "attempt2.json") == {"n": 2}


def test_save_final_and_record_manifest(isolated_runs_root):
    run_store.save_final("run1", "Site__site1", {"status": "ready", "payload": {"id": "site1"}})
    run_store.save_record_manifest("run1", "Site__site1", {"status": "ready"})

    final = run_store.load_json(run_store.record_dir("run1", "Site__site1") / "final.json")
    assert final["status"] == "ready"
    manifest = run_store.load_json(run_store.record_dir("run1", "Site__site1") / "record_manifest.json")
    assert manifest["status"] == "ready"


def test_save_and_load_run_manifest(isolated_runs_root):
    run_store.save_run_manifest("run1", {"run_id": "run1", "status": "running"})
    assert run_store.load_run_manifest("run1") == {"run_id": "run1", "status": "running"}


def test_list_records_and_list_runs(isolated_runs_root):
    run_store.save_final("run1", "Site__site1", {"status": "ready"})
    run_store.save_final("run1", "Citation__pecan", {"status": "unresolved"})
    run_store.save_run_manifest("run2", {"run_id": "run2"})

    assert run_store.list_records("run1") == ["Citation__pecan", "Site__site1"]
    assert run_store.list_records("run2") == []  # manifest only, no records yet
    assert run_store.list_runs() == ["run1", "run2"]


def test_list_records_empty_when_run_does_not_exist(isolated_runs_root):
    assert run_store.list_records("no-such-run") == []
