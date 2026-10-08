"""An Observation is composed from its established experimental context, and checked against it.

The chain cell -> variable -> method -> treatment/factor -> time -> site -> aggregation -> statistics -> design is READ
from what earlier stages built (table reconstruction, Variable->Method map, linked records, design.json) and handed to
Extraction (as the packet) and Conversion (as EXPERIMENTAL_CONTEXT). Conversion's output must agree with it; a
disagreement is a validation error, never silently overwritten.

Real cells behind the rules: Felipe "20±1.0 a" under "Fallow mean ± SE" (ready rows had statistical_encoding null and
the SE inside the value); Kathryn "9.5 [9.3, 9.8]" with "95% confidence limits"; Philippe "–0.69 a" (a signed mean with
a letter, and no statistic named); Smukler EC committed READY in "mg L-1" although EC is in "µS cm-1".
"""

from __future__ import annotations

import pytest

from pipeline import cell_values, context_bundle, method_map as mm, orchestrator
from test_design import philippe_table1
from test_orchestrator import env  # noqa: F401 (fixture)


# --------------------------------------------------------------------------- #
# The cell and its statistic
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text, mean, dispersion, interval, letters", [
    ("20±1.0 a", 20.0, 1.0, None, "a"), ("–0.69 a", -0.69, None, None, "a"), ("9.5 [9.3, 9.8]", 9.5, None, (9.3, 9.8), None),
    ("$0.36 {\\pm} 0.02$ a", 0.36, 0.02, None, "a"), ("88±22 mm", 88.0, 22.0, None, None), ("378", 378.0, None, None, None),
])
def test_a_cell_is_split_into_mean_dispersion_interval_and_letters(text, mean, dispersion, interval, letters):
    cell = cell_values.parse_cell(text)
    assert (cell.mean, cell.dispersion, cell.interval, cell.letters) == (mean, dispersion, interval, letters)


@pytest.mark.parametrize("text", ["0.35-0.4", "< 0.0001", "–", "increased by 20%"])
def test_a_range_a_threshold_a_dash_or_prose_is_not_a_cell_value(text):
    assert cell_values.parse_cell(text) is None


def test_the_statistic_is_named_only_where_the_table_says_it():
    se = cell_values.statistic_basis([("b:0069", "| DAP | Fallow mean ± SE |")], cell_values.parse_cell("20±1.0 a"))
    ci = cell_values.statistic_basis([("b:0176", "ratios and 95% confidence limits prior to")], cell_values.parse_cell("9.5 [9.3, 9.8]"))
    unnamed = cell_values.statistic_basis([("x", "Mean yield by system")], cell_values.parse_cell("3 ± 1"))
    assert (se.name, se.source_anchor) == ("SE", "b:0069") and ci.name == "95% CI" and unnamed.name is None


# --------------------------------------------------------------------------- #
# Composition: the chain is read, never re-decided
# --------------------------------------------------------------------------- #

def _record(entity_type, record_id, **fields):
    payload = {k: {"value": v, "provenance_label": "EXTRACTED",
                   "source": {"locators": [{"kind": "text", "block_anchor": "b:0032"}]}} for k, v in fields.items()}
    return {"entity_type": entity_type, "record_id": record_id, "status": "ready", "detail": {"payload": payload}}


RECORDS = {
    "Method": [_record("Method", "m_caliper", name="Stem basal diameter", description="calliper rule")],
    "Treatment": [_record("Treatment", "t_low", name="PAR t 0-0.1", definition="lowest light class")],
    "Variable": [_record("Variable", "v_inc", name="annual increment", units="mm")],
    "Site": [_record("Site", "s1", name="Chaîne des Puys")],
}


def _inc_candidate():
    candidates = orchestrator._table_classification_to_candidates(philippe_table1(), {})
    return next(c for c in candidates if c.candidate_id == "inc_r1")


def test_the_chain_is_composed_from_the_established_links():
    links = {mm.variable_key("INC (mm)"): mm.MethodLink(mm.LINKED, "hint", "m_caliper", "m_caliper", [], "calliper hint")}
    summary = {"factors": {"PAR t": {"dimension": "treatment"}, "Year": {"dimension": "time"}},
               "tables": [{"anchors": ["b:0053"], "layout": "main_effects"}],
               "sample_size_evidence": [{"anchor": "b:0046", "matches": ["(6, 6, 6)"]}],
               "conflicts": [{"subject": "PAR t", "claim_a": "0.2-0.35", "source_a": "b:0053", "claim_b": "0.2-0.37", "source_b": "b:0046"}]}
    refs = {"treatment_id": "t_low", "variable_id": "v_inc", "site_id": "s1", "method_id": "m_caliper"}
    context, evidence = orchestrator._observation_experimental_context("none", _inc_candidate(), refs, RECORDS, links, summary)
    assert context["cell"]["value_text"] == "1.7 a" and context["cell"]["row_levels"] == {"PAR t": "0-0.1"}
    assert context["method"] == {"status": "linked", "tier": "hint", "reason": "calliper hint", "record_id": "m_caliper",
                                 "name": "Stem basal diameter"}
    assert context["treatment"]["name"] == "PAR t 0-0.1" and context["treatment"]["levels"]["PAR t"]["dimension"] == "treatment"
    assert context["aggregation"] == {"reported_effect_scope": "aggregated_mean", "aggregated_over_factors": ["Year"],
                                      "basis": "table layout (main-effect row)"}
    assert context["time"]["pooled over"] == "2001, 2006"
    assert context["statistics"]["mean"] == 1.7 and context["statistics"]["statistic_name"] is None
    assert context["statistics"]["sample_size_evidence"] == ["b:0046"]
    assert context["design"]["layout"] == "main_effects" and "0.2-0.37" in context["design"]["conflicts"][0]
    assert context["site"].startswith("s1")


def test_an_unestablished_method_is_reported_as_such_never_filled_in():
    links = {mm.variable_key("INC (mm)"): mm.MethodLink(mm.AMBIGUOUS, None, reason="2 Methods fit", candidates=["a", "b"])}
    context, _ = orchestrator._observation_experimental_context("none", _inc_candidate(), {"method_id": ["a", "b"]}, RECORDS, links, None)
    assert context["method"]["status"] == mm.AMBIGUOUS and context["method"]["record_id"] is None
    rendered = context_bundle._render_experimental_context(context)
    assert "method: NOT ESTABLISHED" in rendered and "do not choose one" in rendered


# --------------------------------------------------------------------------- #
# Relationship checks on Conversion's output
# --------------------------------------------------------------------------- #

def _qv(text, numeric, units="mm"):
    return {"value": {"reported_text": text, "reported_numeric_value": numeric, "reported_units": units},
            "provenance_label": "EXTRACTED", "source": {"locators": [{"kind": "table", "table_id": "b:0053"}]}}


def _f(value, label="EXTRACTED"):
    return {"value": value, "provenance_label": label, "source": {"locators": [{"kind": "table", "table_id": "b:0053"}]}}


CONTEXT = {"experimental_context": {
    "cell": {"value_text": "20±1.0 a"},
    "statistics": {"mean_text": "20", "mean": 20.0, "statistic_name": "SE", "statistic_value": 1.0,
                   "statistic_source_anchor": "b:0069", "statistic_source_text": "± SE"},
    "method": {"status": "linked", "record_id": "m_par", "reason": "hint"},
    "aggregation": {"reported_effect_scope": "aggregated_mean", "aggregated_over_factors": ["cultivar mixture"], "basis": "pooling statement"},
    "variable": {"record_id": "v_par", "record_units": "%"},
    "time": {"pooled over": "2005, 2006"},
}}


def _good():
    return {"value": _qv("20", 20.0, "%"), "statistical_encoding": _f({"statistic_name": "SE", "statistic_value": 1.0}),
            "method_id": "m_par", "reported_effect_scope": _f("aggregated_mean"),
            "aggregated_over_factors": _f(["Cultivar mixture"]), "temporal_info": _f({"reported_text": "2005, 2006",
                                                                                     "earliest": "2005-01-01", "latest": "2006-12-31"})}


def test_an_observation_that_agrees_with_its_context_passes():
    assert orchestrator._observation_relationship_errors("Observation", _good(), CONTEXT) == []


@pytest.mark.parametrize("change, field", [
    (lambda p: p.update(value=_qv("1.0", 1.0, "%")), "value"),                                     # the SE taken as the mean
    (lambda p: p.pop("statistical_encoding"), "statistical_encoding"),                            # the named SE dropped
    (lambda p: p.update(statistical_encoding=_f({"statistic_name": "SD", "statistic_value": 1.0})), "statistical_encoding"),
    (lambda p: p.update(method_id="m_other"), "method_id"),                                       # not the map's method
    (lambda p: p.update(reported_effect_scope=_f("treatment_mean")), "reported_effect_scope"),   # a pooled value as a cell
    (lambda p: p.update(aggregated_over_factors=_f(["Year"])), "aggregated_over_factors"),
    (lambda p: p.update(value=_qv("20", 20.0, "mg L-1")), "value.reported_units"),                 # Smukler EC
    (lambda p: p.update(temporal_info=_f({"reported_text": "2006", "earliest": "2006-01-01", "latest": "2006-12-31"})), "temporal_info"),
])
def test_every_disagreement_with_the_context_is_a_validation_error(change, field):
    payload = _good()
    change(payload)
    assert field in [e["field"] for e in orchestrator._observation_relationship_errors("Observation", payload, CONTEXT)]


def test_a_statistic_the_table_never_names_may_not_be_invented():
    context = {"experimental_context": {"statistics": {"mean_text": "-0.69", "mean": -0.69, "statistic_name": None}}}
    payload = {"value": _qv("–0.69", -0.69), "statistical_encoding": _f({"statistic_name": "SE", "statistic_value": 0.1})}
    [error] = orchestrator._observation_relationship_errors("Observation", payload, context)
    assert error["field"] == "statistical_encoding" and "never states" in error["message"]


def test_the_checks_apply_only_to_observations_with_a_context():
    assert orchestrator._observation_relationship_errors("Variable", {"value": _qv("1", 1.0)}, CONTEXT) == []
    assert orchestrator._observation_relationship_errors("Observation", {"value": _qv("1", 1.0)}, {}) == []


def test_the_conversion_note_states_the_established_fields():
    note = orchestrator._experimental_conversion_note(CONTEXT["experimental_context"])
    assert "reported_text '20'" in note and "statistic_name 'SE'" in note and "method_id: m_par" in note
    assert "never one of those levels alone" in note


# --------------------------------------------------------------------------- #
# The packet: the same framework, the chain attached
# --------------------------------------------------------------------------- #

def test_the_observation_packet_carries_the_context_and_its_evidence_blocks(tmp_path):
    from test_document_map import _write_paper

    _write_paper(tmp_path, "o", [
        ("b:0001", "Text", "Stem basal diameter was measured with a calliper rule.", 0),
        ("b:0002", "Caption", "*Table 1. Mean growth by PAR t class and year.*", 0),
        ("b:0003", "Table", "| | INC (mm) |\n|---|---|\n| PAR t 0-0.1 | 1.7 a |", 0),
    ])
    context = {"cell": {"table_anchor": "b:0003", "value_text": "1.7 a", "row_levels": {"PAR t": "0-0.1"}, "column_label": "INC (mm)"},
               "variable": {"label": "INC (mm)"}, "method": {"status": "linked", "record_id": "m1", "tier": "hint", "reason": "calliper"}}
    bundle = context_bundle.build_observation_bundle("o", context, {"table": ["b:0002", "b:0003"], "method": ["b:0001"]}, tmp_path)
    assert bundle.entity_type == "Observation" and bundle.anchors == ("b:0002", "b:0003", "b:0001")
    text = bundle.render()
    assert text.index("EXPERIMENTAL CONTEXT") < text.index("EVIDENCE PACKET")
    assert "literal cell text '1.7 a'" in text and "method: m1" in text
    assert bundle.to_dict()["experimental_context"]["cell"]["value_text"] == "1.7 a"


# --------------------------------------------------------------------------- #
# End to end: a disagreement is fed back and corrected, never overwritten
# --------------------------------------------------------------------------- #

def test_run_record_feeds_a_relationship_error_back_to_conversion(env):
    from test_orchestrator import PAPER_ID, _inv, _paper_payload, make_invoke_sequence

    refs = {"citation_id": PAPER_ID, "site_id": "s", "treatment_id": "t", "method_id": "m"}
    good = _paper_payload("Observation", "o1", refs)                     # treatment_mean, value 2012 at b:0002
    pooled = _paper_payload("Observation", "o1", refs)
    pooled["reported_effect_scope"] = {**pooled["reported_effect_scope"], "value": "aggregated_mean"}
    pooled["aggregated_over_factors"] = {**pooled["aggregated_over_factors"], "value": ["Year"]}
    raw = {"paper_id": PAPER_ID, "entity_type": "Observation", "record_id": "o1", "facts": [
        {"field_name": "value", "raw_value": "2012", "raw_text_excerpt": "Published in 2012.", "anchors": ["b:0002"]}]}
    prompts = []
    inner = make_invoke_sequence([("extractor", _inv("extractor", raw)), ("converter", _inv("converter", pooled)),
                                  ("converter", _inv("converter", good))])

    def invoke(agent, model, prompt, timeout=300):
        prompts.append(prompt)
        return inner(agent, model, prompt, timeout)

    context = {"experimental_context": {"statistics": {"mean_text": "2012", "mean": 2012.0, "statistic_name": None},
                                        "aggregation": {"reported_effect_scope": "treatment_mean", "aggregated_over_factors": [],
                                                        "basis": "cell mean"}}}
    result = orchestrator.run_record(run_id="run1", paper_id=PAPER_ID, entity_type="Observation", record_id="o1",
                                     model="test-model", client=env["client"], invoke=invoke, enable_ai_validation=False,
                                     known_refs=refs, candidate_context=context)
    assert result.status == "ready"
    assert "EXPERIMENTAL_CONTEXT" in prompts[1]
    assert "reported_effect_scope 'aggregated_mean', but this value is a treatment_mean" in prompts[2]
