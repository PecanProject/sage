"""Phase A (multi-record Variable) focused tests: the sealed
`EnumerationCandidate`/`EnumerationResult` schema (pipeline/raw_schema.py)
and the bounded, deterministically-validated `orchestrator.run_enumeration`
pass built on top of it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

PIPELINE_DIR = Path(__file__).resolve().parent.parent / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

from pipeline import orchestrator, run_store
from pipeline.raw_schema import EnumerationCandidate, EnumerationResult

PAPER_ID = "enum_test_paper"


def write_test_paper(papers_root: Path, paper_id: str = PAPER_ID) -> Path:
    pdir = papers_root / paper_id
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "content.md").write_text(
        "Leaf area index was measured for each plot.\n⟦b:0010⟧\n\n"
        "Soil organic carbon was also determined.\n⟦b:0011⟧\n",
        encoding="utf-8",
    )
    return pdir


@pytest.fixture()
def env(tmp_path, monkeypatch):
    papers_root = tmp_path / "papers"
    runs_root = tmp_path / "runs"
    papers_root.mkdir()
    monkeypatch.setenv("IR_PAPERS_ROOT", str(papers_root))
    monkeypatch.setenv("IR_RUNS_ROOT", str(runs_root))
    write_test_paper(papers_root)
    return {"papers_root": papers_root, "runs_root": runs_root}


def _inv(agent: str, parsed_json) -> orchestrator.AgentInvocation:
    import json as _json

    return orchestrator.AgentInvocation(
        agent=agent, model="test-model", prompt="prompt",
        returncode=0, stdout="{}", stderr="",
        final_text=_json.dumps(parsed_json), parsed_json=parsed_json, parse_error=None,
    )


def _inv_parse_error(agent: str) -> orchestrator.AgentInvocation:
    return orchestrator.AgentInvocation(
        agent=agent, model="test-model", prompt="prompt",
        returncode=0, stdout="not json", stderr="",
        final_text="not json", parsed_json=None, parse_error="no valid JSON object found in agent output",
    )


def make_invoke_sequence(items):
    it = iter(items)

    def _invoke(agent, model, prompt, timeout=300):
        return next(it)

    return _invoke


# --------------------------------------------------------------------- #
# Schema: EnumerationCandidate / EnumerationResult
# --------------------------------------------------------------------- #


def test_valid_enumeration_result_constructs():
    result = EnumerationResult.model_validate({
        "entity_type": "Variable",
        "candidates": [
            {"candidate_id": "lai", "description": "Leaf area index.", "anchors": ["b:0010"]},
            {"candidate_id": "soc", "description": "Soil organic carbon.", "anchors": ["b:0011"]},
        ],
    })
    assert len(result.candidates) == 2
    assert result.candidates[0].linked_candidates == {}


def test_duplicate_candidate_ids_rejected():
    with pytest.raises(ValidationError, match="duplicate candidate_id"):
        EnumerationResult.model_validate({
            "entity_type": "Variable",
            "candidates": [
                {"candidate_id": "lai", "description": "Leaf area index.", "anchors": ["b:0010"]},
                {"candidate_id": "lai", "description": "A different one, same id.", "anchors": ["b:0011"]},
            ],
        })


def test_candidate_without_anchor_rejected():
    with pytest.raises(ValidationError):
        EnumerationCandidate.model_validate({
            "candidate_id": "lai", "description": "Leaf area index.", "anchors": [],
        })


def test_candidate_without_description_rejected():
    with pytest.raises(ValidationError):
        EnumerationCandidate.model_validate({
            "candidate_id": "lai", "description": "", "anchors": ["b:0010"],
        })


def test_candidate_without_id_rejected():
    with pytest.raises(ValidationError):
        EnumerationCandidate.model_validate({
            "candidate_id": "", "description": "Leaf area index.", "anchors": ["b:0010"],
        })


def test_empty_enumeration_is_valid():
    # A paper genuinely reporting zero distinct Variables is a legitimate,
    # deterministic outcome -- never an error, never fabricated.
    result = EnumerationResult.model_validate({"entity_type": "Variable", "candidates": []})
    assert result.candidates == []


# --------------------------------------------------------------------- #
# _enumeration_prompt -- must actually carry the real paper_id
# --------------------------------------------------------------------- #


def test_enumeration_prompt_includes_the_real_paper_id():
    # Real regression (pecan, first Phase A run): the prompt never
    # mentioned the actual paper_id anywhere, so the model guessed wrong
    # values ('current', 'paper', or none at all) for every tool call,
    # found nothing, and correctly reported zero candidates -- not because
    # the paper has no variables, but because every read tool call silently
    # failed. The prompt must give the model the literal string to pass.
    prompt = orchestrator._enumeration_prompt("pecan", "Variable")
    assert "pecan" in prompt
    assert "paper_id=`pecan`" in prompt


# --------------------------------------------------------------------- #
# orchestrator.run_enumeration -- bounded retry, anchor-reality check
# --------------------------------------------------------------------- #


def test_run_enumeration_valid_result_first_attempt(env):
    invoke = make_invoke_sequence([
        _inv("extractor", {
            "entity_type": "Variable",
            "candidates": [
                {"candidate_id": "lai", "description": "Leaf area index.", "anchors": ["b:0010"]},
            ],
        }),
    ])
    candidates, error = orchestrator.run_enumeration(
        run_id="run1", paper_id=PAPER_ID, entity_type="Variable", model="test-model", invoke=invoke,
    )
    assert error is None
    assert len(candidates) == 1
    assert candidates[0].candidate_id == "lai"


def test_run_enumeration_empty_candidates_is_success_not_error(env):
    invoke = make_invoke_sequence([_inv("extractor", {"entity_type": "Variable", "candidates": []})])
    candidates, error = orchestrator.run_enumeration(
        run_id="run1", paper_id=PAPER_ID, entity_type="Variable", model="test-model", invoke=invoke,
    )
    assert error is None
    assert candidates == []


def test_run_enumeration_rejects_anchor_not_in_document_then_retries(env):
    invoke = make_invoke_sequence([
        _inv("extractor", {
            "entity_type": "Variable",
            "candidates": [{"candidate_id": "lai", "description": "Leaf area index.", "anchors": ["b:9999"]}],
        }),
        _inv("extractor", {
            "entity_type": "Variable",
            "candidates": [{"candidate_id": "lai", "description": "Leaf area index.", "anchors": ["b:0010"]}],
        }),
    ])
    candidates, error = orchestrator.run_enumeration(
        run_id="run1", paper_id=PAPER_ID, entity_type="Variable", model="test-model", invoke=invoke,
    )
    assert error is None
    assert candidates[0].anchors == ["b:0010"]

    record_key = "Variable__enumeration"
    attempt1 = run_store.load_json(run_store.record_dir("run1", record_key) / "enumeration" / "attempt1.json")
    assert attempt1["validation_errors"]
    assert "does not exist in content.md" in attempt1["validation_errors"][0]["message"]


def test_run_enumeration_bounded_retry_exhausts_and_reports_error(env):
    invoke = make_invoke_sequence([_inv_parse_error("extractor")] * orchestrator.MAX_ENUMERATION_ATTEMPTS)
    candidates, error = orchestrator.run_enumeration(
        run_id="run1", paper_id=PAPER_ID, entity_type="Variable", model="test-model", invoke=invoke,
    )
    assert candidates == []
    assert error is not None

    record_key = "Variable__enumeration"
    final = run_store.load_json(run_store.record_dir("run1", record_key) / "final.json")
    assert final["status"] == "error"


def test_run_enumeration_shape_violation_retries_then_succeeds(env):
    invoke = make_invoke_sequence([
        _inv("extractor", {"entity_type": "Variable", "candidates": [{"candidate_id": "lai"}]}),  # missing fields
        _inv("extractor", {
            "entity_type": "Variable",
            "candidates": [{"candidate_id": "lai", "description": "Leaf area index.", "anchors": ["b:0010"]}],
        }),
    ])
    candidates, error = orchestrator.run_enumeration(
        run_id="run1", paper_id=PAPER_ID, entity_type="Variable", model="test-model", invoke=invoke,
    )
    assert error is None
    assert len(candidates) == 1
