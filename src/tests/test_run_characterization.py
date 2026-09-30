"""Characterization test for the run/result infrastructure changes (run
isolation, run lock, LATEST pointer, config/manifest metadata).

Purpose: those changes are *infrastructure*, so they must not alter the
extraction behavior being evaluated. This test drives `run_paper` through the
same deterministic fake `invoke` used by test_orchestrator's end-to-end tests
and snapshots (a) every per-record artifact under `runs/<run_id>/records/`,
(b) the `records` dict `run_paper` returns, and (c) the results read back
through the layout-agnostic `results_store.load_any_entity_results` API.

The golden snapshot (`fixtures/run_paper_characterization.json`) was captured
from the code BEFORE the infrastructure changes. Only `results/` on-disk paths
(and the manifest, deliberately excluded here) may differ afterwards; nothing
else in the snapshot may change. Run with WRITE_GOLDEN=1 only to (re)capture
against a known-good baseline.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from test_orchestrator import (  # noqa: F401  (env is a pytest fixture)
    PAPER_ID, _build_run_paper_invoke_sequence, env, make_invoke_sequence,
)

from pipeline import orchestrator, results_store, run_store

GOLDEN = Path(__file__).parent / "fixtures" / "run_paper_characterization.json"

_DROP_KEYS = {"ts", "started_at", "finished_at"}


def _normalize(value, run_id: str):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in _DROP_KEYS:
                continue
            if k == "schema_version":
                out[k] = "<SCHEMA>"
                continue
            out[k] = _normalize(v, run_id)
        return out
    if isinstance(value, list):
        return [_normalize(v, run_id) for v in value]
    if isinstance(value, str):
        return value.replace(run_id, "<RUN>")
    return value


def _snapshot_records_tree(run_id: str) -> dict:
    root = run_store.run_dir(run_id) / "records"
    tree = {}
    for path in sorted(root.rglob("*.json")):
        rel = str(path.relative_to(root))
        tree[rel] = _normalize(json.loads(path.read_text(encoding="utf-8")), run_id)
    return tree


def _snapshot_results(run_id: str) -> dict:
    out = {}
    for et in orchestrator.ENTITY_TYPE_TO_PLURAL:
        items = results_store.load_any_entity_results(PAPER_ID, et)
        out[et] = sorted((_normalize(i, run_id) for i in items), key=lambda r: r["record_id"])
    return out


def test_run_paper_infrastructure_changes_do_not_alter_extraction_behavior(env):
    invoke = make_invoke_sequence(_build_run_paper_invoke_sequence())
    outcome = orchestrator.run_paper(
        paper_id=PAPER_ID, model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=False,
    )
    run_id = outcome["run_id"]
    snapshot = {
        "records_tree": _snapshot_records_tree(run_id),
        "outcome_records": _normalize(outcome["records"], run_id),
        "results": _snapshot_results(run_id),
    }
    if os.environ.get("WRITE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(snapshot, indent=1, sort_keys=True), encoding="utf-8")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert json.loads(json.dumps(snapshot, sort_keys=True)) == golden
