"""Run isolation: run-scoped results + LATEST pointer, the per-paper run lock,
ir_service attempt-counter isolation, run-filtered ir-store views, and
run-scoped corrections.

Why this exists (real, observed): a second Extract click started a second
`run_paper` on the same paper; both wrote into the same `ir-store` and a
shared last-writer-wins `results/<paper>/` snapshot, and three different run
ids were found mixed inside one snapshot. These tests pin the fix. The
extraction behavior itself is pinned separately by
test_run_characterization.py.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from test_orchestrator import (  # noqa: F401  (env is a pytest fixture)
    PAPER_ID, _build_run_paper_invoke_sequence, env, make_invoke_sequence,
)

from pipeline import corrections_store, orchestrator, results_store, run_lock, run_store, store


# --------------------------------------------------------------------- #
# results_store: run scoping + LATEST
# --------------------------------------------------------------------- #


@pytest.fixture()
def rs_root(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path))
    return tmp_path


def _rec(record_id, run_id, status="ready"):
    return {"record_id": record_id, "run_id": run_id, "status": status}


def test_two_runs_write_to_distinct_directories_and_never_clobber(rs_root):
    results_store.save_multi_entity_results("p", "Treatment", [_rec("t1", "runA")], run_id="runA")
    results_store.save_multi_entity_results("p", "Treatment", [_rec("t2", "runB")], run_id="runB")
    assert [r["record_id"] for r in results_store.load_multi_entity_results("p", "Treatment", "runA")] == ["t1"]
    assert [r["record_id"] for r in results_store.load_multi_entity_results("p", "Treatment", "runB")] == ["t2"]
    # B's "clear the directory first" rewrite touched only B's own directory.
    results_store.save_multi_entity_results("p", "Treatment", [], run_id="runB")
    assert [r["record_id"] for r in results_store.load_multi_entity_results("p", "Treatment", "runA")] == ["t1"]


def test_readers_without_run_id_follow_latest_only(rs_root):
    results_store.save_entity_result("p", "Citation", {"v": "A"}, run_id="runA")
    results_store.save_entity_result("p", "Citation", {"v": "B"}, run_id="runB")
    assert results_store.latest_run_id("p") is None
    results_store.set_latest("p", "runA")
    assert results_store.load_entity_result("p", "Citation") == {"v": "A"}
    results_store.set_latest("p", "runB")
    assert results_store.load_entity_result("p", "Citation") == {"v": "B"}
    # an explicit run_id always wins over LATEST
    assert results_store.load_entity_result("p", "Citation", run_id="runA") == {"v": "A"}


def test_legacy_flat_layout_is_still_readable_and_writable_without_run_id(rs_root):
    results_store.save_entity_result("p", "Site", {"legacy": True})
    results_store.save_multi_entity_results("p", "Variable", [_rec("v1", None)])
    assert results_store.load_entity_result("p", "Site") == {"legacy": True}
    assert [r["record_id"] for r in results_store.load_any_entity_results("p", "Variable")] == ["v1"]
    assert (rs_root / "p" / "Site.json").is_file()  # flat, exactly as before


def test_latest_pointer_is_written_atomically_and_leaves_no_temp_file(rs_root):
    results_store.set_latest("p", "runA")
    assert sorted(x.name for x in (rs_root / "p").iterdir()) == ["LATEST"]
    assert (rs_root / "p" / "LATEST").read_text() == "runA"


def test_list_run_ids_only_lists_directories_marked_as_runs(rs_root):
    results_store.mark_run_results("p", "runA")
    results_store.save_multi_entity_results("p", "Treatment", [_rec("t", "runB")], run_id="runB")  # no marker
    (rs_root / "p" / "Treatment").mkdir(parents=True)  # a legacy entity dir must never look like a run
    assert results_store.list_run_ids("p") == ["runA"]


# --------------------------------------------------------------------- #
# run_lock
# --------------------------------------------------------------------- #


@pytest.fixture()
def runs_root(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path))
    return tmp_path


def test_lock_refuses_a_second_live_holder(runs_root):
    run_lock.acquire("p", "run1")
    with pytest.raises(run_lock.RunAlreadyActive) as exc:
        run_lock.acquire("p", "run2")
    assert exc.value.holder["run_id"] == "run1"
    assert "already active" in str(exc.value)


def test_lock_for_a_different_paper_is_independent(runs_root):
    run_lock.acquire("p1", "run1")
    run_lock.acquire("p2", "run2")  # must not raise


def test_stale_lock_from_a_dead_process_is_replaced(runs_root):
    path = run_lock.lock_path("p")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"paper_id": "p", "run_id": "dead", "pid": 2 ** 22 + 12345, "started_at": 0}))
    assert run_lock.active_holder("p") is None
    run_lock.acquire("p", "run-new")
    assert run_lock.read_lock("p")["run_id"] == "run-new"


def test_release_never_frees_another_runs_lock(runs_root):
    run_lock.acquire("p", "run1")
    run_lock.release("p", "someone-else")
    assert run_lock.read_lock("p")["run_id"] == "run1"
    run_lock.release("p", "run1")
    assert run_lock.read_lock("p") is None


def test_context_manager_releases_on_exception(runs_root):
    with pytest.raises(ValueError):
        with run_lock.paper_run_lock("p", "run1"):
            raise ValueError("boom")
    assert run_lock.read_lock("p") is None


def test_lock_directory_is_not_reported_as_a_run(runs_root):
    run_lock.acquire("p", "run1")
    run_store.save_run_manifest("run1", {"run_id": "run1"})
    assert run_store.list_runs() == ["run1"]


# --------------------------------------------------------------------- #
# run_paper: lock, manifest lifecycle, LATEST only on completion
# --------------------------------------------------------------------- #


def test_run_paper_manifest_lifecycle_and_latest_moves_only_on_completion(env):
    invoke = make_invoke_sequence(_build_run_paper_invoke_sequence())
    outcome = orchestrator.run_paper(
        paper_id=PAPER_ID, model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=False,
    )
    run_id = outcome["run_id"]
    manifest = run_store.load_run_manifest(run_id)
    assert manifest["run_status"] == "completed"
    # started_at is captured at the START (it used to be stamped twice at the end)
    assert manifest["started_at"] <= manifest["finished_at"]
    assert results_store.latest_run_id(PAPER_ID) == run_id
    assert run_lock.read_lock(PAPER_ID) is None  # released


def test_a_crashed_run_records_failure_keeps_previous_latest_and_releases_the_lock(env):
    good = orchestrator.run_paper(
        paper_id=PAPER_ID, model="test-model", client=env["client"],
        invoke=make_invoke_sequence(_build_run_paper_invoke_sequence()), enable_ai_validation=False,
    )

    def exploding_invoke(agent, model, prompt, timeout=300):
        raise RuntimeError("provider exploded")

    with pytest.raises(RuntimeError, match="provider exploded"):
        orchestrator.run_paper(
            paper_id=PAPER_ID, model="test-model", client=env["client"], invoke=exploding_invoke,
            enable_ai_validation=False, run_id="crashed_run",
        )
    manifest = run_store.load_run_manifest("crashed_run")
    assert manifest["run_status"] == "failed" and "provider exploded" in manifest["error"]
    assert results_store.latest_run_id(PAPER_ID) == good["run_id"]  # reviewers keep seeing the last GOOD run
    assert run_lock.read_lock(PAPER_ID) is None


def test_second_concurrent_run_on_the_same_paper_is_refused_without_side_effects(env):
    started, release = threading.Event(), threading.Event()
    first_result: dict = {}

    def blocking_invoke(agent, model, prompt, timeout=300):
        started.set()
        release.wait(timeout=10)
        raise RuntimeError("stop first run")

    def first():
        try:
            orchestrator.run_paper(
                paper_id=PAPER_ID, model="test-model", client=env["client"], invoke=blocking_invoke,
                enable_ai_validation=False, run_id="first_run",
            )
        except RuntimeError as exc:
            first_result["error"] = str(exc)

    t = threading.Thread(target=first)
    t.start()
    assert started.wait(timeout=10)
    try:
        with pytest.raises(run_lock.RunAlreadyActive):
            orchestrator.run_paper(
                paper_id=PAPER_ID, model="test-model", client=env["client"],
                invoke=make_invoke_sequence([]), enable_ai_validation=False, run_id="second_run",
            )
        # the refused run left nothing behind: no manifest, no results dir
        assert "second_run" not in run_store.list_runs()
        assert "second_run" not in results_store.list_run_ids(PAPER_ID)
    finally:
        release.set()
        t.join(timeout=10)
    assert first_result["error"] == "stop first run"


# --------------------------------------------------------------------- #
# ir_service attempt counters
# --------------------------------------------------------------------- #


def test_attempt_budget_is_isolated_per_run(env):
    client = env["client"]
    bad = {"paper_id": PAPER_ID, "entity_type": "Citation", "record_id": "same_id", "payload": {}}
    for _ in range(4):
        r = client.propose_record(**bad, run_id="runA")
        assert r["valid"] is False and not r.get("forced_flag_unresolved")
    capped = client.propose_record(**bad, run_id="runA")
    assert capped.get("forced_flag_unresolved") is True
    # a different run proposing the SAME record id still has its full budget
    other = client.propose_record(**bad, run_id="runB")
    assert not other.get("forced_flag_unresolved") and other["attempts_remaining"] == 3


def test_attempt_budget_without_run_id_is_unchanged(env):
    client = env["client"]
    bad = {"paper_id": PAPER_ID, "entity_type": "Citation", "record_id": "legacy_id", "payload": {}}
    for _ in range(4):
        client.propose_record(**bad)
    assert client.propose_record(**bad).get("forced_flag_unresolved") is True


# --------------------------------------------------------------------- #
# ir-store views and corrections
# --------------------------------------------------------------------- #


def test_dataset_and_finalize_can_be_restricted_to_one_run(env):
    store.append_record(PAPER_ID, "Site", "site_old", "ready", {"id": "site_old"}, extra={"run_id": "runA"})
    store.append_record(PAPER_ID, "Site", "site_new", "ready", {"id": "site_new"}, extra={"run_id": "runB"})
    all_runs, _ = orchestrator.build_dataset_from_store(PAPER_ID)
    only_a, _ = orchestrator.build_dataset_from_store(PAPER_ID, run_id="runA")
    assert {s["id"] for s in all_runs["sites"]} == {"site_old", "site_new"}  # default unchanged: every run
    assert {s["id"] for s in only_a["sites"]} == {"site_old"}
    summary = orchestrator.finalize_paper(PAPER_ID, run_id="runB")
    assert [r["record_id"] for r in summary["entities"]["Site"]["ready"] + summary["entities"]["Site"]["unresolved"]] == ["site_new"]


def test_corrections_are_scoped_to_the_run_they_were_made_in(tmp_path):
    root = tmp_path
    corrections_store.append_correction("p", "Treatment", "t1", "approve", field_name="name", root=root, run_id="runA")
    corrections_store.append_correction("p", "Treatment", "t1", "note", field_name="name", root=root)  # legacy: no run_id
    assert len(corrections_store.read_for_record("p", "Treatment", "t1", "name", root=root)) == 2  # no filter: unchanged
    in_b = corrections_store.read_for_record("p", "Treatment", "t1", "name", root=root, run_id="runB")
    assert [e["action"] for e in in_b] == ["note"]  # A's approval does not leak into B; legacy entries still apply
    latest_b = corrections_store.latest_action_for_field("p", "Treatment", "t1", "name", root=root, run_id="runB")
    assert latest_b["action"] == "note"
    latest_a = corrections_store.latest_action_for_field("p", "Treatment", "t1", "name", root=root, run_id="runA")
    assert latest_a["action"] == "note"  # the later legacy entry wins by order, unchanged "latest wins" rule
