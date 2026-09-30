"""Item 15: readiness for UNRESOLVED values (plan section 8).

Evidence (stored runs): 206 Observations were committed as `ready`; 81 of them have an UNRESOLVED `value` (36 of 108 in
run 20260919T040723_06df825d) and 8 an UNRESOLVED `variable_name` -- a "ready" measurement with no measurement or no
name -- while 186 have an UNRESOLVED `temporal_info`, which protocol Section 10.1 allows ("if no reliable date window can
be recovered, leave the date fields blank and explain why"). The service accepted all of them: structural validity was the
only gate.

Item 15: `validators.readiness_issues` (Observation: `value` and `variable_name` must be resolved; `temporal_info` need not
be); `propose_record` reports `ready` and the issues (validity and attempts unchanged); `commit_record(status="ready")`
rejects such a record with a 422; the orchestrator commits it as `unresolved` with the payload kept and spends no
AI-validation call on it. The service's fingerprint now also covers `ir_service.py` and `reconstruction.py`.

Fixtures: tests/fixtures/item15/ (real stored payloads). Tests marked SYNTHETIC use invented payloads.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from pipeline import fingerprint, orchestrator, run_store, validators
from test_ir_service import client  # noqa: F401  (pytest fixture)
from test_orchestrator import RAW_EXTRACTION, PAPER_ID, _inv, env as ir_env, make_invoke_sequence  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures" / "item15"


def _real() -> dict[str, list[dict]]:
    return json.loads((FIXTURES / "real_ready_observations.json").read_text())


def _codes(issues) -> list[str]:
    return sorted(i.code for i in issues)


UNRESOLVED = {"value": None, "provenance_label": "UNRESOLVED", "unresolved_reason": "no numeric value is reported"}
RESOLVED_VALUE = {"value": {"reported_text": "12", "reported_units": "g"}, "provenance_label": "EXTRACTED"}
RESOLVED_NAME = {"value": "yield", "provenance_label": "EXTRACTED"}


def _obs(**overrides) -> dict:
    payload = {"id": "o", "value": RESOLVED_VALUE, "variable_name": RESOLVED_NAME,
               "temporal_info": {"value": None, "provenance_label": "UNRESOLVED", "unresolved_reason": "no date"}}
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------- #
# validators.readiness_issues
# --------------------------------------------------------------------- #


def test_real_stored_observations_with_an_unresolved_value_or_name_are_not_ready():
    for record in _real()["value_unresolved"]:
        issues = validators.readiness_issues("Observation", record["payload"], record["record"])
        assert "observation_value_unresolved" in _codes(issues), record["record"]
    for record in _real()["variable_name_unresolved"]:
        issues = validators.readiness_issues("Observation", record["payload"])
        assert "observation_variable_name_unresolved" in _codes(issues), record["record"]


def test_real_stored_observations_that_are_only_undated_stay_ready():
    """Protocol Section 10.1: an unresolved `temporal_info` does not make an Observation unusable."""
    for record in _real()["only_temporal_unresolved"]:
        assert record["payload"]["temporal_info"]["provenance_label"] == "UNRESOLVED"
        assert validators.readiness_issues("Observation", record["payload"]) == [], record["record"]
    for record in _real()["fully_resolved"]:
        assert validators.readiness_issues("Observation", record["payload"]) == []


def test_the_issue_quotes_the_extractions_own_reason_and_names_the_record():
    record = _real()["value_unresolved"][0]
    [issue] = [i for i in validators.readiness_issues("Observation", record["payload"], "obs_1") if i.code == "observation_value_unresolved"]
    assert issue.severity == "error" and issue.entity_type == "Observation" and issue.entity_id == "obs_1"
    assert record["payload"]["value"]["unresolved_reason"] in issue.message and "kept as unresolved" in issue.message


def test_synthetic_both_core_fields_unresolved_gives_two_issues_and_a_resolved_record_none():
    both = _obs(value=UNRESOLVED, variable_name={**UNRESOLVED, "unresolved_reason": "name missing"})
    assert _codes(validators.readiness_issues("Observation", both)) == ["observation_value_unresolved", "observation_variable_name_unresolved"]
    assert validators.readiness_issues("Observation", _obs()) == []


@pytest.mark.parametrize("bad", [None, {}, "12", {"value": None, "provenance_label": "EXTRACTED"}, {"value": None}])
def test_synthetic_an_absent_null_or_valueless_core_field_is_not_ready(bad):
    assert "observation_value_unresolved" in _codes(validators.readiness_issues("Observation", _obs(value=bad)))
    assert validators.readiness_issues("Observation", {k: v for k, v in _obs().items() if k != "value"})  # absent


def test_synthetic_the_unresolved_label_alone_makes_a_field_unresolved_even_with_a_stray_value():
    """Construction rejects an UNRESOLVED field that carries a value, but readiness must not depend on that."""
    stray = {"value": "yield", "provenance_label": "UNRESOLVED", "unresolved_reason": "r"}
    assert _codes(validators.readiness_issues("Observation", _obs(variable_name=stray))) == ["observation_variable_name_unresolved"]


def test_synthetic_the_readiness_scope_is_observation_plus_the_demonstrated_core_identities():
    """Scope after correction-pass Fix 7 (was: Observation only). Identity fields only; nothing else is ever required."""
    assert validators.READINESS_REQUIRED_FIELDS == {"Observation": ("value", "variable_name"), "Variable": ("name",), "Method": ("name",)}
    assert validators.READINESS_ANY_OF_FIELDS == {"Crop": (("cultivar", "common_name"),)}
    # no evidence yet for these, so they are still never blocked by readiness
    for entity in ("Site", "Treatment", "Management", "Citation", "Species", "Coverage", "Study", "TreatmentPair"):
        assert validators.readiness_issues(entity, {"name": UNRESOLVED, "value": UNRESOLVED}) == []


# --------------------------------------------------------------------- #
# the service: propose reports readiness, commit enforces it
# --------------------------------------------------------------------- #


@pytest.fixture()
def svc(client, monkeypatch):
    """The real service endpoints; construction and provenance are stubbed so only readiness is exercised."""
    c, _ = client
    module = sys.modules["ir_service"]
    monkeypatch.setattr(module, "_construction_errors", lambda entity_type, payload: [])
    monkeypatch.setattr(module, "validate_provenance", lambda paper_id, payload: [])
    return c, module


def _propose(c, payload, **kw):
    return c.post("/propose_record", json={"paper_id": "paperA", "entity_type": "Observation", "record_id": "o1", "payload": payload, **kw}).json()


def _commit(c, payload, status, entity="Observation"):
    return c.post("/commit_record", json={"paper_id": "paperA", "entity_type": entity, "record_id": "o1", "payload": payload, "status": status})


def test_propose_reports_not_ready_without_spending_an_attempt_or_calling_it_invalid(svc):
    c, _ = svc
    result = _propose(c, _obs(value=UNRESOLVED))
    assert result["valid"] is True and result["ready"] is False and result["attempts_used"] == 0 and result["errors"] == []
    assert [i["code"] for i in result["readiness_issues"]] == ["observation_value_unresolved"]
    ready = _propose(c, _obs())
    assert ready["valid"] is True and ready["ready"] is True and ready["readiness_issues"] == []


def test_propose_of_an_invalid_payload_is_not_ready_and_computes_no_readiness(svc, monkeypatch):
    c, module = svc
    monkeypatch.setattr(module, "_construction_errors", lambda entity_type, payload: [{"field": "id", "message": "bad"}])
    result = _propose(c, _obs(value=UNRESOLVED))
    assert result["valid"] is False and result["ready"] is False and result["readiness_issues"] == [] and result["attempts_used"] == 1


def test_commit_as_ready_is_rejected_with_a_422_naming_the_unresolved_field(svc):
    c, _ = svc
    r = _commit(c, _obs(value=UNRESOLVED), "ready")
    assert r.status_code == 422
    assert [e["code"] for e in r.json()["detail"]["errors"]] == ["observation_value_unresolved"]
    r = _commit(c, _obs(variable_name=UNRESOLVED), "ready")
    assert r.status_code == 422 and r.json()["detail"]["errors"][0]["code"] == "observation_variable_name_unresolved"


def test_the_same_record_commits_as_unresolved_with_its_payload_kept(svc):
    c, module = svc
    payload = _obs(value=UNRESOLVED)
    r = _commit(c, payload, "unresolved")
    assert r.status_code == 200 and r.json()["committed"] is True
    assert r.json()["entry"]["status"] == "unresolved" and r.json()["entry"]["payload"] == payload


def test_a_resolved_or_only_undated_observation_still_commits_as_ready_and_other_entities_are_unaffected(svc):
    c, _ = svc
    assert _commit(c, _obs(), "ready").status_code == 200          # temporal_info UNRESOLVED in _obs()
    assert _commit(c, {"id": "s", "name": UNRESOLVED}, "ready", entity="Site").status_code == 200


def test_a_rejected_ready_commit_stores_nothing(svc):
    import os
    c, _ = svc
    store_file = Path(os.environ["IR_STORE_ROOT"]) / "paperA.jsonl"
    assert _commit(c, _obs(value=UNRESOLVED), "ready").status_code == 422
    assert not store_file.exists() or "o1" not in store_file.read_text()
    assert _commit(c, _obs(value=UNRESOLVED), "unresolved").status_code == 200      # ... while the unresolved commit is stored
    assert "o1" in store_file.read_text()


# --------------------------------------------------------------------- #
# the orchestrator
# --------------------------------------------------------------------- #


class StubClient:
    """Records every call; propose_record answers with the given readiness."""

    def __init__(self, propose_result):
        self.propose_result, self.commits, self.flags = propose_result, [], []

    def propose_record(self, **kw):
        return dict(self.propose_result)

    def commit_record(self, **kw):
        self.commits.append(kw)
        return {"committed": True, "entry": {"status": kw["status"]}}

    def flag_unresolved(self, **kw):
        self.flags.append(kw)
        return {"recorded": True}


NOT_READY = {"valid": True, "ready": False, "attempts_used": 0, "errors": [],
             "readiness_issues": [{"severity": "error", "code": "observation_value_unresolved", "message": "Observation is not ready: `value` is UNRESOLVED (no numeric value is reported)"}]}
READY = {"valid": True, "ready": True, "readiness_issues": [], "errors": []}


def _run_record(ir_env, client, *, ai=True, agents=None):
    payload = _obs(value=UNRESOLVED)
    seq = [("extractor", _inv("extractor", RAW_EXTRACTION)), ("converter", _inv("converter", payload))]
    seq += agents if agents is not None else ([("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []}))] if ai else [])
    return orchestrator.run_record(run_id="run1", paper_id=PAPER_ID, entity_type="Observation", record_id="o1", model="m", client=client,
                                   invoke=make_invoke_sequence(seq), enable_ai_validation=ai), payload


def test_a_not_ready_record_is_committed_as_unresolved_with_its_payload_and_no_ai_call_is_spent(ir_env):
    stub = StubClient(NOT_READY)
    result, payload = _run_record(ir_env, stub, ai=True, agents=[])   # no ir-validator scripted: calling it would raise
    assert result.status == "unresolved"
    [commit] = stub.commits
    assert commit["status"] == "unresolved" and commit["payload"] == payload and stub.flags == []
    assert commit["run_metadata"]["readiness_issues"][0]["code"] == "observation_value_unresolved"
    detail = result.detail
    assert detail["payload"] == payload and detail["last_candidate_payload"] == payload
    assert detail["last_errors"] == [{"field": "observation_value_unresolved", "message": NOT_READY["readiness_issues"][0]["message"]}]
    manifest = run_store.load_json(run_store.record_dir("run1", "Observation__o1") / "record_manifest.json")
    assert manifest["status"] == "unresolved" and manifest["not_ready"] == ["observation_value_unresolved"]


def test_the_not_ready_result_reads_back_through_the_results_layout_with_payload_and_reason(ir_env):
    result, payload = _run_record(ir_env, StubClient(NOT_READY), agents=[])
    entry = orchestrator._entity_result_file(PAPER_ID, "Observation", "run1", "o1", {"status": result.status, "detail": result.detail})
    assert entry["status"] == "unresolved" and entry["payload"] == payload
    assert "is not ready" in entry["reason"][0]["message"]


def test_a_ready_record_is_committed_as_ready_after_the_ai_validator(ir_env):
    stub = StubClient(READY)
    result, _ = _run_record(ir_env, stub, ai=True)
    assert result.status == "ready" and [c["status"] for c in stub.commits] == ["ready"]


def test_a_response_from_an_older_service_without_a_ready_key_is_treated_as_ready(ir_env):
    old = {"valid": True, "errors": [], "warnings": []}
    stub = StubClient(old)
    result, _ = _run_record(ir_env, stub, ai=True)
    assert result.status == "ready" and stub.commits[0]["status"] == "ready"


def test_end_to_end_against_the_real_service_an_unresolved_value_is_stored_unresolved(ir_env, monkeypatch):
    """Real propose/commit endpoints (construction and provenance stubbed): the record ends `unresolved` in the store with its payload."""
    module = sys.modules["ir_service"]
    monkeypatch.setattr(module, "_construction_errors", lambda entity_type, payload: [])
    monkeypatch.setattr(module, "validate_provenance", lambda paper_id, payload: [])
    result, payload = _run_record(ir_env, ir_env["client"], agents=[])
    assert result.status == "unresolved"
    stored = [json.loads(line) for line in (ir_env["store_root"] / f"{PAPER_ID}.jsonl").read_text().splitlines()]
    entry = [e for e in stored if e["record_id"] == "o1"][-1]
    assert entry["status"] == "unresolved" and entry["payload"] == payload


# --------------------------------------------------------------------- #
# the staleness fingerprint
# --------------------------------------------------------------------- #


def test_the_fingerprint_covers_the_service_and_the_reconstruction_tools(tmp_path, monkeypatch):
    assert set(fingerprint._SCHEMA_FILES) == {"ir_schema.py", "validators.py", "ir_service.py", "reconstruction.py"}
    for name in fingerprint._SCHEMA_FILES:
        (tmp_path / name).write_text(f"# {name}\n")
    monkeypatch.setattr(fingerprint, "PIPELINE_DIR", tmp_path)
    base = fingerprint.schema_fingerprint()
    for name in fingerprint._SCHEMA_FILES:
        (tmp_path / name).write_text(f"# {name}\n# changed\n")
        assert fingerprint.schema_fingerprint() != base, name   # any one of the four files makes a stale service detectable
        (tmp_path / name).write_text(f"# {name}\n")
    assert fingerprint.schema_fingerprint() == base


# --------------------------------------------------------------------- #
# Correction pass, Fix 7: core identity (Variable.name, Method.name, Crop cultivar-or-common_name)
#
# Real hollow "ready" records: Daren Variable leaf_blade_dry_weight (name, description, units and notes ALL UNRESOLVED, each with
# the spurious reason "page number not available"), Daren Crop Trailblazer (cultivar UNRESOLVED, no common name), Felipe
# Variable plant_nitrogen_content (null name) and Felipe Method disease_scoring (null name). Optional metadata is untouched:
# a Variable with no units, no description and no notes is still ready. Fixtures: tests/fixtures/pass2/core_identity_records.json.
# --------------------------------------------------------------------- #


def _core() -> dict:
    return json.loads((Path(__file__).parent / "fixtures" / "pass2" / "core_identity_records.json").read_text())


@pytest.mark.parametrize("key, entity, code", [
    ("daren_variable_leaf_blade_dry_weight", "Variable", "variable_name_unresolved"),
    ("felipe_variable_plant_nitrogen_content", "Variable", "variable_name_unresolved"),
    ("felipe_method_disease_scoring", "Method", "method_name_unresolved"),
    ("daren_crop_trailblazer", "Crop", "crop_identity_unresolved"),
])
def test_the_real_hollow_records_are_not_ready(key, entity, code):
    issues = validators.readiness_issues(entity, _core()[key], key)
    assert _codes(issues) == [code] and issues[0].entity_id == key and "kept as unresolved" in issues[0].message


@pytest.mark.parametrize("key, entity", [
    ("daren_variable_leaf_mean_tilt_angle", "Variable"),    # description UNRESOLVED (optional metadata)
    ("daren_variable_leaf_blade_width", "Variable"),        # units UNRESOLVED
    ("felipe_variable_canopy_light", "Variable"),           # units absent
    ("felipe_method_canopy_par", "Method"),
    ("daren_crop_cave_in_rock", "Crop"),
])
def test_real_records_with_a_resolved_identity_stay_ready_even_with_unresolved_optional_metadata(key, entity):
    record = _core()[key]
    assert validators.readiness_issues(entity, record) == []
    if key == "daren_variable_leaf_mean_tilt_angle":
        assert record["description"]["provenance_label"] == "UNRESOLVED"          # the optional field really is unresolved


def test_a_missing_optional_note_never_makes_a_record_unready():
    payload = {"id": "v", "name": {"value": "x", "provenance_label": "EXTRACTED"}, "description": UNRESOLVED, "units": UNRESOLVED, "notes": UNRESOLVED}
    assert validators.readiness_issues("Variable", payload) == []


def test_the_reason_the_extraction_gave_is_quoted_for_a_variable():
    record = _core()["daren_variable_leaf_blade_dry_weight"]
    [issue] = validators.readiness_issues("Variable", record)
    assert record["name"]["unresolved_reason"] in issue.message


def test_synthetic_a_crop_needs_only_one_of_cultivar_or_common_name():
    named = {"value": "Trailblazer", "provenance_label": "EXTRACTED"}
    assert validators.readiness_issues("Crop", {"cultivar": UNRESOLVED, "common_name": named}) == []
    assert validators.readiness_issues("Crop", {"cultivar": named}) == []
    assert _codes(validators.readiness_issues("Crop", {"cultivar": UNRESOLVED, "common_name": UNRESOLVED})) == ["crop_identity_unresolved"]
    assert _codes(validators.readiness_issues("Crop", {})) == ["crop_identity_unresolved"]


def test_the_service_holds_a_hollow_variable_back_from_ready_and_accepts_it_as_unresolved(svc):
    c, _ = svc
    hollow = _core()["daren_variable_leaf_blade_dry_weight"]
    proposed = c.post("/propose_record", json={"paper_id": "paperA", "entity_type": "Variable", "record_id": "v1", "payload": hollow}).json()
    assert proposed["valid"] is True and proposed["ready"] is False
    assert [i["code"] for i in proposed["readiness_issues"]] == ["variable_name_unresolved"]
    assert _commit(c, hollow, "ready", entity="Variable").status_code == 422
    assert _commit(c, hollow, "unresolved", entity="Variable").status_code == 200        # the payload is kept
