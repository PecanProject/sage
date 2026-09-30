"""Management ordering, guidance and treatment links (plan section 13; the approved order's "Management
ordering/guidance"). The date validator and the harvest warning of that plan section are a separate item (validators.py).

Evidence (real runs 20260919T085532_98015a8a and 20260918T192235_fb6030ba): of the 18 ready Management records, none has
`treatment_ids`, although the paper's own events define its treatments (protocol Section 9.3) -- Felipe's mustard-cover-crop
planting, mowing and residue incorporation belong to the Mustard treatment. Management was extracted BEFORE Treatment (order:
... Crop, Management, Method, Study, Treatment ...), so a link could not even exist. The free-form pass also enumerated
"Winter fallow treatment" and "Winter mustard cover crop treatment" as Management records (they errored: those are Treatments,
protocol Section 6.6), event types were free text ("nitrogen application", "Transplanting of seedlings", "laser leveled")
instead of PEcAn-aligned, and a harvest date carried a year (1997, the publication year) the source never gave for the event.

(1) Management is extracted after Treatment; (2) `treatment_ids` is an OPTIONAL link, offered to enumeration as a
pool and admitted only for a Treatment whose name both the event's description and its cited text state -- never every event to
every treatment, never a `known_ref` that would bind the only ready Treatment; (3) Conversion is guarded so no other id can
appear; (4) the Management guidance follows protocol Sections 6.6, 9.1 and 9.2.

Fixtures: tests/fixtures/item14/ (real). Tests marked SYNTHETIC use invented text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import orchestrator, run_store, vocab
from pipeline.raw_schema import EnumerationCandidate
from pipeline.validators import _load_rendered_blocks
from test_orchestrator import env as ir_env  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures" / "item14"
PAPER = "Felipe-2010-Cultivar"


def _real():
    return json.loads((FIXTURES / "felipe_fb6030ba_treatments_and_events.json").read_text())


def _candidates() -> dict[str, EnumerationCandidate]:
    cs = json.loads((FIXTURES / "felipe_fb6030ba_management_candidates.json").read_text())
    return {c["candidate_id"]: EnumerationCandidate.model_validate(c) for c in cs}


def _treatment(record_id, name):
    return {"entity_type": "Treatment", "record_id": record_id, "status": "ready",
            "detail": {"payload": {"name": {"value": name, "provenance_label": "EXTRACTED"}}}}


def _records() -> dict:
    """The run's real ready Treatments (those with a name)."""
    return {"Treatment": [_treatment(t["record_id"], t["name"]) for t in _real()["treatments"] if t["name"]]}


def _slug(name_fragment: str) -> str:
    rid = next(t["record_id"] for t in _real()["treatments"] if t["name"] and t["name"].lower() == name_fragment.lower())
    return orchestrator._candidate_slug_from_record_id(PAPER, "Treatment", rid)


@pytest.fixture()
def blocks(monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(FIXTURES))
    return _load_rendered_blocks(PAPER)


def _with_link(candidate: EnumerationCandidate, *slugs) -> EnumerationCandidate:
    return candidate.model_copy(update={"linked_candidates": {"treatment_ids": list(slugs)}})


# --------------------------------------------------------------------- #
# ordering
# --------------------------------------------------------------------- #


def test_management_is_extracted_after_treatment_and_nothing_else_moves_relative_to_each_other():
    order = orchestrator._topological_entity_order()
    assert order.index("Treatment") < order.index("Management")
    assert order == ["Citation", "Site", "Species", "Variable", "Coverage", "Crop", "Method", "Study", "Treatment", "Management",
                     "Observation", "TreatmentPair"]
    before = [e for e in order if e != "Management"]
    assert before.index("Treatment") < before.index("Observation") < before.index("TreatmentPair")  # untouched relations


def test_the_link_is_not_a_dependency_so_no_known_ref_ever_binds_a_treatment_to_management():
    """ENTITY_DEPENDENCIES feeds known_refs: a `treatment_ids` entry there would bind the only ready Treatment to EVERY event."""
    assert orchestrator.ENTITY_DEPENDENCIES["Management"] == [("citation_id", "Citation", True)]
    records = {"Citation": {"entity_type": "Citation", "record_id": PAPER, "status": "ready", "detail": {"payload": {}}}, **_records()}
    known_refs, blocked = orchestrator._resolve_known_refs("Management", records)
    assert blocked is None and set(known_refs) == {"citation_id"}
    assert orchestrator.OPTIONAL_LINKS == {"Management": [("treatment_ids", "Treatment")]}


# --------------------------------------------------------------------- #
# the pool offered to enumeration, and list-valued links
# --------------------------------------------------------------------- #


def test_the_management_pool_offers_the_ready_treatments_even_when_there_is_only_one():
    pools = orchestrator._multi_record_link_pools(PAPER, "Management", _records())
    assert {i["name"] for i in pools["treatment_ids"]} >= {"Fallow", "Mustard"}
    assert all(set(i) == {"slug", "record_id", "name", "description"} for i in pools["treatment_ids"])
    one = {"Treatment": [_records()["Treatment"][0]]}
    assert len(orchestrator._multi_record_link_pools(PAPER, "Management", one)["treatment_ids"]) == 1
    assert orchestrator._multi_record_link_pools(PAPER, "Management", {}) == {}                    # no ready Treatment: no pool
    assert "treatment_ids" not in orchestrator._multi_record_link_pools(PAPER, "Observation", _records())


def test_linked_candidates_accept_a_slug_or_a_list_of_slugs():
    both = EnumerationCandidate(candidate_id="a", description="d", anchors=["b:0001"],
                                linked_candidates={"site_id": "ames", "treatment_ids": ["mustard", "fallow"]})
    assert both.linked_candidates == {"site_id": "ames", "treatment_ids": ["mustard", "fallow"]}


def test_the_enumeration_prompt_asks_for_a_list_link_only_when_the_pool_is_a_list_field():
    pool = {"treatment_ids": [{"slug": "mustard", "name": "Mustard"}]}
    with_list = orchestrator._enumeration_prompt("p", "Management", link_pools=pool)
    assert "give a JSON LIST of the slug(s)" in with_list and "ONLY when the source text you cite in `anchors` itself NAMES" in with_list
    assert "for those omit the key entirely" in with_list and 'slug "mustard": \'Mustard\'' in with_list
    single = orchestrator._enumeration_prompt("p", "Observation", link_pools={"treatment_id": [{"slug": "a", "name": "A"}]})
    assert "give a JSON LIST" not in single
    assert orchestrator._enumeration_prompt("p", "Management") == orchestrator._enumeration_prompt("p", "Management", link_pools=None)


def _enumerate(payload_candidates, pool):
    calls = []

    def invoke(agent, model, prompt, timeout=300):
        calls.append(prompt)
        payload = {"entity_type": "Management", "candidates": payload_candidates.pop(0)}
        return orchestrator.AgentInvocation(agent=agent, model=model, prompt=prompt, returncode=0, stdout="{}", stderr="",
                                            final_text=json.dumps(payload), parsed_json=payload, parse_error=None)
    return calls, invoke


def test_run_enumeration_validates_every_slug_of_a_list_link(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(FIXTURES))
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))
    pool = {"treatment_ids": [{"slug": "mustard", "name": "Mustard"}, {"slug": "fallow", "name": "Fallow"}]}
    good = {"candidate_id": "planting", "description": "Mustard planted", "anchors": ["b:0028"], "linked_candidates": {"treatment_ids": ["mustard"]}}
    bad = {**good, "linked_candidates": {"treatment_ids": ["mustard", "invented"]}}
    calls, invoke = _enumerate([[bad], [good]], pool)
    candidates, error = orchestrator.run_enumeration(run_id="r", paper_id=PAPER, entity_type="Management", model="m", invoke=invoke, link_pools=pool)
    assert error is None and len(calls) == 2 and candidates[0].linked_candidates == {"treatment_ids": ["mustard"]}
    assert "'invented'" in calls[1] and "never invent a slug" in calls[1]  # the retry names the slug that is not in the pool


def test_a_list_link_never_breaks_the_table_subsumption_check():
    """Management candidates carry a list value; the old link-overlap check hashed values into sets."""
    c = EnumerationCandidate(candidate_id="a", description="d", anchors=["b:0001"], linked_candidates={"treatment_ids": ["x"]})
    t = EnumerationCandidate(candidate_id="t", description="d", anchors=["b:0002"], linked_candidates={"site_id": "s"})
    assert orchestrator._drop_freeform_candidates_subsumed_by_tables([c], [t]) == [c]


# --------------------------------------------------------------------- #
# verification: a link stands only when the description AND the cited text name the condition
# --------------------------------------------------------------------- #


def _verify(candidate, blocks, records=None):
    return orchestrator._verified_treatment_links(PAPER, candidate, records or _records(), blocks)


def test_real_events_that_name_their_condition_are_linked(blocks):
    c = _candidates()
    for cid in ("mustard-cover-crop-planting", "mustard-residue-incorporation", "cover-crop-mowing"):
        verified, decisions = _verify(_with_link(c[cid], _slug("Mustard")), blocks)
        assert [v["name"] for v in verified] == ["Mustard"], cid
        assert decisions[0]["decision"] == "linked"


def test_real_site_wide_events_are_not_linked_even_when_their_block_names_a_treatment(blocks):
    """b:0028 names the fallow and mustard main plots, but land leveling and compost apply to the whole field."""
    c = _candidates()
    for cid in ("laser-leveling", "compost-application"):
        assert "mustard" in blocks["b:0028"].lower()
        verified, decisions = _verify(_with_link(c[cid], _slug("Mustard"), _slug("Fallow")), blocks)
        assert verified == [] and {d["decision"] for d in decisions} == {"dropped"}
        assert all("description does not name this condition" in d["reason"] for d in decisions)


def test_a_condition_the_cited_text_does_not_name_is_dropped(blocks):
    """The event's description names 'Mustard' but its cited block (b:0030, cultivars/irrigation) does not."""
    weeding = _candidates()["manual-weeding"].model_copy(update={"description": "Manual weeding of the Mustard plots"})
    verified, decisions = _verify(_with_link(weeding, _slug("Mustard")), blocks)
    assert verified == [] and decisions[0]["reason"] == "the event's cited text does not name this condition"


def test_only_ready_treatments_of_this_run_can_be_linked(blocks):
    c = _with_link(_candidates()["mustard-cover-crop-planting"], "not_a_treatment", _slug("Mustard"))
    verified, decisions = _verify(c, blocks)
    assert [v["name"] for v in verified] == ["Mustard"]
    assert [d["decision"] for d in decisions] == ["dropped", "linked"] and decisions[0]["reason"] == "not a ready Treatment of this run"
    not_ready = {"Treatment": [{**t, "status": "unresolved"} for t in _records()["Treatment"]]}
    assert _verify(_with_link(_candidates()["mustard-cover-crop-planting"], _slug("Mustard")), blocks, not_ready)[0] == []


def test_an_event_that_proposes_no_link_has_none_and_a_single_slug_string_is_accepted(blocks):
    assert _verify(_candidates()["mustard-cover-crop-planting"], blocks) == ([], [])
    c = _candidates()["mustard-cover-crop-planting"].model_copy(update={"linked_candidates": {"treatment_ids": _slug("Mustard")}})
    assert [v["name"] for v in _verify(c, blocks)[0]] == ["Mustard"]


def test_synthetic_a_treatment_name_must_match_as_a_whole_phrase_not_a_fragment():
    records = {"Treatment": [_treatment(f"{PAPER}_treatment_till", "till")]}
    c = EnumerationCandidate(candidate_id="x", description="Till plots before sowing", anchors=["b:0001"], linked_candidates={"treatment_ids": ["till"]})
    # the description names 'till' as a phrase, but the cited text only has it inside 'tillage'
    assert orchestrator._verified_treatment_links(PAPER, c, records, {"b:0001": "Tillage was done before sowing."})[0] == []
    # and the other way round: the cited text has the phrase, the description only 'tillage'
    d = c.model_copy(update={"description": "Tillage before sowing"})
    assert orchestrator._verified_treatment_links(PAPER, d, records, {"b:0001": "Till plots were sown."})[0] == []
    assert [v["name"] for v in orchestrator._verified_treatment_links(PAPER, c, records, {"b:0001": "Till plots were sown."})[0]] == ["till"]


# --------------------------------------------------------------------- #
# the guard on Conversion
# --------------------------------------------------------------------- #


CTX = {"treatment_link": {"treatment_ids": ["p_treatment_mustard"], "names": ["Mustard"], "evidence_anchors": ["b:0028"]}}


def _mg(ids, label="EXTRACTED"):
    return {"treatment_ids": {"value": ids, "provenance_label": label}}


def test_only_verified_treatments_may_appear_in_treatment_ids():
    assert orchestrator._management_link_errors("Management", _mg(["p_treatment_mustard"]), CTX) == []
    [error] = orchestrator._management_link_errors("Management", _mg(["p_treatment_mustard", "p_treatment_fallow"]), CTX)
    assert error["field"] == "treatment_ids" and "p_treatment_fallow" in error["message"] and "Omit" in error["message"]
    [everything] = orchestrator._management_link_errors("Management", _mg(["p_treatment_fallow", "p_treatment_mustard"]), None)
    assert "verified ids: none" in everything["message"]


def test_no_link_or_an_unresolved_link_is_always_fine_and_other_entities_are_never_checked():
    assert orchestrator._management_link_errors("Management", {}, None) == []
    assert orchestrator._management_link_errors("Management", _mg([]), None) == []
    assert orchestrator._management_link_errors("Management", {"treatment_ids": None}, None) == []
    assert orchestrator._management_link_errors("Management", _mg(None, "UNRESOLVED"), None) == []
    assert orchestrator._management_link_errors("Observation", _mg(["anything"]), None) == []
    assert orchestrator._management_link_errors("Management", {"treatment_ids": ["a"]}, CTX)[0]["field"] == "treatment_ids"  # a bare list too
    assert orchestrator._management_link_errors("Management", {"treatment_ids": [{"unhashable": 1}]}, CTX)  # a malformed id is a mismatch


def test_the_conversion_prompt_states_the_link_rule(blocks):
    raw = {"paper_id": "p", "entity_type": "Management", "record_id": "r", "facts": []}
    linked = orchestrator._conversion_prompt("p", "Management", "r", raw, None, None, CTX)
    assert "Set `treatment_ids` to exactly that list" in linked and "Never add any other Treatment id" in linked and "'Mustard'" in linked
    unlinked = orchestrator._conversion_prompt("p", "Management", "r", raw, None, None, None)
    assert "omit `treatment_ids`" in unlinked and "do NOT list every Treatment" in unlinked
    assert "treatment_ids" not in orchestrator._conversion_prompt("p", "Observation", "r", raw, None, None, None)


# --------------------------------------------------------------------- #
# pipeline: link decisions are logged, verified links travel, nothing else does
# --------------------------------------------------------------------- #


def test_the_pipeline_carries_only_verified_links_into_the_record_context_and_logs_every_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(FIXTURES))
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(orchestrator, "_resolve_known_refs", lambda entity_type, records: ({}, None))
    c = _candidates()
    mustard, fallow = _slug("Mustard"), _slug("Fallow")
    free = [_with_link(c["mustard-cover-crop-planting"], mustard), _with_link(c["laser-leveling"], mustard, fallow), c["sulfur-application"]]
    monkeypatch.setattr(orchestrator, "run_enumeration", lambda **k: (free, None))
    seen = {}
    def fake_run_record(*, entity_type, record_id, candidate_context=None, **kw):
        seen[record_id] = candidate_context
        return orchestrator.RecordResult(status="ready", entity_type=entity_type, record_id=record_id, detail={"payload": {}, "ai_validation": None})

    monkeypatch.setattr(orchestrator, "run_record", fake_run_record)
    records = {"Citation": {"entity_type": "Citation", "record_id": PAPER, "status": "ready", "detail": {"payload": {}}}, **_records()}
    orchestrator._run_multi_record_entity(run_id="run1", paper_id=PAPER, entity_type="Management", model="m", client=None, invoke=None,
                                          enable_ai_validation=False, this_run_records=records)
    link = {rid.split("_management_")[1]: (ctx or {}).get("treatment_link") for rid, ctx in seen.items()}
    assert link["mustard_cover_crop_planting"]["treatment_ids"] == [next(t["record_id"] for t in _real()["treatments"] if t["name"] == "Mustard")]
    assert link["mustard_cover_crop_planting"]["evidence_anchors"] == ["b:0028"]
    assert link["laser_leveling"] is None and link["sulfur_application"] is None      # site-wide / unlinked events carry no link
    log = json.loads((run_store.record_dir("run1", "Management__enumeration") / "treatment_links" / "attempt1.json").read_text())
    assert [(d["candidate_id"], d["decision"]) for d in log["decisions"]] == [
        ("mustard-cover-crop-planting", "linked"), ("laser-leveling", "dropped"), ("laser-leveling", "dropped")]


def test_a_conversion_that_links_every_treatment_is_rejected_before_it_reaches_the_service(ir_env):
    """End to end through run_record: a Management record whose treatment_ids names Treatments the event does not
    establish never reaches propose_record."""
    from test_orchestrator import RAW_EXTRACTION, PAPER_ID, _inv, make_invoke_sequence

    class NoServiceClient:
        def propose_record(self, **kw):
            raise AssertionError("propose_record must not be reached for an unestablished treatment link")

        def flag_unresolved(self, **kw):
            return {"flagged": True}

    payload = {"id": "m", "treatment_ids": {"value": ["a_treatment", "b_treatment"], "provenance_label": "EXTRACTED"}}
    seq = [("extractor", _inv("extractor", dict(RAW_EXTRACTION, entity_type="Management")))] + [("converter", _inv("converter", payload))] * orchestrator.MAX_CONVERSION_LOOP_SAFETY
    result = orchestrator.run_record(run_id="run1", paper_id=PAPER_ID, entity_type="Management", record_id="m", model="m", client=NoServiceClient(),
                                     invoke=make_invoke_sequence(seq), enable_ai_validation=False)
    assert result.status == "unresolved"
    assert "not established for this event" in json.dumps(result.detail)


# --------------------------------------------------------------------- #
# guidance: protocol Sections 6.6, 9.1, 9.2
# --------------------------------------------------------------------- #


GUIDANCE = orchestrator._ENTITY_IDENTITY_GUIDANCE["Management"]


def test_the_guidance_follows_protocol_6_6_events_are_not_treatments():
    assert "An event is something that HAPPENED to the field" in GUIDANCE
    assert "a winter fallow, a cover-crop treatment -- is a Treatment, not a Management record" in GUIDANCE
    assert "planting, mowing or incorporation events that define it" in GUIDANCE
    assert "one row per event" in GUIDANCE  # the original rule is kept


def test_the_guidance_follows_protocol_9_1_pecan_aligned_event_types():
    assert "PEcAn-aligned" in GUIDANCE
    for event_type in vocab.SEED_EVENT_TYPES:  # the repository's own seed list, not a copy
        assert event_type in GUIDANCE
    assert "(Protocol Section 9.1)" in GUIDANCE


def test_the_guidance_follows_protocol_9_2_infer_occurrence_never_dates():
    assert "Do infer that an event occurred when it is certain" in GUIDANCE
    assert "there was a harvest event if yields or harvested biomass are reported" in GUIDANCE
    assert "do NOT infer or complete event dates -- never supply a year or any part of a date the source does not give" in GUIDANCE
    assert "unreported rates or management histories implied only by local practice" in GUIDANCE
    assert "kept as that statement, without inventing a date" in GUIDANCE


def test_the_other_entity_guidance_is_untouched():
    for entity in ("Treatment", "Observation", "Study", "TreatmentPair", "Coverage"):
        assert "PEcAn" not in orchestrator._ENTITY_IDENTITY_GUIDANCE[entity]
