"""Stop silent losses and wrong ready records.

Each test is a real failure from the overnight corpus run (2026-09-25/26):
  A1  an empty enumeration answer after a stall / provider hiccup was accepted as "none exist" -- Philippe Management
      (2 events one night, 0 the next), Kathryn Variable/Coverage/Study;
  A2  a Crop was bound to the only extracted Species by elimination -- Felipe tomato cultivar AB-2 READY with species
      Brassica nigra;
  A3  an AI concern about the pipeline-assigned record id blocked a whole paper (Smukler); grounded names were withdrawn
      over naming style (16 Felipe Variables);
  A4  a paraphrased / null raw_text_excerpt was retried blind until the record ended in error;
  A5  "Mean" rows became Treatments and hollow Treatments were READY (Kathryn);
  A6  "35°03' N" was rejected against the source's "35°3' N" (Paul), leaving its Site unresolved.
"""

from __future__ import annotations


import pytest

from pipeline import causes, orchestrator, run_store, validators
from pipeline.raw_schema import TableRowGroup
from test_no_answer_handling import TOOL_THEN_STOP, _no_text, _recording, sleeps  # noqa: F401 (fixture)
from test_orchestrator import PAPER_ID, _inv, env, make_invoke_sequence  # noqa: F401 (fixture)


@pytest.fixture(autouse=True)
def _fresh_logs():
    orchestrator._PROVIDER_FAILURE_LOG.clear()
    orchestrator._STALL_LOG.clear()
    orchestrator._ENUMERATION_OUTCOMES.clear()


EMPTY = {"entity_type": "Management", "candidates": []}
ONE = {"entity_type": "Management", "candidates": [
    {"candidate_id": "planting", "description": "Planting.", "anchors": ["b:0002"], "linked_candidates": {}}]}


def _enumerate(invoke, evidence_found=None, entity_type="Management"):
    return orchestrator.run_enumeration(run_id="run1", paper_id=PAPER_ID, entity_type=entity_type, model="test-model",
                                        invoke=invoke, evidence_found=evidence_found)


# --------------------------------------------------------------------------- #
# A1 -- an empty answer after a disturbance is never "none exist"
# --------------------------------------------------------------------------- #

def test_an_empty_answer_after_a_stall_is_reasked_once_and_can_recover(env, sleeps):
    invoke, calls = _recording([("reader", _no_text(TOOL_THEN_STOP)), ("reader", _inv("reader", EMPTY)),
                                ("reader", _inv("reader", ONE))])
    candidates, error = _enumerate(invoke)
    assert error is None and [c.candidate_id for c in candidates] == ["planting"]
    assert "right after an interrupted turn" in calls[2][1]
    assert orchestrator._final_answer_nudge("enumeration") not in calls[2][1]   # a CLEAN re-ask, no answer-now pressure


def test_still_empty_after_the_reask_is_recorded_as_not_retrieved(env, sleeps):
    invoke, _calls = _recording([("reader", _no_text(TOOL_THEN_STOP)), ("reader", _inv("reader", EMPTY)),
                                 ("reader", _inv("reader", EMPTY))])
    candidates, error = _enumerate(invoke)
    assert candidates == [] and error is None
    outcome = orchestrator._ENUMERATION_OUTCOMES[("run1", "Management")]
    assert outcome["reliable"] is False and outcome["cause"] == causes.NOT_RETRIEVED


def test_an_undisturbed_empty_answer_with_no_evidence_is_accepted_without_a_second_call(env, sleeps):
    invoke, calls = _recording([("extractor", _inv("extractor", EMPTY))])
    assert _enumerate(invoke) == ([], None) and len(calls) == 1
    assert ("run1", "Management") not in orchestrator._ENUMERATION_OUTCOMES


def test_an_empty_answer_despite_packet_evidence_is_reasked_but_then_trusted(env, sleeps):
    invoke, calls = _recording([("extractor", _inv("extractor", EMPTY)), ("extractor", _inv("extractor", EMPTY))])
    candidates, error = _enumerate(invoke, evidence_found=["thinning (b:0030)"])
    assert (candidates, error) == ([], None) and "thinning (b:0030)" in calls[1][1]
    assert ("run1", "Management") not in orchestrator._ENUMERATION_OUTCOMES   # read twice, uninterrupted: a judgment
    attempt = run_store.load_json(run_store.record_dir("run1", "Management__enumeration") / "enumeration" / "attempt2.json")
    assert attempt["enumeration_outcome"]["empty_despite_packet_evidence"] == ["thinning (b:0030)"]


def test_an_unreliable_empty_entity_type_becomes_an_explicit_unresolved_record(env, sleeps):
    invoke = make_invoke_sequence([("reader", _no_text(TOOL_THEN_STOP)), ("reader", _inv("reader", EMPTY)),
                                   ("reader", _inv("reader", EMPTY))])
    citation = {"entity_type": "Citation", "record_id": PAPER_ID, "status": "ready", "detail": {"payload": {}}}
    infos = orchestrator._run_multi_record_entity(
        run_id="run1", paper_id=PAPER_ID, entity_type="Management", model="test-model", client=env["client"],
        invoke=invoke, enable_ai_validation=False, this_run_records={"Citation": citation})
    [info] = infos
    assert info["status"] == "unresolved" and "not evidence that the paper reports none" in info["detail"]["last_errors"][0]["message"]
    result = orchestrator._entity_result_file(PAPER_ID, "Management", "run1", info["record_id"], info)
    assert result["unresolved_cause"] == causes.NOT_RETRIEVED


# --------------------------------------------------------------------------- #
# A2 -- a crop's species is linked on evidence, never by elimination
# --------------------------------------------------------------------------- #

def _species(record_id, scientific, common=None):
    payload = {"scientific_name": {"value": scientific, "provenance_label": "EXTRACTED"}}
    if common:
        payload["common_name"] = {"value": common, "provenance_label": "EXTRACTED"}
    return {"record_id": record_id, "status": "ready", "detail": {"payload": payload}}


class _Candidate:
    def __init__(self, anchors):
        self.anchors = anchors


def test_a_crop_whose_evidence_names_no_extracted_species_is_not_linked(env):
    # Felipe: the only Species extracted was Brassica nigra; the tomato cultivar's evidence names tomato.
    records = {"Species": [_species(f"{PAPER_ID}_species_brassica_nigra", "Brassica nigra")]}
    ids, decision = orchestrator._evidenced_species(PAPER_ID, _Candidate(["b:0003"]), records)
    assert ids == [] and decision["decision"] == "species_not_evidenced" and "by elimination" in decision["message"]


def test_a_crop_is_linked_to_the_species_its_own_evidence_names(env):
    # The fixture paper's b:0001 reads "A. Author": the species' "G. epithet" form.
    records = {"Species": [_species("sp_a", "Alpha author"), _species("sp_b", "Beta other")]}
    ids, decision = orchestrator._evidenced_species(PAPER_ID, _Candidate(["b:0001"]), records)
    assert ids == ["sp_a"] and decision["decision"] == "species_evidenced"


def test_the_papers_own_common_name_pairing_links_a_crop(tmp_path, monkeypatch):
    from test_document_map import _write_paper
    _write_paper(tmp_path, "p", [
        ("b:0001", "Text", "The experimental plots were switchgrass ( Panicum virgatum L.) yield trials.", 0),
        ("b:0002", "Text", "This study used three commercially available switchgrass cultivars (Trailblazer, Pathfinder).", 0),
    ])
    monkeypatch.setenv("IR_PAPERS_ROOT", str(tmp_path))
    records = {"Species": [_species("sp_pv", "Panicum virgatum L.")]}
    ids, _ = orchestrator._evidenced_species("p", _Candidate(["b:0002"]), records)
    assert ids == ["sp_pv"]


# --------------------------------------------------------------------------- #
# A3 -- the validator's scope
# --------------------------------------------------------------------------- #

def test_a_concern_about_the_pipeline_assigned_id_is_scoped_out():
    verdict = orchestrator._scope_ai_concerns({"verdict": "suspicious", "issues": [
        {"field": "id", "concern": "the id spells the author Smulker"}]})
    assert verdict["verdict"] == "plausible" and verdict["verdict_before_scoping"] == "suspicious"
    assert verdict["scoped_out"][0]["field"] == "id" and verdict["issues"] == []


def test_real_concerns_survive_scoping():
    verdict = orchestrator._scope_ai_concerns({"verdict": "suspicious", "issues": [
        {"field": "id", "concern": "spelling"}, {"field": "year.value", "concern": "a page number"}]})
    assert verdict["verdict"] == "suspicious" and [i["field"] for i in verdict["issues"]] == ["year.value"]


def _f(value):
    return {"value": value, "provenance_label": "EXTRACTED",
            "source": {"source_document_id": "P", "locators": [{"kind": "text", "block_anchor": "b:0001"}]}}


def test_a_naming_style_concern_keeps_a_grounded_name_as_an_open_concern():
    payload = {"id": "v", "name": _f("N2O emissions")}
    demoted, demotions, open_concerns = orchestrator._demote_concerned_fields("Variable", payload, {"issues": [
        {"field": "name.value", "concern": 'omits the qualifier "soil" that appears in "Soil N2O emissions"'}]})
    assert demoted["name"]["value"] == "N2O emissions" and demotions == []
    assert open_concerns[0]["rule"] == "style_concern_kept"


def test_a_truncated_name_is_still_withdrawn_whatever_the_concern_says():
    payload = {"id": "v", "name": _f("maximum electron transport rate (Jmax")}
    demoted, demotions, _ = orchestrator._demote_concerned_fields("Variable", payload, {"issues": [
        {"field": "name", "concern": "incomplete: omits the closing parenthesis"}]})
    assert demoted["name"]["provenance_label"] == "UNRESOLVED" and demotions[0]["rule"] == "ai_concern_value_withdrawn"


# --------------------------------------------------------------------------- #
# A4 -- a failed excerpt is retried against the real block text
# --------------------------------------------------------------------------- #

def test_the_retry_quotes_the_blocks_the_failing_fact_cites(env):
    parsed = {"facts": [{"field_name": "rate", "raw_value": "x", "raw_text_excerpt": "as NH4NO3", "anchors": ["b:0002"]}]}
    errors = [{"field": "facts[0].raw_text_excerpt", "message": "not found"}]
    [guidance] = orchestrator._regrounding_guidance(PAPER_ID, parsed, errors)
    assert "[b:0002] Published in 2012." in guidance["message"] and "never send null" in guidance["message"]


def test_a_null_excerpt_gets_the_same_guidance(env):
    parsed = {"facts": [{"field_name": "rate", "raw_value": "x", "raw_text_excerpt": None, "anchors": ["b:0003"]}]}
    errors = [{"field": "facts.0.raw_text_excerpt", "message": "Input should be a valid string"}]
    assert "[b:0003] A Title" in orchestrator._regrounding_guidance(PAPER_ID, parsed, errors)[0]["message"]


def test_no_guidance_when_no_fact_failed(env):
    assert orchestrator._regrounding_guidance(PAPER_ID, {"facts": []}, [{"field": "facts", "message": "empty"}]) == []


# --------------------------------------------------------------------------- #
# A5 -- summary rows and hollow Treatments
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label, summary", [
    ("Mean", True), ("Grand mean", True), ("Total", True), ("LSD (0.05)", True), ("P value PAR t", True), ("SE", True),
    ("All systems", True), ("P", False), ("n", False), ("Mean tilt angle", False), ("Mustard", False), ("1", False),
])
def test_summary_and_statistic_rows_are_never_conditions(label, summary):
    row = TableRowGroup(row_group_id="r", factor_values={"System": label}, source_table_anchor="b:0001", cells={"v": "1"})
    assert (orchestrator._summary_row_level(row) is not None) is summary


def test_a_treatment_without_a_name_or_definition_is_not_ready():
    unresolved = {"value": None, "provenance_label": "UNRESOLVED", "unresolved_reason": "x"}
    assert [i.code for i in validators.readiness_issues("Treatment", {"name": unresolved, "definition": unresolved})] == [
        "treatment_name_unresolved", "treatment_definition_unresolved"]
    assert validators.readiness_issues("Treatment", {"name": _f("Fallow"), "definition": _f("winter fallow")}) == []


# --------------------------------------------------------------------------- #
# A6 -- the same coordinate spelled differently grounds; a different one does not
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("reported, ok", [
    ("35°03' N", True), ("35°3′N", True), ("83°25' W", True), ("-83°25′ W", True),
    ("35°30' N", False), ("35°03' S", False), ("83°52' W", False),
])
def test_equivalent_coordinates_ground_and_different_ones_do_not(reported, ok):
    block = "trees growing in and near the Coweeta Hydrologic Laboratory (35°3' N, 83°25′ W), in the southern"
    field = "latitude" if reported.endswith(("N", "S")) else "longitude"
    issues = validators._nested_value_issues(field, {"reported_text": reported, "reported_units": "°"}, [("b:0023", block)])
    assert (not any(i.code == "provenance_reported_text_mismatch" for i in issues)) is ok


def test_a_scientific_name_split_by_a_rendering_break_still_links_the_crop(tmp_path, monkeypatch):
    # Kathryn-2020-Winter b:0041: "'Pacific Gold' India mustard ( Bras sica juncea Czern.)".
    from test_document_map import _write_paper
    _write_paper(tmp_path, "k", [("b:0041", "Text", "39% 'Pacific Gold' India mustard ( Bras sica juncea Czern.), or rye", 0)])
    monkeypatch.setenv("IR_PAPERS_ROOT", str(tmp_path))
    records = {"Species": [_species("sp_bj", "Brassica juncea Czern."), _species("sp_sa", "Sinapis alba L.")]}
    ids, _ = orchestrator._evidenced_species("k", _Candidate(["b:0041"]), records)
    assert ids == ["sp_bj"]
