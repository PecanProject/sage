"""Correction pass, Fix 3: ungrounded AUXILIARY facts / fields no longer sink an otherwise valid record.

Real evidence (Daren-1997-Canopy run 20260919T211137_77879c98, Felipe-2010-Cultivar run felipe_smoke_20260920T132343).
Replaying the stored Extraction attempts of every record that ended in `error` (tests/fixtures/pass2/replay_extractions.json,
last attempt of each): in every one the identity-bearing fact was grounded and only auxiliary facts were not --
`definition`/`measurement_method` (Daren Leaf-area-index), `growth_stage` (Daren Method), `definition`+`unit` (Felipe
harvest index), `measurement_units` (soluble solids), `effect_size_percent` (total fruit), a fact with no anchors
(shoot biomass `unit`) and a Study's design descriptions. On the Conversion side, Daren's internode-length Variable was
lost to an optional `notes` that says "Name of the variable as described in the methods.".

Item 13 dropped such a fact only for TABLE candidates (a known cell value proves the value fact real). This generalises
it by field NAME, per entity type, from the IR schema/protocol: an ungrounded fact named like an identity/value-bearing
field of its entity is never dropped, nothing is dropped without a grounded fact remaining, a pipeline-level error is never
hidden, and Observation is excluded. On the Conversion side only DESCRIPTIVE optional fields may be demoted, and only when
every error is inside them -- the re-validated remainder is then proven grounded by the same deterministic validator.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import orchestrator, run_store, validators
from pipeline.raw_schema import RawExtraction
from test_orchestrator import PAPER_ID, _inv, env, make_invoke_sequence  # noqa: F401  (env is a pytest fixture)

FIXTURES = Path(__file__).parent / "fixtures" / "pass2"


@pytest.fixture()
def real_papers(monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(FIXTURES))


def _replays() -> dict[str, dict]:
    return json.loads((FIXTURES / "replay_extractions.json").read_text())


def _analyse(case_key: str):
    parsed = _replays()[case_key]
    paper, entity_type, _ = case_key.split("::")
    shape_dropped = []
    try:
        extraction = RawExtraction.model_validate(parsed)
    except Exception:
        recovered = orchestrator._recover_extraction_shape(parsed, entity_type)
        assert recovered is not None, "the shape problem should be recoverable"
        extraction, shape_dropped = recovered
    errors = orchestrator._raw_extraction_grounding_errors(paper, extraction)
    kept, remaining, dropped = orchestrator._drop_ungrounded_noncore_facts(entity_type, extraction, errors)
    return extraction, errors, kept, remaining, dropped, shape_dropped


# --------------------------------------------------------------------- #
# the real failures, replayed
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("case, expected_dropped", [
    ("Daren-1997-Canopy::Variable::variable_leaf_area_index", {"definition", "measurement_method"}),
    ("Daren-1997-Canopy::Method::method_leaf_internode_dimensions", {"growth_stage"}),
    ("Felipe-2010-Cultivar::Variable::variable_harvest_index", {"definition", "unit"}),
    ("Felipe-2010-Cultivar::Variable::variable_fruit_soluble_solids", {"measurement_units"}),
    ("Felipe-2010-Cultivar::Variable::variable_total_fruit_biomass", {"effect_size_percent"}),
    ("Felipe-2010-Cultivar::Study::study_tomato_covercrop_cultivar_mixture_experiment", {"cultivar_mixtures", "main_plot_factor"}),
])
def test_every_real_grounding_failure_of_an_auxiliary_fact_is_recovered(real_papers, case, expected_dropped):
    extraction, errors, kept, remaining, dropped, _ = _analyse(case)
    assert errors, "the real attempt did fail grounding"
    assert remaining == [] and {d["field_name"] for d in dropped} == expected_dropped
    assert all(d["rule"] == "ungrounded_auxiliary_fact" and d["errors"] and d["anchors"] for d in dropped)
    assert len(kept.facts) == len(extraction.facts) - len(dropped)
    # nothing that was dropped survives, and everything kept is grounded
    assert orchestrator._raw_extraction_grounding_errors(case.split("::")[0], kept) == []


def test_the_identity_fact_of_each_real_recovered_record_is_untouched(real_papers):
    _, _, kept, _, _, _ = _analyse("Daren-1997-Canopy::Variable::variable_leaf_area_index")
    assert "variable_name" in [f.field_name for f in kept.facts]


def test_a_fact_with_no_anchors_is_recovered_as_an_ungrounded_auxiliary_fact(real_papers):
    """Real Felipe shoot_biomass: its `unit` fact has no anchors, so the whole extraction failed shape validation."""
    case = "Felipe-2010-Cultivar::Variable::variable_shoot_biomass"
    with pytest.raises(Exception):
        RawExtraction.model_validate(_replays()[case])
    _, _, kept, remaining, dropped, shape_dropped = _analyse(case)
    assert [d["field_name"] for d in shape_dropped] == ["unit"] and "anchors" in shape_dropped[0]["errors"][0]
    assert remaining == [] and "variable_name" in [f.field_name for f in kept.facts]


# --------------------------------------------------------------------- #
# mutation tests: what must NOT be dropped
# --------------------------------------------------------------------- #


def _mutated(case: str, mutate):
    parsed = json.loads(json.dumps(_replays()[case]))
    mutate(parsed)
    return RawExtraction.model_validate(parsed), case.split("::")[0], case.split("::")[1]


LAI = "Daren-1997-Canopy::Variable::variable_leaf_area_index"


def test_an_ungrounded_identity_named_fact_is_never_dropped(real_papers):
    def corrupt_name(p):
        next(f for f in p["facts"] if f["field_name"] == "variable_name")["raw_text_excerpt"] = "Leaf Area Index (a paraphrase)"
    extraction, paper, et = _mutated(LAI, corrupt_name)
    errors = orchestrator._raw_extraction_grounding_errors(paper, extraction)
    kept, remaining, dropped = orchestrator._drop_ungrounded_noncore_facts(et, extraction, errors)
    assert dropped == [] and remaining == errors and kept is extraction


def test_nothing_is_dropped_when_no_grounded_fact_would_remain(real_papers):
    def ungroundall(p):
        for f in p["facts"]:
            f["raw_text_excerpt"] = "not in the paper at all " + f["field_name"]
    extraction, paper, et = _mutated(LAI, ungroundall)
    errors = orchestrator._raw_extraction_grounding_errors(paper, extraction)
    assert orchestrator._drop_ungrounded_noncore_facts(et, extraction, errors)[1:] == (errors, [])


def test_a_mixture_of_droppable_and_identity_failures_drops_nothing(real_papers):
    def corrupt(p):
        p["facts"][0]["field_name"] = "variable_name"
        p["facts"][0]["raw_text_excerpt"] = "paraphrased"
    extraction, paper, et = _mutated(LAI, corrupt)
    errors = orchestrator._raw_extraction_grounding_errors(paper, extraction)
    kept, remaining, dropped = orchestrator._drop_ungrounded_noncore_facts(et, extraction, errors)
    assert dropped == [] and remaining == errors


def test_a_pipeline_level_error_is_never_hidden(real_papers):
    extraction, _, et = _mutated(LAI, lambda p: None)
    pipeline_error = {"field": None, "message": "cannot verify raw evidence grounding: content.md missing"}
    assert orchestrator._drop_ungrounded_noncore_facts(et, extraction, [pipeline_error])[1:] == ([pipeline_error], [])


def test_observation_is_excluded_from_the_generalisation(real_papers):
    extraction, paper, _ = _mutated(LAI, lambda p: None)
    errors = orchestrator._raw_extraction_grounding_errors(paper, extraction)
    assert errors
    assert orchestrator._drop_ungrounded_noncore_facts("Observation", extraction, errors)[1:] == (errors, [])
    assert orchestrator._recover_extraction_shape(_replays()["Felipe-2010-Cultivar::Variable::variable_shoot_biomass"], "Observation") is None


def test_a_fact_with_no_usable_name_or_an_identity_name_is_not_shape_recovered(real_papers):
    parsed = json.loads(json.dumps(_replays()["Felipe-2010-Cultivar::Variable::variable_shoot_biomass"]))
    bad = next(f for f in parsed["facts"] if not f.get("anchors"))
    bad["field_name"] = "variable_name"           # the anchor-less fact is now identity-named ...
    bad["raw_value"] = "shoot biomass"            # ... and carries a value (a null-valued one has nothing to protect: test_citation_null_facts)
    assert orchestrator._recover_extraction_shape(parsed, "Variable") is None
    bad["field_name"] = ""
    assert orchestrator._recover_extraction_shape(parsed, "Variable") is None


def test_core_names_per_entity_type_come_from_the_schema_identities():
    core = orchestrator.CORE_FACT_FIELDS
    assert {"name", "variable_name"} <= core["Variable"] and "definition" not in core["Variable"]   # definition is Variable metadata ...
    assert "definition" in core["Treatment"]                                                        # ... but a Treatment's identity
    assert {"event_type", "date"} <= core["Management"] and core["Study"] == frozenset()


# --------------------------------------------------------------------- #
# end to end through run_record
# --------------------------------------------------------------------- #

VAR_ID = PAPER_ID + "_variable_title"
RAW_VAR = {"paper_id": PAPER_ID, "entity_type": "Variable", "record_id": VAR_ID, "facts": [
    {"field_name": "variable_name", "raw_value": "A Title", "raw_text_excerpt": "A Title", "anchors": ["b:0003"]},
    {"field_name": "definition", "raw_value": "x", "raw_text_excerpt": "a definition the paper never wrote", "anchors": ["b:0003"]},
]}


def _src(anchor):
    return {"source_document_id": PAPER_ID, "page_number": 1, "section_path": [], "locators": [{"kind": "text", "block_anchor": anchor}]}


def _var_payload(**extra):
    payload = {"id": VAR_ID, "name": {"value": "A Title", "provenance_label": "EXTRACTED", "source": _src("b:0003")}}
    payload.update(extra)
    return payload


def _run_var(env, sequence):
    return orchestrator.run_record(
        run_id="run1", paper_id=PAPER_ID, entity_type="Variable", record_id=VAR_ID, model="m",
        client=env["client"], invoke=make_invoke_sequence(sequence), enable_ai_validation=False,
    )


def test_run_record_drops_an_ungrounded_auxiliary_fact_and_never_shows_it_to_conversion(env):
    prompts = []
    conv = _inv("converter", _var_payload())

    def invoke(agent, model, prompt, timeout=300):
        prompts.append((agent, prompt))
        return _inv("extractor", RAW_VAR) if agent == "extractor" else conv

    result = orchestrator.run_record(run_id="run1", paper_id=PAPER_ID, entity_type="Variable", record_id=VAR_ID, model="m",
                                     client=env["client"], invoke=invoke, enable_ai_validation=False)
    assert result.status == "ready" and [a for a, _ in prompts].count("extractor") == 1
    assert "a definition the paper never wrote" not in next(p for a, p in prompts if a == "converter")
    assert [d["field_name"] for d in result.detail["dropped_ungrounded_facts"]] == ["definition"]


def test_conversion_side_an_ungrounded_optional_notes_is_demoted_not_fatal(env):
    bad_notes = {"value": "Name of the variable as described in the methods.", "provenance_label": "EXTRACTED", "source": _src("b:0004")}
    result = _run_var(env, [("extractor", _inv("extractor", {**RAW_VAR, "facts": RAW_VAR["facts"][:1]})),
                            ("converter", _inv("converter", _var_payload(notes=bad_notes)))])
    assert result.status == "ready"
    assert "notes" not in result.detail["payload"] and result.detail["payload"]["name"]["value"] == "A Title"
    demoted = result.detail["demoted_optional_fields"]
    assert [d["field"] for d in demoted] == ["notes"] and demoted[0]["value"] == bad_notes["value"]
    assert "not supported by block b:0004" in demoted[0]["errors"][0]
    key = "Variable__" + VAR_ID
    assert run_store.load_json(run_store.record_dir("run1", key) / "record_manifest.json")["demoted_optional_fields"] == ["notes"]
    assert run_store.load_json(run_store.record_dir("run1", key) / "conversion_demotion" / "attempt1.json")["reproposal"]["valid"] is True


def test_conversion_side_an_ungrounded_identity_is_never_demoted(env):
    bad_name = {"value": "Something Else", "provenance_label": "EXTRACTED", "source": _src("b:0003")}
    payload = _var_payload(name=bad_name)
    result = _run_var(env, [("extractor", _inv("extractor", {**RAW_VAR, "facts": RAW_VAR["facts"][:1]}))]
                      + [("converter", _inv("converter", payload))] * 6)
    assert result.status == "unresolved" and "demoted_optional_fields" not in result.detail


def test_conversion_side_identity_and_optional_failing_together_is_not_demoted(env):
    bad_name = {"value": "Something Else", "provenance_label": "EXTRACTED", "source": _src("b:0003")}
    bad_notes = {"value": "made up", "provenance_label": "EXTRACTED", "source": _src("b:0004")}
    result = _run_var(env, [("extractor", _inv("extractor", {**RAW_VAR, "facts": RAW_VAR["facts"][:1]}))]
                      + [("converter", _inv("converter", _var_payload(name=bad_name, notes=bad_notes)))] * 6)
    assert result.status == "unresolved"


def test_the_real_daren_internode_variable_is_valid_once_its_ungrounded_notes_is_demoted(real_papers):
    real = json.loads((FIXTURES / "daren_internode_variable.json").read_text())
    payload, errors = real["payload"], real["errors"]
    demotion = orchestrator._demote_auxiliary_payload_fields("Variable", payload, errors)
    assert demotion is not None
    reduced, demoted = demotion
    assert [d["field"] for d in demoted] == ["notes"] and "notes" not in reduced
    assert validators.validate_provenance("Daren-1997-Canopy", payload) != []          # the original really fails
    assert validators.validate_provenance("Daren-1997-Canopy", reduced) == []          # the remainder is grounded


def test_the_auxiliary_payload_registry_lists_only_descriptive_optional_fields():
    reg = orchestrator.AUXILIARY_PAYLOAD_FIELDS
    identities_and_values = {"name", "cultivar", "event_type", "value", "variable_name", "statistical_encoding",
                             "reported_effect_scope", "aggregated_over_factors", "temporal_info", "amount", "date"}
    assert all(not (fields & identities_and_values) for fields in reg.values())
