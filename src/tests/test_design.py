"""The experimental-design intermediate -- main-effect tables, representability, conflicts.

Real cases:
  - Philippe-2007-Six Tables 1-3 are MAIN-EFFECT tables (PAR-class rows, then Year rows); the classification called them
    "cell-level, nothing pooled", so unlocked Observations would have been wrong treatment means. BSD1/BSD6 are single
    years (their Year rows are blank) and are NOT pooled over Year; INC is.
  - Year rows (and Kathryn's "Mean" over systems) are pooled over the treatment factor: extracted, not representable.
  - PARt class bounds: tables "0.2-0.35" vs b:0046 "0.2-0.37"; overall "0.1 to 0.4" (b:0030) vs 0-0.35.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import design, orchestrator
from pipeline.context_bundle import build_context_bundle
from pipeline.document_map import build_document_map
from pipeline.evidence_index import build_evidence_index
from pipeline.raw_schema import TableClassification, TableFactor, TableRowGroup, TableValueColumn, TableVariable
from test_document_map import _write_paper

PAPERS = Path(__file__).resolve().parents[1] / "paper"


def _row(rid, levels, cells):
    return TableRowGroup(row_group_id=rid, factor_values=levels, source_table_anchor="b:0053", cells=cells)


def philippe_table1(role="treatment_response"):
    """Philippe Table 1 as reconstructed: PAR-class rows (BSD1, BSD6, INC), then Year rows (INC only)."""
    return TableClassification(
        applicable=True, table_role=role, table_anchors=["b:0053"],
        reason="each set of rows is pooled over the other factor" if role == "aggregated_summary" else None,
        factors=[TableFactor(name="PAR t", dimension="treatment", encoding="rows"),
                 TableFactor(name="Year", dimension="time", encoding="rows")],
        variables=[TableVariable(label="BSD 1 (mm)"), TableVariable(label="INC (mm)")],
        value_columns=[TableValueColumn(value_column_id="bsd1", variable="BSD 1 (mm)"),
                       TableValueColumn(value_column_id="inc", variable="INC (mm)")],
        row_groups=[
            _row("r1", {"PAR t": "0-0.1"}, {"bsd1": "6.8", "inc": "1.7 a"}),
            _row("r2", {"PAR t": "0.1-0.2"}, {"bsd1": "7.2", "inc": "2.7 b"}),
            _row("r3", {"PAR t": "0.2-0.35"}, {"bsd1": "7.3", "inc": "3.8 c"}),
            _row("r4", {"Year": "2001"}, {"bsd1": None, "inc": "1.5 A"}),
            _row("r5", {"Year": "2006"}, {"bsd1": None, "inc": "4.1 D"}),
        ],
    )


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #

def test_a_table_whose_rows_each_set_one_factor_is_a_main_effect_table():
    assert design.is_main_effect_layout(philippe_table1())
    crossed = philippe_table1()
    crossed.row_groups[0].factor_values["Year"] = "2001"
    assert not design.is_main_effect_layout(crossed)


def test_a_column_is_pooled_over_a_factor_only_if_it_varies_with_it():
    table = philippe_table1()
    par_row = table.row_groups[0]
    assert design.cell_pooling(table, par_row, "bsd1") is None                       # a single year, not pooled
    inc = design.cell_pooling(table, par_row, "inc")
    assert inc.pooled_over == ["Year"] and inc.representation == design.REPRESENTABLE and inc.has_treatment


def test_a_value_pooled_over_the_treatment_factor_is_blocked_by_representation():
    table = philippe_table1()
    year = design.cell_pooling(table, table.row_groups[3], "inc")
    assert year.pooled_over == ["PAR t"] and year.representation == design.BLOCKED_BY_REPRESENTATION
    assert "no Treatment" in year.reason


def test_a_mean_row_over_the_treatment_is_blocked_and_its_time_span_is_known():
    table = philippe_table1()
    mean = design.summary_row_pooling(table, _row("m", {"PAR t": "Mean"}, {"inc": "2.8"}), "Mean")
    assert mean.representation == design.BLOCKED_BY_REPRESENTATION
    assert design.time_span(table, ["Year"]) == "2001, 2006"


# --------------------------------------------------------------------------- #
# Candidates: aggregated means, blocked cells, never an invented Treatment
# --------------------------------------------------------------------------- #

def test_pooled_cells_become_aggregated_means_and_treatment_pooled_cells_are_recorded_not_enumerated():
    blocked = []
    candidates = orchestrator._table_classification_to_candidates(philippe_table1(), {}, None, blocked)
    by_id = {c.candidate_id: c for c in candidates}
    assert set(by_id) == {"bsd1_r1", "bsd1_r2", "bsd1_r3", "inc_r1", "inc_r2", "inc_r3"}   # no Year-row candidates
    assert by_id["inc_r1"].context["reported_effect_scope"] == "aggregated_mean"
    assert by_id["inc_r1"].context["aggregated_over_factors"] == ["Year"]
    assert by_id["inc_r1"].context["pooled_time_levels"] == "2001, 2006"
    assert "reported_effect_scope" not in by_id["bsd1_r1"].context                          # not pooled
    assert [(b["factor_levels"], b["value_text"], b["status"]) for b in blocked] == [
        ({"Year": "2001"}, "1.5 A", design.BLOCKED_BY_REPRESENTATION),
        ({"Year": "2006"}, "4.1 D", design.BLOCKED_BY_REPRESENTATION)]


def test_an_aggregated_summary_explained_by_its_layout_is_no_longer_dropped():
    candidates = orchestrator._table_classification_to_candidates(philippe_table1("aggregated_summary"), {})
    assert len(candidates) == 6


def test_the_conversion_prompt_labels_layout_pooling_inferred_not_quoted():
    candidate = orchestrator._table_classification_to_candidates(philippe_table1(), {})
    context = next(c for c in candidate if c.candidate_id == "inc_r1").context
    prompt = orchestrator._conversion_prompt("p", "Observation", "o", {"facts": []}, None, None, context)
    assert "MAIN-EFFECT MEAN" in prompt and "INFERRED" in prompt and "do not narrow temporal_info" in prompt


# --------------------------------------------------------------------------- #
# Conflicts
# --------------------------------------------------------------------------- #

@pytest.fixture
def conflict_index(tmp_path):
    _write_paper(tmp_path, "c", [
        ("b:0001", "Text", "Summary: the stand was thinned to obtain a gradient of PAR t ; 0–0.35.", 0),
        ("b:0002", "SectionHeader", "## Methods", 0),
        ("b:0003", "Text", "Saplings were sampled over the PAR t range of 0.1 to 0.4 in stands of 500 to 4000 stems.", 0),
        ("b:0004", "Text", "PAR t was divided into three classes (0-0.1, 0.1-0.2 and 0.2-0.37).", 0),
        ("b:0005", "SectionHeader", "## Discussion", 0),
        ("b:0006", "Text", "When PAR t increased from 0.1 to 0.35, Vcmax increased.", 0),
    ])
    return build_evidence_index(build_document_map("c", tmp_path))


def test_conflicting_class_bounds_and_overall_ranges_are_recorded_with_both_sources(conflict_index):
    conflicts = design.factor_range_conflicts([philippe_table1()], conflict_index)
    assert {(c.kind, c.claim_a, c.claim_b, c.source_b) for c in conflicts} == {
        ("class_bound_differs", "0.2-0.35", "0.2-0.37", "b:0004"),
        ("overall_range_differs", "0-0.35 (span of the table's classes)", "0.1 to 0.4", "b:0003"),
    }
    assert all(c.resolution_status == "unresolved" for c in conflicts)


def test_the_design_summary_carries_factors_layout_sample_size_and_conflicts(conflict_index):
    summary = design.design_summary({"b_0053": philippe_table1()}, conflict_index)
    assert summary["tables"][0]["layout"] == "main_effects"
    assert summary["factors"]["PAR t"]["levels"] == ["0-0.1", "0.1-0.2", "0.2-0.35"]
    assert len(summary["conflicts"]) == 2


# --------------------------------------------------------------------------- #
# Treatment packets carry small definition tables in full
# --------------------------------------------------------------------------- #

def test_a_small_definition_table_is_given_in_full_to_treatment(tmp_path):
    _write_paper(tmp_path, "t", [
        ("b:0001", "SectionHeader", "## Methods", 0),
        ("b:0002", "Caption", "*Table 1. The five systems evaluated.*", 0),
        ("b:0003", "Table", "| System | Compost | Cover crop |\n|---|---|---|\n| 1 | 0 | Legume-rye |\n| 2 | 114 | Legume-rye |\n"
                            "| 3 | 114 | Mustard |", 0),
    ])
    bundle = build_context_bundle("t", "Treatment", "enumeration", papers_root=tmp_path)
    item = next(i for i in bundle.items if i.anchor == "b:0003")
    assert item.evidence_type == "table" and "| 3 | 114 | Mustard |" in item.text


def test_philippe_design_conflicts_on_the_real_paper():
    if not (PAPERS / "Philippe-2007-Six" / "content.md").is_file():
        pytest.skip("Philippe-2007-Six is not prepared on this machine")
    index = build_evidence_index(build_document_map("Philippe-2007-Six", PAPERS))
    conflicts = design.factor_range_conflicts([philippe_table1()], index)
    assert {(c.claim_b, c.source_b) for c in conflicts} == {("0.2-0.37", "b:0046"), ("0.1 to 0.4", "b:0030")}
