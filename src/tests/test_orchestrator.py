import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline import orchestrator, run_store
from pipeline import results_store as results_store_module

PIPELINE_DIR = Path(__file__).resolve().parent.parent / "pipeline"

PAPER_ID = "orch_test_paper"


def write_test_paper(papers_root: Path, paper_id: str = PAPER_ID) -> Path:
    pdir = papers_root / paper_id
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "content.md").write_text(
        "A. Author\n⟦b:0001⟧\n\nPublished in 2012.\n⟦b:0002⟧\n\nA Title\n⟦b:0003⟧\n\n"
        "Reported as a treatment_mean summary statistic.\n⟦b:0004⟧\n",
        encoding="utf-8",
    )
    return pdir


@pytest.fixture()
def env(tmp_path, monkeypatch):
    store_root = tmp_path / "ir-store"
    papers_root = tmp_path / "papers"
    runs_root = tmp_path / "runs"
    results_root = tmp_path / "results"
    papers_root.mkdir()

    monkeypatch.setenv("IR_STORE_ROOT", str(store_root))
    monkeypatch.setenv("IR_PAPERS_ROOT", str(papers_root))
    monkeypatch.setenv("IR_RUNS_ROOT", str(runs_root))
    monkeypatch.setenv("IR_RESULTS_ROOT", str(results_root))

    for mod in ("ir_service", "store", "content_reader", "validators", "fingerprint"):
        sys.modules.pop(mod, None)
    sys.path.insert(0, str(PIPELINE_DIR))
    import ir_service as svc

    svc._attempt_counts.clear()
    write_test_paper(papers_root)

    client = orchestrator.IRServiceClient(TestClient(svc.app))
    return {
        "client": client, "store_root": store_root, "papers_root": papers_root,
        "runs_root": runs_root, "results_root": results_root,
    }


def _inv(agent: str, parsed_json: dict) -> orchestrator.AgentInvocation:
    return orchestrator.AgentInvocation(
        agent=agent, model="test-model", prompt="prompt",
        returncode=0, stdout="{}", stderr="",
        final_text=json.dumps(parsed_json), parsed_json=parsed_json, parse_error=None,
    )


def _inv_parse_error(agent: str) -> orchestrator.AgentInvocation:
    return orchestrator.AgentInvocation(
        agent=agent, model="test-model", prompt="prompt",
        returncode=0, stdout="not json at all", stderr="",
        final_text="not json at all", parsed_json=None, parse_error="no valid JSON object found in agent output",
    )


def make_invoke_sequence(items):
    it = iter(items)

    def _invoke(agent, model, prompt, timeout=300):
        try:
            expected_agent, invocation = next(it)
        except StopIteration:
            raise AssertionError(f"invoke_agent called more times than expected (extra call for agent={agent!r})")
        assert agent == expected_agent, f"expected next call to agent {expected_agent!r}, got {agent!r}"
        return invocation

    return _invoke


RAW_EXTRACTION = {
    "paper_id": PAPER_ID, "entity_type": "Citation", "record_id": PAPER_ID,
    "facts": [
        {"field_name": "author", "raw_value": "A. Author", "raw_text_excerpt": "A. Author", "anchors": ["b:0001"]},
        {"field_name": "year", "raw_value": "2012", "raw_text_excerpt": "Published in 2012.", "anchors": ["b:0002"]},
        {"field_name": "title", "raw_value": "A Title", "raw_text_excerpt": "A Title", "anchors": ["b:0003"]},
    ],
}


def _src(anchor):
    return {"source_document_id": PAPER_ID, "page_number": 1, "section_path": [], "locators": [{"kind": "text", "block_anchor": anchor}]}


def valid_citation_payload():
    return {
        "id": PAPER_ID,
        "author": {"value": "A. Author", "provenance_label": "EXTRACTED", "source": _src("b:0001")},
        "year": {"value": 2012, "provenance_label": "EXTRACTED", "source": _src("b:0002")},
        "title": {"value": "A Title", "provenance_label": "EXTRACTED", "source": _src("b:0003")},
        "persistent_identifier": {
            "value": None, "provenance_label": "UNRESOLVED",
            "unresolved_reason": "No DOI appears in the visible content.", "source": _src("b:0003"),
        },
    }


def invalid_citation_payload_missing_pid():
    payload = valid_citation_payload()
    payload["persistent_identifier"] = None  # bare null -- the exact historical failure mode
    return payload



def test_happy_path_commits_ready(env):
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", valid_citation_payload())),
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
    ])

    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )

    assert result.status == "ready"

    store_file = env["store_root"] / f"{PAPER_ID}.jsonl"
    assert store_file.exists()
    entry = json.loads(store_file.read_text().strip().splitlines()[-1])
    assert entry["status"] == "ready"
    assert entry["run_id"] == "run1"
    assert "schema_version" in entry
    assert entry["ai_validation"]["verdict"] == "plausible"

    record_key = "Citation__" + PAPER_ID
    assert run_store.list_records("run1") == [record_key]
    final = run_store.load_json(run_store.record_dir("run1", record_key) / "final.json")
    assert final["status"] == "ready"


def test_ai_validation_can_be_disabled(env):
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", valid_citation_payload())),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=False,
    )
    assert result.status == "ready"
    assert result.detail["ai_validation"] is None


def test_ai_validation_plausible_verdict_continues_normally(env):
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", valid_citation_payload())),
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "ready"
    assert result.detail["ai_validation"]["verdict"] == "plausible"
    record_key = "Citation__" + PAPER_ID
    manifest = run_store.load_json(run_store.record_dir("run1", record_key) / "record_manifest.json")
    assert manifest["attempts"]["ai_validation"] == 1
    assert manifest["attempts"]["conversion"] == 1  # no correction call made


def test_ai_validation_suspicious_triggers_exactly_one_correction_then_commits(env):
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", valid_citation_payload())),
        ("ir-validator", _inv("ir-validator", {
            "verdict": "suspicious",
            "issues": [{"field": "year", "concern": "looks like a page number, not a year"}],
        })),
        ("converter", _inv("converter", valid_citation_payload())),  # the one bounded correction
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),  # re-checked, now clean
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "ready"
    assert result.detail["ai_validation"]["verdict"] == "plausible"
    record_key = "Citation__" + PAPER_ID
    manifest = run_store.load_json(run_store.record_dir("run1", record_key) / "record_manifest.json")
    assert manifest["attempts"]["ai_validation"] == 2
    assert manifest["attempts"]["conversion"] == 2  # original + the one correction
    val_dir = run_store.record_dir("run1", record_key) / "ai_validation"
    assert sorted(p.name for p in val_dir.iterdir()) == ["attempt1.json", "attempt2.json"]


def test_ai_validation_still_suspicious_after_correction_is_unresolved_not_committed(env):
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", valid_citation_payload())),
        ("ir-validator", _inv("ir-validator", {
            "verdict": "suspicious", "issues": [{"field": "year", "concern": "implausible"}],
        })),
        ("converter", _inv("converter", valid_citation_payload())),  # the one bounded correction
        ("ir-validator", _inv("ir-validator", {
            "verdict": "suspicious", "issues": [{"field": "year", "concern": "still implausible"}],
        })),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "unresolved"
    assert result.detail["flag_result"]["recorded"] is True
    store_file = env["store_root"] / f"{PAPER_ID}.jsonl"
    entries = [json.loads(l) for l in store_file.read_text().strip().splitlines()]
    assert entries[-1]["status"] == "unresolved"


def test_ai_validation_correction_failing_deterministic_validation_falls_back_to_last_known_good(env):
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", valid_citation_payload())),
        ("ir-validator", _inv("ir-validator", {
            "verdict": "suspicious", "issues": [{"field": "persistent_identifier", "concern": "check this"}],
        })),
        ("converter", _inv("converter", invalid_citation_payload_missing_pid())),  # correction fails validation
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "ready"
    # The committed payload is the ORIGINAL valid one, not the failed
    # correction attempt (which had a null persistent_identifier).
    assert result.detail["payload"]["persistent_identifier"] == valid_citation_payload()["persistent_identifier"]
    assert result.detail["ai_validation"]["verdict"] == "suspicious"

    # The rejected correction attempt itself is still fully on disk for audit.
    record_key = "Citation__" + PAPER_ID
    conv_dir = run_store.record_dir("run1", record_key) / "conversion"
    assert sorted(p.name for p in conv_dir.iterdir()) == ["attempt1.json", "attempt2.json"]
    val_dir = run_store.record_dir("run1", record_key) / "conversion_validation"
    rejected_attempt = run_store.load_json(val_dir / "attempt2.json")
    assert rejected_attempt["valid"] is False

    # No infinite loop: exactly one AI Validator call, one correction attempt.
    manifest = run_store.load_json(run_store.record_dir("run1", record_key) / "record_manifest.json")
    assert manifest["attempts"]["ai_validation"] == 1
    assert manifest["attempts"]["conversion"] == 2


def test_ai_validation_correction_succeeding_deterministic_validation_commits_corrected_payload(env):
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", valid_citation_payload())),
        ("ir-validator", _inv("ir-validator", {
            "verdict": "suspicious", "issues": [{"field": "year", "concern": "looks like a page number"}],
        })),
        ("converter", _inv("converter", valid_citation_payload())),  # correction: still valid
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),  # re-checked, now clean
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "ready"
    assert result.detail["ai_validation"]["verdict"] == "plausible"
    record_key = "Citation__" + PAPER_ID
    manifest = run_store.load_json(run_store.record_dir("run1", record_key) / "record_manifest.json")
    assert manifest["attempts"]["ai_validation"] == 2
    assert manifest["attempts"]["conversion"] == 2


def test_ai_validation_correction_failure_never_produces_unresolved_for_a_valid_payload(env):
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", valid_citation_payload())),
        ("ir-validator", _inv("ir-validator", {
            "verdict": "suspicious", "issues": [{"field": "author", "concern": "footnote marker?"}],
        })),
        ("converter", _inv("converter", invalid_citation_payload_missing_pid())),
    ])
    orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    store_file = env["store_root"] / f"{PAPER_ID}.jsonl"
    entries = [json.loads(l) for l in store_file.read_text().strip().splitlines()]
    assert entries[-1]["status"] == "ready"


def test_normal_invalid_payload_without_ai_validation_involvement_still_flags_unresolved(env):
    invoke = make_invoke_sequence(
        [("extractor", _inv("extractor", RAW_EXTRACTION))]
        + [("converter", _inv("converter", invalid_citation_payload_missing_pid()))] * 6
    )
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "unresolved"
    assert result.detail["flag_result"]["recorded"] is True


def test_ai_validation_correction_is_bounded_no_infinite_loop(env):
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", valid_citation_payload())),
        ("ir-validator", _inv("ir-validator", {"verdict": "suspicious", "issues": []})),
        ("converter", _inv("converter", valid_citation_payload())),
        ("ir-validator", _inv("ir-validator", {"verdict": "suspicious", "issues": []})),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "unresolved"  # exhausted the one correction, still suspicious


# --------------------------------------------------------------------- #
# Bounded correction loop
# --------------------------------------------------------------------- #


def test_conversion_retries_after_deterministic_validation_error_then_succeeds(env):
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", invalid_citation_payload_missing_pid())),  # attempt 1: fails
        ("converter", _inv("converter", valid_citation_payload())),  # attempt 2: fixed
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "ready"

    record_key = "Citation__" + PAPER_ID
    conv_dir = run_store.record_dir("run1", record_key) / "conversion"
    assert sorted(p.name for p in conv_dir.iterdir()) == ["attempt1.json", "attempt2.json"]
    val_dir = run_store.record_dir("run1", record_key) / "conversion_validation"
    attempt1 = run_store.load_json(val_dir / "attempt1.json")
    assert attempt1["valid"] is False
    attempt2 = run_store.load_json(val_dir / "attempt2.json")
    assert attempt2["valid"] is True


def test_conversion_exhausting_attempts_flags_unresolved(env):
    invoke = make_invoke_sequence(
        [("extractor", _inv("extractor", RAW_EXTRACTION))]
        + [("converter", _inv("converter", invalid_citation_payload_missing_pid()))] * 6
    )
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "unresolved"
    assert result.detail["flag_result"]["recorded"] is True

    store_file = env["store_root"] / f"{PAPER_ID}.jsonl"
    entries = [json.loads(l) for l in store_file.read_text().strip().splitlines()]
    assert entries[-1]["status"] == "unresolved"
    assert entries[-1]["payload"]["blocks_examined"]  # orchestrator, not the model, authored this evidence list


def test_conversion_stops_before_wasting_a_call_once_server_turn_cap_exhausted(env):
    def alternating_invalid_payload(n: int) -> dict:
        payload = valid_citation_payload()
        if n % 2 == 1:
            payload["persistent_identifier"] = None  # construction error A
        else:
            payload["year"] = ["not", "an", "int"]  # construction error B
        return payload

    invoke = make_invoke_sequence(
        [("extractor", _inv("extractor", RAW_EXTRACTION))]
        + [("converter", _inv("converter", alternating_invalid_payload(n))) for n in range(1, 5)]
    )
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "unresolved"

    record_key = "Citation__" + PAPER_ID
    conv_dir = run_store.record_dir("run1", record_key) / "conversion"
    assert sorted(p.name for p in conv_dir.iterdir()) == [f"attempt{i}.json" for i in range(1, 5)]


def test_conversion_stops_on_stagnation_when_error_repeats_unchanged(env):
    invoke = make_invoke_sequence(
        [("extractor", _inv("extractor", RAW_EXTRACTION))]
        + [("converter", _inv("converter", invalid_citation_payload_missing_pid()))] * 4
    )
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "unresolved"

    record_key = "Citation__" + PAPER_ID
    conv_dir = run_store.record_dir("run1", record_key) / "conversion"
    assert sorted(p.name for p in conv_dir.iterdir()) == ["attempt1.json", "attempt2.json"]


def test_conversion_stagnation_check_does_not_confuse_different_fields_failing_the_same_way(env):
    def payload_with_bad(field: str) -> dict:
        payload = valid_citation_payload()
        payload[field] = dict(payload[field])
        payload[field]["value"] = "this text is not in content.md anywhere"
        return payload

    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", payload_with_bad("author"))),  # attempt 1: author mismatch
        ("converter", _inv("converter", payload_with_bad("title"))),  # attempt 2: DIFFERENT field mismatch
        ("converter", _inv("converter", valid_citation_payload())),  # attempt 3: fixed
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "ready"

    record_key = "Citation__" + PAPER_ID
    conv_dir = run_store.record_dir("run1", record_key) / "conversion"
    # Must have reached attempt 3 -- stagnation must NOT fire for two
    # different fields that both happen to fail with the same error code.
    assert sorted(p.name for p in conv_dir.iterdir()) == ["attempt1.json", "attempt2.json", "attempt3.json"]


def test_conversion_prompt_directs_provenance_mismatch_toward_anchor_first():
    mismatch_errors = [{
        "severity": "error", "code": "provenance_value_mismatch",
        "message": "name: value='NH4 uptake rate' is not supported by block b:0051.",
    }]
    prompt = orchestrator._conversion_prompt(
        "no_such_paper", "Variable", "rec1", {"facts": []}, mismatch_errors, known_refs=None,
    )
    assert "CITED ANCHOR" in prompt
    assert "different anchor" in prompt.lower()
    assert "reword" in prompt.lower()
    assert "CANDIDATE_ANCHOR_TEXTS" in prompt


def test_conversion_prompt_provenance_mismatch_fetches_real_anchor_text(env):
    mismatch_errors = [{
        "severity": "error", "code": "provenance_value_mismatch",
        "message": "author: value='A. Author' is not supported by block b:0002.",
    }]
    prompt = orchestrator._conversion_prompt(
        PAPER_ID, "Citation", PAPER_ID, RAW_EXTRACTION, mismatch_errors, known_refs=None,
    )
    assert "A. Author" in prompt
    assert "Published in 2012." in prompt
    assert "A Title" in prompt
    # Never fetch or list an anchor that wasn't already in RAW_EVIDENCE.
    assert "b:0099" not in prompt


def test_conversion_prompt_omits_anchor_guidance_for_unrelated_errors():
    other_errors = [{"severity": "error", "code": "missing_required_field", "message": "id is required"}]
    prompt = orchestrator._conversion_prompt(
        "no_such_paper", "Variable", "rec1", {"facts": []}, other_errors, known_refs=None,
    )
    assert "CITED ANCHOR" not in prompt
    assert "CANDIDATE_ANCHOR_TEXTS" not in prompt


def test_conversion_prompt_directs_missing_inference_basis_toward_reason_or_unresolved():
    errors = [{
        "field": "is_raw_replicate_level",
        "message": "Value error, provenance_label=INFERRED requires unresolved_reason populated as an inference-basis note",
    }]
    prompt = orchestrator._conversion_prompt(
        "no_such_paper", "Observation", "rec1", {"facts": []}, errors, known_refs=None,
    )
    assert "unresolved_reason" in prompt
    assert "UNRESOLVED" in prompt


def test_conversion_prompt_directs_aggregation_contract_toward_the_actual_rule():
    errors = [{
        "field": "",
        "message": "Value error, reported_effect_scope=treatment_mean requires aggregated_over_factors to be "
                   "present as EXTRACTED with an empty list (not applicable != absent)",
    }]
    prompt = orchestrator._conversion_prompt(
        "no_such_paper", "Observation", "rec1", {"facts": []}, errors, known_refs=None,
    )
    assert "aggregated_over_factors" in prompt
    assert "treatment_mean" in prompt
    assert "aggregated_mean" in prompt


def test_conversion_prompt_with_no_prior_errors_is_unchanged():
    # Existing valid (first-attempt, no-retry) behavior must be unaffected.
    prompt = orchestrator._conversion_prompt("no_such_paper", "Citation", "rec1", {"facts": []}, [], known_refs=None)
    assert "previous attempt" not in prompt.lower()
    assert "CITED ANCHOR" not in prompt


def test_enumeration_prompt_includes_identity_guidance_for_generalized_entities():
    for entity_type, expected_snippet in [
        ("Site", "physical location"),
        ("Species", "taxonomic identity"),
        ("Method", "measurement/analytical procedure"),
        ("Crop", "cultivar/variety"),
        ("Management", "one row per event"),
        ("Study", "real-world experiment"),
        ("TreatmentPair", "EXPLICIT, NAMED comparison"),
        ("Coverage", "data-availability rollup"),
        ("Treatment", "EACH NAMED LEVEL"),
        ("Observation", "EACH broken-out"),
    ]:
        prompt = orchestrator._enumeration_prompt("no_such_paper", entity_type)
        assert expected_snippet in prompt, f"{entity_type}: missing {expected_snippet!r}"


def test_enumeration_prompt_omits_identity_guidance_for_variable_only():
    prompt = orchestrator._enumeration_prompt("no_such_paper", "Variable")
    for guidance in orchestrator._ENTITY_IDENTITY_GUIDANCE.values():
        assert guidance not in prompt


def test_enumeration_prompt_treatment_guidance_addresses_the_confirmed_co2_merge():
    prompt = orchestrator._enumeration_prompt("no_such_paper", "Treatment")
    assert "ambient" in prompt.lower() and "elevated" in prompt.lower()


def test_enumeration_prompt_observation_guidance_addresses_the_confirmed_grid_collapse():
    prompt = orchestrator._enumeration_prompt("no_such_paper", "Observation")
    assert "for each population and maturity" in prompt or "for each system" in prompt
    assert "Mean" in prompt or "cross-group summary" in prompt


def test_conversion_parse_error_counts_as_failed_attempt_and_retries(env):
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv_parse_error("converter")),  # garbled output -- must not crash the loop
        ("converter", _inv("converter", valid_citation_payload())),
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "ready"



def test_extraction_never_producing_facts_ends_as_error_not_silent(env):
    invoke = make_invoke_sequence([
        ("extractor", _inv_parse_error("extractor")),
        ("extractor", _inv("extractor", {"paper_id": PAPER_ID, "entity_type": "Citation", "record_id": PAPER_ID, "facts": []})),
        ("extractor", _inv("extractor", {"paper_id": PAPER_ID, "entity_type": "Citation", "record_id": PAPER_ID, "facts": []})),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "error"

    record_key = "Citation__" + PAPER_ID
    final = run_store.load_json(run_store.record_dir("run1", record_key) / "final.json")
    assert final["status"] == "error"
    assert "message" in final

    # No propose_record/commit_record call should ever have been made.
    store_file = env["store_root"] / f"{PAPER_ID}.jsonl"
    assert not store_file.exists()


def test_extraction_recovers_from_one_lost_attempt_to_a_provider_crash(env):
    bad_extraction = {
        "paper_id": PAPER_ID, "entity_type": "Citation", "record_id": PAPER_ID,
        "facts": [{"field_name": "title", "raw_value": "A Title", "raw_text_excerpt": "A Title", "anchors": []}],
    }
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", bad_extraction)),  # attempt 1: real shape failure
        ("extractor", _inv_parse_error("extractor")),  # attempt 2: provider-level crash, no text at all
        ("extractor", _inv("extractor", RAW_EXTRACTION)),  # attempt 3: only reachable with the raised cap
        ("converter", _inv("converter", valid_citation_payload())),
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "ready"
    record_key = "Citation__" + PAPER_ID
    record_manifest = run_store.load_json(run_store.record_dir("run1", record_key) / "record_manifest.json")
    assert record_manifest["attempts"]["extraction"] == 3



def test_extractor_prompt_file_requires_anchor_for_null_facts():
    prompt_path = PIPELINE_DIR.parent / "opencode-config" / "agents" / "extractor.md"
    text = prompt_path.read_text(encoding="utf-8")
    assert "must cite at least one real anchor" in text.lower()
    assert '"raw_value": null' in text  # the worked example demonstrates the rule, not just states it
    # the null-valued worked-example fact must carry a non-empty anchors list
    assert '"anchors": ["b:0011"]' in text


def test_converter_prompt_file_documents_table_locator_table_id():
    prompt_path = PIPELINE_DIR.parent / "opencode-config" / "agents" / "converter.md"
    text = prompt_path.read_text(encoding="utf-8")
    assert "table_id" in text
    assert "kind" in text.lower() and "table" in text.lower()
    # documents that table_id and block_anchor take the SAME value
    assert "same" in text.lower()


def test_converter_prompt_file_requires_verbatim_use_of_known_species_id():
    prompt_path = PIPELINE_DIR.parent / "opencode-config" / "agents" / "converter.md"
    text = prompt_path.read_text(encoding="utf-8")
    assert "species_id" in text
    assert "verbatim" in text.lower()


# --------------------------------------------------------------------- #
# JSON extraction / parsing helpers
# --------------------------------------------------------------------- #


def test_extract_final_text_takes_last_message_text_parts():
    stdout = "\n".join([
        json.dumps({"part": {"type": "tool", "tool": "read_section"}}),
        json.dumps({"part": {"type": "text", "messageID": "m1", "text": "ignored earlier message"}}),
        json.dumps({"part": {"type": "text", "messageID": "m2", "text": "```json\n"}}),
        json.dumps({"part": {"type": "text", "messageID": "m2", "text": '{"a": 1}\n```'}}),
    ])
    text = orchestrator._extract_final_text(stdout)
    assert text == '```json\n{"a": 1}\n```'
    parsed, err = orchestrator._parse_json_block(text)
    assert parsed == {"a": 1}
    assert err is None


def test_parse_json_block_reports_error_on_garbage():
    parsed, err = orchestrator._parse_json_block("not json, sorry")
    assert parsed is None
    assert err


def _empty_invocation(agent="extractor") -> orchestrator.AgentInvocation:
    return orchestrator.AgentInvocation(
        agent=agent, model="test-model", prompt="p", returncode=0, stdout="{}", stderr="",
        final_text=None, parsed_json=None, parse_error="no final assistant text found in agent output",
    )


def _real_invocation(agent="extractor") -> orchestrator.AgentInvocation:
    return orchestrator.AgentInvocation(
        agent=agent, model="test-model", prompt="p", returncode=0, stdout="{}", stderr="",
        final_text='{"a": 1}', parsed_json={"a": 1}, parse_error=None,
    )


def test_invoke_agent_retries_internally_on_empty_response_then_succeeds(monkeypatch):
    calls = []
    sleeps = []
    responses = [_empty_invocation(), _empty_invocation(), _real_invocation()]

    def fake_once(agent, model, prompt, timeout):
        calls.append(1)
        return responses[len(calls) - 1]

    monkeypatch.setattr(orchestrator, "_invoke_agent_once", fake_once)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda s: sleeps.append(s))

    result = orchestrator.invoke_agent("extractor", "test-model", "p")

    assert len(calls) == 3  # 1 real MAX_EXTRACTION_ATTEMPTS slot, 2 internal empty-response retries
    assert result.final_text == '{"a": 1}'
    assert len(sleeps) == 2  # backoff before each internal retry, not after the final success


def test_invoke_agent_gives_up_after_exhausting_empty_response_retries(monkeypatch):
    calls = []

    def fake_once(agent, model, prompt, timeout):
        calls.append(1)
        return _empty_invocation()

    monkeypatch.setattr(orchestrator, "_invoke_agent_once", fake_once)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda s: None)

    result = orchestrator.invoke_agent("extractor", "test-model", "p")

    # 1 real call + MAX_EMPTY_RESPONSE_RETRIES extra -- never more, never fewer.
    assert len(calls) == 1 + orchestrator.MAX_EMPTY_RESPONSE_RETRIES
    assert result.final_text is None
    assert result.parse_error == "no final assistant text found in agent output"


def test_invoke_agent_never_retries_a_real_but_invalid_response(monkeypatch):
    calls = []

    def fake_once(agent, model, prompt, timeout):
        calls.append(1)
        return orchestrator.AgentInvocation(
            agent=agent, model=model, prompt=prompt, returncode=0, stdout="{}", stderr="",
            final_text="not valid json", parsed_json=None, parse_error="no valid JSON object found in agent output",
        )

    monkeypatch.setattr(orchestrator, "_invoke_agent_once", fake_once)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("must not sleep/retry")))

    result = orchestrator.invoke_agent("extractor", "test-model", "p")

    assert len(calls) == 1
    assert result.parsed_json is None
    assert result.parse_error == "no valid JSON object found in agent output"


def test_invoke_agent_never_retries_a_missing_executable(monkeypatch):
    calls = []

    def fake_once(agent, model, prompt, timeout):
        calls.append(1)
        return orchestrator.AgentInvocation(
            agent=agent, model=model, prompt=prompt, returncode=-1, stdout="", stderr="",
            final_text=None, parsed_json=None, parse_error="opencode executable not found: [Errno 2] ...",
        )

    monkeypatch.setattr(orchestrator, "_invoke_agent_once", fake_once)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("must not sleep/retry")))

    result = orchestrator.invoke_agent("extractor", "test-model", "p")

    assert len(calls) == 1
    assert "opencode executable not found" in result.parse_error


def test_extraction_all_empty_response_failures_tagged_provider_empty_response(env):
    invoke = make_invoke_sequence([
        ("extractor", _empty_invocation()),
        ("extractor", _empty_invocation()),
        ("extractor", _empty_invocation()),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "error"
    assert result.detail["failure_class"] == "provider_empty_response"


def test_extraction_mixed_failure_is_not_tagged_provider_empty_response(env):
    invoke = make_invoke_sequence([
        ("extractor", _empty_invocation()),
        ("extractor", _inv("extractor", {"paper_id": PAPER_ID, "entity_type": "Citation", "record_id": PAPER_ID, "facts": []})),  # empty facts array: real content failure
        ("extractor", _empty_invocation()),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "error"
    assert "failure_class" not in result.detail


_MALFORMED_TOOL_CALL_STDOUT = (
    '{"type":"tool_use","part":{"type":"tool","tool":"invalid","state":{"status":"completed",'
    '"input":{"tool":"read_section<|channel|>commentary","error":"Model tried to call '
    'unavailable tool \'read_section<|channel|>commentary\'. Available tools: invalid, '
    'list_sections, read_document_start, read_nearby, read_section, read_section_by_path, '
    'read_table, read_table_cell, read_table_row."},'
    '"output":"The arguments provided to the tool are invalid: Model tried to call unavailable '
    'tool \'read_section<|channel|>commentary\'."}}}'
)


def test_has_malformed_harmony_tool_call_detects_the_real_captured_signature():
    assert orchestrator._has_malformed_harmony_tool_call(_MALFORMED_TOOL_CALL_STDOUT) is True


def test_has_malformed_harmony_tool_call_false_for_a_clean_response():
    clean_stdout = '{"type":"text","part":{"type":"text","text":"{\\"a\\": 1}"}}'
    assert orchestrator._has_malformed_harmony_tool_call(clean_stdout) is False


def test_has_malformed_harmony_tool_call_requires_both_parts_of_the_signature():
    assert orchestrator._has_malformed_harmony_tool_call("some <|channel|> text with no tool error") is False
    assert orchestrator._has_malformed_harmony_tool_call("Model tried to call unavailable tool 'foo'") is False


def _malformed_invocation(agent="extractor") -> orchestrator.AgentInvocation:
    return orchestrator.AgentInvocation(
        agent=agent, model="test-model", prompt="p", returncode=0,
        stdout=_MALFORMED_TOOL_CALL_STDOUT, stderr="",
        final_text=None, parsed_json=None,
        parse_error="provider_malformed_response: the model's own harmony-format channel token leaked "
                    "into a tool-call name (a known gpt-oss-120b/vLLM defect, not a real tool call and "
                    "not a content/shape problem) -- discarding this attempt's output rather than "
                    "trusting a response tainted by it.",
        had_malformed_tool_call=True,
    )


def _malformed_but_valid_looking_invocation(agent="extractor") -> orchestrator.AgentInvocation:
    return orchestrator.AgentInvocation(
        agent=agent, model="test-model", prompt="p", returncode=0,
        stdout=_MALFORMED_TOOL_CALL_STDOUT, stderr="",
        final_text='{"a": 1}', parsed_json={"a": 1}, parse_error=None,
        had_malformed_tool_call=True,
    )


def test_invoke_agent_retries_internally_on_malformed_tool_call_then_succeeds(monkeypatch):
    calls = []
    responses = [_malformed_invocation(), _malformed_invocation(), _real_invocation()]

    def fake_once(agent, model, prompt, timeout):
        calls.append(1)
        return responses[len(calls) - 1]

    monkeypatch.setattr(orchestrator, "_invoke_agent_once", fake_once)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda s: None)

    result = orchestrator.invoke_agent("extractor", "test-model", "p")

    assert len(calls) == 3
    assert result.final_text == '{"a": 1}'
    assert result.had_malformed_tool_call is False


def test_invoke_agent_retries_even_when_malformed_response_has_valid_looking_output(monkeypatch):
    calls = []

    def fake_once(agent, model, prompt, timeout):
        calls.append(1)
        return _malformed_but_valid_looking_invocation()

    monkeypatch.setattr(orchestrator, "_invoke_agent_once", fake_once)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda s: None)

    result = orchestrator.invoke_agent("extractor", "test-model", "p")

    # 1 real call + MAX_EMPTY_RESPONSE_RETRIES extra -- retried the full
    # budget despite every attempt having "valid-looking" final_text/JSON.
    assert len(calls) == 1 + orchestrator.MAX_EMPTY_RESPONSE_RETRIES
    assert result.had_malformed_tool_call is True
    assert result.parsed_json == {"a": 1}  # returned as-is on the caller; run_record's own
    # `result.parsed_json is None` check is what actually matters end-to-end (tested below) --
    # invoke_agent itself never mutates a caller-visible field beyond what _invoke_agent_once set.


def test_invoke_agent_gives_up_after_exhausting_malformed_tool_call_retries(monkeypatch):
    calls = []

    def fake_once(agent, model, prompt, timeout):
        calls.append(1)
        return _malformed_invocation()

    monkeypatch.setattr(orchestrator, "_invoke_agent_once", fake_once)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda s: None)

    result = orchestrator.invoke_agent("extractor", "test-model", "p")

    assert len(calls) == 1 + orchestrator.MAX_EMPTY_RESPONSE_RETRIES
    assert result.had_malformed_tool_call is True
    assert result.parsed_json is None
    assert "provider_malformed_response" in result.parse_error


def test_extraction_all_malformed_tool_call_failures_tagged_provider_malformed_response(env):
    invoke = make_invoke_sequence([
        ("extractor", _malformed_invocation()),
        ("extractor", _malformed_invocation()),
        ("extractor", _malformed_invocation()),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "error"
    assert result.detail["failure_class"] == "provider_malformed_response"


def test_extraction_malformed_takes_priority_over_empty_when_mixed(env):
    invoke = make_invoke_sequence([
        ("extractor", _malformed_invocation()),
        ("extractor", _empty_invocation()),
        ("extractor", _malformed_invocation()),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "error"
    assert result.detail["failure_class"] == "provider_malformed_response"


def test_extraction_malformed_mixed_with_genuine_content_failure_is_not_tagged(env):
    invoke = make_invoke_sequence([
        ("extractor", _malformed_invocation()),
        ("extractor", _inv("extractor", {"paper_id": PAPER_ID, "entity_type": "Citation", "record_id": PAPER_ID, "facts": []})),
        ("extractor", _malformed_invocation()),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "error"
    assert "failure_class" not in result.detail


# --------------------------------------------------------------------- #
# Health / staleness guard
# --------------------------------------------------------------------- #


def test_extraction_with_missing_anchors_is_rejected_and_retried(env):
    bad_extraction = {
        "paper_id": PAPER_ID, "entity_type": "Citation", "record_id": PAPER_ID,
        "facts": [{"field_name": "title", "raw_value": "A Title", "raw_text_excerpt": "A Title", "anchors": []}],
    }
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", bad_extraction)),
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", valid_citation_payload())),
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "ready"
    record_key = "Citation__" + PAPER_ID
    attempt1 = run_store.load_json(run_store.record_dir("run1", record_key) / "extraction" / "attempt1.json")
    assert attempt1["validation_errors"]  # anchors min_length=1 violation recorded


def test_extraction_fact_with_null_raw_value_is_valid(env):
    extraction_with_null_fact = dict(RAW_EXTRACTION)
    extraction_with_null_fact["facts"] = RAW_EXTRACTION["facts"] + [
        {"field_name": "persistent_identifier", "raw_value": None, "raw_text_excerpt": "Published in 2012.",
         "anchors": ["b:0002"], "notes": "no DOI visible"}
    ]
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", extraction_with_null_fact)),
        ("converter", _inv("converter", valid_citation_payload())),
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "ready"
    record_key = "Citation__" + PAPER_ID
    attempt1 = run_store.load_json(run_store.record_dir("run1", record_key) / "extraction" / "attempt1.json")
    assert attempt1["validation_errors"] == []


def test_null_fact_with_empty_anchors_is_rejected_same_as_a_valued_fact(env):
    extraction_with_ungrounded_null_fact = dict(RAW_EXTRACTION)
    extraction_with_ungrounded_null_fact["facts"] = RAW_EXTRACTION["facts"] + [
        {"field_name": "persistent_identifier", "raw_value": None, "raw_text_excerpt": "",
         "anchors": [], "notes": "no DOI visible"}
    ]
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", extraction_with_ungrounded_null_fact)),
        ("extractor", _inv("extractor", RAW_EXTRACTION)),  # corrected retry: fact omitted instead
        ("converter", _inv("converter", valid_citation_payload())),
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
    )
    assert result.status == "ready"
    record_key = "Citation__" + PAPER_ID
    attempt1 = run_store.load_json(run_store.record_dir("run1", record_key) / "extraction" / "attempt1.json")
    assert attempt1["validation_errors"] == [
        {"field": "facts.3.anchors", "message": "List should have at least 1 item after validation, not 0"}
    ]


# --------------------------------------------------------------------- #
# known_refs -- fixes the "invented UNKNOWN for site_id" failure mode
# --------------------------------------------------------------------- #

TREATMENT_RAW_EXTRACTION = {
    "paper_id": PAPER_ID, "entity_type": "Treatment", "record_id": "t1",
    "facts": [
        {"field_name": "name", "raw_value": "A Title", "raw_text_excerpt": "A Title", "anchors": ["b:0003"]},
        {"field_name": "definition", "raw_value": "A. Author", "raw_text_excerpt": "A. Author", "anchors": ["b:0001"]},
    ],
}


def valid_treatment_payload_with_ref(site_id="site_1"):
    return {
        "id": "t1",
        "citation_id": PAPER_ID,
        "site_id": site_id,
        "study_id": {"value": None, "provenance_label": "UNRESOLVED", "unresolved_reason": "isolated paper", "source": _src("b:0001")},
        "name": {"value": "A Title", "provenance_label": "EXTRACTED", "source": _src("b:0003")},
        "definition": {"value": "A. Author", "provenance_label": "EXTRACTED", "source": _src("b:0001")},
    }


def test_known_refs_rejects_fabricated_placeholder_and_retries(env):
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", TREATMENT_RAW_EXTRACTION)),
        ("converter", _inv("converter", valid_treatment_payload_with_ref(site_id="UNKNOWN"))),  # fabricated
        ("converter", _inv("converter", valid_treatment_payload_with_ref(site_id="site_1"))),  # corrected
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Treatment", record_id="t1",
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
        known_refs={"site_id": "site_1"},
    )
    assert result.status == "ready"
    record_key = "Treatment__t1"
    attempt1 = run_store.load_json(run_store.record_dir("run1", record_key) / "conversion_validation" / "attempt1.json")
    assert attempt1["valid"] is False
    assert attempt1["source"] == "orchestrator_known_refs_check"
    assert "UNKNOWN" in attempt1["errors"][0]["message"]
    # The known_refs check runs before propose_record -- no wasted server-side attempt.
    conv_dir = run_store.record_dir("run1", record_key) / "conversion"
    assert sorted(p.name for p in conv_dir.iterdir()) == ["attempt1.json", "attempt2.json"]


def test_known_refs_correct_value_passes_straight_through(env):
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", TREATMENT_RAW_EXTRACTION)),
        ("converter", _inv("converter", valid_treatment_payload_with_ref(site_id="site_1"))),
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Treatment", record_id="t1",
        model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=True,
        known_refs={"site_id": "site_1"},
    )
    assert result.status == "ready"



def test_graph_check_skips_unresolved_and_validates_ready_only(env):
    from pipeline import store as store_mod

    store_mod.append_record(
        paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID, status="ready",
        payload=valid_citation_payload(),
    )
    store_mod.append_record(
        paper_id=PAPER_ID, entity_type="Site", record_id="s1", status="unresolved",
        payload={"field": "name", "reason": "unclear", "blocks_examined": ["b:0001"], "conflict_explanation": "x"},
    )
    result = orchestrator.graph_check(PAPER_ID)
    assert result["constructed"] is True
    assert result["counts"]["citations"] == 1
    assert result["counts"]["sites"] == 0  # the unresolved Site never joins the graph
    assert any("s1" in s for s in result["skipped_not_ready"])
    assert result["issues"] == []  # a single valid Citation has nothing to conflict with


def test_graph_check_latest_entry_wins_for_same_record_key(env):
    from pipeline import store as store_mod

    payload1 = valid_citation_payload()
    store_mod.append_record(paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID, status="ready", payload=payload1)
    # A later append for the SAME record_key supersedes the earlier one --
    # store.py's own "latest line wins" contract.
    store_mod.append_record(paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID, status="unresolved",
                             payload={"field": "title", "reason": "retracted", "blocks_examined": ["b:0003"], "conflict_explanation": "x"})
    result = orchestrator.graph_check(PAPER_ID)
    assert result["counts"]["citations"] == 0
    assert any(PAPER_ID in s for s in result["skipped_not_ready"])


def test_graph_check_does_not_yet_catch_dangling_site_ref(env):
    from pipeline import store as store_mod

    treatment = valid_treatment_payload_with_ref(site_id="no_such_site")
    store_mod.append_record(paper_id=PAPER_ID, entity_type="Treatment", record_id="t1", status="ready", payload=treatment)
    result = orchestrator.graph_check(PAPER_ID)
    assert result["constructed"] is True
    codes = [i["code"] for i in result["issues"]]
    assert "dangling_site_ref" not in codes  # no such check exists today


def test_entity_type_to_plural_covers_all_twelve_entities():
    from pipeline.ir_schema import ENTITY_MODELS

    assert set(orchestrator.ENTITY_TYPE_TO_PLURAL.keys()) == set(ENTITY_MODELS.keys())
    assert len(orchestrator.ENTITY_TYPE_TO_PLURAL) == 12


def test_graph_check_handles_new_entities_end_to_end(env):
    from pipeline import store as store_mod

    store_mod.append_record(paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID, status="ready", payload=valid_citation_payload())
    store_mod.append_record(
        paper_id=PAPER_ID, entity_type="Species", record_id="sp1", status="ready",
        payload={"id": "sp1", "genus": {"value": "Panicum", "provenance_label": "EXTRACTED", "source": _src("b:0001")},
                 "species_epithet": {"value": "virgatum", "provenance_label": "EXTRACTED", "source": _src("b:0001")},
                 "scientific_name": {"value": "Panicum virgatum", "provenance_label": "EXTRACTED", "source": _src("b:0001")}},
    )
    store_mod.append_record(
        paper_id=PAPER_ID, entity_type="Crop", record_id="crop1", status="ready",
        payload={"id": "crop1", "citation_id": PAPER_ID, "species_id": "sp1"},
    )
    store_mod.append_record(
        paper_id=PAPER_ID, entity_type="Variable", record_id="v1", status="ready",
        payload={"id": "v1", "name": {"value": "A Title", "provenance_label": "EXTRACTED", "source": _src("b:0003")}},
    )
    result = orchestrator.graph_check(PAPER_ID)
    assert result["constructed"] is True
    assert result["counts"]["crops"] == 1
    assert result["counts"]["variables"] == 1
    assert [i for i in result["issues"] if i["severity"] == "error"] == []


def test_topological_entity_order_respects_all_dependency_edges():
    order = orchestrator._topological_entity_order()
    assert set(order) == set(orchestrator.ENTITY_DEPENDENCIES.keys())
    assert len(order) == 12
    pos = {e: i for i, e in enumerate(order)}
    for entity_type, deps in orchestrator.ENTITY_DEPENDENCIES.items():
        for field, prereq_type, _required in deps:
            assert pos[prereq_type] < pos[entity_type], f"{prereq_type} must precede {entity_type} (field {field})"


def test_topological_order_is_deterministic_across_calls():
    assert orchestrator._topological_entity_order() == orchestrator._topological_entity_order()


def test_resolve_known_refs_blocks_when_required_prereq_not_ready():
    this_run = {"Citation": {"status": "unresolved"}}
    known_refs, reason = orchestrator._resolve_known_refs("Method", this_run)
    assert known_refs is None
    assert "Citation" in reason and "not 'ready'" in reason


def test_resolve_known_refs_blocks_when_prereq_never_attempted():
    known_refs, reason = orchestrator._resolve_known_refs("Treatment", {})
    assert known_refs is None
    assert "not attempted" in reason


def test_resolve_known_refs_blocks_treatment_pair_needing_two_distinct_treatments():
    this_run = {
        "Citation": {"status": "ready", "record_id": "paperA"},
        "Treatment": {"status": "ready", "record_id": "paperA_treatment"},
    }
    known_refs, reason = orchestrator._resolve_known_refs("TreatmentPair", this_run)
    assert known_refs is None
    assert "2 distinct Treatment records" in reason


def test_resolve_known_refs_required_refs_propagate_correctly():
    this_run = {
        "Citation": {"status": "ready", "record_id": "paperA"},
        "Site": {"status": "ready", "record_id": "paperA_site"},
    }
    known_refs, reason = orchestrator._resolve_known_refs("Treatment", this_run)
    assert reason is None
    assert known_refs == {"citation_id": "paperA", "site_id": "paperA_site"}


def test_resolve_known_refs_optional_refs_omitted_without_blocking():
    this_run = {
        "Citation": {"status": "ready", "record_id": "paperA"},
        "Site": {"status": "ready", "record_id": "paperA_site"},
        "Treatment": {"status": "ready", "record_id": "paperA_treatment"},
        "Method": {"status": "ready", "record_id": "paperA_method"},
        # Species/Crop/Variable never attempted -- all optional for Observation.
    }
    known_refs, reason = orchestrator._resolve_known_refs("Observation", this_run)
    assert reason is None
    assert known_refs == {
        "citation_id": "paperA", "site_id": "paperA_site",
        "treatment_id": "paperA_treatment", "method_id": "paperA_method",
    }
    assert "species_id" not in known_refs and "crop_id" not in known_refs and "variable_id" not in known_refs


def test_resolve_known_refs_study_uses_list_field_name():
    this_run = {"Citation": {"status": "ready", "record_id": "paperA"}}
    known_refs, reason = orchestrator._resolve_known_refs("Study", this_run)
    assert reason is None
    assert known_refs == {"citation_ids": "paperA"}


def test_ref_mismatch_handles_list_shaped_field():
    # Study.citation_ids is a list -- membership, not equality.
    assert orchestrator._ref_mismatch(["paperA"], "paperA") is False
    assert orchestrator._ref_mismatch(["paperB"], "paperA") is True
    assert orchestrator._ref_mismatch("paperA", "paperA") is False
    assert orchestrator._ref_mismatch("paperB", "paperA") is True


def test_entity_result_file_shapes_are_stable_across_statuses():
    ready = orchestrator._entity_result_file(
        PAPER_ID, "Citation", "run1", PAPER_ID,
        {"status": "ready", "detail": {"payload": {"id": PAPER_ID}, "ai_validation": {"verdict": "plausible"}}},
    )
    assert ready["payload"] == {"id": PAPER_ID} and ready["ai_validation"] == {"verdict": "plausible"}

    blocked = orchestrator._entity_result_file(
        PAPER_ID, "TreatmentPair", "run1", "x", {"status": "blocked", "reason": "needs 2 treatments"},
    )
    assert blocked["payload"] is None and blocked["reason"] == "needs 2 treatments"

    for r in (ready, blocked):
        assert set(r.keys()) >= {"paper_id", "entity_type", "record_id", "run_id", "status", "payload", "ai_validation"}


PAPER_RUN_ORDER = ["Citation", "Site", "Species", "Variable", "Coverage", "Crop", "Management", "Method", "Study", "Treatment", "Observation"]


def _p_ef(value, anchor="b:0001", label="EXTRACTED", reason=None):
    d = {"value": value, "provenance_label": label, "source": _src(anchor)}
    if reason:
        d["unresolved_reason"] = reason
    return d


def _paper_payload(entity_type: str, record_id: str, refs: dict) -> dict:
    if entity_type == "Citation":
        return {
            "id": record_id,
            "author": _p_ef("A. Author", "b:0001"), "year": _p_ef(2012, "b:0002"), "title": _p_ef("A Title", "b:0003"),
            "persistent_identifier": _p_ef(None, "b:0003", "UNRESOLVED", "no DOI"),
        }
    if entity_type == "Site":
        return {"id": record_id, "name": _p_ef("A Title", "b:0003")}
    if entity_type == "Species":
        return {"id": record_id, "genus": _p_ef("A.", "b:0001"), "species_epithet": _p_ef("Author", "b:0001"), "scientific_name": _p_ef("A. Author", "b:0001")}
    if entity_type == "Variable":
        return {"id": record_id, "name": _p_ef("A Title", "b:0003")}
    if entity_type == "Coverage":
        return {"id": record_id, "citation_id": refs["citation_id"], "site_id": refs["site_id"], "variable_id": refs.get("variable_id")}
    if entity_type == "Crop":
        return {"id": record_id, "citation_id": refs["citation_id"], "species_id": refs["species_id"]}
    if entity_type == "Management":
        return {
            "id": record_id, "citation_id": refs["citation_id"],
            "event_type": _p_ef("A Title", "b:0003"),
            "date": _p_ef({"reported_text": "2012", "earliest": None, "latest": None, "relative_timing": None, "relative_timing_days": None}, "b:0002"),
        }
    if entity_type == "Method":
        return {"id": record_id, "citation_id": refs["citation_id"], "name": _p_ef("A Title", "b:0003"), "description": _p_ef("A Title", "b:0003")}
    if entity_type == "Study":
        return {"id": record_id, "citation_ids": [refs["citation_ids"]]}
    if entity_type == "Treatment":
        return {
            "id": record_id, "citation_id": refs["citation_id"], "site_id": refs["site_id"],
            "study_id": _p_ef(None, "b:0001", "UNRESOLVED", "isolated paper"),
            "name": _p_ef("A Title", "b:0003"), "definition": _p_ef("A Title", "b:0003"),
        }
    if entity_type == "Observation":
        return {
            "id": record_id, "dataset_id": PAPER_ID,
            "citation_id": refs["citation_id"], "site_id": refs["site_id"],
            "treatment_id": refs["treatment_id"], "method_id": refs["method_id"],
            "variable_name": _p_ef("A Title", "b:0003"),
            "value": _p_ef({"reported_text": "2012", "reported_numeric_value": 2012.0, "reported_units": "unit"}, "b:0002"),
            "reported_effect_scope": _p_ef("treatment_mean", "b:0004"),
            "aggregated_over_factors": _p_ef([], "b:0001"),
            "temporal_info": _p_ef({"reported_text": "2012", "earliest": None, "latest": None, "relative_timing": None, "relative_timing_days": None}, "b:0002"),
            "is_raw_replicate_level": _p_ef(None, "b:0001", "UNRESOLVED", "replication level not stated"),
        }
    raise AssertionError(f"no fixture payload defined for {entity_type}")



def test_two_variable_candidates_produce_two_unique_ready_records(env):
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", {
            "entity_type": "Variable",
            "candidates": [
                {"candidate_id": "lai", "description": "Leaf area index.", "anchors": ["b:0003"]},
                {"candidate_id": "soc", "description": "Soil organic carbon.", "anchors": ["b:0004"]},
            ],
        })),
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", {"id": f"{PAPER_ID}_variable_lai", "name": _p_ef("A Title", "b:0003")})),
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", {"id": f"{PAPER_ID}_variable_soc", "name": _p_ef("A Title", "b:0003")})),
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
    ])
    record_infos = orchestrator._run_multi_record_entity(
        run_id="run1", paper_id=PAPER_ID, entity_type="Variable", model="test-model",
        client=env["client"], invoke=invoke, enable_ai_validation=True, this_run_records={},
    )
    assert len(record_infos) == 2
    ids = {r["record_id"] for r in record_infos}
    assert ids == {f"{PAPER_ID}_variable_lai", f"{PAPER_ID}_variable_soc"}
    assert all(r["status"] == "ready" for r in record_infos)

    # Each record independently committed to ir-store under its own id.
    store_file = env["store_root"] / f"{PAPER_ID}.jsonl"
    entries = [json.loads(l) for l in store_file.read_text().strip().splitlines()]
    committed_ids = {e["record_id"] for e in entries if e["status"] == "ready"}
    assert committed_ids == ids


def test_one_variable_candidate_failure_does_not_affect_sibling(env):
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", {
            "entity_type": "Variable",
            "candidates": [
                {"candidate_id": "broken", "description": "Will fail extraction entirely.", "anchors": ["b:0003"]},
                {"candidate_id": "lai", "description": "Leaf area index.", "anchors": ["b:0003"]},
            ],
        })),
        ("extractor", _inv_parse_error("extractor")),
        ("extractor", _inv_parse_error("extractor")),
        ("extractor", _inv_parse_error("extractor")),
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", {"id": f"{PAPER_ID}_variable_lai", "name": _p_ef("A Title", "b:0003")})),
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
    ])
    record_infos = orchestrator._run_multi_record_entity(
        run_id="run1", paper_id=PAPER_ID, entity_type="Variable", model="test-model",
        client=env["client"], invoke=invoke, enable_ai_validation=True, this_run_records={},
    )
    assert len(record_infos) == 2
    by_id = {r["record_id"]: r for r in record_infos}
    assert by_id[f"{PAPER_ID}_variable_broken"]["status"] == "error"
    assert by_id[f"{PAPER_ID}_variable_lai"]["status"] == "ready"


def test_known_refs_resolves_single_ready_variable_for_soft_dependency():
    this_run_records = {
        "Citation": {"status": "ready", "record_id": "p"},
        "Site": {"status": "ready", "record_id": "p_site"},
        "Variable": [{"status": "ready", "record_id": "p_variable_lai"}],
    }
    known_refs, reason = orchestrator._resolve_known_refs("Coverage", this_run_records)
    assert reason is None
    assert known_refs["variable_id"] == "p_variable_lai"


def test_known_refs_omits_variable_when_multiple_ready_and_ambiguous():
    this_run_records = {
        "Citation": {"status": "ready", "record_id": "p"},
        "Site": {"status": "ready", "record_id": "p_site"},
        "Variable": [
            {"status": "ready", "record_id": "p_variable_lai"},
            {"status": "ready", "record_id": "p_variable_soc"},
        ],
    }
    known_refs, reason = orchestrator._resolve_known_refs("Coverage", this_run_records)
    assert reason is None
    assert "variable_id" not in known_refs


def test_known_refs_omits_variable_when_none_ready():
    this_run_records = {
        "Citation": {"status": "ready", "record_id": "p"},
        "Site": {"status": "ready", "record_id": "p_site"},
        "Variable": [],
    }
    known_refs, reason = orchestrator._resolve_known_refs("Coverage", this_run_records)
    assert reason is None
    assert "variable_id" not in known_refs



def test_two_treatment_candidates_produce_two_unique_ready_records(env):
    this_run_records = {
        "Citation": {"status": "ready", "record_id": PAPER_ID},
        "Site": {"status": "ready", "record_id": f"{PAPER_ID}_site"},
    }
    ambient_payload = {
        "id": f"{PAPER_ID}_treatment_ambient_co2", "citation_id": PAPER_ID, "site_id": f"{PAPER_ID}_site",
        "study_id": _p_ef(None, "b:0001", "UNRESOLVED", "isolated paper"),
        "name": _p_ef("A. Author", "b:0001"), "definition": _p_ef("A. Author", "b:0001"),
    }
    elevated_payload = {
        "id": f"{PAPER_ID}_treatment_elevated_co2", "citation_id": PAPER_ID, "site_id": f"{PAPER_ID}_site",
        "study_id": _p_ef(None, "b:0001", "UNRESOLVED", "isolated paper"),
        "name": _p_ef("A Title", "b:0003"), "definition": _p_ef("A Title", "b:0003"),
    }
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", {
            "entity_type": "Treatment",
            "candidates": [
                {"candidate_id": "ambient_co2", "description": "Ambient CO2 (375 ppm) treatment.", "anchors": ["b:0003"]},
                {"candidate_id": "elevated_co2", "description": "Elevated CO2 (700 ppm) treatment.", "anchors": ["b:0004"]},
            ],
        })),
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", ambient_payload)),
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", elevated_payload)),
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
    ])
    record_infos = orchestrator._run_multi_record_entity(
        run_id="run1", paper_id=PAPER_ID, entity_type="Treatment", model="test-model",
        client=env["client"], invoke=invoke, enable_ai_validation=True, this_run_records=this_run_records,
    )
    assert len(record_infos) == 2
    ids = {r["record_id"] for r in record_infos}
    assert ids == {f"{PAPER_ID}_treatment_ambient_co2", f"{PAPER_ID}_treatment_elevated_co2"}
    assert all(r["status"] == "ready" for r in record_infos)

    store_file = env["store_root"] / f"{PAPER_ID}.jsonl"
    entries = [json.loads(l) for l in store_file.read_text().strip().splitlines()]
    committed_ids = {e["record_id"] for e in entries if e["status"] == "ready" and e["entity_type"] == "Treatment"}
    assert committed_ids == ids


def test_one_treatment_candidate_failure_does_not_affect_sibling(env):
    this_run_records = {
        "Citation": {"status": "ready", "record_id": PAPER_ID},
        "Site": {"status": "ready", "record_id": f"{PAPER_ID}_site"},
    }
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", {
            "entity_type": "Treatment",
            "candidates": [
                {"candidate_id": "broken", "description": "Will fail extraction entirely.", "anchors": ["b:0003"]},
                {"candidate_id": "ambient_co2", "description": "Ambient CO2 (375 ppm) treatment.", "anchors": ["b:0003"]},
            ],
        })),
        ("extractor", _inv_parse_error("extractor")),
        ("extractor", _inv_parse_error("extractor")),
        ("extractor", _inv_parse_error("extractor")),
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", {
            "id": f"{PAPER_ID}_treatment_ambient_co2", "citation_id": PAPER_ID, "site_id": f"{PAPER_ID}_site",
            "study_id": _p_ef(None, "b:0001", "UNRESOLVED", "isolated paper"),
            "name": _p_ef("A. Author", "b:0001"), "definition": _p_ef("A. Author", "b:0001"),
        })),
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
    ])
    record_infos = orchestrator._run_multi_record_entity(
        run_id="run1", paper_id=PAPER_ID, entity_type="Treatment", model="test-model",
        client=env["client"], invoke=invoke, enable_ai_validation=True, this_run_records=this_run_records,
    )
    assert len(record_infos) == 2
    by_id = {r["record_id"]: r for r in record_infos}
    assert by_id[f"{PAPER_ID}_treatment_broken"]["status"] == "error"
    assert by_id[f"{PAPER_ID}_treatment_ambient_co2"]["status"] == "ready"


def test_known_refs_resolves_treatment_pair_citation_but_defers_both_treatment_slots():
    this_run_records = {
        "Citation": {"status": "ready", "record_id": "p"},
        "Treatment": [
            {"status": "ready", "record_id": "p_treatment_ambient_co2"},
            {"status": "ready", "record_id": "p_treatment_elevated_co2"},
        ],
    }
    known_refs, reason = orchestrator._resolve_known_refs("TreatmentPair", this_run_records)
    assert reason is None
    assert known_refs == {"citation_id": "p"}
    assert "treatment_id_1" not in known_refs and "treatment_id_2" not in known_refs


def test_apply_candidate_links_resolves_treatment_pair_slots_from_linked_candidates():
    this_run_records = {
        "Citation": {"status": "ready", "record_id": PAPER_ID},
        "Treatment": [
            {"status": "ready", "record_id": f"{PAPER_ID}_treatment_ambient_co2"},
            {"status": "ready", "record_id": f"{PAPER_ID}_treatment_elevated_co2"},
        ],
    }
    known_refs, reason = orchestrator._resolve_known_refs("TreatmentPair", this_run_records)
    assert reason is None
    candidate = EnumerationCandidate(
        candidate_id="co2_contrast", description="ambient vs elevated CO2", anchors=["b:0001"],
        linked_candidates={"treatment_id_1": "ambient_co2", "treatment_id_2": "elevated_co2"},
    )
    resolved = orchestrator._apply_candidate_links(PAPER_ID, "TreatmentPair", this_run_records, known_refs, candidate)
    assert resolved["treatment_id_1"] == f"{PAPER_ID}_treatment_ambient_co2"
    assert resolved["treatment_id_2"] == f"{PAPER_ID}_treatment_elevated_co2"


def test_apply_candidate_links_falls_back_to_full_allowed_set_for_both_treatment_pair_slots_when_unlinked():
    this_run_records = {
        "Citation": {"status": "ready", "record_id": PAPER_ID},
        "Treatment": [
            {"status": "ready", "record_id": f"{PAPER_ID}_treatment_ambient_co2"},
            {"status": "ready", "record_id": f"{PAPER_ID}_treatment_elevated_co2"},
        ],
    }
    known_refs, reason = orchestrator._resolve_known_refs("TreatmentPair", this_run_records)
    assert reason is None
    candidate = EnumerationCandidate(candidate_id="co2_contrast", description="x", anchors=["b:0001"])
    resolved = orchestrator._apply_candidate_links(PAPER_ID, "TreatmentPair", this_run_records, known_refs, candidate)
    allowed = {f"{PAPER_ID}_treatment_ambient_co2", f"{PAPER_ID}_treatment_elevated_co2"}
    assert set(resolved["treatment_id_1"]) == allowed
    assert set(resolved["treatment_id_2"]) == allowed


def test_known_refs_still_blocks_treatment_pair_when_only_one_treatment_ready():
    this_run_records = {
        "Citation": {"status": "ready", "record_id": "p"},
        "Treatment": [{"status": "ready", "record_id": "p_treatment_ambient_co2"}],
    }
    known_refs, reason = orchestrator._resolve_known_refs("TreatmentPair", this_run_records)
    assert known_refs is None
    assert "2 distinct Treatment records" in reason
    assert "only 1" in reason


def test_resolve_known_refs_does_not_crash_when_single_slot_prereq_is_multi_record():
    this_run_records = {
        "Citation": {"status": "ready", "record_id": "p"},
        "Site": {"status": "ready", "record_id": "p_site"},
        "Treatment": [{"status": "ready", "record_id": "p_treatment_ambient_co2"}],
        "Method": {"status": "ready", "record_id": "p_method"},
    }
    known_refs, reason = orchestrator._resolve_known_refs("Observation", this_run_records)
    assert reason is None
    assert known_refs["treatment_id"] == "p_treatment_ambient_co2"


def test_treatment_pair_becomes_reachable_and_commits_once_two_treatments_exist(env):
    this_run_records = {
        "Citation": {"status": "ready", "record_id": PAPER_ID},
        "Treatment": [
            {"status": "ready", "record_id": f"{PAPER_ID}_treatment_ambient_co2"},
            {"status": "ready", "record_id": f"{PAPER_ID}_treatment_elevated_co2"},
        ],
    }
    pair_payload = {
        "id": f"{PAPER_ID}_treatmentpair_co2_contrast",
        "citation_id": PAPER_ID,
        "treatment_id_1": f"{PAPER_ID}_treatment_ambient_co2",
        "treatment_id_2": f"{PAPER_ID}_treatment_elevated_co2",
        "comparison_factor": _p_ef("A Title", "b:0003"),
        "comparison_label": _p_ef("A. Author", "b:0001"),
    }
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", {
            "entity_type": "TreatmentPair",
            "candidates": [{
                "candidate_id": "co2_contrast", "description": "ambient vs elevated CO2", "anchors": ["b:0001"],
                "linked_candidates": {"treatment_id_1": "ambient_co2", "treatment_id_2": "elevated_co2"},
            }],
        })),
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", pair_payload)),
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
    ])
    record_infos = orchestrator._run_multi_record_entity(
        run_id="run1", paper_id=PAPER_ID, entity_type="TreatmentPair", model="test-model",
        client=env["client"], invoke=invoke, enable_ai_validation=True, this_run_records=this_run_records,
    )
    assert len(record_infos) == 1
    assert record_infos[0]["status"] == "ready"
    payload = record_infos[0]["detail"]["payload"]
    assert payload["treatment_id_1"] == f"{PAPER_ID}_treatment_ambient_co2"
    assert payload["treatment_id_2"] == f"{PAPER_ID}_treatment_elevated_co2"


def test_treatment_pair_blocked_skips_enumeration_call_entirely(env):
    this_run_records = {
        "Citation": {"status": "ready", "record_id": PAPER_ID},
        "Treatment": [{"status": "ready", "record_id": f"{PAPER_ID}_treatment_ambient_co2"}],
    }
    invoke = make_invoke_sequence([])
    record_infos = orchestrator._run_multi_record_entity(
        run_id="run1", paper_id=PAPER_ID, entity_type="TreatmentPair", model="test-model",
        client=env["client"], invoke=invoke, enable_ai_validation=True, this_run_records=this_run_records,
    )
    assert len(record_infos) == 1
    assert record_infos[0]["status"] == "blocked"
    assert "2 distinct Treatment records" in record_infos[0]["reason"]

from pipeline.raw_schema import EnumerationCandidate  # noqa: E402


def _ready_multi(record_id, name):
    return {
        "status": "ready", "record_id": record_id,
        "detail": {"payload": {"id": record_id, "name": _p_ef(name, "b:0003")}},
    }


def _observation_payload(record_id, treatment_id, method_id, variable_id=None, site_id=None):
    payload = {
        "id": record_id, "dataset_id": PAPER_ID,
        "citation_id": PAPER_ID, "site_id": site_id or f"{PAPER_ID}_site",
        "treatment_id": treatment_id, "method_id": method_id,
        "variable_name": _p_ef("A Title", "b:0003"),
        "value": _p_ef({"reported_text": "A Title", "reported_numeric_value": 1.0, "reported_units": "unit"}, "b:0003"),
        "reported_effect_scope": _p_ef("treatment_mean", "b:0004"),
        "aggregated_over_factors": _p_ef([], "b:0001"),
        "temporal_info": _p_ef(
            {"reported_text": "Published in 2012.", "earliest": None, "latest": None,
             "relative_timing": None, "relative_timing_days": None},
            "b:0002",
        ),
        "is_raw_replicate_level": _p_ef(None, "b:0001", "UNRESOLVED", "replication level not stated"),
    }
    if variable_id:
        payload["variable_id"] = variable_id
    return payload


def test_multi_record_link_pools_empty_when_prereq_unambiguous():
    this_run_records = {"Treatment": [_ready_multi(f"{PAPER_ID}_treatment_ambient_co2", "ambient")]}
    assert orchestrator._multi_record_link_pools(PAPER_ID, "Observation", this_run_records) == {}


def test_multi_record_link_pools_lists_ready_candidates_when_ambiguous():
    this_run_records = {
        "Treatment": [
            _ready_multi(f"{PAPER_ID}_treatment_ambient_co2", "ambient"),
            _ready_multi(f"{PAPER_ID}_treatment_elevated_co2", "elevated CO2"),
        ],
    }
    pools = orchestrator._multi_record_link_pools(PAPER_ID, "Observation", this_run_records)
    assert {item["slug"] for item in pools["treatment_id"]} == {"ambient_co2", "elevated_co2"}
    assert {item["name"] for item in pools["treatment_id"]} == {"ambient", "elevated CO2"}


def test_multi_record_link_pools_empty_for_treatment_itself():
    this_run_records = {"Citation": {"status": "ready", "record_id": PAPER_ID}}
    assert orchestrator._multi_record_link_pools(PAPER_ID, "Treatment", this_run_records) == {}


def test_apply_candidate_links_uses_verified_link():
    this_run_records = {
        "Treatment": [
            _ready_multi(f"{PAPER_ID}_treatment_ambient_co2", "ambient"),
            _ready_multi(f"{PAPER_ID}_treatment_elevated_co2", "elevated CO2"),
        ],
    }
    candidate = EnumerationCandidate(
        candidate_id="obs1", description="x", anchors=["b:0001"],
        linked_candidates={"treatment_id": "elevated_co2"},
    )
    resolved = orchestrator._apply_candidate_links(PAPER_ID, "Observation", this_run_records, {}, candidate)
    assert resolved["treatment_id"] == f"{PAPER_ID}_treatment_elevated_co2"


def test_apply_candidate_links_falls_back_to_allowed_set_when_link_unverified():
    this_run_records = {
        "Treatment": [
            _ready_multi(f"{PAPER_ID}_treatment_ambient_co2", "ambient"),
            _ready_multi(f"{PAPER_ID}_treatment_elevated_co2", "elevated CO2"),
        ],
    }
    candidate = EnumerationCandidate(
        candidate_id="obs1", description="x", anchors=["b:0001"],
        linked_candidates={"treatment_id": "not_a_real_slug"},
    )
    resolved = orchestrator._apply_candidate_links(PAPER_ID, "Observation", this_run_records, {}, candidate)
    assert resolved["treatment_id"] == sorted([f"{PAPER_ID}_treatment_ambient_co2", f"{PAPER_ID}_treatment_elevated_co2"])


def test_apply_candidate_links_omits_optional_field_when_ambiguous_and_unlinked():
    this_run_records = {
        "Variable": [
            _ready_multi(f"{PAPER_ID}_variable_lai", "leaf area index"),
            _ready_multi(f"{PAPER_ID}_variable_nh4", "NH4 uptake rate"),
        ],
    }
    candidate = EnumerationCandidate(candidate_id="obs1", description="x", anchors=["b:0001"])
    resolved = orchestrator._apply_candidate_links(PAPER_ID, "Observation", this_run_records, {}, candidate)
    assert "variable_id" not in resolved


def test_apply_candidate_links_noop_for_single_record_dependencies():
    this_run_records = {
        "Citation": {"status": "ready", "record_id": PAPER_ID},
        "Site": {"status": "ready", "record_id": f"{PAPER_ID}_site"},
    }
    known_refs = {"citation_id": PAPER_ID, "site_id": f"{PAPER_ID}_site"}
    candidate = EnumerationCandidate(candidate_id="ambient_co2", description="x", anchors=["b:0001"])
    resolved = orchestrator._apply_candidate_links(PAPER_ID, "Treatment", this_run_records, known_refs, candidate)
    assert resolved == known_refs


def test_ref_mismatch_allowed_set_membership():
    assert orchestrator._ref_mismatch("id_a", ["id_a", "id_b"]) is False
    assert orchestrator._ref_mismatch("id_c", ["id_a", "id_b"]) is True
    assert orchestrator._ref_mismatch(["id_a"], ["id_a", "id_b"]) is False


def test_ref_mismatch_never_crashes_on_an_unhashable_candidate_value():
    # report a mismatch, not raise.
    malformed = {"value": None, "provenance_label": "UNRESOLVED", "unresolved_reason": "..."}
    assert orchestrator._ref_mismatch(malformed, ["id_a", "id_b"]) is True
    assert orchestrator._ref_mismatch(malformed, "id_a") is True
    assert orchestrator._ref_mismatch([malformed, "id_a"], ["id_a", "id_b"]) is False
    assert orchestrator._ref_mismatch([malformed], ["id_a", "id_b"]) is True

    message = orchestrator._ref_expectation_message("treatment_id", ["id_a", "id_b"], malformed)
    assert repr(malformed) in message


def test_run_enumeration_rejects_linked_candidate_slug_not_in_pool(env):
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", {
            "entity_type": "Observation",
            "candidates": [{
                "candidate_id": "obs1", "description": "x", "anchors": ["b:0003"],
                "linked_candidates": {"treatment_id": "made_up_slug"},
            }],
        })),
        ("extractor", _inv("extractor", {
            "entity_type": "Observation",
            "candidates": [{
                "candidate_id": "obs1", "description": "x", "anchors": ["b:0003"],
                "linked_candidates": {"treatment_id": "ambient_co2"},
            }],
        })),
    ])
    candidates, error = orchestrator.run_enumeration(
        run_id="run1", paper_id=PAPER_ID, entity_type="Observation", model="test-model", invoke=invoke,
        link_pools={"treatment_id": [{"slug": "ambient_co2", "record_id": "x", "name": "ambient"}]},
    )
    assert error is None
    assert candidates[0].linked_candidates == {"treatment_id": "ambient_co2"}


def test_two_observation_candidates_link_to_different_treatment_variable_pairs(env):
    this_run_records = {
        "Citation": {"status": "ready", "record_id": PAPER_ID},
        "Site": {"status": "ready", "record_id": f"{PAPER_ID}_site"},
        "Method": {"status": "ready", "record_id": f"{PAPER_ID}_method"},
        "Treatment": [
            _ready_multi(f"{PAPER_ID}_treatment_ambient_co2", "ambient"),
            _ready_multi(f"{PAPER_ID}_treatment_elevated_co2", "elevated CO2"),
        ],
        "Variable": [
            _ready_multi(f"{PAPER_ID}_variable_leaf_area_index", "leaf area index"),
            _ready_multi(f"{PAPER_ID}_variable_nh4_uptake_rate", "NH4 uptake rate"),
        ],
    }
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", {
            "entity_type": "Observation",
            "candidates": [
                {
                    "candidate_id": "lai_ambient", "description": "LAI under ambient CO2.",
                    "anchors": ["b:0003"],
                    "linked_candidates": {"treatment_id": "ambient_co2", "variable_id": "leaf_area_index"},
                },
                {
                    "candidate_id": "nh4_elevated", "description": "NH4 uptake under elevated CO2.",
                    "anchors": ["b:0004"],
                    "linked_candidates": {"treatment_id": "elevated_co2", "variable_id": "nh4_uptake_rate"},
                },
            ],
        })),
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", _observation_payload(
            f"{PAPER_ID}_observation_lai_ambient",
            f"{PAPER_ID}_treatment_ambient_co2", f"{PAPER_ID}_method",
            variable_id=f"{PAPER_ID}_variable_leaf_area_index",
        ))),
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", _observation_payload(
            f"{PAPER_ID}_observation_nh4_elevated",
            f"{PAPER_ID}_treatment_elevated_co2", f"{PAPER_ID}_method",
            variable_id=f"{PAPER_ID}_variable_nh4_uptake_rate",
        ))),
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
    ])

    record_infos = orchestrator._run_multi_record_entity(
        run_id="run1", paper_id=PAPER_ID, entity_type="Observation", model="test-model",
        client=env["client"], invoke=invoke, enable_ai_validation=True, this_run_records=this_run_records,
    )
    assert len(record_infos) == 2
    by_id = {r["record_id"]: r for r in record_infos}
    assert by_id[f"{PAPER_ID}_observation_lai_ambient"]["status"] == "ready"
    assert by_id[f"{PAPER_ID}_observation_nh4_elevated"]["status"] == "ready"

    lai = by_id[f"{PAPER_ID}_observation_lai_ambient"]["detail"]["payload"]
    nh4 = by_id[f"{PAPER_ID}_observation_nh4_elevated"]["detail"]["payload"]
    assert lai["treatment_id"] == f"{PAPER_ID}_treatment_ambient_co2"
    assert lai["variable_id"] == f"{PAPER_ID}_variable_leaf_area_index"
    assert nh4["treatment_id"] == f"{PAPER_ID}_treatment_elevated_co2"
    assert nh4["variable_id"] == f"{PAPER_ID}_variable_nh4_uptake_rate"


def test_one_observation_candidate_failure_does_not_affect_sibling(env):
    this_run_records = {
        "Citation": {"status": "ready", "record_id": PAPER_ID},
        "Site": {"status": "ready", "record_id": f"{PAPER_ID}_site"},
        "Method": {"status": "ready", "record_id": f"{PAPER_ID}_method"},
        "Treatment": [_ready_multi(f"{PAPER_ID}_treatment_ambient_co2", "ambient")],
    }
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", {
            "entity_type": "Observation",
            "candidates": [
                {"candidate_id": "broken", "description": "Will fail extraction entirely.", "anchors": ["b:0003"]},
                {"candidate_id": "lai_ambient", "description": "LAI under ambient CO2.", "anchors": ["b:0003"]},
            ],
        })),
        ("extractor", _inv_parse_error("extractor")),
        ("extractor", _inv_parse_error("extractor")),
        ("extractor", _inv_parse_error("extractor")),
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", _observation_payload(
            f"{PAPER_ID}_observation_lai_ambient", f"{PAPER_ID}_treatment_ambient_co2", f"{PAPER_ID}_method",
        ))),
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
    ])
    record_infos = orchestrator._run_multi_record_entity(
        run_id="run1", paper_id=PAPER_ID, entity_type="Observation", model="test-model",
        client=env["client"], invoke=invoke, enable_ai_validation=True, this_run_records=this_run_records,
    )
    assert len(record_infos) == 2
    by_id = {r["record_id"]: r for r in record_infos}
    assert by_id[f"{PAPER_ID}_observation_broken"]["status"] == "error"
    assert by_id[f"{PAPER_ID}_observation_lai_ambient"]["status"] == "ready"


def test_observation_conversion_rejects_wrong_linked_treatment_then_succeeds(env):
    this_run_records = {
        "Citation": {"status": "ready", "record_id": PAPER_ID},
        "Site": {"status": "ready", "record_id": f"{PAPER_ID}_site"},
        "Method": {"status": "ready", "record_id": f"{PAPER_ID}_method"},
        "Treatment": [
            _ready_multi(f"{PAPER_ID}_treatment_ambient_co2", "ambient"),
            _ready_multi(f"{PAPER_ID}_treatment_elevated_co2", "elevated CO2"),
        ],
    }
    candidate = EnumerationCandidate(
        candidate_id="lai_ambient", description="LAI under ambient CO2.", anchors=["b:0003"],
        linked_candidates={"treatment_id": "ambient_co2"},
    )
    base_known_refs, reason = orchestrator._resolve_known_refs("Observation", this_run_records)
    assert reason is None
    known_refs = orchestrator._apply_candidate_links(PAPER_ID, "Observation", this_run_records, base_known_refs, candidate)
    assert known_refs["treatment_id"] == f"{PAPER_ID}_treatment_ambient_co2"

    wrong_payload = _observation_payload(
        f"{PAPER_ID}_observation_lai_ambient", f"{PAPER_ID}_treatment_elevated_co2", f"{PAPER_ID}_method",
    )
    right_payload = _observation_payload(
        f"{PAPER_ID}_observation_lai_ambient", f"{PAPER_ID}_treatment_ambient_co2", f"{PAPER_ID}_method",
    )
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", wrong_payload)),
        ("converter", _inv("converter", right_payload)),
        ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []})),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Observation",
        record_id=f"{PAPER_ID}_observation_lai_ambient", model="test-model",
        client=env["client"], invoke=invoke, enable_ai_validation=True, known_refs=known_refs,
    )
    assert result.status == "ready"
    assert result.detail["payload"]["treatment_id"] == f"{PAPER_ID}_treatment_ambient_co2"


def test_observation_ambiguous_allowed_set_refuses_to_guess_and_skips_conversion(env):
    this_run_records = {
        "Citation": {"status": "ready", "record_id": PAPER_ID},
        "Site": {"status": "ready", "record_id": f"{PAPER_ID}_site"},
        "Method": {"status": "ready", "record_id": f"{PAPER_ID}_method"},
        "Treatment": [
            _ready_multi(f"{PAPER_ID}_treatment_ambient_co2", "ambient"),
            _ready_multi(f"{PAPER_ID}_treatment_elevated_co2", "elevated CO2"),
        ],
    }
    candidate = EnumerationCandidate(candidate_id="obs1", description="x", anchors=["b:0003"])
    base_known_refs, reason = orchestrator._resolve_known_refs("Observation", this_run_records)
    assert reason is None
    known_refs = orchestrator._apply_candidate_links(PAPER_ID, "Observation", this_run_records, base_known_refs, candidate)
    assert known_refs["treatment_id"] == sorted([f"{PAPER_ID}_treatment_ambient_co2", f"{PAPER_ID}_treatment_elevated_co2"])

    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Observation",
        record_id=f"{PAPER_ID}_observation_obs1", model="test-model",
        client=env["client"], invoke=invoke, enable_ai_validation=True, known_refs=known_refs,
    )
    assert result.status == "unresolved"
    assert result.detail["last_errors"][0]["field"] == "treatment_id"
    assert "ambiguous" in result.detail["last_errors"][0]["message"]
    assert result.detail["last_candidate_payload"] is None


def test_refuse_to_guess_gate_is_selective_a_resolved_field_never_triggers_it(env):
    known_refs = {
        "site_id": f"{PAPER_ID}_site",  # resolved -- must not trigger the gate
        "treatment_id": [f"{PAPER_ID}_treatment_ambient_co2", f"{PAPER_ID}_treatment_elevated_co2"],  # ambiguous
    }
    invoke = make_invoke_sequence([
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
    ])
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Observation",
        record_id=f"{PAPER_ID}_observation_obs2", model="test-model",
        client=env["client"], invoke=invoke, enable_ai_validation=True, known_refs=known_refs,
    )
    assert result.status == "unresolved"
    fields_named = {e["field"] for e in result.detail["last_errors"]}
    assert fields_named == {"treatment_id"}


def test_known_refs_and_links_unaffected_when_dependencies_have_one_ready_record_each():
    this_run_records = {
        "Citation": {"status": "ready", "record_id": PAPER_ID},
        "Site": {"status": "ready", "record_id": f"{PAPER_ID}_site"},
        "Method": {"status": "ready", "record_id": f"{PAPER_ID}_method"},
        "Treatment": [_ready_multi(f"{PAPER_ID}_treatment_ambient_co2", "ambient")],
        "Variable": [_ready_multi(f"{PAPER_ID}_variable_leaf_area_index", "leaf area index")],
    }
    known_refs, reason = orchestrator._resolve_known_refs("Observation", this_run_records)
    assert reason is None
    assert known_refs["treatment_id"] == f"{PAPER_ID}_treatment_ambient_co2"
    assert known_refs["variable_id"] == f"{PAPER_ID}_variable_leaf_area_index"

    candidate = EnumerationCandidate(candidate_id="obs1", description="x", anchors=["b:0001"])
    resolved = orchestrator._apply_candidate_links(PAPER_ID, "Observation", this_run_records, known_refs, candidate)
    assert resolved == known_refs  # no-op: nothing left ambiguous to refine


def _build_run_paper_invoke_sequence():
    """Builds the exact (agent, AgentInvocation) sequence run_paper will
    walk through -- extraction then conversion for each of the 11
    non-blocked entities, in dependency order, with each conversion payload
    correctly using the known_refs produced by earlier entities in the
    sequence. TreatmentPair issues no calls at all (blocked before
    invoke). Variable (Phase A: multi-record) additionally issues ONE
    enumeration call first, reporting exactly one candidate -- keeping this
    fixture's overall shape (one Variable record produced) identical to
    before multi-record support existed, while exercising the real
    enumeration -> run_record() control flow."""
    items = []
    produced_record_ids: dict[str, str] = {}  # entity_type -> the REAL record_id it will be produced under
    for entity_type in PAPER_RUN_ORDER:
        if entity_type in results_store_module.MULTI_RECORD_ENTITY_TYPES:
            enumeration_result = {
                "entity_type": entity_type,
                "candidates": [{"candidate_id": "x", "description": "The single reported variable.", "anchors": ["b:0003"]}],
            }
            items.append(("extractor", _inv("extractor", enumeration_result)))
            record_id = f"{PAPER_ID}_{entity_type.lower()}_x"
        else:
            record_id = orchestrator._entity_record_id(PAPER_ID, entity_type)
        known_refs, reason = orchestrator._resolve_known_refs(entity_type, {
            et: {"status": "ready", "record_id": rid} for et, rid in produced_record_ids.items()
        })
        assert reason is None, f"fixture error: {entity_type} unexpectedly blocked: {reason}"
        raw_extraction = {
            "paper_id": PAPER_ID, "entity_type": entity_type, "record_id": record_id,
            "facts": [{"field_name": "x", "raw_value": "A Title", "raw_text_excerpt": "A Title", "anchors": ["b:0003"]}],
        }
        items.append(("extractor", _inv("extractor", raw_extraction)))
        payload = _paper_payload(entity_type, record_id, known_refs or {})
        items.append(("converter", _inv("converter", payload)))
        produced_record_ids[entity_type] = record_id
    return items


def test_run_paper_end_to_end_dependency_propagation_and_blocking(env):
    invoke = make_invoke_sequence(_build_run_paper_invoke_sequence())
    outcome = orchestrator.run_paper(
        paper_id=PAPER_ID, model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=False,
    )

    assert outcome["entity_order"] == orchestrator._topological_entity_order()
    records = outcome["records"]

    # All 11 attempted entities committed ready; TreatmentPair blocked without ever invoking an agent.
    for et in PAPER_RUN_ORDER:
        if et in results_store_module.MULTI_RECORD_ENTITY_TYPES:
            assert isinstance(records[et], list) and len(records[et]) == 1
            assert records[et][0]["status"] == "ready", f"{et}: {records[et]}"
        else:
            assert records[et]["status"] == "ready", f"{et}: {records[et]}"
    assert isinstance(records["TreatmentPair"], list) and len(records["TreatmentPair"]) == 1
    assert records["TreatmentPair"][0]["status"] == "blocked"
    assert "2 distinct Treatment records" in records["TreatmentPair"][0]["reason"]

    # One run_id shared across the whole paper run.
    run_id = outcome["run_id"]
    assert run_store.load_run_manifest(run_id)["kind"] == "run-paper"

    from pipeline import results_store

    site_results = results_store.load_multi_entity_results(PAPER_ID, "Site")
    assert len(site_results) == 1
    site_result = site_results[0]
    treatment_results = results_store.load_multi_entity_results(PAPER_ID, "Treatment")
    assert len(treatment_results) == 1
    assert treatment_results[0]["payload"]["site_id"] == site_result["record_id"]

    species_results = results_store.load_multi_entity_results(PAPER_ID, "Species")
    crop_results = results_store.load_multi_entity_results(PAPER_ID, "Crop")
    assert len(species_results) == 1 and len(crop_results) == 1
    assert crop_results[0]["payload"]["species_id"] == species_results[0]["record_id"]

    # All 12 entity types produced a results file, including the blocked one.
    for et in orchestrator.ENTITY_TYPE_TO_PLURAL:
        if et in results_store_module.MULTI_RECORD_ENTITY_TYPES:
            on_disk_list = results_store.load_multi_entity_results(PAPER_ID, et)
            assert len(on_disk_list) == 1
            assert on_disk_list[0]["entity_type"] == et
            assert on_disk_list[0]["run_id"] == run_id
            continue
        on_disk = results_store.load_entity_result(PAPER_ID, et)
        assert on_disk["entity_type"] == et
        assert on_disk["run_id"] == run_id


def test_run_paper_never_uses_stale_historical_records(env):
    from pipeline import store as store_mod, results_store

    store_mod.append_record(
        paper_id=PAPER_ID, entity_type="Species", record_id="some_old_species_id", status="ready",
        payload={"id": "some_old_species_id", "genus": _p_ef("A."), "species_epithet": _p_ef("Author"), "scientific_name": _p_ef("A. Author")},
        extra={"run_id": "some_ancient_run"},
    )

    invoke = make_invoke_sequence(_build_run_paper_invoke_sequence())
    outcome = orchestrator.run_paper(
        paper_id=PAPER_ID, model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=False,
    )

    # Species/Crop are multi-record now (Universal Multi-Record pass).
    crop_results = results_store.load_multi_entity_results(PAPER_ID, "Crop")
    species_results = results_store.load_multi_entity_results(PAPER_ID, "Species")
    assert len(crop_results) == 1 and len(species_results) == 1
    crop_result, species_result = crop_results[0], species_results[0]
    # Crop must reference the Species produced THIS run, never the stale one.
    assert crop_result["payload"]["species_id"] == species_result["record_id"]
    assert crop_result["payload"]["species_id"] != "some_old_species_id"
    assert species_result["run_id"] == outcome["run_id"]


def test_run_paper_writes_directory_with_all_twelve_files(env):
    from pipeline import results_store

    invoke = make_invoke_sequence(_build_run_paper_invoke_sequence())
    orchestrator.run_paper(paper_id=PAPER_ID, model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=False)

    paper_dir = results_store.paper_dir(PAPER_ID)
    single_record_files = sorted(p.name for p in paper_dir.glob("*.json"))
    single_record_types = [
        et for et in orchestrator.ENTITY_TYPE_TO_PLURAL if et not in results_store_module.MULTI_RECORD_ENTITY_TYPES
    ]
    assert single_record_files == sorted(f"{et}.json" for et in single_record_types)

    # Multi-record types (Phase A: Variable) get their own directory
    # instead of a flat <Entity>.json file -- one file per record_id.
    for et in results_store_module.MULTI_RECORD_ENTITY_TYPES:
        assert (paper_dir / et).is_dir()
        assert len(list((paper_dir / et).glob("*.json"))) == 1


# --------------------------------------------------------------------- #
# Unified full-paper result (finalize)
# --------------------------------------------------------------------- #


def test_finalize_paper_all_twelve_entity_types_present_even_when_absent(env):
    from pipeline import results_store

    result = orchestrator.finalize_paper(PAPER_ID)
    assert set(result["entity_types"]) == set(orchestrator.ENTITY_TYPE_TO_PLURAL.keys())
    assert len(result["entity_types"]) == 12
    # Nothing committed yet -- every entity type must show as structurally
    # absent (both lists empty), never fabricated.
    for et in result["entity_types"]:
        assert result["entities"][et] == {"ready": [], "unresolved": []}
    assert result["graph_constructed"] is True
    assert result["dataset"] is not None
    assert results_store.result_path(PAPER_ID).exists()


def test_finalize_paper_distinguishes_ready_unresolved_and_absent(env):
    from pipeline import store as store_mod

    store_mod.append_record(paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID, status="ready", payload=valid_citation_payload())
    store_mod.append_record(
        paper_id=PAPER_ID, entity_type="Site", record_id="s1", status="unresolved",
        payload={"field": "name", "reason": "unclear", "blocks_examined": ["b:0001"], "conflict_explanation": "x"},
        extra={"run_id": "run1", "schema_version": "abc123"},
    )
    result = orchestrator.finalize_paper(PAPER_ID)

    assert len(result["entities"]["Citation"]["ready"]) == 1
    assert result["entities"]["Citation"]["unresolved"] == []
    assert result["entities"]["Citation"]["ready"][0]["record_id"] == PAPER_ID

    assert result["entities"]["Site"]["ready"] == []
    assert len(result["entities"]["Site"]["unresolved"]) == 1
    assert result["entities"]["Site"]["unresolved"][0]["run_id"] == "run1"
    assert result["entities"]["Site"]["unresolved"][0]["schema_version"] == "abc123"

    # Never extracted at all -- absent, distinct from unresolved.
    assert result["entities"]["Treatment"] == {"ready": [], "unresolved": []}


def test_finalize_paper_preserves_full_payload_and_lineage(env):
    from pipeline import store as store_mod

    payload = valid_citation_payload()
    store_mod.append_record(
        paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID, status="ready", payload=payload,
        extra={"run_id": "run42", "schema_version": "fp1", "ai_validation": {"verdict": "plausible", "issues": []}},
    )
    result = orchestrator.finalize_paper(PAPER_ID)
    entry = result["entities"]["Citation"]["ready"][0]
    assert entry["payload"] == payload  # full provenance-bearing payload preserved verbatim
    assert entry["run_id"] == "run42"
    assert entry["schema_version"] == "fp1"
    assert entry["ai_validation"] == {"verdict": "plausible", "issues": []}
    assert entry["ts"] is not None


def test_finalize_paper_dataset_none_when_graph_construction_fails(env):
    from pipeline import store as store_mod

    bad_payload = valid_citation_payload()
    bad_payload["persistent_identifier"] = None
    store_mod.append_record(paper_id=PAPER_ID, entity_type="Citation", record_id="bad_citation", status="ready", payload=bad_payload)
    result = orchestrator.finalize_paper(PAPER_ID)
    assert result["graph_constructed"] is False
    assert result["dataset"] is None
    assert result["graph_construction_errors"]
    assert len(result["entities"]["Citation"]["ready"]) == 1


def test_finalize_paper_is_deterministic_apart_from_timestamp(env):
    from pipeline import store as store_mod

    store_mod.append_record(paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID, status="ready", payload=valid_citation_payload())
    r1 = orchestrator.finalize_paper(PAPER_ID)
    r2 = orchestrator.finalize_paper(PAPER_ID)
    r1.pop("finalized_at")
    r2.pop("finalized_at")
    assert r1 == r2


def test_finalize_paper_writes_to_results_store(env):
    from pipeline import results_store, store as store_mod

    store_mod.append_record(paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID, status="ready", payload=valid_citation_payload())
    orchestrator.finalize_paper(PAPER_ID)
    on_disk = results_store.load_result(PAPER_ID)
    assert on_disk["paper_id"] == PAPER_ID
    assert len(on_disk["entities"]["Citation"]["ready"]) == 1


def test_health_endpoint_reports_schema_fingerprint(env):
    import ir_service as svc  # reloaded fresh by the `env` fixture

    resp = TestClient(svc.app).get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "schema_fingerprint" in body and len(body["schema_fingerprint"]) == 16
