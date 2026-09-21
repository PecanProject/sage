"""Correction pass, Fix 8: `source.page_number` comes from provenance.json, never from a model.

Real evidence (Daren run 20260919T211137_77879c98 + Felipe run felipe_smoke_20260920T132343): across all committed
payloads, 160 `page_number` values were 1 (118) or 0 (42) while the cited blocks sit on pages 2-6 -- the model is never
given page data (a RawExtraction carries none), so every value was an invention. Worse, two records were made hollow by it:
Daren Crop Trailblazer (cultivar UNRESOLVED, "Page number not provided in extraction for anchor b:0119") and Daren Variable
leaf_blade_dry_weight (name, description, units and notes all UNRESOLVED, each "... page number not available").

Now the orchestrator sets each ExtractedField's `source.page_number` from the 1-indexed PDF page of the first cited block that
provenance.json can place (the convention the review UI already uses: `page_id` "page_0" is page 1), or None when it cannot:
never a guess. `page_number` is Optional in the IR and the Conversion prompt says so.

Fixtures (real): tests/fixtures/pass2/Felipe-2010-Cultivar/provenance.json and felipe_ready_payloads.json.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline import content_reader, ir_schema, orchestrator, run_store
from pipeline.ir_schema import ENTITY_MODELS
from test_orchestrator import PAPER_ID, RAW_EXTRACTION, _inv, env, make_invoke_sequence, valid_citation_payload  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures" / "pass2"
FELIPE = "Felipe-2010-Cultivar"


@pytest.fixture()
def felipe(monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(FIXTURES))
    return json.loads((FIXTURES / FELIPE / "provenance.json").read_text())


def _pages(node, out=None):
    """Every page_number in a payload, with the anchors its field cites."""
    out = [] if out is None else out
    if isinstance(node, dict):
        source = node.get("source")
        if "provenance_label" in node and isinstance(source, dict) and "locators" in source:
            out.append((source.get("page_number"), [l["block_anchor"] for l in source["locators"]]))
        for v in node.values():
            _pages(v, out)
    elif isinstance(node, list):
        for v in node:
            _pages(v, out)
    return out


def _expected(provenance, anchor):
    return int(provenance[anchor]["page_id"].rsplit("_", 1)[-1]) + 1


# --------------------------------------------------------------------- #
# the page lookup
# --------------------------------------------------------------------- #


def test_physical_page_is_one_indexed_like_the_review_ui():
    assert content_reader.physical_page("page_0") == 1 and content_reader.physical_page("page_5") == 6
    assert content_reader.physical_page(None) is None and content_reader.physical_page("cover") is None


def test_the_real_felipe_anchors_map_to_their_real_pages(felipe):
    assert content_reader.page_for_anchor(FELIPE, "b:0002", FIXTURES) == 1            # the title, page_0
    for anchor in ("b:0026", "b:0030", "b:0056", "b:0069"):
        assert content_reader.page_for_anchor(FELIPE, anchor, FIXTURES) == _expected(felipe, anchor)
    assert content_reader.page_for_anchor(FELIPE, "⟦b:0030⟧", FIXTURES) == _expected(felipe, "b:0030")   # bracketed form
    assert content_reader.page_for_anchor(FELIPE, "b:9999", FIXTURES) is None and content_reader.page_for_anchor(FELIPE, "", FIXTURES) is None
    assert content_reader.page_for_anchor("no_such_paper", "b:0001", FIXTURES) is None


# --------------------------------------------------------------------- #
# the real payloads
# --------------------------------------------------------------------- #


def test_the_real_committed_payloads_carried_invented_page_numbers(felipe):
    real = json.loads((FIXTURES / "felipe_ready_payloads.json").read_text())
    values = [p for payload in real.values() for p, _ in _pages(payload)]
    assert values and set(values) <= {0, 1}
    wrong = [(p, a) for payload in real.values() for p, anchors in _pages(payload) for a in anchors[:1] if p != _expected(felipe, a)]
    assert wrong, "the invented numbers really do disagree with provenance"


def test_every_field_of_every_real_payload_gets_its_true_page(felipe):
    real = json.loads((FIXTURES / "felipe_ready_payloads.json").read_text())
    for key, payload in real.items():
        fixed = orchestrator._apply_source_pages(FELIPE, payload)
        pairs = _pages(fixed)
        assert pairs, key
        for page, anchors in pairs:
            assert page == _expected(felipe, anchors[0]), (key, anchors)
    assert all(p >= 2 for p, _ in _pages(orchestrator._apply_source_pages(FELIPE, real["Observation::fruit_hue_mustard"])))


def test_the_input_payload_is_not_mutated(felipe):
    payload = json.loads((FIXTURES / "felipe_ready_payloads.json").read_text())["Observation::fruit_hue_mustard"]
    before = copy.deepcopy(payload)
    fixed = orchestrator._apply_source_pages(FELIPE, payload)
    assert payload == before and fixed is not payload


def test_the_first_placeable_locator_decides_and_an_unplaceable_one_is_skipped(felipe):
    def field(*anchors):
        return {"value": "x", "provenance_label": "EXTRACTED", "source": {
            "source_document_id": FELIPE, "page_number": 1, "locators": [{"kind": "text", "block_anchor": a} for a in anchors]}}
    fixed = orchestrator._apply_source_pages(FELIPE, {"a": field("b:9999", "b:0030"), "b": field("b:0030", "b:0002")})
    assert fixed["a"]["source"]["page_number"] == _expected(felipe, "b:0030")
    assert fixed["b"]["source"]["page_number"] == _expected(felipe, "b:0030")     # first locator, not the lowest page


def test_an_unplaceable_field_is_unknown_never_guessed(felipe):
    field = {"value": "x", "provenance_label": "EXTRACTED", "source": {
        "source_document_id": FELIPE, "page_number": 1, "locators": [{"kind": "text", "block_anchor": "b:9999"}]}}
    assert orchestrator._apply_source_pages(FELIPE, {"a": field})["a"]["source"]["page_number"] is None
    assert orchestrator._apply_source_pages("paper_without_provenance", {"a": field})["a"]["source"]["page_number"] is None


def test_only_extracted_field_shaped_dicts_are_touched(felipe):
    payload = {"id": "x", "source": {"page_number": 7}, "notes": "n", "list": [{"source": {"page_number": 7}}]}
    assert orchestrator._apply_source_pages(FELIPE, payload) == payload


# --------------------------------------------------------------------- #
# the schema, the prompt, the pipeline
# --------------------------------------------------------------------- #


def test_page_number_is_optional_in_the_ir_and_a_null_one_validates():
    assert "page_number" not in ir_schema.ExtractionSource.model_json_schema()["required"]
    site = {"id": "s", "name": {"value": "Farm", "provenance_label": "EXTRACTED", "source": {
        "source_document_id": "p", "page_number": None, "locators": [{"kind": "text", "block_anchor": "b:0001"}]}}}
    assert ENTITY_MODELS["Site"].model_validate(site).name.source.page_number is None
    del site["name"]["source"]["page_number"]
    assert ENTITY_MODELS["Site"].model_validate(site).name.source.page_number is None


def test_the_conversion_prompt_tells_the_model_the_page_is_not_its_job():
    prompt = orchestrator._conversion_prompt("p", "Variable", "r", {"facts": []}, [])
    assert "page_number` is filled in by the pipeline" in prompt and "never mark a field UNRESOLVED because a page number is not known" in prompt


def test_run_record_commits_the_provenance_pages_whatever_the_model_wrote(env):
    provenance = {"b:0001": "page_0", "b:0002": "page_2", "b:0003": "page_4"}
    (env["papers_root"] / PAPER_ID / "provenance.json").write_text(json.dumps(
        {a: {"block_type": "Text", "page_id": p, "section_path": [], "rendered_in_content_md": True} for a, p in provenance.items()}))
    payload = valid_citation_payload()
    assert {p for p, _ in _pages(payload)} == {1}                      # the model wrote 1 everywhere
    result = orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID, model="m", client=env["client"],
        invoke=make_invoke_sequence([("extractor", _inv("extractor", RAW_EXTRACTION)), ("converter", _inv("converter", payload))]),
        enable_ai_validation=False)
    assert result.status == "ready"
    for page, anchors in _pages(result.detail["payload"]):
        assert page == int(provenance[anchors[0]][-1]) + 1
    stored = run_store.load_json(run_store.record_dir("run1", "Citation__" + PAPER_ID) / "final.json")["payload"]
    assert [p for p, _ in _pages(stored)] == [p for p, _ in _pages(result.detail["payload"])]
