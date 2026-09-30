"""Item 9: provider failures in Step B (`run_table_classification` / the pass).

Real evidence (live Step B checks): 65% of raw invocations in the first pass came back with no final
text, a later regime hit the 300 s limit, and gpt-oss leaked a harmony token into tool-call names.
Before this item such a failure (a) consumed one of the 3 numbered attempts although the model gave
no answer to correct, (b) was fed back to the model as if it were a validation error, (c) was never
cached, so Observation's pass repeated Treatment's failed tables, and (d) was recorded nowhere as a
class. Now every failed attempt carries a `failure_class`; provider classes have their own small
budget with one cooldown round; genuine failures use only the numbered attempts; a terminal failure
is cached for the run and disclosed in the manifest as `table_pass_failures`.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from pipeline import orchestrator, run_store

PAPER = "syn"
KEY = "table_classification__b_0001"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    papers = tmp_path / "papers"
    (papers / PAPER).mkdir(parents=True)
    (papers / PAPER / "content.md").write_text("| a | 1.0 |\n|---|---|\n| b | 2.0 |\n⟦b:0001⟧\n", encoding="utf-8")
    (papers / PAPER / "provenance.json").write_text(json.dumps(
        {"b:0001": {"block_type": "Table", "page_id": "page_1", "section_path": [], "rendered_in_content_md": True}}), encoding="utf-8")
    monkeypatch.setenv("IR_PAPERS_ROOT", str(papers))
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))
    return tmp_path


def _inv(*, final_text="x", parsed=None, parse_error=None, returncode=0, stderr="", malformed=False):
    return orchestrator.AgentInvocation(
        agent="extractor", model="m", prompt="p", returncode=returncode, stdout="", stderr=stderr,
        final_text=final_text, parsed_json=parsed, parse_error=parse_error, had_malformed_tool_call=malformed,
    )


EMPTY = lambda: _inv(final_text=None, parse_error="no final assistant text found in agent output")  # noqa: E731
TIMEOUT = lambda: _inv(final_text=None, returncode=-1, stderr="\n[orchestrator] timed out after 300s",  # noqa: E731
                       parse_error="no final assistant text found in agent output")
MALFORMED = lambda: _inv(final_text="{}", malformed=True,  # noqa: E731
                         parse_error="provider_malformed_response: harmony-format tool-call name leak detected")
NOT_JSON = lambda: _inv(final_text="sorry, no table here", parse_error="no JSON object found")  # noqa: E731


def _valid(anchor="b:0001") -> dict:
    return {
        "applicable": True, "table_anchors": [anchor],
        "value_columns": [{"value_column_id": "v", "variable_name_hint": "y"}],
        "row_groups": [{"row_group_id": "r1", "factor_values": {"k": "a"}, "source_table_anchor": anchor, "cells": {"v": "1.0"}},
                       {"row_group_id": "r2", "factor_values": {"k": "b"}, "source_table_anchor": anchor, "cells": {"v": "2.0"}}],
    }


def _answer(payload):
    return _inv(final_text=json.dumps(payload), parsed=payload)


def _script(items):
    """A fake invoke returning the scripted invocations in order; records every prompt."""
    prompts, it = [], iter(items)

    def invoke(agent, model, prompt, timeout=300):
        prompts.append(prompt)
        try:
            return next(it)()
        except StopIteration:
            raise AssertionError(f"invoke called more than scripted ({len(prompts)} calls)")
    return invoke, prompts


def _classify(invoke, run_id="r1"):
    return orchestrator.run_table_classification(
        run_id=run_id, paper_id=PAPER, seed_table_anchor="b:0001", other_tables=[], model="m", invoke=invoke)


def _artifacts(run_id="r1"):
    d = run_store.record_dir(run_id, KEY) / "table_classification"
    return [json.loads(p.read_text()) for p in sorted(d.glob("attempt*.json"), key=lambda p: int(p.stem.removeprefix("attempt")))]


def _final(run_id="r1"):
    return json.loads((run_store.record_dir(run_id, KEY) / "final.json").read_text())


# --------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("make, expected", [
    (EMPTY, "provider_empty"), (TIMEOUT, "provider_timeout"), (MALFORMED, "provider_malformed"), (NOT_JSON, "invalid_json"),
    (lambda: _inv(final_text=None, parse_error="opencode executable not found: [Errno 2]", returncode=-1), "provider_unavailable"),
    (lambda: _answer({"a": 1}), None),
])
def test_invocation_failures_are_classified(make, expected):
    assert orchestrator.classify_invocation_failure(make()) == expected


def test_a_real_subprocess_timeout_is_classified_as_a_timeout_not_an_empty_response(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="opencode", timeout=5, output=b"partial", stderr=b"")
    monkeypatch.setattr(orchestrator.subprocess, "run", boom)
    result = orchestrator._invoke_agent_once("extractor", "m", "prompt", 5)
    assert orchestrator.classify_invocation_failure(result) == "provider_timeout"
    assert result.stdout == "partial"  # the bytes stdout of a timed-out call is still decoded


def test_provider_classes_are_exactly_the_no_answer_classes():
    assert orchestrator.PROVIDER_FAILURE_CLASSES == {"provider_empty", "provider_timeout", "provider_malformed", "provider_unavailable"}
    assert {"invalid_json", "schema_invalid", "validation_failure"} <= orchestrator.FAILURE_CLASSES - orchestrator.PROVIDER_FAILURE_CLASSES


# --------------------------------------------------------------------- #
# provider failures: their own bounded budget, one cooldown round
# --------------------------------------------------------------------- #


def test_empty_responses_are_bounded_classified_and_never_consume_a_numbered_attempt(env):
    n = orchestrator.MAX_PROVIDER_FAILURE_ROUNDS
    invoke, prompts = _script([EMPTY] * (n + 3))
    result, error = _classify(invoke)
    assert result is None and "provider failure (provider_empty)" in error
    assert len(prompts) == n  # the provider budget, not the 3 numbered attempts
    artifacts = _artifacts()
    assert [a["failure_class"] for a in artifacts] == ["provider_empty"] * n
    assert [a["numbered_attempt"] for a in artifacts] == [None] * n and {a["failure_kind"] for a in artifacts} == {"provider"}
    final = _final()
    assert (final["status"], final["failure_class"], final["failure_kind"]) == ("error", "provider_empty", "provider")
    assert (final["numbered_attempts"], final["provider_failure_rounds"], final["failure_classes"]) == (0, n, ["provider_empty"] * n)


def test_a_provider_failure_does_not_use_up_the_genuine_attempts(env):
    bad = {"applicable": True, "table_anchors": ["b:0001"], "value_columns": "nope", "row_groups": []}
    invoke, prompts = _script([EMPTY, lambda: _answer(bad), lambda: _answer(bad), lambda: _answer(bad)])
    result, error = _classify(invoke)
    assert result is None and len(prompts) == 1 + orchestrator.MAX_TABLE_CLASSIFICATION_ATTEMPTS  # all 3 numbered attempts still ran
    final = _final()
    assert (final["numbered_attempts"], final["provider_failure_rounds"], final["failure_kind"]) == (3, 1, "extraction")
    assert final["failure_classes"] == ["provider_empty", "schema_invalid", "schema_invalid", "schema_invalid"]


def test_a_provider_failure_is_retried_with_the_identical_prompt_not_as_validation_feedback(env):
    invoke, prompts = _script([EMPTY, lambda: _answer(_valid())])
    result, error = _classify(invoke)
    assert error is None and result is not None and len(prompts) == 2
    assert prompts[1] == prompts[0] and "failed validation" not in prompts[1] and "no final assistant text" not in prompts[1]
    assert [a["failure_class"] for a in _artifacts()] == ["provider_empty", None]  # the success is recorded with no class
    assert _artifacts()[1]["numbered_attempt"] == 1


def test_provider_feedback_never_replaces_genuine_feedback(env):
    """A schema error followed by a provider failure: the retry still carries the schema error."""
    bad = {"applicable": True, "table_anchors": ["b:0001"], "value_columns": "nope", "row_groups": []}
    invoke, prompts = _script([lambda: _answer(bad), TIMEOUT, lambda: _answer(_valid())])
    result, _ = _classify(invoke)
    assert result is not None and "failed validation" in prompts[1] and prompts[2] == prompts[1]


def test_the_cooldown_grows_between_provider_rounds_and_never_follows_the_last(env, monkeypatch):
    n = orchestrator.MAX_PROVIDER_FAILURE_ROUNDS
    sleeps = []
    monkeypatch.setattr(orchestrator.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(orchestrator, "TABLE_PROVIDER_COOLDOWN_SECONDS", 7)
    invoke, _ = _script([TIMEOUT, MALFORMED] + [TIMEOUT] * (n - 2))
    result, _ = _classify(invoke)
    # a wait after every TIMEOUT round except the last; none after the harmony leak (round 2: not an outage)
    assert result is None and sleeps == [orchestrator.provider_cooldown_seconds(k) for k in range(1, n) if k != 2]
    assert sleeps[0] == 7 and sleeps == sorted(sleeps)
    assert _final()["failure_classes"] == ["provider_timeout", "provider_malformed"] + ["provider_timeout"] * (n - 2)
    assert _final()["failure_kind"] == "provider"


def test_a_missing_executable_is_terminal_at_once_with_no_cooldown(env, monkeypatch):
    monkeypatch.setattr(orchestrator.time, "sleep", lambda s: pytest.fail("no cooldown for a missing binary"))
    invoke, prompts = _script([lambda: _inv(final_text=None, parse_error="opencode executable not found: x", returncode=-1)] * 3)
    result, _ = _classify(invoke)
    assert result is None and len(prompts) == 1 and _final()["failure_class"] == "provider_unavailable"


# --------------------------------------------------------------------- #
# genuine failures: numbered attempts only
# --------------------------------------------------------------------- #


def test_schema_invalid_answers_use_only_the_numbered_attempts_with_no_cooldown(env, monkeypatch):
    monkeypatch.setattr(orchestrator.time, "sleep", lambda s: pytest.fail("a genuine failure never waits"))
    bad = {"applicable": True, "table_anchors": ["b:0001"], "value_columns": "nope", "row_groups": []}
    invoke, prompts = _script([lambda: _answer(bad)] * 5)
    assert _classify(invoke)[0] is None
    assert len(prompts) == orchestrator.MAX_TABLE_CLASSIFICATION_ATTEMPTS
    final = _final()
    assert (final["failure_kind"], final["provider_failure_rounds"], final["numbered_attempts"]) == ("extraction", 0, 3)
    assert [a["numbered_attempt"] for a in _artifacts()] == [1, 2, 3] and {a["failure_class"] for a in _artifacts()} == {"schema_invalid"}
    assert "failed validation" in prompts[1]  # a genuine failure IS fed back


def test_invalid_json_text_is_the_models_failure_and_counts_as_an_attempt(env):
    invoke, prompts = _script([NOT_JSON] * 5)
    assert _classify(invoke)[0] is None and len(prompts) == orchestrator.MAX_TABLE_CLASSIFICATION_ATTEMPTS
    assert _final()["failure_class"] == "invalid_json" and _final()["failure_kind"] == "extraction"


def test_a_grounding_or_anchor_failure_is_a_validation_failure(env):
    invoke, prompts = _script([lambda: _answer(_valid("b:9999"))] * 5)
    assert _classify(invoke)[0] is None and len(prompts) == orchestrator.MAX_TABLE_CLASSIFICATION_ATTEMPTS
    assert {a["failure_class"] for a in _artifacts()} == {"validation_failure"}
    assert _final()["failure_kind"] == "extraction"


# --------------------------------------------------------------------- #
# caching
# --------------------------------------------------------------------- #


def test_a_valid_non_treatment_answer_is_a_success_and_is_cached_with_no_retry(env):
    weather = {"table_role": "weather_context", "reason": "monthly temperature and rainfall per location", "table_anchors": ["b:0001"],
               "value_columns": [], "row_groups": []}
    invoke, prompts = _script([lambda: _answer(weather)])
    first, error = _classify(invoke)
    assert error is None and first.table_role == "weather_context" and len(prompts) == 1
    assert _final()["status"] == "success"
    again, error = _classify(lambda *a, **k: pytest.fail("a cached success must not call the model"))
    assert error is None and again.table_role == "weather_context"
    # and the pass (Treatment's turn, then Observation's) makes no second call either
    out = orchestrator.run_table_classification_pass(run_id="r1", paper_id=PAPER, model="m", invoke=lambda *a, **k: pytest.fail("cached"))
    assert out == {}  # not applicable / no rows -> nothing to enumerate, and no retry


@pytest.mark.parametrize("script, kind", [([EMPTY] * orchestrator.MAX_PROVIDER_FAILURE_ROUNDS, "provider"), ([NOT_JSON] * 3, "extraction")])
def test_a_terminal_failure_is_cached_so_the_next_pass_does_not_repeat_it(env, script, kind):
    invoke, prompts = _script(script)
    first, message = _classify(invoke)
    assert first is None and _final()["failure_kind"] == kind
    calls_after_first = len(prompts)
    second, message2 = _classify(lambda *a, **k: pytest.fail("the terminal failure is cached for the run"))
    assert second is None and message2 == message and len(prompts) == calls_after_first
    out = orchestrator.run_table_classification_pass(run_id="r1", paper_id=PAPER, model="m", invoke=lambda *a, **k: pytest.fail("cached"))
    assert out == {}  # the free-form pass still gets its shot at the table


def test_a_bare_legacy_error_record_is_not_treated_as_final_and_is_retried(env):
    run_store.save_final("r1", KEY, {"status": "error", "message": "attempt 3: old failure"})
    invoke, prompts = _script([lambda: _answer(_valid())])
    result, error = _classify(invoke)
    assert error is None and result is not None and len(prompts) == 1


def test_a_fresh_run_does_not_inherit_another_runs_failure(env):
    _classify(_script([EMPTY] * orchestrator.MAX_PROVIDER_FAILURE_ROUNDS)[0], run_id="r1")
    invoke, prompts = _script([lambda: _answer(_valid())])
    assert _classify(invoke, run_id="r2")[0] is not None and len(prompts) == 1


# --------------------------------------------------------------------- #
# manifest disclosure
# --------------------------------------------------------------------- #


def test_the_manifest_summary_discloses_each_failed_table_with_its_cause_and_kind(env):
    n = orchestrator.MAX_PROVIDER_FAILURE_ROUNDS
    _classify(_script([TIMEOUT] + [MALFORMED] * (n - 1))[0])
    summary = orchestrator.summarize_table_pass("r1", PAPER, {})
    [failure] = summary["table_pass_failures"]
    assert failure["table"] == "b_0001" and failure["failure_class"] == "provider_malformed"
    assert failure["failure_kind"] == "provider" and failure["failure_classes"] == ["provider_timeout"] + ["provider_malformed"] * (n - 1)
    assert (failure["numbered_attempts"], failure["provider_failure_rounds"]) == (0, n)
    assert "provider failure (provider_malformed)" in failure["message"]


def test_a_successful_table_is_not_listed_as_a_failure_and_extraction_failures_are_kept_apart(env):
    _classify(_script([lambda: _answer(_valid())])[0], run_id="ok")
    assert orchestrator.summarize_table_pass("ok", PAPER, {})["table_pass_failures"] == []
    _classify(_script([NOT_JSON] * 3)[0], run_id="bad")
    [failure] = orchestrator.summarize_table_pass("bad", PAPER, {})["table_pass_failures"]
    assert failure["failure_kind"] == "extraction" and failure["failure_class"] == "invalid_json"


def test_the_provider_budget_and_cooldown_are_recorded_in_every_manifest():
    constants = orchestrator._run_constants()
    assert constants["MAX_PROVIDER_FAILURE_ROUNDS"] == orchestrator.MAX_PROVIDER_FAILURE_ROUNDS
    assert "TABLE_PROVIDER_COOLDOWN_SECONDS" in constants and "MAX_TABLE_CLASSIFICATION_ATTEMPTS" in constants
    assert {"PROVIDER_COOLDOWN_GROWTH", "PROVIDER_COOLDOWN_CAP_SECONDS", "MAX_CONSECUTIVE_PROVIDER_TERMINALS"} <= set(constants)


# --------------------------------------------------------------------- #
# Step B stalls (see tests/test_no_answer_handling.py for the evidence)
# --------------------------------------------------------------------- #

_STALL_STDOUT = "\n".join(json.dumps(e) for e in (
    {"type": "step_start", "part": {"type": "step-start"}},
    {"type": "tool_use", "part": {"type": "tool", "tool": "read_table", "state": {"status": "completed", "input": {}, "output": "..."}}},
    {"type": "step_finish", "part": {"type": "step-finish", "reason": "tool-calls"}},
    {"type": "step_finish", "part": {"type": "step-finish", "reason": "stop"}},
))
STALL = lambda: orchestrator.AgentInvocation(  # noqa: E731
    agent="extractor", model="m", prompt="p", returncode=0, stdout=_STALL_STDOUT, stderr="",
    final_text=None, parsed_json=None, parse_error="no final assistant text found in agent output")


def test_a_step_b_stall_is_retried_at_once_asking_for_the_classification(env, monkeypatch):
    monkeypatch.setattr(orchestrator.time, "sleep", lambda s: pytest.fail("a stall is never waited out"))
    invoke, prompts = _script([STALL, lambda: _answer(_valid())])
    result, error = _classify(invoke)
    assert error is None and result is not None
    assert orchestrator._final_answer_nudge("table_classification") in prompts[1]
    first = _artifacts()[0]
    assert first["failure_class"] == "no_final_answer" and first["numbered_attempt"] is None and first["failure_kind"] == "extraction"


def test_step_b_stalls_are_bounded_and_cached_as_the_models_failure(env, monkeypatch):
    monkeypatch.setattr(orchestrator.time, "sleep", lambda s: pytest.fail("a stall is never waited out"))
    invoke, prompts = _script([STALL] * (orchestrator.MAX_FINAL_ANSWER_RETRIES + 1))
    result, error = _classify(invoke)
    assert result is None and len(prompts) == orchestrator.MAX_FINAL_ANSWER_RETRIES + 1
    assert _final()["failure_class"] == "no_final_answer" and _final()["failure_kind"] == "extraction"
