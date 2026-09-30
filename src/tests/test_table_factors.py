"""Factor dimension + encoding (`TableClassification.factors`), Treatment
semantics applied on top of it, and aggregated-source handling
(`pooled_factors`, `aggregated_summary`).

The governing rule these tests pin: factor roles describe DIMENSION + ENCODING
only. They never declare that every experimental factor becomes a Treatment.
Per protocol Section 6.3 a Treatment must not carry information that belongs in
another field -- a cultivar/population is `crop`, a date/growth stage is `time`,
a location is `site` -- so only `treatment`-dimension factors (plus the site,
which every Treatment references) form a Treatment candidate's identity. A
design with no treatment dimension yields NO Treatment candidates; it is
reported as an explicit current-IR limitation (L1), never patched with an
invented Treatment.

Nothing here targets a record count: the expected counts below follow from the
declared structure of small synthetic tables.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from pipeline import orchestrator, run_store
from pipeline.raw_schema import (
    PooledFactor, TableClassification, TableFactor, TableRowGroup, TableValueColumn,
)


def _crop_time_site_table(**overrides) -> TableClassification:
    """Your example 1: crop -> rows, time -> columns, site -> context. Structural
    information only; it must not imply any Treatment."""
    kwargs = dict(
        table_role="treatment_response", table_anchors=["b:0001"],
        factors=[
            TableFactor(name="Population", dimension="crop", encoding="rows"),
            TableFactor(name="Maturity", dimension="time", encoding="columns"),
            TableFactor(name="Location", dimension="site", encoding="context"),
        ],
        context_levels={"Location": "Ames"},
        value_columns=[
            TableValueColumn(value_column_id="veg", variable_name_hint="tiller weight", units_hint="g",
                             factor_levels={"Maturity": "Vegetative"}),
            TableValueColumn(value_column_id="elo", variable_name_hint="tiller weight", units_hint="g",
                             factor_levels={"Maturity": "Elongating"}),
        ],
        row_groups=[
            TableRowGroup(row_group_id="tb", factor_values={"Population": "Trailblazer"}, source_table_anchor="b:0001",
                          cells={"veg": "0.06", "elo": "0.43"}),
            TableRowGroup(row_group_id="pf", factor_values={"Population": "Pathfinder"}, source_table_anchor="b:0001",
                          cells={"veg": "0.03", "elo": "0.55"}),
        ],
    )
    kwargs.update(overrides)
    return TableClassification(**kwargs)


def _treatment_columns_variable_rows(**overrides) -> TableClassification:
    """Your example 2: treatment -> columns, variable -> rows."""
    kwargs = dict(
        table_role="treatment_response", table_anchors=["b:0002"],
        factors=[
            TableFactor(name="Winter treatment", dimension="treatment", encoding="columns"),
            TableFactor(name="Variable", dimension="variable", encoding="rows"),
        ],
        value_columns=[
            TableValueColumn(value_column_id="fallow", variable_name_hint="value", factor_levels={"Winter treatment": "Fallow"}),
            TableValueColumn(value_column_id="mustard", variable_name_hint="value", factor_levels={"Winter treatment": "Mustard"}),
        ],
        row_groups=[
            TableRowGroup(row_group_id="par", factor_values={"Variable": "PAR intercepted (%)"}, source_table_anchor="b:0002",
                          cells={"fallow": "20", "mustard": "15"}),
            TableRowGroup(row_group_id="shoot", factor_values={"Variable": "Shoot biomass"}, source_table_anchor="b:0002",
                          cells={"fallow": "70", "mustard": "54"}),
        ],
    )
    kwargs.update(overrides)
    return TableClassification(**kwargs)


# --------------------------------------------------------------------- #
# schema validation of declared factors
# --------------------------------------------------------------------- #


def test_your_two_structural_examples_are_valid():
    assert _crop_time_site_table().factor_dimension("Maturity") == "time"
    assert _treatment_columns_variable_rows().factor_dimension("Winter treatment") == "treatment"


def test_a_legacy_classification_with_no_factors_is_untouched():
    tc = TableClassification(
        applicable=True, table_anchors=["b:0001"],
        value_columns=[TableValueColumn(value_column_id="y", variable_name_hint="yield", site_hint="Ames")],
        row_groups=[TableRowGroup(row_group_id="r", factor_values={"Anything": "x"}, source_table_anchor="b:0001", cells={"y": "1"})],
    )
    assert tc.factors == [] and tc.factor_dimension("Anything") is None


@pytest.mark.parametrize("mutation, match", [
    (dict(row_groups=[TableRowGroup(row_group_id="r", factor_values={"Undeclared": "x"}, source_table_anchor="b:0001", cells={})]),
     "not declared in factors"),
    (dict(row_groups=[TableRowGroup(row_group_id="r", factor_values={"Maturity": "x"}, source_table_anchor="b:0001", cells={})]),
     "encoding must be 'rows'"),
    (dict(value_columns=[TableValueColumn(value_column_id="c", variable_name_hint="v", factor_levels={"Population": "x"})], row_groups=[], reason="r"),
     "encoding must be 'columns'"),
    (dict(value_columns=[TableValueColumn(value_column_id="c", variable_name_hint="v", factor_levels={"Ghost": "x"})], row_groups=[], reason="r"),
     "not declared in factors"),
    (dict(context_levels={"Population": "x"}), "encoding must be 'context'"),
    (dict(context_levels={"Ghost": "x"}), "not declared in factors"),
])
def test_every_level_key_must_be_declared_with_the_matching_encoding(mutation, match):
    with pytest.raises(ValidationError, match=match):
        _crop_time_site_table(**mutation)


def test_duplicate_factor_names_are_rejected():
    dup = [TableFactor(name="X", dimension="crop", encoding="rows"), TableFactor(name="X", dimension="time", encoding="columns")]
    with pytest.raises(ValidationError, match="unique"):
        _crop_time_site_table(factors=dup, context_levels={}, value_columns=[], row_groups=[], reason="r")


def test_factor_levels_without_declared_factors_are_rejected():
    with pytest.raises(ValidationError, match="factors is empty"):
        TableClassification(
            applicable=True, table_anchors=["b:0001"],
            value_columns=[TableValueColumn(value_column_id="c", variable_name_hint="v", factor_levels={"Maturity": "x"})],
            row_groups=[TableRowGroup(row_group_id="r", source_table_anchor="b:0001", cells={"c": "1"})],
        )


def test_an_unknown_dimension_or_encoding_is_rejected():
    with pytest.raises(ValidationError):
        TableFactor(name="X", dimension="cultivar", encoding="rows")
    with pytest.raises(ValidationError):
        TableFactor(name="X", dimension="crop", encoding="diagonal")


# --------------------------------------------------------------------- #
# Treatment semantics on top of the roles
# --------------------------------------------------------------------- #


def test_crop_time_site_dimensions_never_become_a_treatment():
    """Your example 1: structural information without implying Treatments."""
    treatments, covered = orchestrator._table_classifications_to_treatment_candidates([_crop_time_site_table()], {})
    assert treatments == []
    # ...but the table WAS analysed for Treatment (and has none), so the free-form pass is
    # not invited to re-invent Treatments out of it.
    assert covered == {"b:0001"}


def test_observation_candidates_still_carry_every_dimension_of_the_cell():
    candidates = orchestrator._table_classification_to_candidates(_crop_time_site_table(), {})
    assert len(candidates) == 4  # 2 populations x 2 maturity columns (blank cells would be skipped)
    veg_tb = next(c for c in candidates if c.candidate_id == "veg_tb")
    assert "Population=Trailblazer" in veg_tb.description and "Maturity=Vegetative" in veg_tb.description
    assert "Location=Ames" in veg_tb.description and veg_tb.known_value == "0.06"


def test_observation_linking_sees_column_and_context_levels():
    pools = {"site_id": [{"slug": "ames_ia", "name": "Ames", "record_id": "x"}, {"slug": "mead_ne", "name": "Mead", "record_id": "y"}]}
    veg_tb = next(c for c in orchestrator._table_classification_to_candidates(_crop_time_site_table(), pools) if c.candidate_id == "veg_tb")
    assert veg_tb.linked_candidates == {"site_id": "ames_ia"}  # the site came from context_levels, not from any row


def test_treatment_columns_and_variable_rows_yield_treatments_from_the_treatment_dimension_only():
    """Your example 2: the two treatment levels come from the COLUMNS; the variable rows
    (a `variable` dimension) contribute nothing to a Treatment's identity."""
    treatments, covered = orchestrator._table_classifications_to_treatment_candidates([_treatment_columns_variable_rows()], {})
    assert sorted(t.description for t in treatments) == [
        "Experimental condition: Winter treatment=Fallow", "Experimental condition: Winter treatment=Mustard",
    ]
    assert covered == {"b:0002"}
    observations = orchestrator._table_classification_to_candidates(_treatment_columns_variable_rows(), {})
    assert len(observations) == 4  # 2 variable rows x 2 treatment columns


def test_a_declared_treatment_factor_plus_a_site_forms_the_combination_and_crop_time_do_not():
    tc = TableClassification(
        table_role="treatment_response", table_anchors=["b:0003"],
        factors=[
            TableFactor(name="Tillage", dimension="treatment", encoding="rows"),
            TableFactor(name="Cultivar", dimension="crop", encoding="rows"),
            TableFactor(name="Year", dimension="time", encoding="rows"),
            TableFactor(name="Site", dimension="site", encoding="columns"),
        ],
        value_columns=[
            TableValueColumn(value_column_id="a", variable_name_hint="yield", factor_levels={"Site": "Ames"}),
            TableValueColumn(value_column_id="m", variable_name_hint="yield", factor_levels={"Site": "Mead"}),
        ],
        row_groups=[
            TableRowGroup(row_group_id="r1", factor_values={"Tillage": "no-till", "Cultivar": "X", "Year": "2019"},
                          source_table_anchor="b:0003", cells={"a": "1", "m": "2"}),
            TableRowGroup(row_group_id="r2", factor_values={"Tillage": "no-till", "Cultivar": "Y", "Year": "2020"},
                          source_table_anchor="b:0003", cells={"a": "3", "m": "4"}),
        ],
    )
    treatments, _ = orchestrator._table_classifications_to_treatment_candidates([tc], {})
    # cultivar and year rows collapse into the same Treatment; only tillage x site remain
    assert sorted(t.description for t in treatments) == [
        "Experimental condition: Site=Ames, Tillage=no-till", "Experimental condition: Site=Mead, Tillage=no-till",
    ]


def test_a_legacy_classification_still_treats_every_row_factor_as_before():
    tc = TableClassification(
        applicable=True, table_anchors=["b:0004"],
        value_columns=[TableValueColumn(value_column_id="y", variable_name_hint="yield", site_hint="Ames")],
        row_groups=[TableRowGroup(row_group_id="r", factor_values={"Population": "P", "Maturity": "V"},
                                  source_table_anchor="b:0004", cells={"y": "1"})],
    )
    treatments, covered = orchestrator._table_classifications_to_treatment_candidates([tc], {})
    assert [t.description for t in treatments] == ["Experimental condition: Maturity=V, Population=P, Site=Ames"]
    assert covered == {"b:0004"}


def test_a_legacy_classification_with_no_contribution_is_still_not_reported_covered():
    tc = TableClassification(
        applicable=True, table_anchors=["b:0005"], value_columns=[TableValueColumn(value_column_id="y", variable_name_hint="yield")],
        row_groups=[TableRowGroup(row_group_id="r", source_table_anchor="b:0005", cells={"y": "1"})],
    )
    assert orchestrator._table_classifications_to_treatment_candidates([tc], {}) == ([], set())


# --------------------------------------------------------------------- #
# deterministic cross-check of declared dimensions
# --------------------------------------------------------------------- #


def _population_declared_as_treatment() -> TableClassification:
    return _crop_time_site_table(factors=[
        TableFactor(name="Population", dimension="treatment", encoding="rows"),  # the model's (wrong) declaration
        TableFactor(name="Maturity", dimension="time", encoding="columns"),
        TableFactor(name="Location", dimension="site", encoding="context"),
    ])


CROP_POOL = [{"slug": "trailblazer", "name": "Trailblazer"}, {"slug": "pathfinder", "name": "Pathfinder"}]


def test_a_factor_declared_treatment_whose_levels_are_all_ready_crops_is_corrected_to_crop():
    tc, overrides = orchestrator._reconcile_factor_dimensions(_population_declared_as_treatment(), {"crop": CROP_POOL, "site": []})
    assert tc.factor_dimension("Population") == "crop"
    assert overrides == [{
        "factor": "Population", "declared": "treatment", "corrected_to": "crop",
        "reason": "every level ['Pathfinder', 'Trailblazer'] is exactly a ready crop record of this run",
    }]
    assert orchestrator._table_classifications_to_treatment_candidates([tc], {})[0] == []


def test_a_partial_match_is_left_as_declared_never_guessed():
    tc, overrides = orchestrator._reconcile_factor_dimensions(
        _population_declared_as_treatment(), {"crop": [{"slug": "trailblazer", "name": "Trailblazer"}], "site": []},
    )
    assert overrides == [] and tc.factor_dimension("Population") == "treatment"


def test_a_substring_is_not_an_exact_match():
    tc, overrides = orchestrator._reconcile_factor_dimensions(
        _population_declared_as_treatment(),
        {"crop": [{"slug": "a", "name": "Trailblazer Cycle 2"}, {"slug": "b", "name": "Pathfinder HY"}], "site": []},
    )
    assert overrides == []


def test_an_ambiguous_pool_entry_is_not_used():
    dup = CROP_POOL + [{"slug": "trailblazer_again", "name": "Trailblazer"}]
    assert orchestrator._reconcile_factor_dimensions(_population_declared_as_treatment(), {"crop": dup, "site": []})[1] == []


def test_a_treatment_factor_whose_levels_are_all_sites_is_corrected_to_site():
    tc = _treatment_columns_variable_rows(factors=[
        TableFactor(name="Winter treatment", dimension="treatment", encoding="columns"),
        TableFactor(name="Variable", dimension="variable", encoding="rows"),
    ])
    corrected, overrides = orchestrator._reconcile_factor_dimensions(
        tc, {"crop": [], "site": [{"slug": "fallow", "name": "Fallow"}, {"slug": "mustard", "name": "Mustard"}]},
    )
    assert overrides and corrected.factor_dimension("Winter treatment") == "site"


def test_only_a_treatment_declaration_is_ever_overridden():
    tc, overrides = orchestrator._reconcile_factor_dimensions(_crop_time_site_table(), {"crop": CROP_POOL, "site": [{"slug": "ames", "name": "Ames"}]})
    assert overrides == [] and tc.factor_dimension("Maturity") == "time"


def test_no_pools_means_no_override():
    tc = _population_declared_as_treatment()
    assert orchestrator._reconcile_factor_dimensions(tc, None) == (tc, [])
    assert orchestrator._reconcile_factor_dimensions(tc, {"crop": [], "site": []})[1] == []


def test_dimension_pools_are_built_from_this_runs_ready_records():
    def rec(entity, rid, payload, status="ready"):
        return {"entity_type": entity, "record_id": rid, "status": status, "detail": {"payload": payload}}

    def ef(v):
        return {"value": v, "provenance_label": "EXTRACTED"}

    runs = {
        "Crop": [rec("Crop", "p_crop_trailblazer", {"cultivar": ef("Trailblazer")}), rec("Crop", "p_crop_x", {"cultivar": ef("X")}, "unresolved")],
        "Site": [rec("Site", "p_site_ames_ia", {"name": ef("Ames, IA")})],
    }
    pools = orchestrator._dimension_pools("p", runs)
    assert pools["crop"] == [{"slug": "trailblazer", "name": "Trailblazer"}]
    assert pools["site"] == [{"slug": "ames_ia", "name": "Ames, IA"}]


# --------------------------------------------------------------------- #
# pooled factors (protocol 7.4) and aggregated sources
# --------------------------------------------------------------------- #

POOL_NOTE = "Data show the mean ± standard error for all cultivar mixture treatments, since no differences were observed amongst them."


def _pooled_table(pooled_dimension="treatment", with_treatment_factor=True, **overrides) -> TableClassification:
    """Felipe-shaped: values per cover-crop treatment, pooled over the cultivar mixtures."""
    factors = [TableFactor(name="Variable", dimension="variable", encoding="rows")]
    columns = [TableValueColumn(value_column_id="f", variable_name_hint="value"), TableValueColumn(value_column_id="m", variable_name_hint="value")]
    if with_treatment_factor:
        factors.append(TableFactor(name="Cover crop", dimension="treatment", encoding="columns"))
        columns = [
            TableValueColumn(value_column_id="f", variable_name_hint="value", factor_levels={"Cover crop": "Fallow"}),
            TableValueColumn(value_column_id="m", variable_name_hint="value", factor_levels={"Cover crop": "Mustard"}),
        ]
    kwargs = dict(
        table_role="treatment_response", table_anchors=["b:0069"], factors=factors, value_columns=columns,
        row_groups=[TableRowGroup(row_group_id="r1", factor_values={"Variable": "Total fruit"}, source_table_anchor="b:0069", cells={"f": "352", "m": "252"})],
        pooled_factors=[PooledFactor(name="cultivar mixture", dimension=pooled_dimension, evidence_anchor="b:0070", evidence_excerpt=POOL_NOTE)],
    )
    kwargs.update(overrides)
    return TableClassification(**kwargs)


def test_a_representable_pooled_table_yields_aggregated_mean_context_and_only_the_retained_treatments():
    tc = _pooled_table()
    assert orchestrator._pooled_representability(tc) == (True, None)
    candidates = orchestrator._table_classification_to_candidates(tc, {})
    assert len(candidates) == 2
    for c in candidates:
        assert c.context["reported_effect_scope"] == "aggregated_mean"
        assert c.context["aggregated_over_factors"] == ["cultivar mixture"]
        assert c.context["pooling_evidence"] == [{"factor": "cultivar mixture", "anchor": "b:0070", "excerpt": POOL_NOTE}]
    treatments, _ = orchestrator._table_classifications_to_treatment_candidates([tc], {})
    assert sorted(t.description for t in treatments) == ["Experimental condition: Cover crop=Fallow", "Experimental condition: Cover crop=Mustard"]
    # the pooled factor is NOT multiplied into six manufactured mixture-specific treatments
    assert not any("mixture" in t.description for t in treatments)


def test_an_unpooled_table_has_no_context():
    # Only the cell's identity -- no pooling, no aggregation.
    assert all(set(c.context) == {"cell"} for c in orchestrator._table_classification_to_candidates(_crop_time_site_table(), {}))


def test_values_pooled_over_the_site_are_not_representable_and_generate_nothing_L2():
    tc = _pooled_table(pooled_dimension="site")
    assert orchestrator._pooled_representability(tc) == (False, orchestrator.LIMITATION_L2)
    assert orchestrator._table_classification_to_candidates(tc, {}) == []
    assert orchestrator._table_classifications_to_treatment_candidates([tc], {}) == ([], set())


def test_pooled_with_no_retained_treatment_is_not_representable_L1():
    tc = _pooled_table(with_treatment_factor=False)
    assert orchestrator._pooled_representability(tc) == (False, orchestrator.LIMITATION_L1)
    assert orchestrator._table_classification_to_candidates(tc, {}) == []


def test_a_pooled_factor_cannot_also_be_a_declared_row_or_column_factor():
    with pytest.raises(ValidationError, match="ABSENT from the table"):
        _pooled_table(pooled_factors=[PooledFactor(name="Variable", dimension="other", evidence_anchor="b:0070", evidence_excerpt="x")])


def test_pooled_evidence_must_be_literal_source_text(tmp_path, monkeypatch):
    papers = tmp_path / "papers"
    (papers / "syn").mkdir(parents=True)
    (papers / "syn" / "content.md").write_text(f"| a | 1.0 |\n|---|---|\n| b | 2.0 |\n⟦b:0069⟧\n\n{POOL_NOTE}\n⟦b:0070⟧\n", encoding="utf-8")
    prov = {"b:0069": {"block_type": "Table", "page_id": "page_1", "section_path": []},
            "b:0070": {"block_type": "Text", "page_id": "page_1", "section_path": []}}
    for n, (cell) in enumerate(["1.0", "2.0"]):
        prov[f"b:9{n:03d}"] = {"block_type": "TableCell", "page_id": "page_1", "section_path": [], "parent_table_anchor": "b:0069",
                               "row_index": n, "col_index": 0, "cell_text": cell}
    (papers / "syn" / "provenance.json").write_text(json.dumps(prov), encoding="utf-8")
    monkeypatch.setenv("IR_PAPERS_ROOT", str(papers))
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))

    def answer(excerpt):
        return {
            "table_role": "treatment_response", "table_anchors": ["b:0069"],
            "factors": [{"name": "Variable", "dimension": "variable", "encoding": "rows"}],
            "variables": [{"label": "a"}, {"label": "b"}],   # a row-named variable must be declared (undeclared levels are retried)
            "pooled_factors": [{"name": "cultivar mixture", "dimension": "treatment", "evidence_anchor": "b:0070", "evidence_excerpt": excerpt}],
            "value_columns": [{"value_column_id": "v", "variable_name_hint": "y"}],
            "row_groups": [{"row_group_id": "r1", "factor_values": {"Variable": "a"}, "source_table_anchor": "b:0069", "cells": {"v": "1.0"}},
                           {"row_group_id": "r2", "factor_values": {"Variable": "b"}, "source_table_anchor": "b:0069", "cells": {"v": "2.0"}}],
        }

    prompts = []
    excerpts = iter(["the values were averaged over all mixtures", POOL_NOTE])  # 1st: paraphrase, 2nd: literal

    def invoke(agent, model, prompt, timeout=300):
        prompts.append(prompt)
        payload = answer(next(excerpts))
        return orchestrator.AgentInvocation(agent=agent, model=model, prompt=prompt, returncode=0, stdout="{}", stderr="",
                                            final_text=json.dumps(payload), parsed_json=payload, parse_error=None)

    result, error = orchestrator.run_table_classification(
        run_id="r1", paper_id="syn", seed_table_anchor="b:0069", other_tables=[], model="m", invoke=invoke,
    )
    assert error is None and result.pooled_factors[0].evidence_excerpt == POOL_NOTE
    assert len(prompts) == 2 and "must be the literal source text stating the pooling" in prompts[1]


# --------------------------------------------------------------------- #
# candidate context reaches Conversion; prompts otherwise byte-identical
# --------------------------------------------------------------------- #

RAW = {"paper_id": "p", "entity_type": "Observation", "record_id": "r", "facts": []}


def test_conversion_prompt_is_unchanged_without_context_and_carries_a_hint_block_with_it():
    base = orchestrator._conversion_prompt("p", "Observation", "r", RAW, None, None)
    assert orchestrator._conversion_prompt("p", "Observation", "r", RAW, None, None, None) == base
    assert orchestrator._conversion_prompt("p", "Observation", "r", RAW, None, None, {}) == base
    ctx = {"reported_effect_scope": "aggregated_mean", "aggregated_over_factors": ["cultivar mixture"],
           "pooling_evidence": [{"factor": "cultivar mixture", "anchor": "b:0070", "excerpt": POOL_NOTE}]}
    prompt = orchestrator._conversion_prompt("p", "Observation", "r", RAW, None, None, ctx)
    assert "CANDIDATE_CONTEXT" in prompt and "NOT verified source text" in prompt
    assert "\"aggregated_mean\" (never \"treatment_mean\" for a pooled value)" in prompt
    assert "cultivar mixture" in prompt


def test_a_context_without_pooling_gets_the_hint_block_but_no_aggregation_instruction():
    prompt = orchestrator._conversion_prompt("p", "Observation", "r", RAW, None, None, {"anything": "else"})
    assert "CANDIDATE_CONTEXT" in prompt and "MEANS POOLED" not in prompt


# --------------------------------------------------------------------- #
# explicit L1 limitation + aggregated sources in the run record
# --------------------------------------------------------------------- #


def _save_classification(run_id, seed, classification):
    run_store.save_final(run_id, f"table_classification__{seed}", {"status": "success", "classification": classification.model_dump()})


@pytest.fixture()
def runs_env(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("IR_PAPERS_ROOT", str(tmp_path / "papers"))
    return tmp_path


def _ready(entity, rid):
    return {"entity_type": entity, "record_id": rid, "status": "ready", "detail": {"payload": {}}}


def test_observations_are_blocked_with_an_explicit_L1_reason_when_no_table_has_a_treatment_dimension(runs_env):
    _save_classification("r1", "b_0001", _crop_time_site_table())
    records = {"Citation": _ready("Citation", "p"), "Site": [_ready("Site", "p_site_ames")], "Treatment": []}
    out = orchestrator._run_multi_record_entity(
        run_id="r1", paper_id="p", entity_type="Observation", model="m", client=None, invoke=None,
        enable_ai_validation=False, this_run_records=records,
    )
    assert len(out) == 1 and out[0]["status"] == "blocked"
    assert "required prerequisite Treatment" in out[0]["reason"]
    assert orchestrator.LIMITATION_L1 in out[0]["reason"] and "not an extraction failure" in out[0]["reason"]


def test_no_l1_note_when_a_table_does_have_a_treatment_dimension(runs_env):
    _save_classification("r1", "b_0002", _treatment_columns_variable_rows())
    records = {"Citation": _ready("Citation", "p"), "Site": [_ready("Site", "p_site_ames")], "Treatment": []}
    out = orchestrator._run_multi_record_entity(
        run_id="r1", paper_id="p", entity_type="Observation", model="m", client=None, invoke=None,
        enable_ai_validation=False, this_run_records=records,
    )
    assert out[0]["status"] == "blocked" and orchestrator.LIMITATION_L1 not in out[0]["reason"]


def test_no_l1_note_for_legacy_classifications_or_when_no_tables_were_classified(runs_env):
    legacy = TableClassification(
        applicable=True, table_anchors=["b:0001"], value_columns=[TableValueColumn(value_column_id="y", variable_name_hint="y")],
        row_groups=[TableRowGroup(row_group_id="r", factor_values={"K": "v"}, source_table_anchor="b:0001", cells={"y": "1"})],
    )
    _save_classification("r1", "b_0001", legacy)
    assert orchestrator._treatment_dimension_limitation("r1", "p", {}) is None
    assert orchestrator._treatment_dimension_limitation("empty_run", "p", {}) is None


def test_the_run_summary_registers_aggregated_sources_and_limitations(runs_env):
    summary_table = TableClassification(
        table_role="aggregated_summary", reason="values averaged across locations and maturities", table_anchors=["b:0761"],
        row_groups=[TableRowGroup(row_group_id="r", source_table_anchor="b:0761", cells={})],
    )
    _save_classification("r1", "b_0761", summary_table)
    _save_classification("r1", "b_0069", _pooled_table())
    _save_classification("r1", "b_0500", _pooled_table(pooled_dimension="site", table_anchors=["b:0500"],
                         row_groups=[TableRowGroup(row_group_id="r1", factor_values={"Variable": "x"}, source_table_anchor="b:0500", cells={"f": "1", "m": "2"})]))
    _save_classification("r1", "b_0001", _crop_time_site_table())

    summary = orchestrator.summarize_table_pass("r1", "p", {})
    by_anchor = {tuple(s["table_anchors"]): s for s in summary["aggregated_sources"]}
    assert by_anchor[("b:0761",)]["kind"] == "aggregated_summary" and by_anchor[("b:0761",)]["representable_in_current_ir"] is False
    assert by_anchor[("b:0069",)]["representable_in_current_ir"] is True
    assert by_anchor[("b:0069",)]["aggregated_over_factors"] == ["cultivar mixture"]
    assert by_anchor[("b:0500",)]["representable_in_current_ir"] is False
    assert by_anchor[("b:0500",)]["why_not_represented"] == orchestrator.LIMITATION_L2
    assert {t["table_role"] for t in summary["tables"]} == {"aggregated_summary", "treatment_response"}
    assert [lim["code"] for lim in summary["limitations"]] == ["L2"]  # b:0069 has a treatment dimension, so no L1


def test_the_summary_reports_l1_when_only_crop_time_site_tables_exist(runs_env):
    _save_classification("r1", "b_0001", _crop_time_site_table())
    summary = orchestrator.summarize_table_pass("r1", "p", {})
    assert [lim["code"] for lim in summary["limitations"]] == ["L1"] and summary["aggregated_sources"] == []
    assert summary["tables"][0]["has_treatment_dimension"] is False


# --------------------------------------------------------------------- #
# a column `treatment_level_hint` on a table with declared factors (real
# Felipe Table 1 classification accepted from the live model: DAP=time/rows,
# Variable=variable/rows, and Fallow/Mustard given ONLY as column hints)
# --------------------------------------------------------------------- #


def _felipe_accepted_classification(**overrides) -> TableClassification:
    kwargs = dict(
        table_role="treatment_response", table_anchors=["b:0069"],
        factors=[
            TableFactor(name="DAP", dimension="time", encoding="rows"),
            TableFactor(name="Variable", dimension="variable", encoding="rows"),
        ],
        value_columns=[
            TableValueColumn(value_column_id="fallow", variable_name_hint="measurement", treatment_level_hint="Fallow"),
            TableValueColumn(value_column_id="mustard", variable_name_hint="measurement", treatment_level_hint="Mustard"),
        ],
        row_groups=[
            TableRowGroup(row_group_id="par", factor_values={"DAP": "60", "Variable": "PAR intercepted (%)"},
                          source_table_anchor="b:0069", cells={"fallow": "20", "mustard": "15"}),
            TableRowGroup(row_group_id="shoot", factor_values={"DAP": "60", "Variable": "Shoot biomass"},
                          source_table_anchor="b:0069", cells={"fallow": "70", "mustard": "54"}),
        ],
    )
    kwargs.update(overrides)
    return TableClassification(**kwargs)


def test_a_declared_table_whose_treatment_lives_in_a_column_hint_has_a_treatment_dimension():
    tc = _felipe_accepted_classification()
    assert tc.factors and not any(f.dimension == "treatment" for f in tc.factors)  # no treatment FACTOR declared
    assert orchestrator._has_treatment_dimension(tc) is True


def test_a_declared_table_with_neither_a_treatment_factor_nor_a_hint_still_has_none():
    assert orchestrator._has_treatment_dimension(_crop_time_site_table()) is False  # not loosened for Daren-shaped tables


def test_felipes_accepted_classification_is_not_reported_as_L1_and_yields_only_fallow_and_mustard(runs_env):
    tc = _felipe_accepted_classification()
    treatments, covered = orchestrator._table_classifications_to_treatment_candidates([tc], {})
    assert sorted(t.description for t in treatments) == ["Experimental condition: Treatment=Fallow", "Experimental condition: Treatment=Mustard"]
    assert covered == {"b:0069"}  # DAP (time) and Variable rows add no further combinations

    _save_classification("r1", "b_0069", tc)
    assert orchestrator._treatment_dimension_limitation("r1", "p", {}) is None
    summary = orchestrator.summarize_table_pass("r1", "p", {})
    assert summary["limitations"] == [] and summary["tables"][0]["has_treatment_dimension"] is True

    records = {"Citation": _ready("Citation", "p"), "Site": [_ready("Site", "p_site_ames")], "Treatment": []}
    out = orchestrator._run_multi_record_entity(
        run_id="r1", paper_id="p", entity_type="Observation", model="m", client=None, invoke=None,
        enable_ai_validation=False, this_run_records=records,
    )
    assert orchestrator.LIMITATION_L1 not in out[0]["reason"]


def test_a_pooled_declared_table_with_a_column_treatment_hint_stays_representable_not_L1():
    """The pooled path is where the false L1 actually bit: pooled + hint-only treatment was
    rejected as 'no retained treatment' and generated nothing."""
    tc = _felipe_accepted_classification(
        pooled_factors=[PooledFactor(name="cultivar mixture", dimension="treatment", evidence_anchor="b:0070", evidence_excerpt=POOL_NOTE)],
    )
    assert orchestrator._pooled_representability(tc) == (True, None)
    observations = orchestrator._table_classification_to_candidates(tc, {})
    assert len(observations) == 4 and all(c.context["aggregated_over_factors"] == ["cultivar mixture"] for c in observations)
    treatments, _ = orchestrator._table_classifications_to_treatment_candidates([tc], {})
    assert len(treatments) == 2 and not any("mixture" in t.description for t in treatments)
