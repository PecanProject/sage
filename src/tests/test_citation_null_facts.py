"""A RawFact that reports NO value no longer sinks an extraction for want of a literal excerpt.

Real evidence (Felipe-2010-Cultivar Citation, first answers of runs felipe_final_20260921T063002, felipe_methodfix3_20260921T025636
and felipe_smoke_20260920T132343). The extractor's contract lets a fact report "looked, not stated" with `raw_value: null`, but the
excerpt is still a required string that the grounding gate holds to the literal-text rule, so the model's honest answer about an
absent field failed in three different ways: prose ("No journal name visible in the rendered content."), an empty excerpt, and
empty strings with `anchors=[]` (a shape failure). Title, author and year were grounded every time. The auxiliary-fact rule could not drop those
facts because `persistent_identifier` is a CORE Citation name -- so ONE fact with no value cost the whole attempt, and in two runs
the retry then met a provider outage and Citation (hence the entire paper) ended in error.

Now a fact whose `raw_value` is null or blank is droppable under any usable name, through the same dropped-facts mechanism and
log. A fact WITH a value keeps every protection: an ungrounded valued fact of a core name still fails, at least one grounded fact
must remain, Observation is excluded, and the grounding check itself is unchanged.

Fixtures (real): tests/fixtures/pass2/citation_attempt1_answers.json (the three stored first answers) and the trimmed Felipe content.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import orchestrator, run_store
from pipeline.raw_schema import RawExtraction
from test_orchestrator import (  # noqa: F401  (env is a pytest fixture)
    PAPER_ID, RAW_EXTRACTION, _inv, env, make_invoke_sequence, valid_citation_payload,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pass2"
FELIPE = "Felipe-2010-Cultivar"
BLOCK_5 = "This article is published with open access at Springerlink.com"   # the real literal text of b:0005 (after the "# The Author(s) 2010." prefix)


@pytest.fixture(autouse=True)
def real_papers(monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(FIXTURES))


def _answer(key: str) -> dict:
    return json.loads(json.dumps(json.loads((FIXTURES / "citation_attempt1_answers.json").read_text())[key]))


def _analyse(parsed: dict, entity_type: str = "Citation"):
    extraction = RawExtraction.model_validate(parsed)
    errors = orchestrator._raw_extraction_grounding_errors(FELIPE, extraction)
    kept, remaining, dropped = orchestrator._drop_ungrounded_noncore_facts(entity_type, extraction, errors)
    return extraction, errors, kept, remaining, dropped


def _names(extraction: RawExtraction) -> list[str]:
    return [f.field_name for f in extraction.facts]


# --------------------------------------------------------------------- #
# A-C: the three real first answers
# --------------------------------------------------------------------- #


def test_the_real_final_answer_with_prose_excerpts_is_recovered_without_a_retry():
    """felipe_final: 2 grounding errors before (journal + persistent_identifier, prose excerpts); now both dropped and logged."""
    extraction, errors, kept, remaining, dropped = _analyse(_answer("final"))
    assert sorted(e["field"] for e in errors) == ["facts[3].raw_text_excerpt", "facts[4].raw_text_excerpt"]
    assert remaining == [] and sorted(d["field_name"] for d in dropped) == ["journal", "persistent_identifier"]
    assert _names(kept) == ["title", "author", "year"]
    assert orchestrator._raw_extraction_grounding_errors(FELIPE, kept) == []
    for entry in dropped:                                     # logged with everything needed to audit it
        assert entry["rule"] == "ungrounded_auxiliary_fact" and entry["errors"] and entry["anchors"] == ["b:0005"]
    assert dropped[1]["raw_text_excerpt"] == "No DOI or other persistent identifier found in the visible content."
    # the reason the old rule refused: a core-named fact blocked the whole drop
    assert not orchestrator._is_droppable_fact_name("Citation", "persistent_identifier")


def test_the_real_methodfix3_answer_with_empty_excerpts_is_recovered():
    extraction, errors, kept, remaining, dropped = _analyse(_answer("methodfix3"))
    assert len(errors) == 5 and remaining == []
    assert sorted(d["field_name"] for d in dropped) == ["issue", "journal", "pages", "persistent_identifier", "volume"]
    assert _names(kept) == ["title", "authors", "year"]                                  # every grounded fact stays
    assert orchestrator._raw_extraction_grounding_errors(FELIPE, kept) == []


def test_the_real_smoke_answer_with_no_anchors_is_recovered_through_the_shape_path():
    """felipe_smoke: blank values, empty excerpts and `anchors=[]` -- a shape failure, previously fatal (persistent_identifier is core)."""
    parsed = _answer("smoke")
    with pytest.raises(Exception):
        RawExtraction.model_validate(parsed)
    recovered = orchestrator._recover_extraction_shape(parsed, "Citation")
    assert recovered is not None
    extraction, shape_dropped = recovered
    assert _names(extraction) == ["title", "author", "year"]
    assert sorted(d["field_name"] for d in shape_dropped) == ["issue", "journal", "pages", "persistent_identifier", "volume"]
    assert all(d["rule"] == "ungrounded_auxiliary_fact" and d["errors"] and d["anchors"] == [] for d in shape_dropped)
    assert orchestrator._raw_extraction_grounding_errors(FELIPE, extraction) == []


# --------------------------------------------------------------------- #
# the helper
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("raw_value, droppable", [(None, True), ("", True), ("   ", True), ("10.1000/x", False), ("0", False)])
def test_only_a_null_or_blank_value_makes_a_core_named_fact_droppable(raw_value, droppable):
    fact = {"field_name": "persistent_identifier", "raw_value": raw_value, "raw_text_excerpt": "x", "anchors": ["b:0005"]}
    assert orchestrator._is_droppable_fact("Citation", fact) is droppable
    assert orchestrator._is_droppable_fact("Citation", RawExtraction.model_validate({
        "paper_id": "p", "entity_type": "Citation", "record_id": "p", "facts": [fact]}).facts[0]) is droppable


def test_a_fact_with_no_usable_name_is_never_droppable_and_a_non_core_name_always_is():
    assert not orchestrator._is_droppable_fact("Citation", {"field_name": "", "raw_value": None})
    assert not orchestrator._is_droppable_fact("Citation", {"field_name": None, "raw_value": None})
    assert orchestrator._is_droppable_fact("Citation", {"field_name": "journal", "raw_value": "Plant and Soil"})   # unchanged auxiliary-fact rule


# --------------------------------------------------------------------- #
# D-F: what must still fail
# --------------------------------------------------------------------- #


def test_a_valued_core_fact_with_ungrounded_evidence_still_fails():
    parsed = _answer("final")
    parsed["facts"][0]["raw_text_excerpt"] = "Cultivar mixtures of processing tomato (paraphrased)"        # title: valued + core
    extraction, errors, kept, remaining, dropped = _analyse(parsed)
    assert dropped == [] and remaining == errors and kept is extraction and len(errors) == 3       # title + the two null facts stand


def test_a_valued_core_identifier_with_an_invented_excerpt_still_fails():
    """An invented DOI must never be laundered by the null-value rule."""
    parsed = _answer("final")
    pid = parsed["facts"][4]
    pid["raw_value"], pid["raw_text_excerpt"] = "10.1007/s11104-010-0000-0", "doi:10.1007/s11104-010-0000-0"
    extraction, errors, kept, remaining, dropped = _analyse(parsed)
    assert dropped == [] and remaining == errors


def test_nothing_is_dropped_when_no_grounded_fact_would_remain():
    only_null = {"paper_id": FELIPE, "entity_type": "Citation", "record_id": FELIPE, "facts": [
        {"field_name": "journal", "raw_value": None, "raw_text_excerpt": "No journal name.", "anchors": ["b:0005"]},
        {"field_name": "persistent_identifier", "raw_value": None, "raw_text_excerpt": "No DOI.", "anchors": ["b:0005"]}]}
    extraction, errors, kept, remaining, dropped = _analyse(only_null)
    assert len(errors) == 2 and dropped == [] and remaining == errors
    # the same on the shape path: every fact is bad, so there is nothing to recover to
    shape = json.loads(json.dumps(only_null))
    for fact in shape["facts"]:
        fact["anchors"] = []
    assert orchestrator._recover_extraction_shape(shape, "Citation") is None


def test_observation_is_excluded_on_both_paths():
    extraction, errors, *_ = _analyse(_answer("final"))
    assert errors and orchestrator._drop_ungrounded_noncore_facts("Observation", extraction, errors)[1:] == (errors, [])
    assert orchestrator._recover_extraction_shape(_answer("smoke"), "Observation") is None


def test_a_pipeline_level_error_is_still_never_hidden():
    extraction = RawExtraction.model_validate(_answer("final"))
    pipeline_error = {"field": None, "message": "cannot verify raw evidence grounding: content.md missing"}
    assert orchestrator._drop_ungrounded_noncore_facts("Citation", extraction, [pipeline_error])[1:] == ([pipeline_error], [])


# --------------------------------------------------------------------- #
# G: a grounded null fact is kept
# --------------------------------------------------------------------- #


def test_a_grounded_null_fact_is_retained_and_only_the_ungrounded_one_is_dropped():
    """The convention the agent documents (null value, a real passage that was checked) must keep working."""
    parsed = _answer("final")
    parsed["facts"][3]["raw_text_excerpt"] = BLOCK_5                          # journal: null value, literal passage -> grounded
    extraction, errors, kept, remaining, dropped = _analyse(parsed)
    assert [e["field"] for e in errors] == ["facts[4].raw_text_excerpt"]
    assert [d["field_name"] for d in dropped] == ["persistent_identifier"] and remaining == []
    assert _names(kept) == ["title", "author", "year", "journal"]
    assert next(f for f in kept.facts if f.field_name == "journal").raw_value is None


def test_a_fully_grounded_answer_is_untouched():
    parsed = _answer("final")
    for fact in parsed["facts"][3:]:
        fact["raw_text_excerpt"] = BLOCK_5
    extraction, errors, kept, remaining, dropped = _analyse(parsed)
    assert errors == [] and dropped == [] and kept is extraction and len(kept.facts) == 5


# --------------------------------------------------------------------- #
# H: end to end
# --------------------------------------------------------------------- #

NULL_FACTS = [
    {"field_name": "journal", "raw_value": None, "raw_text_excerpt": "No journal name visible in the rendered content.", "anchors": ["b:0003"]},
    {"field_name": "persistent_identifier", "raw_value": None, "raw_text_excerpt": "No DOI found in the visible content.", "anchors": ["b:0003"]},
]


def test_run_record_succeeds_on_numbered_attempt_one_and_never_shows_the_dropped_facts_to_conversion(env):
    raw = {**RAW_EXTRACTION, "facts": RAW_EXTRACTION["facts"] + NULL_FACTS}
    prompts: list[tuple[str, str]] = []
    answers = {"extractor": _inv("extractor", raw), "converter": _inv("converter", valid_citation_payload())}

    def invoke(agent, model, prompt, timeout=300):
        prompts.append((agent, prompt))
        return answers[agent]

    result = orchestrator.run_record(run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID, model="test-model",
                                     client=env["client"], invoke=invoke, enable_ai_validation=False)
    assert result.status == "ready"
    assert [a for a, _ in prompts].count("extractor") == 1                                  # no retry
    manifest = run_store.load_json(run_store.record_dir("run1", "Citation__" + PAPER_ID) / "record_manifest.json")
    assert manifest["attempts"]["extraction"] == 1 and "provider_failure_rounds" not in manifest["attempts"]
    assert sorted(d["field_name"] for d in result.detail["dropped_ungrounded_facts"]) == ["journal", "persistent_identifier"]
    assert all(d["rule"] == "ungrounded_auxiliary_fact" and d["errors"] for d in result.detail["dropped_ungrounded_facts"])
    conversion_prompt = next(p for a, p in prompts if a == "converter")
    assert "No journal name visible" not in conversion_prompt and "No DOI found" not in conversion_prompt
    assert "A Title" in conversion_prompt                                                    # the grounded facts did reach it


def test_run_record_still_retries_when_a_valued_core_fact_is_ungrounded(env):
    bad = {**RAW_EXTRACTION, "facts": [dict(RAW_EXTRACTION["facts"][0], raw_text_excerpt="A. Author (paraphrased)")] + RAW_EXTRACTION["facts"][1:] + NULL_FACTS}
    good = RAW_EXTRACTION
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID, model="test-model", client=env["client"],
        invoke=make_invoke_sequence([("extractor", _inv("extractor", bad)), ("extractor", _inv("extractor", good)),
                                     ("converter", _inv("converter", valid_citation_payload()))]),
        enable_ai_validation=False)
    assert result.status == "ready"
    manifest = run_store.load_json(run_store.record_dir("run1", "Citation__" + PAPER_ID) / "record_manifest.json")
    assert manifest["attempts"]["extraction"] == 2                                          # the valued failure kept its retry
