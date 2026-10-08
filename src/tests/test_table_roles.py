"""`TableClassification.table_role` -- what KIND of table a reconstructed table
is -- and its use to gate candidate generation.

Real evidence: a weather table (Location x Month: temperature, precipitation)
was classified as an applicable, cell-level data table, so it produced 8
"Treatments" like `ames_ia_may` and 32 Observation candidates. The fix is
semantic, not paper-specific: only a `treatment_response` table may feed
cell-level Treatment/Observation enumeration; an `aggregated_summary` stays
identifiable as a source of aggregated data (protocol Section 7.4) that the
current IR cannot express as a normal treatment-combination row; a
`weather_context` or `non_enumerable` table never produces candidates.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from pipeline import orchestrator
from pipeline.raw_schema import TableClassification, TableRowGroup, TableValueColumn

ROLES = ["treatment_response", "aggregated_summary", "weather_context", "non_enumerable"]


def _weather_like(**kwargs) -> TableClassification:
    """A Location x Month table -- structurally a perfectly good table."""
    return TableClassification(
        table_anchors=["b:0048"],
        value_columns=[
            TableValueColumn(value_column_id="tmax", variable_name_hint="maximum temperature", units_hint="C"),
            TableValueColumn(value_column_id="precip", variable_name_hint="precipitation", units_hint="mm"),
        ],
        row_groups=[
            TableRowGroup(row_group_id="ames_may", factor_values={"Location": "Ames, IA", "Month": "May"},
                          source_table_anchor="b:0048", cells={"tmax": "28.5", "precip": "122"}),
            TableRowGroup(row_group_id="ames_june", factor_values={"Location": "Ames, IA", "Month": "June"},
                          source_table_anchor="b:0048", cells={"tmax": "31.8", "precip": "188"}),
        ],
        **kwargs,
    )


# --------------------------------------------------------------------- #
# schema: role is authoritative, legacy flags are derived / must agree
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("role, applicable, scope", [
    ("treatment_response", True, "cell_level"),
    ("aggregated_summary", True, "aggregated_summary"),
    ("weather_context", False, "cell_level"),
    ("non_enumerable", False, "cell_level"),
])
def test_the_legacy_flags_are_derived_from_the_role(role, applicable, scope):
    tc = _weather_like(table_role=role, reason=None if role == "treatment_response" else "because")
    assert (tc.table_role, tc.applicable, tc.aggregation_scope) == (role, applicable, scope)


@pytest.mark.parametrize("legacy, role", [
    ({"applicable": True}, "treatment_response"),
    ({"applicable": True, "aggregation_scope": "aggregated_summary", "reason": "pooled"}, "aggregated_summary"),
    ({"applicable": False, "reason": "regression table"}, "non_enumerable"),
])
def test_a_legacy_payload_without_a_role_is_upgraded_from_its_flags(legacy, role):
    assert _weather_like(**legacy).table_role == role


def test_a_payload_with_neither_role_nor_applicable_is_rejected():
    with pytest.raises(ValidationError, match="table_role is required"):
        _weather_like()


@pytest.mark.parametrize("kwargs, match", [
    ({"table_role": "treatment_response", "applicable": False, "reason": "x"}, "contradicts applicable"),
    ({"table_role": "weather_context", "applicable": True, "reason": "x"}, "contradicts applicable"),
    ({"table_role": "aggregated_summary", "aggregation_scope": "cell_level", "reason": "x"}, "contradicts aggregation_scope"),
    ({"table_role": "treatment_response", "aggregation_scope": "aggregated_summary", "reason": "x"}, "contradicts aggregation_scope"),
])
def test_contradictory_role_and_legacy_flags_are_rejected(kwargs, match):
    with pytest.raises(ValidationError, match=match):
        _weather_like(**kwargs)


@pytest.mark.parametrize("role", ["aggregated_summary", "weather_context", "non_enumerable"])
def test_every_role_except_treatment_response_requires_a_reason(role):
    with pytest.raises(ValidationError, match="reason"):
        _weather_like(table_role=role)


def test_an_unknown_role_is_rejected():
    with pytest.raises(ValidationError):
        _weather_like(table_role="soil_thing", reason="x")


def test_round_trip_through_model_dump_keeps_role_and_legacy_fields_consistent():
    tc = _weather_like(table_role="weather_context", reason="monthly climate")
    again = TableClassification.model_validate(json.loads(json.dumps(tc.model_dump())))
    assert (again.table_role, again.applicable, again.aggregation_scope) == ("weather_context", False, "cell_level")


def test_a_cached_classification_written_before_roles_existed_still_loads():
    # shape of a real cached final.json["classification"] from a pre-role run
    cached = {
        "applicable": False, "aggregation_scope": "aggregated_summary",
        "reason": "reports LSD statistics, not per-instance measured values", "table_anchors": ["b:0178"],
        "value_columns": [], "row_groups": [],
    }
    tc = TableClassification.model_validate(cached)
    assert tc.table_role == "non_enumerable" and tc.applicable is False


# --------------------------------------------------------------------- #
# candidate generation is gated by role
# --------------------------------------------------------------------- #


def test_a_treatment_response_table_still_generates_candidates():
    tc = _weather_like(table_role="treatment_response")
    assert len(orchestrator._table_classification_to_candidates(tc, {})) == 4
    treatments, covered = orchestrator._table_classifications_to_treatment_candidates([tc], {})
    assert len(treatments) == 2 and covered == {"b:0048"}


@pytest.mark.parametrize("role", ["aggregated_summary", "weather_context", "non_enumerable"])
def test_no_other_role_produces_observation_or_treatment_candidates(role):
    tc = _weather_like(table_role=role, reason="explained")
    assert orchestrator._table_classification_to_candidates(tc, {}) == []
    treatments, covered = orchestrator._table_classifications_to_treatment_candidates([tc], {})
    assert treatments == [] and covered == set()


def test_a_weather_table_never_becomes_treatments_named_after_months():
    """The observed defect: Location x Month rows became `ames_ia_may` etc."""
    tc = _weather_like(table_role="weather_context", reason="monthly temperature and precipitation per location")
    treatments, _ = orchestrator._table_classifications_to_treatment_candidates([tc], {})
    assert [t.candidate_id for t in treatments] == []


# --------------------------------------------------------------------- #
# Step B prompt and pass
# --------------------------------------------------------------------- #

PAPER_SPECIFIC_WORDS = ["daren", "felipe", "ames", "mead", "trailblazer", "switchgrass", "tomato", "mustard", "table 1", "table 2", "table 7"]


def _role_section(prompt: str) -> str:
    return prompt[prompt.index("- table_role:"): prompt.index("- table_anchors:")]


def test_the_prompt_defines_all_four_roles_in_generic_protocol_terms():
    prompt = orchestrator._table_classification_prompt("p", "b:0001", [])
    section = _role_section(prompt)
    for role in ROLES:
        assert f"'{role}'" in section
    lowered = section.lower()
    assert not [w for w in PAPER_SPECIFIC_WORDS if w in lowered], "role definitions must not be paper-specific"
    # soil/plant/gas measurements are responses, not context (protocol target variables)
    assert "NOT weather_context" in section
    assert "applicable, aggregation_scope" not in prompt  # the old keys are no longer requested


def test_the_prompt_states_that_mapping_fields_are_json_objects_not_lists():
    prompt = orchestrator._table_classification_prompt("p", "b:0001", [])
    assert "JSON OBJECTS" in prompt and "write {} when empty, never []" in prompt
    assert "context_levels and each value column's factor_levels" in prompt


def test_the_prompt_sends_statistical_model_output_tables_to_non_enumerable():
    section = _role_section(orchestrator._table_classification_prompt("p", "b:0001", []))
    assert "equations, regression coefficients, R-squared values or test statistics" in section
    assert "rather than measured quantities is 'non_enumerable'" in section
    lowered = section.lower()
    assert not [w for w in PAPER_SPECIFIC_WORDS if w in lowered]


def test_the_pass_accepts_a_role_only_answer_and_drops_context_and_non_enumerable_tables(tmp_path, monkeypatch):
    papers = tmp_path / "papers"
    pdir = papers / "syn"
    pdir.mkdir(parents=True)
    (pdir / "content.md").write_text(
        "| a | 1.0 |\n|---|---|\n| b | 2.0 |\n⟦b:0001⟧\n\nText.\n⟦b:0002⟧\n\n| c | 3.0 |\n|---|---|\n| d | 4.0 |\n⟦b:0003⟧\n", encoding="utf-8",
    )
    prov = {
        "b:0001": {"block_type": "Table", "page_id": "page_1", "section_path": []},
        "b:0002": {"block_type": "Text", "page_id": "page_1", "section_path": []},
        "b:0003": {"block_type": "Table", "page_id": "page_1", "section_path": []},
    }
    for n, (parent, text) in enumerate([("b:0001", "1.0"), ("b:0001", "2.0"), ("b:0003", "3.0"), ("b:0003", "4.0")]):
        prov[f"b:9{n:03d}"] = {"block_type": "TableCell", "page_id": "page_1", "section_path": [], "parent_table_anchor": parent,
                               "row_index": n, "col_index": 0, "cell_text": text}
    (pdir / "provenance.json").write_text(json.dumps(prov), encoding="utf-8")
    monkeypatch.setenv("IR_PAPERS_ROOT", str(papers))
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))

    def answer(prompt):
        if "anchor 'b:0001'" in prompt:  # role-only payload: no legacy flags at all
            return {"table_role": "treatment_response", "table_anchors": ["b:0001"],
                    "value_columns": [{"value_column_id": "v", "variable_name_hint": "y"}],
                    "row_groups": [{"row_group_id": "r1", "factor_values": {"k": "a"}, "source_table_anchor": "b:0001", "cells": {"v": "1.0"}},
                                   {"row_group_id": "r2", "factor_values": {"k": "b"}, "source_table_anchor": "b:0001", "cells": {"v": "2.0"}}]}
        return {"table_role": "weather_context", "reason": "monthly climate data", "table_anchors": ["b:0003"]}

    def invoke(agent, model, prompt, timeout=300):
        payload = answer(prompt)
        return orchestrator.AgentInvocation(agent=agent, model=model, prompt=prompt, returncode=0, stdout="{}", stderr="",
                                            final_text=json.dumps(payload), parsed_json=payload, parse_error=None)

    out = orchestrator.run_table_classification_pass(run_id="r1", paper_id="syn", model="m", invoke=invoke)
    assert list(out) == ["b:0001"] and out["b:0001"].table_role == "treatment_response"
    # the weather table was classified and its role recorded on disk, just not returned for candidate generation
    cached = orchestrator._load_cached_table_classification("r1", "table_classification__b_0003")
    assert cached is not None and cached.table_role == "weather_context" and cached.applicable is False
