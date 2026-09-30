"""ONE canonical spelling for the Management treatment link (`treatment_ids`).

Real bug (run felipe_smoke_20260920T132343, Felipe-2010-Cultivar, Management): the enumeration prompt taught the link key
with a hard-coded example, `{"treatment_id": "ambient_co2"}`, even when the offered field was the list `treatment_ids`.
The model copied it: its mustard planting / mowing / incorporation events came back linked with the SINGULAR key
`{"treatment_id": "mustard"}`. `run_enumeration` only validated keys that were offered fields, so the singular key was
silently ignored -- no link was ever proposed to `_verified_treatment_links`, the link verification never ran, and the
run wrote no `treatment_links` artifact at all.

Now: the prompt example uses a field this entity type is actually offered (a list for `_ids`), a singular/plural near-miss is
REJECTED with feedback naming the canonical key (never accepted, never silently ignored), and at the last attempt the
mis-keyed link is dropped and logged. The list form is verified exactly as before.

Fixtures (real): tests/fixtures/pass2/felipe_management_enumeration_attempt1.json (the real answer with the singular key)
and the trimmed real Felipe content.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import orchestrator, run_store
from pipeline.raw_schema import EnumerationCandidate

FIXTURES = Path(__file__).parent / "fixtures" / "pass2"
PAPER = "Felipe-2010-Cultivar"
POOL = {"treatment_ids": [
    {"slug": "fallow", "record_id": f"{PAPER}_treatment_fallow", "name": "Fallow", "description": None},
    {"slug": "mustard", "record_id": f"{PAPER}_treatment_mustard", "name": "Mustard", "description": None},
]}


@pytest.fixture()
def real_env(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(FIXTURES))
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))
    return tmp_path


def _real_answer() -> dict:
    return json.loads((FIXTURES / "felipe_management_enumeration_attempt1.json").read_text())["parsed_json"]


def _fixed(answer: dict) -> dict:
    """The same real answer with the canonical key (a list)."""
    out = json.loads(json.dumps(answer))
    for c in out["candidates"]:
        links = c.get("linked_candidates") or {}
        if "treatment_id" in links:
            links["treatment_ids"] = [links.pop("treatment_id")]
    return out


def _invoke(answers):
    calls = []
    queue = list(answers)

    def invoke(agent, model, prompt, timeout=300):
        calls.append(prompt)
        payload = queue.pop(0)
        return orchestrator.AgentInvocation(agent=agent, model=model, prompt=prompt, returncode=0, stdout="{}", stderr="",
                                            final_text=json.dumps(payload), parsed_json=payload, parse_error=None)
    return calls, invoke


def _enumerate(answers, run_id="r1"):
    calls, invoke = _invoke(answers)
    candidates, error = orchestrator.run_enumeration(
        run_id=run_id, paper_id=PAPER, entity_type="Management", model="m", invoke=invoke, link_pools=POOL)
    return candidates, error, calls


# --------------------------------------------------------------------- #
# the prompt
# --------------------------------------------------------------------- #


def test_the_management_prompt_example_uses_the_offered_list_field():
    prompt = orchestrator._enumeration_prompt("p", "Management", link_pools=POOL)
    assert '{"treatment_ids": ["ambient_co2"]}' in prompt and '{"treatment_id":' not in prompt
    assert "spelled exactly 'treatment_ids' (plural, a list)" in prompt


def test_the_prompts_of_other_entity_types_keep_their_singular_example_unchanged():
    single = orchestrator._enumeration_prompt("p", "Observation", link_pools={"treatment_id": [{"slug": "a", "name": "A"}]})
    assert '{"treatment_id": "ambient_co2"}' in single and "spelled exactly" not in single
    only_variable = orchestrator._enumeration_prompt("p", "Observation", link_pools={"variable_id": [{"slug": "a", "name": "A"}]})
    assert '{"variable_id": "ambient_co2"}' in only_variable


# --------------------------------------------------------------------- #
# the real answer with the singular key
# --------------------------------------------------------------------- #


def test_the_real_answer_really_used_the_singular_key():
    links = {c["candidate_id"]: c.get("linked_candidates") for c in _real_answer()["candidates"]}
    assert links["mustard_planting"] == {"treatment_id": "mustard"} and links["laser_leveling"] == {}


def test_the_singular_key_is_rejected_with_feedback_and_the_retry_succeeds(real_env):
    candidates, error, calls = _enumerate([_real_answer(), _fixed(_real_answer())])
    assert error is None and len(calls) == 2
    assert "uses the link key 'treatment_id'" in calls[1] and "'treatment_ids' (plural: a JSON LIST of slugs)" in calls[1]
    by_id = {c.candidate_id: c for c in candidates}
    assert by_id["mustard_planting"].linked_candidates == {"treatment_ids": ["mustard"]}
    first = run_store.load_json(run_store.record_dir("r1", "Management__enumeration") / "enumeration" / "attempt1.json")
    assert first["validation_errors"][0]["field"] == "linked_candidates"


def test_the_correct_list_form_is_accepted_at_once(real_env):
    candidates, error, calls = _enumerate([_fixed(_real_answer())])
    assert error is None and len(calls) == 1
    assert {c.candidate_id: c.linked_candidates for c in candidates}["cover_crop_mowing"] == {"treatment_ids": ["mustard"]}


def test_at_the_last_attempt_a_misnamed_link_is_dropped_and_logged_never_accepted(real_env):
    candidates, error, calls = _enumerate([_real_answer()] * orchestrator.MAX_ENUMERATION_ATTEMPTS)
    assert error is None and len(calls) == orchestrator.MAX_ENUMERATION_ATTEMPTS
    assert all("treatment_id" not in (c.linked_candidates or {}) and "treatment_ids" not in (c.linked_candidates or {}) for c in candidates)
    last = run_store.load_json(run_store.record_dir("r1", "Management__enumeration") / "enumeration" / f"attempt{len(calls)}.json")
    assert {d["candidate_id"] for d in last["dropped_link_keys"]} == {"mustard_planting", "cover_crop_mowing", "mustard_incorporation"}
    assert all(d["canonical"] == "treatment_ids" for d in last["dropped_link_keys"])


def test_unrelated_keys_and_offered_keys_are_left_alone():
    def c(links):
        return EnumerationCandidate(candidate_id="x", description="d", anchors=["b:0001"], linked_candidates=links)
    assert orchestrator._misnamed_link_keys([c({"treatment_ids": ["a"], "colour": "red"})], POOL) == []
    assert orchestrator._misnamed_link_keys([c({"treatment_id": "a"})], POOL) == [("x", "treatment_id", "treatment_ids")]
    assert orchestrator._misnamed_link_keys([c({"treatment_id": "a"})], None) == []
    assert orchestrator._misnamed_link_keys([c({"treatment_id": "a"})], {"treatment_id": []}) == []   # already canonical


# --------------------------------------------------------------------- #
# verification now actually executes
# --------------------------------------------------------------------- #


def _ready_treatments() -> dict:
    return {"Treatment": [
        {"entity_type": "Treatment", "record_id": f"{PAPER}_treatment_{n}", "status": "ready",
         "detail": {"payload": {"name": {"value": n.capitalize(), "provenance_label": "EXTRACTED"}}}}
        for n in ("fallow", "mustard")]}


def _run_management(monkeypatch, candidates):
    monkeypatch.setattr(orchestrator, "_resolve_known_refs", lambda entity_type, records: ({}, None))
    monkeypatch.setattr(orchestrator, "run_enumeration", lambda **k: (candidates, None))
    seen = {}

    def fake_run_record(*, entity_type, record_id, candidate_context=None, **kw):
        seen[record_id.split("_management_")[1]] = candidate_context
        return orchestrator.RecordResult(status="ready", entity_type=entity_type, record_id=record_id,
                                         detail={"payload": {}, "ai_validation": None})
    monkeypatch.setattr(orchestrator, "run_record", fake_run_record)
    records = {"Citation": {"entity_type": "Citation", "record_id": PAPER, "status": "ready", "detail": {"payload": {}}},
               **_ready_treatments()}
    orchestrator._run_multi_record_entity(run_id="r1", paper_id=PAPER, entity_type="Management", model="m", client=None,
                                          invoke=None, enable_ai_validation=False, this_run_records=records)
    return seen


def test_verification_runs_for_the_canonical_key_and_links_only_what_the_text_names(real_env, monkeypatch):
    candidates, _, _ = _enumerate([_fixed(_real_answer())])
    seen = _run_management(monkeypatch, candidates)
    link = seen["mustard_planting"]["treatment_link"]
    assert link["treatment_ids"] == [f"{PAPER}_treatment_mustard"] and link["evidence_anchors"] == ["b:0028"]
    assert seen["cover_crop_mowing"]["treatment_link"]["treatment_ids"] == [f"{PAPER}_treatment_mustard"]
    assert seen["laser_leveling"] is None          # a site-wide event proposes no link and gets none
    log = json.loads((run_store.record_dir("r1", "Management__enumeration") / "treatment_links" / "attempt1.json").read_text())
    assert {(d["candidate_id"], d["decision"]) for d in log["decisions"]} == {
        ("mustard_planting", "linked"), ("cover_crop_mowing", "linked"), ("mustard_incorporation", "linked")}


def test_an_unsupported_treatment_link_is_still_dropped_by_the_verification(real_env, monkeypatch):
    """The fixed key only makes verification RUN; it does not make a link true. Linking the mowing event to Fallow -- a
    condition the event's own description does not name -- is dropped."""
    answer = _fixed(_real_answer())
    next(c for c in answer["candidates"] if c["candidate_id"] == "cover_crop_mowing")["linked_candidates"] = {"treatment_ids": ["fallow"]}
    candidates, _, _ = _enumerate([answer])
    seen = _run_management(monkeypatch, candidates)
    assert seen["cover_crop_mowing"] is None
    log = json.loads((run_store.record_dir("r1", "Management__enumeration") / "treatment_links" / "attempt1.json").read_text())
    dropped = [d for d in log["decisions"] if d["candidate_id"] == "cover_crop_mowing"]
    assert [d["decision"] for d in dropped] == ["dropped"] and "does not name this condition" in dropped[0]["reason"]
