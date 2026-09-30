"""Provider failures at RECORD level use the separate provider budget.

Real evidence (run felipe_smoke_20260920T132343, Felipe-2010-Cultivar): 15 of 345 model calls surfaced as provider
failures after `invoke_agent`'s own internal retries -- 13 in record-level Extraction, 2 in enumeration. The provider budget had
given Step B a separate provider budget, but `run_record` and `run_enumeration` still counted every failed call as one
of their numbered attempts: 5 of the 6 records that ended in error had lost an attempt to a provider failure (Crop
enumeration lost 2 of its 3), so the model never got its full chance to correct a genuine problem.

Now ONE mechanism (`_ProviderBudget`, the Step B one, generalised) serves all three loops: a provider-failed round
spends `MAX_PROVIDER_FAILURE_ROUNDS` (with a growing cooldown), never a numbered attempt; a genuinely wrong answer keeps the
ordinary retry behaviour; a terminal provider failure is disclosed, as a provider failure, on the record and in the
run manifest.
"""

from __future__ import annotations

import pytest

from pipeline import orchestrator, run_store
from test_orchestrator import (  # noqa: F401  (env is a pytest fixture)
    PAPER_ID, RAW_EXTRACTION, _empty_invocation, _inv, _inv_parse_error, _malformed_invocation, env,
    make_invoke_sequence, valid_citation_payload,
)

SHAPE_BAD = {  # a genuine, correctable shape failure: a fact with no anchors
    "paper_id": PAPER_ID, "entity_type": "Citation", "record_id": PAPER_ID,
    "facts": [{"field_name": "title", "raw_value": "A Title", "raw_text_excerpt": "A Title", "anchors": []}],
}
KEY = "Citation__" + PAPER_ID


def _run(env, sequence, run_id="run1"):
    return orchestrator.run_record(
        run_id=run_id, paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID, model="test-model",
        client=env["client"], invoke=make_invoke_sequence(sequence), enable_ai_validation=False,
    )


def _extractor(inv):
    return ("extractor", inv)


@pytest.fixture(autouse=True)
def _clean_log():
    orchestrator._PROVIDER_FAILURE_LOG.clear()
    yield
    orchestrator._PROVIDER_FAILURE_LOG.clear()


# --------------------------------------------------------------------- #
# record-level Extraction
# --------------------------------------------------------------------- #


def test_a_provider_failure_does_not_consume_a_numbered_extraction_attempt(env):
    """empty, bad, bad, good: before the fix the empty call was attempt 1 and the record errored; now the model still
    gets all three of its own attempts (bad, bad, good) and the record is ready."""
    result = _run(env, [
        _extractor(_empty_invocation()),
        _extractor(_inv("extractor", SHAPE_BAD)),
        _extractor(_inv("extractor", SHAPE_BAD)),
        _extractor(_inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", valid_citation_payload())),
    ])
    assert result.status == "ready"
    manifest = run_store.load_json(run_store.record_dir("run1", KEY) / "record_manifest.json")
    assert manifest["attempts"]["extraction"] == 4          # four invocations ...
    assert manifest["attempts"]["provider_failure_rounds"] == 1   # ... one of them a provider round
    first = run_store.load_json(run_store.record_dir("run1", KEY) / "extraction" / "attempt1.json")
    assert first["failure_kind"] == "provider" and first["failure_class"] == "provider_empty" and first["numbered_attempt"] is None
    # the model got no "feedback" about the empty round: the retry prompt carries no error list from it
    assert first["validation_errors"][0]["message"] == "no final assistant text found in agent output"


def test_genuine_content_failures_still_use_exactly_the_ordinary_attempts(env):
    result = _run(env, [_extractor(_inv("extractor", SHAPE_BAD))] * orchestrator.MAX_EXTRACTION_ATTEMPTS)
    assert result.status == "error"
    assert "failure_kind" not in result.detail and "failure_class" not in result.detail
    manifest = run_store.load_json(run_store.record_dir("run1", KEY) / "record_manifest.json")
    assert manifest["attempts"]["extraction"] == orchestrator.MAX_EXTRACTION_ATTEMPTS
    assert "provider_failure_rounds" not in manifest["attempts"]      # nothing provider-related is claimed


def test_invalid_json_text_is_the_models_own_answer_and_stays_a_numbered_attempt(env):
    result = _run(env, [_extractor(_inv_parse_error("extractor"))] * orchestrator.MAX_EXTRACTION_ATTEMPTS)
    assert result.status == "error" and "failure_kind" not in result.detail


def test_a_spent_provider_budget_ends_the_record_as_a_disclosed_provider_failure(env):
    n = orchestrator.MAX_PROVIDER_FAILURE_ROUNDS
    result = _run(env, [_extractor(_empty_invocation()), _extractor(_malformed_invocation())] + [_extractor(_empty_invocation())] * (n - 2))
    assert result.status == "error"
    d = result.detail
    assert d["failure_kind"] == "provider" and d["numbered_attempts"] == 0
    assert d["provider_failure_rounds"] == n
    assert d["failure_classes"] == ["provider_empty", "provider_malformed"] + ["provider_empty"] * (n - 2)
    assert d["failure_class"] == "provider_malformed_response"   # the existing label is unchanged
    manifest = run_store.load_json(run_store.record_dir("run1", KEY) / "record_manifest.json")
    assert manifest["failure_kind"] == "provider" and manifest["provider_failure_rounds"] == n


def test_a_genuine_failure_plus_a_spent_provider_budget_is_not_called_a_provider_failure_only(env):
    n = orchestrator.MAX_PROVIDER_FAILURE_ROUNDS
    result = _run(env, [_extractor(_empty_invocation()), _extractor(_inv("extractor", SHAPE_BAD))] + [_extractor(_empty_invocation())] * (n - 1))
    assert result.status == "error"
    assert "failure_class" not in result.detail                  # a real content failure rules out the noise label
    assert result.detail["failure_kind"] == "provider"           # ...but the loop did end because the provider gave out
    assert result.detail["numbered_attempts"] == 1 and result.detail["provider_failure_rounds"] == n


def test_the_cooldown_runs_between_provider_rounds_only(env, monkeypatch):
    sleeps = []
    monkeypatch.setattr(orchestrator.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(orchestrator, "TABLE_PROVIDER_COOLDOWN_SECONDS", 7)
    _run(env, [_extractor(_empty_invocation()), _extractor(_inv("extractor", RAW_EXTRACTION)),
               ("converter", _inv("converter", valid_citation_payload()))])
    assert sleeps == [7]                                          # one provider round -> one cooldown, none after the answer


def test_a_missing_executable_is_terminal_at_once(env):
    missing = orchestrator.AgentInvocation(
        agent="extractor", model="m", prompt="p", returncode=-1, stdout="", stderr="",
        final_text=None, parsed_json=None, parse_error="opencode executable not found: x")
    result = _run(env, [_extractor(missing)])
    assert result.status == "error" and result.detail["provider_failure_rounds"] == 1
    assert result.detail["failure_classes"] == ["provider_unavailable"]


# --------------------------------------------------------------------- #
# enumeration
# --------------------------------------------------------------------- #

GOOD_ENUM = {"entity_type": "Variable", "candidates": [
    {"candidate_id": "lai", "description": "Leaf area index.", "anchors": ["b:0001"]}]}
BAD_ENUM = {"entity_type": "Variable", "candidates": [{"candidate_id": "lai"}]}   # shape failure: the model's own


def _enumerate(env, sequence, run_id="run1"):
    return orchestrator.run_enumeration(
        run_id=run_id, paper_id=PAPER_ID, entity_type="Variable", model="test-model",
        invoke=make_invoke_sequence([("extractor", i) for i in sequence]),
    )


def test_a_provider_failure_does_not_consume_a_numbered_enumeration_attempt(env):
    """Real Felipe Crop enumeration: no-text, harmony leak, then a good answer -- it survived only because the good
    answer was attempt 3. With two genuine failures in the mix the old loop would have run out; now it does not."""
    candidates, error = _enumerate(env, [_empty_invocation(), _inv("e", BAD_ENUM), _inv("e", BAD_ENUM), _inv("e", GOOD_ENUM)])
    assert error is None and [c.candidate_id for c in candidates] == ["lai"]
    first = run_store.load_json(run_store.record_dir("run1", "Variable__enumeration") / "enumeration" / "attempt1.json")
    assert first["failure_kind"] == "provider" and first["numbered_attempt"] is None


def test_genuine_enumeration_failures_keep_the_ordinary_attempts(env):
    candidates, error = _enumerate(env, [_inv("e", BAD_ENUM)] * orchestrator.MAX_ENUMERATION_ATTEMPTS)
    assert candidates == [] and error is not None
    final = run_store.load_json(run_store.record_dir("run1", "Variable__enumeration") / "final.json")
    assert final["status"] == "error" and "failure_kind" not in final


def test_a_terminal_enumeration_provider_failure_is_disclosed_as_one(env):
    n = orchestrator.MAX_PROVIDER_FAILURE_ROUNDS
    candidates, error = _enumerate(env, [_empty_invocation(), _malformed_invocation()] + [_empty_invocation()] * (n - 2))
    assert candidates == [] and "provider failure" in error
    final = run_store.load_json(run_store.record_dir("run1", "Variable__enumeration") / "final.json")
    assert final["failure_kind"] == "provider" and final["provider_failure_rounds"] == n and final["numbered_attempts"] == 0
    assert final["failure_classes"] == ["provider_empty", "provider_malformed"] + ["provider_empty"] * (n - 2)


def test_the_run_summary_counts_provider_rounds_by_stage_and_names_terminal_loops(env):
    n = orchestrator.MAX_PROVIDER_FAILURE_ROUNDS
    _run(env, [_extractor(_empty_invocation())] * n)                                                # terminal extraction
    _enumerate(env, [_empty_invocation(), _inv("e", GOOD_ENUM)])                                    # absorbed, not terminal
    summary = orchestrator.summarize_provider_failures("run1")
    assert summary["total_rounds"] == n + 1
    assert summary["by_stage"] == {"extraction": n, "enumeration": 1}
    assert summary["by_class"] == {"provider_empty": n + 1}
    assert summary["terminal"] == [{"stage": "extraction", "record_key": KEY, "failure_class": "provider_empty"}]
    assert summary["outage_mode"] == []                                                              # nothing was fast-failed
    assert orchestrator.summarize_provider_failures("run1")["total_rounds"] == 0                   # popped: never leaks across runs


def test_the_completed_run_manifest_carries_provider_failures(env):
    from test_orchestrator import _build_run_paper_invoke_sequence

    outcome = orchestrator.run_paper(
        paper_id=PAPER_ID, model="test-model", client=env["client"],
        invoke=make_invoke_sequence(_build_run_paper_invoke_sequence()), enable_ai_validation=False,
    )
    manifest = run_store.load_run_manifest(outcome["run_id"])
    assert manifest["provider_failures"] == {"total_rounds": 0, "by_stage": {}, "by_class": {}, "terminal": [], "outage_mode": [],
                                             "model_no_answer": {"total_rounds": 0, "by_stage": {}, "terminal": []}}
