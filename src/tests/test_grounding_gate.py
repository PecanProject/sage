"""Item 13: grounding (plan section 14; decision Q7).

Evidence (stored Extraction attempts, all runs): 2,892 `raw_text_excerpt` grounding failures. 905 of them contain an
ellipsis, e.g. "SS were determined ... measured on a digital refractometer." -- literal stretches of a longer passage
with the middle left out, which a whole-string substring check can never match. 635 of those 905 (70%) are literal,
in-order stretches and now pass; the other 270 (and all 1,987 without an ellipsis: annotations like "**mowed**" or
"(header row ...)", text that is not in the cited block, wrong anchors) still fail. The cited blocks were also joined in
the order the model listed them, not document order, and a table candidate's whole attempt was thrown away because of one
ungrounded auxiliary fact (a unit, a note) even though the fact carrying the table's known value was perfectly grounded.

Item 13: (a) ellipsis excerpts -- every stretch must be literal source text, in the written order, no per-segment
leniency; (b) cited blocks are read in document order; (c) TABLE CANDIDATES ONLY: an ungrounded AUXILIARY fact is dropped
and logged as `dropped_ungrounded_facts` and never reaches Conversion, but only when the value-bearing fact is grounded;
an ungrounded value-bearing fact is still an error. The grounding CHECK itself is not loosened for annotations or
fabrications: they are still ungrounded (and, for a table candidate's auxiliary fact, dropped, never accepted).

Fixtures: tests/fixtures/item13/ (real Felipe blocks and real failed excerpts). Tests marked SYNTHETIC use invented text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import orchestrator
from pipeline.raw_schema import RawExtraction
from test_orchestrator import RAW_EXTRACTION, env as ir_env, valid_citation_payload, _inv, PAPER_ID  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures" / "item13"


def _ok(excerpt: str, text: str) -> bool:
    return orchestrator._excerpt_supported(excerpt, text)[0]


TEXT = "Plants were sampled at 39, 75, and 111 DAP. Shoots were oven-dried at 60 C for 48 h and weighed on a balance."


# --------------------------------------------------------------------- #
# (a) ellipsis excerpts
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("excerpt", [
    "Plants were sampled at 39, 75, and 111 DAP. Shoots were oven-dried at 60 C for 48 h and weighed on a balance.",  # whole, unchanged
    "Plants were sampled ... weighed on a balance.",
    "Plants were sampled … weighed on a balance.",                       # the unicode ellipsis
    "Plants were sampled at 39 ... oven-dried at 60 C ... weighed on a balance",  # several elisions
    "... Shoots were oven-dried at 60 C for 48 h ...",                        # leading and trailing elisions
    "Plants   were  sampled ...  weighed on a  balance.",                     # whitespace differences
])
def test_ellipsis_excerpts_with_every_stretch_literal_and_in_order_pass(excerpt):
    assert _ok(excerpt, TEXT)


@pytest.mark.parametrize("excerpt", [
    "weighed on a balance ... Plants were sampled",                          # right stretches, wrong order
    "Plants were sampled ... dried in a forced-draft oven",                   # a stretch not in the source
    "Plants were sampled ... (header row) ... weighed on a balance",          # an annotation is a segment that is not in the source
    "Plants were sampled (see Methods) ... weighed on a balance",
    "Plants were sampled ... Plants were sampled",                            # the same stretch twice: only one occurrence exists
    "Plants were paraphrased ... weighed on a balance",
])
def test_ellipsis_excerpts_with_a_stretch_out_of_order_missing_annotated_or_repeated_fail(excerpt):
    supported, why = orchestrator._excerpt_supported(excerpt, TEXT)
    assert not supported and why and "is not found, in the order written" in why


def test_a_failure_names_the_segment_that_broke_it():
    _, why = orchestrator._excerpt_supported("Plants were sampled ... dried in a forced-draft oven", TEXT)
    assert "segment 2 ('dried in a forced-draft oven')" in why


@pytest.mark.parametrize("excerpt", ["a ... b", "39 ... x", "Plants were sampled ... 60 ... balance"])
def test_a_segment_too_short_to_ground_anything_is_refused(excerpt):
    supported, why = orchestrator._excerpt_supported(excerpt, TEXT)
    assert not supported and "too short to ground anything" in why


def test_an_excerpt_that_is_only_an_ellipsis_quotes_nothing():
    assert orchestrator._excerpt_supported("...", TEXT) == (False, "raw_text_excerpt '...' quotes nothing (only an ellipsis).")
    assert not _ok("… …", TEXT)


def test_a_literal_excerpt_is_checked_exactly_as_before_and_a_source_that_contains_the_ellipsis_still_matches():
    assert _ok("oven-dried at 60 C", TEXT)
    assert orchestrator._excerpt_supported("a paraphrase of the passage", TEXT) == (False, None)   # no ellipsis: the caller's own message
    assert _ok("values ... were rounded", "The values ... were rounded to two places.")             # literal '...' in the source: exact match first
    # typography and scientific-notation spacing are handled per segment exactly as for a plain excerpt
    assert _ok("determined … extracted with KCl", "Soil NH 4 + -N was determined, then extracted with KCl.")


def test_a_compact_match_needs_every_segment_to_match_that_way_together():
    text = "Soil NH 4 + -N was determined and NO 3 - -N was determined."
    assert _ok("NH4+-N was determined ... NO3--N was", text)


# --------------------------------------------------------------------- #
# real failed excerpts (Felipe)
# --------------------------------------------------------------------- #


@pytest.fixture()
def felipe(monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(FIXTURES))


def _real():
    return json.loads((FIXTURES / "felipe_real_failed_excerpts.json").read_text())


def _gate(excerpt, anchors):
    fact = {"field_name": "f", "raw_value": "x", "raw_text_excerpt": excerpt, "anchors": anchors}
    extraction = RawExtraction.model_validate({"paper_id": "Felipe-2010-Cultivar", "entity_type": "Observation", "record_id": "r", "facts": [fact]})
    return orchestrator._raw_extraction_grounding_errors("Felipe-2010-Cultivar", extraction)


def test_real_ellipsis_excerpts_the_old_gate_rejected_now_pass(felipe):
    cases = _real()["ellipsis_now_passes"]
    assert cases
    for case in cases:
        assert _gate(case["excerpt"], case["anchors"]) == [], case["excerpt"][:60]


def test_real_excerpts_with_markup_or_text_not_in_the_block_still_fail(felipe):
    still = _real()["ellipsis_still_fails"] + _real()["other_still_fails"]
    assert still
    for case in still:
        errors = _gate(case["excerpt"], case["anchors"])
        assert errors and errors[0]["field"] == "facts[0].raw_text_excerpt", case["excerpt"][:60]


# --------------------------------------------------------------------- #
# (b) document-order joining
# --------------------------------------------------------------------- #


def test_a_quote_spanning_two_adjacent_cited_blocks_passes_whatever_order_they_are_cited_in(ir_env):
    quote = "A. Author Published in 2012."  # the end of b:0001 into b:0002 (adjacent blocks of the test paper)
    for anchors in (["b:0001", "b:0002"], ["b:0002", "b:0001"], ["b:0002", "b:0001", "b:0002"]):
        extraction = RawExtraction.model_validate({"paper_id": PAPER_ID, "entity_type": "Citation", "record_id": "r", "facts": [
            {"field_name": "x", "raw_value": "2012", "raw_text_excerpt": quote, "anchors": anchors}]})
        assert orchestrator._raw_extraction_grounding_errors(PAPER_ID, extraction) == [], anchors


def test_reading_the_blocks_in_document_order_does_not_make_a_reversed_quote_pass(ir_env):
    extraction = RawExtraction.model_validate({"paper_id": PAPER_ID, "entity_type": "Citation", "record_id": "r", "facts": [
        {"field_name": "x", "raw_value": "2012", "raw_text_excerpt": "Published in 2012. A. Author", "anchors": ["b:0001", "b:0002"]}]})
    assert orchestrator._raw_extraction_grounding_errors(PAPER_ID, extraction)


def test_a_quote_from_an_uncited_block_still_fails(ir_env):
    extraction = RawExtraction.model_validate({"paper_id": PAPER_ID, "entity_type": "Citation", "record_id": "r", "facts": [
        {"field_name": "x", "raw_value": "2012", "raw_text_excerpt": "Published in 2012.", "anchors": ["b:0003"]}]})
    assert orchestrator._raw_extraction_grounding_errors(PAPER_ID, extraction)


# --------------------------------------------------------------------- #
# (c) Q7: dropping ungrounded auxiliary facts (table candidates only)
# --------------------------------------------------------------------- #


def _fact(name, raw_value, excerpt, anchors=("b:0002",)):
    return {"field_name": name, "raw_value": raw_value, "raw_text_excerpt": excerpt, "anchors": list(anchors)}


def _extraction(*facts):
    return RawExtraction.model_validate({"paper_id": PAPER_ID, "entity_type": "Observation", "record_id": "r", "facts": list(facts)})


VALUE = _fact("value", "2012", "Published in 2012.")                       # grounded and carries the known value
AUX_BAD = _fact("units", "kg DM", "(header row) kg DM m-2")                # an annotation-style excerpt: ungrounded
AUX_GOOD = _fact("note", "author", "A. Author", ("b:0001",))


def _drop(ir_env, *facts, known="2012"):
    extraction = _extraction(*facts)
    errors = orchestrator._raw_extraction_grounding_errors(PAPER_ID, extraction)
    return extraction, errors, orchestrator._drop_ungrounded_auxiliary_facts(extraction, known, errors)


def test_an_ungrounded_auxiliary_fact_is_dropped_and_logged_when_the_value_fact_is_grounded(ir_env):
    extraction, errors, (kept, remaining, dropped) = _drop(ir_env, VALUE, AUX_BAD, AUX_GOOD)
    assert errors and remaining == []
    assert [f.field_name for f in kept.facts] == ["value", "note"]           # the ungrounded one is gone, the grounded auxiliary stays
    assert len(dropped) == 1 and dropped[0]["field_name"] == "units" and dropped[0]["raw_text_excerpt"] == "(header row) kg DM m-2"
    assert dropped[0]["anchors"] == ["b:0002"] and "not found" in dropped[0]["errors"][0]
    assert [f.field_name for f in extraction.facts] == ["value", "units", "note"]  # the original object is untouched


def test_an_annotation_or_fabrication_is_still_ungrounded_it_is_dropped_never_accepted(ir_env):
    """The gate is unchanged: a dropped fact is not 'grounded', it is removed and disclosed."""
    _, errors, (kept, _, dropped) = _drop(ir_env, VALUE, AUX_BAD)
    assert errors and errors[0]["field"] == "facts[1].raw_text_excerpt"
    assert AUX_BAD["raw_text_excerpt"] not in json.dumps(kept.model_dump())
    assert dropped


def test_an_ungrounded_value_bearing_fact_is_never_dropped(ir_env):
    bad_value = _fact("value", "2012", "(header row) 2012")                      # carries the known value but is not in the text
    _, errors, (kept, remaining, dropped) = _drop(ir_env, bad_value, AUX_GOOD)
    assert remaining == errors and dropped == [] and kept.facts == _extraction(bad_value, AUX_GOOD).facts


def test_a_grounded_value_fact_does_not_excuse_an_ungrounded_fact_that_also_carries_the_value(ir_env):
    also_value = _fact("value_again", "2012", "(see Table) 2012")
    _, errors, (_, remaining, dropped) = _drop(ir_env, VALUE, also_value)
    assert remaining == errors and dropped == []


def test_nothing_is_dropped_when_no_grounded_fact_carries_the_known_value(ir_env):
    _, errors, (_, remaining, dropped) = _drop(ir_env, AUX_BAD, AUX_GOOD, known="2012")
    assert remaining == errors and dropped == []


def test_a_pipeline_level_grounding_error_is_never_hidden(ir_env):
    extraction = _extraction(VALUE, AUX_BAD)
    pipeline_error = {"field": None, "message": "cannot verify raw evidence grounding: content.md missing"}
    assert orchestrator._drop_ungrounded_auxiliary_facts(extraction, "2012", [pipeline_error])[1:] == ([pipeline_error], [])
    # ... even next to an ordinary per-fact error: nothing is dropped and every error stands
    fact_error = orchestrator._raw_extraction_grounding_errors(PAPER_ID, extraction)[0]
    errors = [fact_error, pipeline_error]
    kept, remaining, dropped = orchestrator._drop_ungrounded_auxiliary_facts(extraction, "2012", errors)
    assert remaining == errors and dropped == [] and kept is extraction


def test_a_fact_citing_a_block_that_does_not_exist_is_an_ungrounded_auxiliary_fact_too(ir_env):
    ghost = _fact("method", "x", "some text", ("b:9999",))
    _, errors, (kept, remaining, dropped) = _drop(ir_env, VALUE, ghost)
    assert "do not exist in content.md" in errors[0]["message"] and remaining == [] and [d["field_name"] for d in dropped] == ["method"]


def _run(ir_env, extractions, known_value):
    prompts = []
    seq = iter([("extractor", e) for e in extractions] + [("converter", valid_citation_payload()), ("ir-validator", {"verdict": "plausible", "issues": []})])

    def invoke(agent, model, prompt, timeout=300):
        prompts.append((agent, prompt))
        expected, payload = next(seq)
        assert agent == expected
        return _inv(agent, payload)

    result = orchestrator.run_record(run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID, model="m",
                                     client=ir_env["client"], invoke=invoke, enable_ai_validation=True, known_value=known_value)
    return result, prompts


def test_a_table_candidate_keeps_its_attempt_and_the_dropped_fact_never_reaches_conversion(ir_env):
    extraction = dict(RAW_EXTRACTION, facts=RAW_EXTRACTION["facts"] + [AUX_BAD])
    result, prompts = _run(ir_env, [extraction], known_value="2012")
    assert result.status == "ready"
    assert [a for a, _ in prompts].count("extractor") == 1                      # one attempt: nothing was retried
    conversion_prompt = next(p for a, p in prompts if a == "converter")
    assert "(header row) kg DM m-2" not in conversion_prompt and '"units"' not in conversion_prompt   # never reaches Conversion
    assert result.detail["dropped_ungrounded_facts"][0]["field_name"] == "units"
    from pipeline import run_store
    attempt = run_store.load_json(run_store.record_dir("run1", "Citation__" + PAPER_ID) / "extraction" / "attempt1.json")
    assert attempt["dropped_ungrounded_facts"][0]["raw_text_excerpt"] == "(header row) kg DM m-2"
    manifest = run_store.load_json(run_store.record_dir("run1", "Citation__" + PAPER_ID) / "record_manifest.json")
    assert manifest["dropped_ungrounded_facts"] == 1


def test_a_free_form_candidate_gets_only_the_conservative_generalisation(ir_env):
    """Correction pass, Fix 3 (supersedes "never given this leniency"): with no known table value the Item 13 rule is
    generalised by field NAME -- an ungrounded fact that is not named like an identity of the entity type is dropped and
    logged -- but an ungrounded identity-named fact ('title' for a Citation) is still an error and is retried."""
    bad = dict(RAW_EXTRACTION, facts=RAW_EXTRACTION["facts"] + [AUX_BAD])
    result, prompts = _run(ir_env, [bad], known_value=None)
    assert [a for a, _ in prompts].count("extractor") == 1          # not retried: the auxiliary fact was dropped
    dropped = result.detail["dropped_ungrounded_facts"]
    assert [d["field_name"] for d in dropped] == ["units"] and dropped[0]["rule"] == "ungrounded_auxiliary_fact"

    bad_identity = dict(RAW_EXTRACTION, facts=[_fact("title", "A Title", "(header) A Title", ("b:0003",))] + RAW_EXTRACTION["facts"][:2])
    result, prompts = _run(ir_env, [bad_identity, RAW_EXTRACTION], known_value=None)
    assert [a for a, _ in prompts].count("extractor") == 2          # an ungrounded identity-named fact is never dropped
    assert "dropped_ungrounded_facts" not in result.detail


def test_a_table_candidate_whose_value_fact_is_ungrounded_is_still_an_extraction_error(ir_env):
    bad_value = dict(RAW_EXTRACTION, facts=[_fact("value", "2012", "(header row) 2012"), AUX_GOOD])
    result, prompts = _run(ir_env, [bad_value, bad_value, bad_value], known_value="2012")
    assert result.status == "error" and [a for a, _ in prompts].count("extractor") == orchestrator.MAX_EXTRACTION_ATTEMPTS
