"""No-answer turns on every stage: a stall is not an outage, and only an outage is waited out.

Real evidence (Felipe-2010-Cultivar, run 20260923T132453_7595c3bf): 4 records spent 44 of the run's 111 minutes in
provider cooldowns (20/60/180/300 s) after failed calls that each lasted ~0.5 s. Of the 19 "provider failure" rounds, 12
were STALLS -- the model used a tool, the tool answered, and the model's next turn produced ~100 output tokens that never
arrived as text, with identical token counts round after round (Variable plant_nutrient_content: 125 then 107, every
round). Repeating the identical prompt after a wait only reproduced it. Now:
  - a stall, on any stage (extraction, enumeration, Step B, conversion), gets up to MAX_FINAL_ANSWER_RETRIES immediate
    retries with a short "answer now" instruction, outside the provider budget;
  - a provider-classed failure is waited out only with evidence of an outage (`_outage_evidence`): a timeout, a missing
    executable, a provider error other than the harmony-format defect, or no response at all. A harmony leak or a clean
    empty turn is retried at once, asking for the answer directly.

Fixture (real, tool outputs trimmed): tests/fixtures/no_answer/felipe_no_answer_rounds_20260923T132453_7595c3bf.json --
every extraction round of Variable plant_nutrient_content, Variable harvest_index and Coverage disease_severity_score.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import orchestrator, run_store
from test_orchestrator import PAPER_ID, RAW_EXTRACTION, _inv, env, make_invoke_sequence, valid_citation_payload  # noqa: F401

RECORDED = json.loads((Path(__file__).parent / "fixtures" / "no_answer" / "felipe_no_answer_rounds_20260923T132453_7595c3bf.json").read_text())
MAX = orchestrator.MAX_FINAL_ANSWER_RETRIES


@pytest.fixture(autouse=True)
def _fresh_logs():
    orchestrator._PROVIDER_FAILURE_LOG.clear()
    orchestrator._STALL_LOG.clear()


@pytest.fixture()
def sleeps(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr(orchestrator.time, "sleep", lambda seconds: waited.append(seconds))
    return waited


def _recorded(key: str, index: int) -> orchestrator.AgentInvocation:
    fields = {k: v for k, v in RECORDED[key][index].items() if k != "recorded_failure_class"}
    return orchestrator.AgentInvocation(**fields)


def _events(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events)


TOOL_THEN_STOP = _events(
    {"type": "step_start", "part": {"type": "step-start"}},
    {"type": "tool_use", "part": {"type": "tool", "tool": "read_document_start", "state": {"status": "completed", "input": {}, "output": "..."}}},
    {"type": "step_finish", "part": {"type": "step-finish", "reason": "tool-calls"}},
    {"type": "step_start", "part": {"type": "step-start"}},
    {"type": "step_finish", "part": {"type": "step-finish", "reason": "stop"}},
)
CLEAN_EMPTY_TURN = _events(
    {"type": "step_start", "part": {"type": "step-start"}},
    {"type": "step_finish", "part": {"type": "step-finish", "reason": "stop"}},
)


def _no_text(stdout: str = "", *, returncode: int = 0, stderr: str = "", malformed: bool = False) -> orchestrator.AgentInvocation:
    return orchestrator.AgentInvocation(
        agent="extractor", model="test-model", prompt="p", returncode=returncode, stdout=stdout, stderr=stderr,
        final_text=None, parsed_json=None, parse_error="no final assistant text found in agent output",
        had_malformed_tool_call=malformed,
    )


def _error_event(message: str) -> str:
    return _events({"type": "step_start", "part": {"type": "step-start"}},
                   {"type": "error", "error": {"name": "UnknownError", "data": {"message": message}}})


def _recording(sequence):
    inner = make_invoke_sequence(sequence)
    calls: list[tuple[str, str]] = []

    def invoke(agent, model, prompt, timeout=300):
        calls.append((agent, prompt))
        return inner(agent, model, prompt, timeout)

    return invoke, calls


def _src(anchor):
    return {"source_document_id": PAPER_ID, "section_path": [], "locators": [{"kind": "text", "block_anchor": anchor}]}


VARIABLE_RAW = {"paper_id": PAPER_ID, "entity_type": "Variable", "record_id": "v", "facts": [
    {"field_name": "name", "raw_value": "summary statistic", "raw_text_excerpt": "Reported as a treatment_mean summary statistic.", "anchors": ["b:0004"]}]}
VARIABLE_PAYLOAD = {"id": "v", "name": {"value": "summary statistic", "provenance_label": "EXTRACTED", "source": _src("b:0004")}}


def _variable_record(env, invoke, run_id="run1"):
    return orchestrator.run_record(run_id=run_id, paper_id=PAPER_ID, entity_type="Variable", record_id="v", model="test-model",
                                   client=env["client"], invoke=invoke, enable_ai_validation=False)


# --------------------------------------------------------------------- #
# what counts as an outage
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("invocation, outage", [
    (None, True),                                                                                   # nothing to judge: wait
    (_no_text("", returncode=-1, stderr="\n[orchestrator] timed out after 300s"), True),            # timeout
    (_no_text(""), True),                                                                           # no response at all
    (_no_text("{}"), True),                                                                         # no typed events either
    (_no_text(_error_event("litellm.RateLimitError: 429 Too Many Requests")), True),                # a real provider error
    (_no_text(_error_event("litellm.BadRequestError: Hosted_vllmException - unexpected tokens remaining in message header")), False),
    (_no_text("anything", malformed=True), False),                                                  # harmony leak in a tool name
    (_no_text(CLEAN_EMPTY_TURN), False),                                                            # provider answered, no text
    (_no_text(TOOL_THEN_STOP), False),                                                              # a stall
])
def test_outage_evidence(invocation, outage):
    assert orchestrator._outage_evidence(invocation) is outage


def test_the_recorded_felipe_rounds_are_stalls_and_harmony_leaks_not_outages():
    stalls = [RECORDED["variable_plant_nutrient_content"][i] for i in (0, 1, 2, 3)] + RECORDED["coverage_disease_severity"][:4]
    assert all(r["recorded_failure_class"] == "provider_empty" for r in stalls)                    # what they were counted as
    for key, index in [("variable_plant_nutrient_content", i) for i in range(4)] + [("coverage_disease_severity", i) for i in range(4)]:
        assert orchestrator._ended_without_answer_after_tools(_recorded(key, index))                # what they are
    for index, record in enumerate(RECORDED["variable_harvest_index"]):
        if record["recorded_failure_class"] == "provider_malformed":
            assert not orchestrator._outage_evidence(_recorded("variable_harvest_index", index))    # never worth a wait


# --------------------------------------------------------------------- #
# record-level extraction
# --------------------------------------------------------------------- #


def test_replay_plant_nutrient_content_no_longer_waits_and_the_answer_now_retry_can_succeed(env, sleeps):
    """Before: 5 rounds, 20+60+180+300 s of cooldowns, the identical prompt each time, error. Now: the recorded stalls are
    retried at once with the answer-now instruction; here the model answers the second one."""
    invoke, calls = _recording([("extractor", _recorded("variable_plant_nutrient_content", 0)),
                                ("extractor", _recorded("variable_plant_nutrient_content", 1)),
                                ("extractor", _inv("extractor", VARIABLE_RAW)), ("converter", _inv("converter", VARIABLE_PAYLOAD))])
    result = _variable_record(env, invoke)
    assert result.status == "ready" and sleeps == []
    nudge = orchestrator._final_answer_nudge("extraction", "Variable")
    assert nudge not in calls[0][1] and nudge in calls[1][1] and nudge in calls[2][1]
    summary = orchestrator.summarize_provider_failures("run1")
    assert summary["total_rounds"] == 0 and summary["model_no_answer"]["total_rounds"] == 2


def test_replay_of_all_recorded_stalls_is_bounded_quick_and_disclosed_as_the_models(env, sleeps):
    stall_rounds = [("extractor", _recorded("variable_plant_nutrient_content", i % 4)) for i in range(MAX + 1)]
    invoke, calls = _recording(stall_rounds)
    result = _variable_record(env, invoke)
    assert result.status == "error" and result.detail["failure_class"] == "no_final_answer"
    assert len(calls) == MAX + 1 and sleeps == []                                                  # was 560 s of cooldowns
    assert orchestrator.summarize_provider_failures("run1")["model_no_answer"]["terminal"] == [
        {"stage": "extraction", "record_key": "Variable__v"}]


def test_a_coverage_record_that_answered_on_its_fifth_round_still_can(env, sleeps):
    """The replayed Felipe Coverage record answered after four no-answer turns: the patience is kept, the waiting is not."""
    sequence = [("extractor", _recorded("coverage_disease_severity", i)) for i in range(4)]
    invoke, calls = _recording(sequence + [("extractor", _inv("extractor", VARIABLE_RAW)), ("converter", _inv("converter", VARIABLE_PAYLOAD))])
    assert _variable_record(env, invoke).status == "ready" and sleeps == [] and len(calls) == 6


def test_a_harmony_leak_is_retried_at_once_asking_for_the_answer(env, sleeps):
    invoke, calls = _recording([("extractor", _recorded("variable_harvest_index", 0)),
                                ("extractor", _inv("extractor", VARIABLE_RAW)), ("converter", _inv("converter", VARIABLE_PAYLOAD))])
    result = _variable_record(env, invoke)
    assert result.status == "ready" and sleeps == []
    assert orchestrator._final_answer_nudge("extraction", "Variable") in calls[1][1]
    assert orchestrator.summarize_provider_failures("run1")["by_class"] == {"provider_malformed": 1}   # still a provider class


def test_a_timeout_is_still_waited_out_and_the_same_prompt_repeated(env, sleeps):
    timeout = _no_text("", returncode=-1, stderr="\n[orchestrator] timed out after 300s")
    invoke, calls = _recording([("extractor", timeout), ("extractor", _inv("extractor", VARIABLE_RAW)),
                                ("converter", _inv("converter", VARIABLE_PAYLOAD))])
    assert _variable_record(env, invoke).status == "ready"
    assert sleeps == [orchestrator.provider_cooldown_seconds(1)] and calls[0][1] == calls[1][1]


def test_a_real_provider_error_event_is_waited_out(env, sleeps):
    invoke, _ = _recording([("extractor", _no_text(_error_event("litellm.APIConnectionError: connection reset"))),
                            ("extractor", _inv("extractor", VARIABLE_RAW)), ("converter", _inv("converter", VARIABLE_PAYLOAD))])
    assert _variable_record(env, invoke).status == "ready" and sleeps == [orchestrator.provider_cooldown_seconds(1)]


# --------------------------------------------------------------------- #
# enumeration and conversion
# --------------------------------------------------------------------- #

GOOD_ENUMERATION = {"entity_type": "Variable", "candidates": [{"candidate_id": "lai", "description": "Leaf area index.", "anchors": ["b:0001"]}]}


def test_enumeration_stall_gets_the_answer_now_retry(env, sleeps):
    invoke, calls = _recording([("extractor", _no_text(TOOL_THEN_STOP)), ("extractor", _inv("extractor", GOOD_ENUMERATION))])
    candidates, error = orchestrator.run_enumeration(run_id="run1", paper_id=PAPER_ID, entity_type="Variable", model="test-model", invoke=invoke)
    assert error is None and [c.candidate_id for c in candidates] == ["lai"] and sleeps == []
    assert orchestrator._final_answer_nudge("enumeration") in calls[1][1]
    attempt1 = run_store.load_json(run_store.record_dir("run1", "Variable__enumeration") / "enumeration" / "attempt1.json")
    assert attempt1["failure_class"] == "no_final_answer" and attempt1["numbered_attempt"] is None


def test_enumeration_stalls_end_as_no_final_answer(env, sleeps):
    invoke, calls = _recording([("extractor", _no_text(TOOL_THEN_STOP))] * (MAX + 1))
    candidates, error = orchestrator.run_enumeration(run_id="run1", paper_id=PAPER_ID, entity_type="Variable", model="test-model", invoke=invoke)
    assert candidates == [] and error and sleeps == []
    final = run_store.load_json(run_store.record_dir("run1", "Variable__enumeration") / "final.json")
    assert final["failure_class"] == "no_final_answer" and final["failure_kind"] == "extraction"


def test_a_conversion_stall_keeps_the_real_feedback_and_asks_for_the_answer(env, sleeps):
    """A no-answer conversion round used to replace the previous round's real validation errors with a parse error."""
    bad = valid_citation_payload()
    bad["year"]["value"] = 1999                                                       # not in b:0002 -> provenance error
    invoke, calls = _recording([
        ("extractor", _inv("extractor", RAW_EXTRACTION)),
        ("converter", _inv("converter", bad)),
        ("converter", orchestrator.AgentInvocation(agent="converter", model="test-model", prompt="p", returncode=0, stdout=TOOL_THEN_STOP,
                                                   stderr="", final_text=None, parsed_json=None, parse_error="no final assistant text found in agent output")),
        ("converter", _inv("converter", valid_citation_payload())),
    ])
    result = orchestrator.run_record(run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID, model="test-model",
                                     client=env["client"], invoke=invoke, enable_ai_validation=False)
    assert result.status == "ready" and sleeps == []
    third = calls[3][1]
    assert "1999" in third and "provenance_value_mismatch" in third                   # the real feedback survived the stall
    assert orchestrator._final_answer_nudge("conversion") in third
    manifest = run_store.load_json(run_store.record_dir("run1", "Citation__" + PAPER_ID) / "record_manifest.json")
    assert manifest["attempts"]["conversion_no_answer_rounds"] == 1
