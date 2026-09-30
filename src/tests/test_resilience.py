"""A small early mistake must cost one field, not the paper.

Real case (Philippe-2007-Six, run 20260927T094252_bcb55cd2): the conversion wrote the Site name "Chaène des Puys" for
b:0028's "Chaîne des Puys" on three retries; the Site ended unresolved with no committed payload (its coordinates,
elevation and soil all grounded), and Coverage, Treatment, Observation and TreatmentPair were never attempted. In the
same run Table 3 was lost because every retry was a new session told "Stop reading and do not call any more tools.
Using what you have already read ..." -- it had read nothing, and answered "the table was not read".

  A. the table is in the classification prompt; the answer-now retry no longer claims earlier reading;
  B. a rejected value is shown the closest verbatim span of its cited block, and a value differing from that span only
     in mangled non-ASCII letters takes the block's own characters (recorded, never for digits);
  C. a field failing grounding twice is withdrawn (UNRESOLVED, the error as its reason) and the rest re-proposed; a Site
     is identified by any grounded identity field, coordinates included;
  D. extract first, link later: an attempted-but-unready prerequisite is a PENDING link target; the dependent is
     extracted and validated, and committed unresolved (BLOCKED_PREREQUISITE, `pending_links`), never ready;
  E. a run can be resumed from one entity type, reusing everything before it.
"""

from __future__ import annotations

import json

import pytest

from pipeline import causes, orchestrator, results_store, run_store
from test_document_map import _write_paper
from test_orchestrator import PAPER_ID, _inv, _paper_payload, env, make_invoke_sequence  # noqa: F401 (env fixture)

B0028 = ("Measurements were performed in a 25-year-old natural P. sylvestris stand in the Chaîne des Puys, a "
         "mid-elevation volcanic range (45°42′ N, 2°58′ E, 900 m a.s.l.).")


# --------------------------------------------------------------------------- #
# A. the table is in the prompt; the retry does not claim earlier reading
# --------------------------------------------------------------------------- #

def test_the_classification_prompt_carries_the_table_caption_and_rows_verbatim(tmp_path, monkeypatch):
    _write_paper(tmp_path, "t", [("b:0001", "Caption", "*Table 3. Mean values by PARt class and year.*", 0),
                                 ("b:0002", "Table", "| | Na |\n|---|---|\n| PARt 0–0.1 | 0.89 a |\n| Year 2001 | 1.18 B |", 0)])
    monkeypatch.setenv("IR_PAPERS_ROOT", str(tmp_path))
    prompt = orchestrator._table_classification_prompt("t", "b:0002", [])
    section = prompt[prompt.index("THE TABLE"):]
    assert "Table 3. Mean values by PARt class and year." in section
    assert "| PARt 0–0.1 | 0.89 a |\n| Year 2001 | 1.18 B |" in section            # one row per line


def test_the_answer_now_retry_says_the_session_is_new_and_where_the_text_is():
    nudge = orchestrator._final_answer_nudge("table_classification")
    assert "new session" in nudge and "THE TABLE" in nudge
    assert "already read" not in nudge and "do not call any more tools" not in nudge.lower()


# --------------------------------------------------------------------------- #
# B. closest verbatim span; non-ASCII repair
# --------------------------------------------------------------------------- #

def _mismatch(value, anchor="b:0028"):
    return {"code": "provenance_value_mismatch", "severity": "error",
            "message": f"name: value={value!r} is not supported by block {anchor}. Provide a locator whose block text "
                       f"contains the cited value."}


@pytest.fixture
def site_paper(tmp_path, monkeypatch):
    _write_paper(tmp_path, "s", [("b:0028", "Text", B0028, 0)])
    monkeypatch.setenv("IR_PAPERS_ROOT", str(tmp_path))
    return "s"


def test_a_mangled_accent_takes_the_blocks_own_characters(site_paper):
    payload = {"name": {"value": "Chaène des Puys", "provenance_label": "EXTRACTED"}}
    repaired, [repair] = orchestrator._verbatim_repairs(site_paper, payload, [_mismatch("Chaène des Puys")])
    assert repaired["name"]["value"] == "Chaîne des Puys" and (repair["from"], repair["to"]) == ("Chaène des Puys", "Chaîne des Puys")
    assert payload["name"]["value"] == "Chaène des Puys"                                       # input untouched


@pytest.mark.parametrize("value, other", [("45°42′ N", "45°43′ N"), ("Chaine des Puys", "Chaîne des Puys"),
                                          ("Chaîne des Pugs", "Chaîne des Puys")])
def test_a_digit_an_ascii_letter_or_a_different_word_is_never_repaired(value, other):
    assert not orchestrator._only_non_ascii_letters_differ(value, other)


def test_the_retry_is_shown_the_closest_verbatim_span(site_paper):
    [hint] = orchestrator._closest_span_hints(site_paper, [_mismatch("Chaène des Puys")])
    assert "block b:0028 contains 'Chaîne des Puys'" in hint and "character for character" in hint


# --------------------------------------------------------------------------- #
# C. withdraw a persistently ungrounded field; identity by any grounded field
# --------------------------------------------------------------------------- #

def test_a_field_failing_grounding_twice_is_withdrawn_with_its_reason():
    payload = {"id": "x", "name": {"value": "Chaène des Puys", "provenance_label": "EXTRACTED"},
               "elevation": {"value": {"reported_text": "900 m"}, "provenance_label": "EXTRACTED"}}
    failures: dict = {}
    assert orchestrator._withdraw_persistently_ungrounded(payload, [_mismatch("Chaène des Puys")], failures) is None
    reduced, [withdrawn] = orchestrator._withdraw_persistently_ungrounded(payload, [_mismatch("Chaène des Puys")], failures)
    assert reduced["name"]["provenance_label"] == "UNRESOLVED" and reduced["name"]["value"] is None
    assert "never matched its cited text" in reduced["name"]["unresolved_reason"]
    assert reduced["elevation"] == payload["elevation"] and withdrawn["attempts_failed"] == 2


def test_a_shape_error_or_a_reference_is_never_withdrawn():
    payload = {"name": {"value": "x", "provenance_label": "EXTRACTED"}, "site_id": "s"}
    shape = [{"field": "name", "message": "Input should be a valid string"}]
    failures = {"name": 5}
    assert orchestrator._withdraw_persistently_ungrounded(payload, shape, failures) is None


def test_the_site_is_identified_by_its_coordinates_when_its_name_is_withdrawn():
    detail = {"payload": {"name": {"value": None, "provenance_label": "UNRESOLVED"},
                          "latitude": {"value": {"reported_text": "45°42′ N"}, "provenance_label": "EXTRACTED"}},
              "last_errors": [{"field": "site_name_unresolved", "message": "name is UNRESOLVED"}]}
    record = {"entity_type": "Site", "record_id": "s", "status": "unresolved", "detail": detail}
    assert orchestrator._identity_established(record, orchestrator.IDENTITY_REFERENCEABLE_FIELDS["Site"])


def test_run_record_withdraws_the_name_and_keeps_the_grounded_record(env):
    from test_auxiliary_demotion import RAW_VAR, VAR_ID, _var_payload
    bad_name = {"value": "Something Else", "provenance_label": "EXTRACTED",
                "source": {"source_document_id": PAPER_ID, "page_number": 1, "section_path": [],
                           "locators": [{"kind": "text", "block_anchor": "b:0003"}]}}
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Variable", record_id=VAR_ID, model="m", client=env["client"],
        invoke=make_invoke_sequence([("extractor", _inv("extractor", {**RAW_VAR, "facts": RAW_VAR["facts"][:1]}))]
                                    + [("converter", _inv("converter", _var_payload(name=bad_name)))] * 2),
        enable_ai_validation=False)
    assert result.status == "unresolved" and result.detail["payload"]["name"]["provenance_label"] == "UNRESOLVED"
    assert [w["field"] for w in result.detail["withdrawn_fields"]] == ["name"]


# --------------------------------------------------------------------------- #
# D. extract first, link later
# --------------------------------------------------------------------------- #

def _records(site_status="unresolved"):
    return {"Citation": {"entity_type": "Citation", "record_id": "c", "status": "ready", "detail": {}},
            "Site": [{"entity_type": "Site", "record_id": "s", "status": site_status, "detail": {}}],
            "Treatment": [{"entity_type": "Treatment", "record_id": "p_treatment_low", "status": "unresolved", "detail": {}},
                          {"entity_type": "Treatment", "record_id": "p_treatment_high", "status": "ready", "detail": {}}]}


def test_an_unready_site_is_a_pending_link_target_not_a_block():
    records = _records()
    assert orchestrator._resolve_known_refs("Treatment", records)[1] is not None             # before: blocked
    view, pending = orchestrator._with_pending("Treatment", records)
    known, blocked = orchestrator._resolve_known_refs("Treatment", view)
    assert blocked is None and known["site_id"] == "s" and pending["s"]["status"] == "unresolved"


def test_a_pending_treatment_is_reached_only_by_the_candidates_own_link():
    view, pending = orchestrator._with_pending("Observation", _records("ready"))
    unlinked = orchestrator.EnumerationCandidate(candidate_id="o", description="d", anchors=["b:1"])
    resolved = orchestrator._apply_candidate_links("p", "Observation", view, {}, unlinked)
    assert "treatment_id" not in resolved                                                     # no allowed set
    assert orchestrator._link_refusals("p", "Observation", view, resolved, unlinked)[0]["cause"] == causes.AMBIGUOUS
    linked = orchestrator.EnumerationCandidate(candidate_id="o", description="d", anchors=["b:1"],
                                               linked_candidates={"treatment_id": "low"})
    resolved = orchestrator._apply_candidate_links("p", "Observation", view, {}, linked)
    assert resolved["treatment_id"] == "p_treatment_low"
    assert orchestrator._pending_refs({"treatment_id": "p_treatment_low", "id": "s"}, pending, "Observation") == {
        "treatment_id": "p_treatment_low"}


def test_a_record_linked_to_a_pending_site_is_extracted_but_never_ready(env):
    refs = {"citation_id": PAPER_ID, "site_id": "s"}
    raw = {"paper_id": PAPER_ID, "entity_type": "Method", "record_id": "m1", "facts": [
        {"field_name": "name", "raw_value": "A Title", "raw_text_excerpt": "A Title", "anchors": ["b:0003"]}]}
    payload = _paper_payload("Coverage", "c1", {**refs, "variable_id": None})
    raw["entity_type"], raw["record_id"] = "Coverage", "c1"
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Coverage", record_id="c1", model="m", client=env["client"],
        invoke=make_invoke_sequence([("extractor", _inv("extractor", raw)), ("converter", _inv("converter", payload))]),
        enable_ai_validation=False, known_refs=refs,
        pending_prerequisites={"s": {"prerequisite": "Site", "status": "unresolved"}})
    assert result.status == "unresolved"
    assert result.detail["unresolved_cause"] == causes.BLOCKED_PREREQUISITE
    assert result.detail["pending_links"] == {"site_id": "s"} and result.detail["payload"]["site_id"] == "s"


# --------------------------------------------------------------------------- #
# E. resume from one entity type
# --------------------------------------------------------------------------- #

def test_a_run_resumes_from_an_entity_type_reusing_everything_before_it(env, monkeypatch):
    run_id = "r1"
    order = orchestrator._topological_entity_order()
    run_store.save_run_manifest(run_id, {"run_id": run_id, "paper_id": PAPER_ID, "started_at": 1.0})
    citation = {"status": "ready", "payload": {"id": PAPER_ID}}
    run_store.save_final(run_id, f"Citation__{PAPER_ID}", citation)
    results_store.save_entity_result(PAPER_ID, "Citation", {"record_id": PAPER_ID, "entity_type": "Citation",
                                                            "status": "ready"}, run_id=run_id)
    run_store.save_final(run_id, f"Site__{PAPER_ID}_site", {"status": "unresolved"})
    run_store.save_final(run_id, "table_classification__b_0001", {"status": "error", "message": "not read",
                                                                   "failure_class": "validation_failure"})
    run_store.save_final(run_id, "table_classification__b_0002", {"status": "success", "classification": {}})
    records, note = orchestrator._resume_state(PAPER_ID, run_id, order, "Site")
    assert records["Citation"]["status"] == "ready" and records["Citation"]["detail"] == citation
    assert "Site" not in records and {"Treatment", "Coverage", "Observation", "TreatmentPair", "Management"}.isdisjoint(note["kept"])
    assert {"Citation", "Species", "Variable", "Method", "Crop", "Study"} <= set(note["kept"])     # not Site-dependent
    moved = set(note["superseded"]["records"])
    assert {f"Site__{PAPER_ID}_site", "table_classification__b_0001"} <= moved
    assert "table_classification__b_0002" not in moved                                      # a good table is reused
    assert (run_store.run_dir(run_id) / "superseded").is_dir()


def test_resume_refuses_an_unknown_entity_or_another_papers_run(env):
    run_store.save_run_manifest("r2", {"run_id": "r2", "paper_id": "other"})
    order = orchestrator._topological_entity_order()
    with pytest.raises(ValueError):
        orchestrator._resume_state(PAPER_ID, "r2", order, "Site")
    with pytest.raises(ValueError):
        orchestrator._resume_state("other", "r2", order, "Nope")
