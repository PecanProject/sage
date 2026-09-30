"""Provider resilience: a burst of empty / harmony-malformed responses must not kill a record, and a real outage must not
multiply the waiting across the whole run.

Real evidence (stored call timestamps of six live Felipe runs, 2026-09-20/21): the provider fails in short bursts that
recur through a run -- each ended within 0.5 to 5.7 minutes -- while a record's provider patience was two rounds and one
20 s cooldown (about two minutes). Records that met a burst died in the middle of it, and a dead Citation blocks every later
entity. Now a loop waits out a burst (5 rounds, cooldowns 20/60/180/300 s), and after
MAX_CONSECUTIVE_PROVIDER_TERMINALS loops in a row end on a spent budget with no successful call in between, further loops
get one round each until any call succeeds -- so a genuine outage costs minutes, not hours.

Numbered attempts, prompts, extraction/conversion logic and every validation rule are untouched (their tests pin that).
"""

from __future__ import annotations

import pytest

from pipeline import orchestrator, run_store
from test_orchestrator import (  # noqa: F401  (env is a pytest fixture)
    PAPER_ID, RAW_EXTRACTION, _build_run_paper_invoke_sequence, _empty_invocation, _inv, _malformed_invocation, env,
    make_invoke_sequence, valid_citation_payload,
)

N = orchestrator.MAX_PROVIDER_FAILURE_ROUNDS
KEY = "Citation__" + PAPER_ID
LONGEST_OBSERVED_BURST_SECONDS = 5.7 * 60     # stored timestamps of the six live runs


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(orchestrator, "TABLE_PROVIDER_COOLDOWN_SECONDS", 20)   # the production base (conftest zeroes it)
    orchestrator._PROVIDER_FAILURE_LOG.clear()
    orchestrator._PROVIDER_OUTAGE.clear()
    yield
    orchestrator._PROVIDER_FAILURE_LOG.clear()
    orchestrator._PROVIDER_OUTAGE.clear()


@pytest.fixture()
def sleeps(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr(orchestrator.time, "sleep", lambda seconds: waited.append(seconds))
    return waited


def _record(env, sequence, run_id="run1"):
    return orchestrator.run_record(
        run_id=run_id, paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID, model="test-model",
        client=env["client"], invoke=make_invoke_sequence(sequence), enable_ai_validation=False,
    )


def _good_record_tail():
    return [("extractor", _inv("extractor", RAW_EXTRACTION)), ("converter", _inv("converter", valid_citation_payload()))]


# --------------------------------------------------------------------- #
# the schedule
# --------------------------------------------------------------------- #


def test_the_cooldown_grows_and_is_capped():
    assert [orchestrator.provider_cooldown_seconds(k) for k in range(1, 7)] == [20, 60, 180, 300, 300, 300]
    assert orchestrator.provider_cooldown_seconds(0) == 20                     # never below the base


def test_one_loops_patience_outlasts_the_longest_burst_seen_in_live_runs():
    waits = sum(orchestrator.provider_cooldown_seconds(k) for k in range(1, N))
    assert N >= 4 and waits > LONGEST_OBSERVED_BURST_SECONDS


# --------------------------------------------------------------------- #
# a burst is survived, by every loop that calls a model
# --------------------------------------------------------------------- #


def test_a_record_survives_a_burst_of_empty_and_malformed_rounds_and_uses_no_numbered_attempt(env, sleeps):
    burst = [("extractor", _empty_invocation()) if i % 2 == 0 else ("extractor", _malformed_invocation()) for i in range(N - 1)]
    result = _record(env, burst + _good_record_tail())
    assert result.status == "ready"
    manifest = run_store.load_json(run_store.record_dir("run1", KEY) / "record_manifest.json")
    assert manifest["attempts"]["provider_failure_rounds"] == N - 1 and manifest["attempts"]["extraction"] == N   # N-1 failed + the answer
    assert sleeps == [orchestrator.provider_cooldown_seconds(k) for k in range(1, N)] [: N - 1]
    assert sum(sleeps) > 60


def test_a_burst_one_round_longer_than_the_budget_is_still_a_disclosed_provider_failure(env, sleeps):
    result = _record(env, [("extractor", _empty_invocation())] * N)
    assert result.status == "error" and result.detail["failure_kind"] == "provider"
    assert result.detail["numbered_attempts"] == 0 and result.detail["provider_failure_rounds"] == N
    assert len(sleeps) == N - 1                                                # no wait after the final failed round


def test_enumeration_survives_a_burst(env, sleeps):
    good = {"entity_type": "Variable", "candidates": [{"candidate_id": "lai", "description": "Leaf area index.", "anchors": ["b:0001"]}]}
    sequence = [("extractor", _empty_invocation())] * (N - 1) + [("extractor", _inv("e", good))]
    candidates, error = orchestrator.run_enumeration(
        run_id="run1", paper_id=PAPER_ID, entity_type="Variable", model="test-model", invoke=make_invoke_sequence(sequence))
    assert error is None and [c.candidate_id for c in candidates] == ["lai"]


# --------------------------------------------------------------------- #
# a real outage is bounded
# --------------------------------------------------------------------- #


def _spend_a_budget(run_id: str = "run1") -> orchestrator._ProviderBudget:
    budget = orchestrator._ProviderBudget(run_id, "k", "extraction")
    while not budget.failed("provider_empty"):
        pass
    return budget


def test_after_consecutive_terminal_loops_each_further_loop_gets_a_single_round(sleeps):
    for _ in range(orchestrator.MAX_CONSECUTIVE_PROVIDER_TERMINALS):
        assert _spend_a_budget().rounds == N                                   # full patience until the outage is evident
    fast = orchestrator._ProviderBudget("run1", "later", "extraction")
    assert fast.round_limit() == 1 and fast.failed("provider_empty") is True and fast.rounds == 1
    summary = orchestrator.summarize_provider_failures("run1")
    assert summary["outage_mode"] == ["later"]                                 # disclosed, with the record it applied to


def test_the_outage_counter_is_per_run():
    for _ in range(orchestrator.MAX_CONSECUTIVE_PROVIDER_TERMINALS):
        _spend_a_budget("run_a")
    assert orchestrator._ProviderBudget("run_b", "k", "extraction").round_limit() == N


def test_a_missing_executable_is_not_evidence_of_an_outage():
    budget = orchestrator._ProviderBudget("run1", "k", "extraction")
    for _ in range(orchestrator.MAX_CONSECUTIVE_PROVIDER_TERMINALS + 2):
        assert orchestrator._ProviderBudget("run1", "k", "extraction").failed("provider_unavailable") is True
    assert budget.round_limit() == N


def test_any_successful_call_in_a_run_restores_full_patience(env):
    """The run's invoke wrapper clears the counter on the first call that returns an answer."""
    run_id = "outage_run"
    orchestrator._PROVIDER_OUTAGE[run_id] = orchestrator.MAX_CONSECUTIVE_PROVIDER_TERMINALS
    inner = make_invoke_sequence(_build_run_paper_invoke_sequence())
    seen: list = []

    def spy(agent, model, prompt, timeout=300):
        seen.append(orchestrator._PROVIDER_OUTAGE.get(run_id))        # the state each call starts with
        return inner(agent, model, prompt, timeout)

    orchestrator.run_paper(paper_id=PAPER_ID, model="test-model", client=env["client"], invoke=spy,
                           enable_ai_validation=False, run_id=run_id)
    assert seen[0] == orchestrator.MAX_CONSECUTIVE_PROVIDER_TERMINALS and all(v is None for v in seen[1:])


def test_a_provider_failed_call_does_not_reset_the_outage_counter(env):
    run_id = "outage_run2"
    orchestrator._PROVIDER_OUTAGE[run_id] = orchestrator.MAX_CONSECUTIVE_PROVIDER_TERMINALS

    def failing(agent, model, prompt, timeout=300):
        return _empty_invocation()

    orchestrator.run_paper(paper_id=PAPER_ID, model="test-model", client=env["client"], invoke=failing,
                           enable_ai_validation=False, run_id=run_id)
    summary = run_store.load_run_manifest(run_id)["provider_failures"]
    assert summary["total_rounds"] > 0 and summary["outage_mode"]        # every loop stayed in fast-fail mode


def test_the_new_constants_are_recorded_in_the_run_manifest():
    constants = orchestrator._run_constants()
    assert constants["MAX_PROVIDER_FAILURE_ROUNDS"] == N
    assert {"PROVIDER_COOLDOWN_GROWTH", "PROVIDER_COOLDOWN_CAP_SECONDS", "MAX_CONSECUTIVE_PROVIDER_TERMINALS"} <= set(constants)
