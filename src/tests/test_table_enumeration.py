"""Tests for the table-enumeration mechanism (Steps A-D, see
`pipeline/orchestrator.py`'s own module-level design comment above
`run_table_enumeration`) -- the deterministic table classification +
cross-product pipeline built to fix a confirmed real collapse: free-form
enumeration produced only ONE Observation candidate per measure for a
real Daren-1997-Canopy table with 6 populations x 3 maturities x 2 sites
x 4 measures (up to 144 real reported values), in both a run before and a
run after the entity-identity-guidance fix.

The model is never actually invoked here, same discipline as
test_orchestrator.py: `invoke_agent` is swapped for a canned sequence of
`AgentInvocation`s so the control-flow logic is tested deterministically
and fast. Step C (`_table_classification_to_candidates`) and the sanity
check are pure functions and need no mocking at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

PIPELINE_DIR = Path(__file__).resolve().parent.parent / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

from pipeline import orchestrator, run_store  # noqa: E402
from pipeline.raw_schema import EnumerationCandidate, TableClassification, TableRowGroup, TableValueColumn  # noqa: E402

PAPER_ID = "table_enum_test_paper"

# A small table with 3 data rows (needed so the sanity check's "dropped
# more than half" threshold has real headroom to distinguish a faithful
# reconstruction from a bad one -- see test_sanity_check_flags_dropped_data).
CONTENT_MD = (
    "# Results\n⟦b:0001⟧\n\n"
    "*Table 1. Yield by treatment.*\n⟦b:0002⟧\n\n"
    "| Treatment | Yield |\n|---|---|\n"
    "| control | 3.2 |\n| n_fert | 4.1 |\n| high_fert | 5.5 |\n"
    "⟦b:0006⟧\n\n"
    "Some narrative text about yield.\n⟦b:0007⟧\n"
)

PROVENANCE = {
    "b:0001": {"block_type": "SectionHeader", "page_id": "page_0", "section_path": ["Results"]},
    "b:0002": {"block_type": "Caption", "page_id": "page_0", "section_path": ["Results"]},
    "b:0006": {"block_type": "Table", "page_id": "page_0", "section_path": ["Results"]},
    "b:0007": {"block_type": "Text", "page_id": "page_0", "section_path": ["Results"]},
    "b:9001": {"block_type": "TableCell", "page_id": "page_0", "section_path": ["Results"],
               "parent_table_anchor": "b:0006", "row_index": 0, "col_index": 0, "cell_text": "Treatment"},
    "b:9002": {"block_type": "TableCell", "page_id": "page_0", "section_path": ["Results"],
               "parent_table_anchor": "b:0006", "row_index": 0, "col_index": 1, "cell_text": "Yield"},
    "b:9003": {"block_type": "TableCell", "page_id": "page_0", "section_path": ["Results"],
               "parent_table_anchor": "b:0006", "row_index": 1, "col_index": 0, "cell_text": "control"},
    "b:9004": {"block_type": "TableCell", "page_id": "page_0", "section_path": ["Results"],
               "parent_table_anchor": "b:0006", "row_index": 1, "col_index": 1, "cell_text": "3.2"},
    "b:9005": {"block_type": "TableCell", "page_id": "page_0", "section_path": ["Results"],
               "parent_table_anchor": "b:0006", "row_index": 2, "col_index": 0, "cell_text": "n_fert"},
    "b:9006": {"block_type": "TableCell", "page_id": "page_0", "section_path": ["Results"],
               "parent_table_anchor": "b:0006", "row_index": 2, "col_index": 1, "cell_text": "4.1"},
    "b:9007": {"block_type": "TableCell", "page_id": "page_0", "section_path": ["Results"],
               "parent_table_anchor": "b:0006", "row_index": 3, "col_index": 0, "cell_text": "high_fert"},
    "b:9008": {"block_type": "TableCell", "page_id": "page_0", "section_path": ["Results"],
               "parent_table_anchor": "b:0006", "row_index": 3, "col_index": 1, "cell_text": "5.5"},
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    papers_root = tmp_path / "papers"
    runs_root = tmp_path / "runs"
    papers_root.mkdir()
    pdir = papers_root / PAPER_ID
    pdir.mkdir()
    (pdir / "content.md").write_text(CONTENT_MD, encoding="utf-8")
    (pdir / "provenance.json").write_text(json.dumps(PROVENANCE), encoding="utf-8")

    monkeypatch.setenv("IR_PAPERS_ROOT", str(papers_root))
    monkeypatch.setenv("IR_RUNS_ROOT", str(runs_root))
    return {"papers_root": papers_root, "runs_root": runs_root}


def make_invoke_sequence(items):
    """items: list of (expected_agent_name, AgentInvocation)."""
    it = iter(items)

    def _invoke(agent, model, prompt, timeout=300):
        try:
            expected_agent, invocation = next(it)
        except StopIteration:
            raise AssertionError(f"invoke_agent called more times than expected (extra call for agent={agent!r})")
        assert agent == expected_agent, f"expected next call to agent {expected_agent!r}, got {agent!r}"
        return invocation

    return _invoke


def _table_classification_inv(payload: dict) -> orchestrator.AgentInvocation:
    return orchestrator.AgentInvocation(
        agent="extractor", model="test-model", prompt="prompt",
        returncode=0, stdout="{}", stderr="",
        final_text=json.dumps(payload), parsed_json=payload, parse_error=None,
    )


def valid_classification_payload() -> dict:
    return {
        "applicable": True, "reason": None, "table_anchors": ["b:0006"],
        "value_columns": [
            {"value_column_id": "yield", "variable_name_hint": "yield", "units_hint": None, "site_hint": None},
        ],
        "row_groups": [
            {"row_group_id": "control", "factor_values": {"Treatment": "control"},
             "source_table_anchor": "b:0006", "cells": {"yield": "3.2"}},
            {"row_group_id": "n_fert", "factor_values": {"Treatment": "n_fert"},
             "source_table_anchor": "b:0006", "cells": {"yield": "4.1"}},
            {"row_group_id": "high_fert", "factor_values": {"Treatment": "high_fert"},
             "source_table_anchor": "b:0006", "cells": {"yield": "5.5"}},
        ],
    }


# --------------------------------------------------------------------- #
# 1. TableClassification schema (frozen interface, own consistency checks)
# --------------------------------------------------------------------- #

def test_table_classification_requires_reason_when_not_applicable():
    with pytest.raises(ValidationError):
        TableClassification(applicable=False, table_anchors=["b:0001"])


def test_table_classification_requires_reason_when_applicable_but_no_row_groups():
    with pytest.raises(ValidationError):
        TableClassification(applicable=True, table_anchors=["b:0001"])


def test_table_classification_rejects_duplicate_value_column_ids():
    with pytest.raises(ValidationError):
        TableClassification(
            applicable=True, table_anchors=["b:0001"],
            value_columns=[
                TableValueColumn(value_column_id="x", variable_name_hint="X"),
                TableValueColumn(value_column_id="x", variable_name_hint="X2"),
            ],
            row_groups=[TableRowGroup(row_group_id="r1", source_table_anchor="b:0001", cells={"x": "1"})],
        )


def test_table_classification_rejects_row_group_citing_unknown_value_column():
    with pytest.raises(ValidationError):
        TableClassification(
            applicable=True, table_anchors=["b:0001"],
            value_columns=[TableValueColumn(value_column_id="x", variable_name_hint="X")],
            row_groups=[TableRowGroup(row_group_id="r1", source_table_anchor="b:0001", cells={"unknown": "1"})],
        )


def test_table_classification_rejects_row_group_source_anchor_not_in_table_anchors():
    with pytest.raises(ValidationError):
        TableClassification(
            applicable=True, table_anchors=["b:0001"],
            value_columns=[TableValueColumn(value_column_id="x", variable_name_hint="X")],
            row_groups=[TableRowGroup(row_group_id="r1", source_table_anchor="b:9999", cells={"x": "1"})],
        )


def test_table_classification_accepts_a_well_formed_reconstruction():
    tc = TableClassification(
        applicable=True, table_anchors=["b:0001"],
        value_columns=[TableValueColumn(value_column_id="yield_ames", variable_name_hint="yield", site_hint="Ames")],
        row_groups=[TableRowGroup(row_group_id="control", source_table_anchor="b:0001",
                                   factor_values={"Treatment": "control"}, cells={"yield_ames": "3.2"})],
    )
    assert tc.applicable is True


# --------------------------------------------------------------------- #
# 2. _numeric_tokens / _table_classification_sanity_check
# --------------------------------------------------------------------- #

def test_numeric_tokens_extracts_every_number_regardless_of_packing():
    assert orchestrator._numeric_tokens("0.19 0.90 1.16") == ["0.19", "0.90", "1.16"]
    assert orchestrator._numeric_tokens("") == []
    assert orchestrator._numeric_tokens(None) == []


def test_sanity_check_passes_for_a_faithful_reconstruction(env):
    tc = TableClassification(
        applicable=True, table_anchors=["b:0006"],
        value_columns=[TableValueColumn(value_column_id="yield", variable_name_hint="yield")],
        row_groups=[
            TableRowGroup(row_group_id="control", source_table_anchor="b:0006",
                          factor_values={"Treatment": "control"}, cells={"yield": "3.2"}),
            TableRowGroup(row_group_id="n_fert", source_table_anchor="b:0006",
                          factor_values={"Treatment": "n_fert"}, cells={"yield": "4.1"}),
            TableRowGroup(row_group_id="high_fert", source_table_anchor="b:0006",
                          factor_values={"Treatment": "high_fert"}, cells={"yield": "5.5"}),
        ],
    )
    assert orchestrator._table_classification_sanity_check(tc, PAPER_ID) is None


def test_sanity_check_flags_dropped_data(env):
    # Real raw numeric content = 3 (3.2, 4.1, 5.5); only reconstructing 1
    # is well under half -- exactly the confirmed failure mode (Daren
    # content.md anchor b:0178's mis-clustered continuation rows).
    tc = TableClassification(
        applicable=True, table_anchors=["b:0006"],
        value_columns=[TableValueColumn(value_column_id="yield", variable_name_hint="yield")],
        row_groups=[
            TableRowGroup(row_group_id="control", source_table_anchor="b:0006",
                          factor_values={"Treatment": "control"}, cells={"yield": "3.2"}),
        ],
    )
    error = orchestrator._table_classification_sanity_check(tc, PAPER_ID)
    assert error is not None
    assert "dropped" in error


def test_sanity_check_flags_fabricated_data(env):
    tc = TableClassification(
        applicable=True, table_anchors=["b:0006"],
        value_columns=[TableValueColumn(value_column_id="yield", variable_name_hint="yield")],
        row_groups=[
            TableRowGroup(row_group_id="control", source_table_anchor="b:0006", cells={"yield": "3.2"}),
            TableRowGroup(row_group_id="n_fert", source_table_anchor="b:0006", cells={"yield": "4.1"}),
            TableRowGroup(row_group_id="high_fert", source_table_anchor="b:0006", cells={"yield": "5.5"}),
            TableRowGroup(row_group_id="invented", source_table_anchor="b:0006", cells={"yield": "9.9"}),
        ],
    )
    error = orchestrator._table_classification_sanity_check(tc, PAPER_ID)
    assert error is not None
    assert "MORE" in error


def test_sanity_check_ignores_a_table_with_no_numeric_content(env):
    # applicable but nothing numeric in the source at all -> nothing to
    # check the reconstruction against; must not raise/flag spuriously.
    tc = TableClassification(
        applicable=True, table_anchors=["b:0001"],  # SectionHeader, no numeric cells
        value_columns=[TableValueColumn(value_column_id="x", variable_name_hint="x")],
        row_groups=[TableRowGroup(row_group_id="r", source_table_anchor="b:0001", cells={"x": "some text"})],
    )
    assert orchestrator._table_classification_sanity_check(tc, PAPER_ID) is None


# --------------------------------------------------------------------- #
# 3. _match_row_group_to_pool
# --------------------------------------------------------------------- #

def test_match_row_group_exact_after_normalization():
    pool = [{"slug": "ey_ff_ldmdc1", "record_id": "...", "name": "Ey x FF LDMDC1"}]
    assert orchestrator._match_row_group_to_pool({"Population": "Ey x FF LDMDC1"}, pool) == "ey_ff_ldmdc1"


def test_match_row_group_no_match_returns_none():
    pool = [{"slug": "trailblazer", "record_id": "...", "name": "Trailblazer"}]
    assert orchestrator._match_row_group_to_pool({"Population": "Pathfinder"}, pool) is None


def test_match_row_group_ambiguous_returns_none():
    pool = [{"slug": "a", "record_id": "...", "name": "Same"}, {"slug": "b", "record_id": "...", "name": "Same"}]
    assert orchestrator._match_row_group_to_pool({"Population": "Same"}, pool) is None


def test_match_row_group_empty_factor_values_returns_none():
    assert orchestrator._match_row_group_to_pool({}, [{"slug": "a", "record_id": "...", "name": "A"}]) is None


def test_match_row_group_substring_fallback_when_no_exact_match():
    # Real shape: a Site pool's `name` is the long institution name, never
    # matchable exactly against a table's short site_hint like "Ames" --
    # the slug ("ames_ia") DOES contain it as a substring.
    pool = [{"slug": "ames_ia", "record_id": "...", "name": "Iowa State University Agronomy and Agricultural Engineering Research Center"}]
    assert orchestrator._match_row_group_to_pool({"Site": "Ames"}, pool) == "ames_ia"


def test_match_row_group_substring_fallback_requires_unique_match():
    pool = [
        {"slug": "ames_ia", "record_id": "...", "name": "Ames Station"},
        {"slug": "ames_east", "record_id": "...", "name": "Ames East Station"},
    ]
    assert orchestrator._match_row_group_to_pool({"Site": "Ames"}, pool) is None


def test_match_row_group_substring_fallback_ignores_very_short_hints():
    pool = [{"slug": "ab_station", "record_id": "...", "name": "AB Station"}]
    assert orchestrator._match_row_group_to_pool({"Site": "ab"}, pool) is None


def test_match_row_group_prefers_exact_match_over_substring():
    pool = [
        {"slug": "ames", "record_id": "...", "name": "Ames"},
        {"slug": "ames_annex", "record_id": "...", "name": "Ames Annex"},
    ]
    # "Ames" exactly matches the first entry; the substring fallback (which
    # would be ambiguous between both) must never even be consulted.
    assert orchestrator._match_row_group_to_pool({"Site": "Ames"}, pool) == "ames"


# --------------------------------------------------------------------- #
# 4. _table_classification_to_candidates (Step C, pure cross-product)
# --------------------------------------------------------------------- #

def test_step_c_produces_one_candidate_per_nonblank_row_x_column():
    tc = TableClassification(
        applicable=True, table_anchors=["b:0001"],
        value_columns=[
            TableValueColumn(value_column_id="yield_ames", variable_name_hint="yield", site_hint="Ames"),
            TableValueColumn(value_column_id="yield_mead", variable_name_hint="yield", site_hint="Mead"),
        ],
        row_groups=[
            TableRowGroup(row_group_id="control", source_table_anchor="b:0001",
                          factor_values={"Treatment": "control"},
                          cells={"yield_ames": "3.2", "yield_mead": None}),
            TableRowGroup(row_group_id="n_fert", source_table_anchor="b:0001",
                          factor_values={"Treatment": "n_fert"},
                          cells={"yield_ames": "4.1", "yield_mead": "5.0"}),
        ],
    )
    candidates = orchestrator._table_classification_to_candidates(tc, {})
    # control x Mead is blank -> skipped, so 3 not 4.
    assert len(candidates) == 3
    assert len({c.candidate_id for c in candidates}) == 3
    for c in candidates:
        assert c.anchors == ["b:0001"]
        assert isinstance(c, EnumerationCandidate)


def test_step_c_not_applicable_produces_no_candidates():
    tc = TableClassification(applicable=False, reason="not a data table", table_anchors=["b:0001"])
    assert orchestrator._table_classification_to_candidates(tc, {}) == []


def test_step_c_sets_known_value_to_the_reported_cell_text():
    # Phase 1.3 (extraction-vs-known-table-value cross-check): Step C
    # already knows the exact cell value it cross-produced this candidate
    # from -- carry it through on known_value so the orchestrator can
    # later verify Extraction actually read THIS cell, not a different one.
    tc = TableClassification(
        applicable=True, table_anchors=["b:0001"],
        value_columns=[TableValueColumn(value_column_id="yield", variable_name_hint="yield")],
        row_groups=[TableRowGroup(row_group_id="control", source_table_anchor="b:0001",
                                   factor_values={"Treatment": "control"}, cells={"yield": "  3.2  "})],
    )
    candidates = orchestrator._table_classification_to_candidates(tc, {})
    assert candidates[0].known_value == "3.2"


def test_step_c_description_includes_factor_values_site_and_reported_value():
    tc = TableClassification(
        applicable=True, table_anchors=["b:0001"],
        value_columns=[TableValueColumn(value_column_id="yield_ames", variable_name_hint="total yield", site_hint="Ames")],
        row_groups=[TableRowGroup(row_group_id="trailblazer_veg", source_table_anchor="b:0001",
                                   factor_values={"Population": "Trailblazer", "Maturity": "Vegetative"},
                                   cells={"yield_ames": "0.19"})],
    )
    candidates = orchestrator._table_classification_to_candidates(tc, {})
    assert len(candidates) == 1
    d = candidates[0].description
    for expected in ("total yield", "Trailblazer", "Vegetative", "Ames", "0.19"):
        assert expected in d


def test_step_c_links_candidate_when_factor_value_exactly_matches_pool():
    pool = {"treatment_id": [{"slug": "trailblazer", "record_id": "p_treatment_trailblazer", "name": "Trailblazer"}]}
    tc = TableClassification(
        applicable=True, table_anchors=["b:0001"],
        value_columns=[TableValueColumn(value_column_id="yield", variable_name_hint="yield")],
        row_groups=[TableRowGroup(row_group_id="trailblazer", source_table_anchor="b:0001",
                                   factor_values={"Population": "Trailblazer"}, cells={"yield": "1.0"})],
    )
    candidates = orchestrator._table_classification_to_candidates(tc, pool)
    assert candidates[0].linked_candidates == {"treatment_id": "trailblazer"}


def test_step_c_leaves_unlinked_when_no_exact_match():
    pool = {"treatment_id": [{"slug": "trailblazer", "record_id": "...", "name": "Trailblazer"}]}
    tc = TableClassification(
        applicable=True, table_anchors=["b:0001"],
        value_columns=[TableValueColumn(value_column_id="yield", variable_name_hint="yield")],
        row_groups=[TableRowGroup(row_group_id="unknown_pop", source_table_anchor="b:0001",
                                   factor_values={"Population": "Some Other Population"}, cells={"yield": "1.0"})],
    )
    candidates = orchestrator._table_classification_to_candidates(tc, pool)
    assert candidates[0].linked_candidates == {}


def test_step_c_links_site_id_from_value_column_site_hint():
    # Real Daren-1997-Canopy shape: site information lives on the VALUE
    # COLUMN (Table 2's "Ames"/"Mead" sub-columns), not on the row's own
    # factor_values (Population/Maturity) -- site linking must still work.
    pool = {"site_id": [{"slug": "ames_ia", "record_id": "...", "name": "Iowa State University Agronomy and Agricultural Engineering Research Center"}]}
    tc = TableClassification(
        applicable=True, table_anchors=["b:0119"],
        value_columns=[TableValueColumn(value_column_id="total_yield_ames", variable_name_hint="total yield", site_hint="Ames")],
        row_groups=[TableRowGroup(row_group_id="trailblazer_veg", source_table_anchor="b:0119",
                                   factor_values={"Population": "Trailblazer"}, cells={"total_yield_ames": "0.19"})],
    )
    candidates = orchestrator._table_classification_to_candidates(tc, pool)
    assert candidates[0].linked_candidates == {"site_id": "ames_ia"}


def test_step_c_does_not_link_site_id_when_no_site_hint():
    pool = {"site_id": [{"slug": "ames_ia", "record_id": "...", "name": "Ames"}]}
    tc = TableClassification(
        applicable=True, table_anchors=["b:0001"],
        value_columns=[TableValueColumn(value_column_id="yield", variable_name_hint="yield")],  # no site_hint
        row_groups=[TableRowGroup(row_group_id="r", source_table_anchor="b:0001",
                                   factor_values={"Population": "Trailblazer"}, cells={"yield": "1.0"})],
    )
    candidates = orchestrator._table_classification_to_candidates(tc, pool)
    assert "site_id" not in candidates[0].linked_candidates


def test_step_c_links_method_id_from_value_column_method_hint():
    pool = {"method_id": [{"slug": "hand_clipping_harvest", "record_id": "...", "name": "hand-clipping"}]}
    tc = TableClassification(
        applicable=True, table_anchors=["b:0001"],
        value_columns=[TableValueColumn(value_column_id="yield", variable_name_hint="total yield", method_hint="hand-clipping harvest")],
        row_groups=[TableRowGroup(row_group_id="r", source_table_anchor="b:0001",
                                   factor_values={"Population": "Trailblazer"}, cells={"yield": "1.0"})],
    )
    candidates = orchestrator._table_classification_to_candidates(tc, pool)
    assert candidates[0].linked_candidates == {"method_id": "hand_clipping_harvest"}


def test_match_row_group_resolves_population_and_site_via_scoring_not_flat_match():
    # Real Daren-1997-Canopy shape: two Treatment pool entries share the
    # IDENTICAL name ('Trailblazer') -- site information lives only in the
    # slug. A flat "any value matches" check is ambiguous here; the scored
    # match correctly picks the entry accounting for BOTH values.
    pool = [
        {"slug": "trailblazer_ames", "record_id": "...", "name": "Trailblazer"},
        {"slug": "trailblazer_mead", "record_id": "...", "name": "Trailblazer"},
    ]
    assert orchestrator._match_row_group_to_pool({"Population": "Trailblazer", "Site": "Ames"}, pool) == "trailblazer_ames"
    assert orchestrator._match_row_group_to_pool({"Population": "Trailblazer", "Site": "Mead"}, pool) == "trailblazer_mead"


def test_match_row_group_refuses_a_genuine_tie_rather_than_guessing():
    # Real architectural case: a split-plot design models Population and
    # Maturity as SEPARATE Treatment entities. A row carrying both, against
    # a pool containing one entry per dimension (both equally real, both
    # equally supported by the row's own values), must not silently prefer
    # one over the other -- a single treatment_id cannot represent both.
    pool = [
        {"slug": "trailblazer_ames", "record_id": "...", "name": "Trailblazer"},
        {"slug": "vegetative_ames", "record_id": "...", "name": "Vegetative"},
    ]
    factor_values = {"Population": "Trailblazer", "Maturity": "Vegetative", "Site": "Ames"}
    assert orchestrator._match_row_group_to_pool(factor_values, pool) is None


# --------------------------------------------------------------------- #
# 5. Real Daren-1997-Canopy Table 2 regression anchor (content.md anchor
#    b:0119) -- the actual real paper/table that exposed the free-form
#    collapse this whole mechanism exists to fix.
# --------------------------------------------------------------------- #

DAREN_TABLE2_VALUE_COLUMNS = [
    TableValueColumn(value_column_id="total_yield_ames", variable_name_hint="total yield", site_hint="Ames", units_hint="kg DM m-2"),
    TableValueColumn(value_column_id="total_yield_mead", variable_name_hint="total yield", site_hint="Mead", units_hint="kg DM m-2"),
    TableValueColumn(value_column_id="leaf_blade_dry_wt_ames", variable_name_hint="leaf blade dry weight", site_hint="Ames", units_hint="kg DM m-2"),
    TableValueColumn(value_column_id="leaf_blade_dry_wt_mead", variable_name_hint="leaf blade dry weight", site_hint="Mead", units_hint="kg DM m-2"),
    TableValueColumn(value_column_id="leaf_sheath_dry_wt_ames", variable_name_hint="leaf sheath dry weight", site_hint="Ames", units_hint="kg DM m-2"),
    TableValueColumn(value_column_id="leaf_sheath_dry_wt_mead", variable_name_hint="leaf sheath dry weight", site_hint="Mead", units_hint="kg DM m-2"),
    TableValueColumn(value_column_id="stem_dry_wt_ames", variable_name_hint="stem dry weight", site_hint="Ames", units_hint="kg DM m-2"),
    TableValueColumn(value_column_id="stem_dry_wt_mead", variable_name_hint="stem dry weight", site_hint="Mead", units_hint="kg DM m-2"),
]

# Real raw cell data, content.md anchor b:0119 (verified against the real
# paper during this session's audit): each population's row packs 3
# space-separated values (one per maturity) into a SINGLE raw geometric
# cell per value-column -- exactly the reconstruction judgment Step B is
# asked to perform. These constants encode the hand-verified CORRECT
# split, i.e. the expected Step B output, so this test exercises Step C's
# cross-product against real data rather than a synthetic fixture.
_TRAILBLAZER = {
    "total_yield_ames": "0.19 0.90 1.16", "total_yield_mead": "0.30 1.26 1.09",
    "leaf_blade_dry_wt_ames": "0.13 0.38 0.34", "leaf_blade_dry_wt_mead": "0.18 0.41 0.30",
    "leaf_sheath_dry_wt_ames": "0.06 0.25 0.22", "leaf_sheath_dry_wt_mead": "0.11 0.29 0.23",
    "stem_dry_wt_ames": "0 0.27 0.60", "stem_dry_wt_mead": "0.01 0.56 0.56",
}
_PATHFINDER = {
    "total_yield_ames": "0.23 0.91 1.23", "total_yield_mead": "0.29 1.32 1.42",
    "leaf_blade_dry_wt_ames": "0.15 0.34 0.37", "leaf_blade_dry_wt_mead": "0.17 0.38 0.29",
    "leaf_sheath_dry_wt_ames": "0.08 0.22 0.25", "leaf_sheath_dry_wt_mead": "0.10 0.28 0.26",
    "stem_dry_wt_ames": "0 0.35 0.61", "stem_dry_wt_mead": "0.02 0.66 0.87",
}
_CAVE_IN_ROCK_VEGETATIVE = {
    "total_yield_ames": "0.22", "total_yield_mead": "0.57",
    "leaf_blade_dry_wt_ames": "0.12", "leaf_blade_dry_wt_mead": "0.33",
    "leaf_sheath_dry_wt_ames": "0.07", "leaf_sheath_dry_wt_mead": "0.23",
    "stem_dry_wt_ames": "0.03", "stem_dry_wt_mead": "0.01",
}
_MATURITIES = ["Vegetative", "Elongating", "Reproductive"]


def _split_population_rows(population: str, packed: dict) -> list[TableRowGroup]:
    split = {k: v.split() for k, v in packed.items()}
    return [
        TableRowGroup(
            row_group_id=f"{population.lower().replace('-', '_')}_{maturity.lower()}",
            source_table_anchor="b:0119",
            factor_values={"Population": population, "Maturity": maturity},
            cells={k: split[k][i] for k in packed},
        )
        for i, maturity in enumerate(_MATURITIES)
    ]


def daren_table2_classification() -> TableClassification:
    row_groups = (
        _split_population_rows("Trailblazer", _TRAILBLAZER)
        + _split_population_rows("Pathfinder", _PATHFINDER)
        + [TableRowGroup(
            row_group_id="cave_in_rock_vegetative", source_table_anchor="b:0119",
            factor_values={"Population": "Cave-in-Rock", "Maturity": "Vegetative"},
            cells=_CAVE_IN_ROCK_VEGETATIVE,
        )]
    )
    return TableClassification(applicable=True, table_anchors=["b:0119"],
                                value_columns=DAREN_TABLE2_VALUE_COLUMNS, row_groups=row_groups)


def test_daren_table2_reconstruction_produces_the_real_expected_candidate_count():
    """Regression anchor tied to the real paper/table that exposed the
    free-form enumeration collapse (see orchestrator.py's
    run_table_enumeration module comment): free-form enumeration produced
    exactly ONE candidate for this table's 'total yield' measure (and
    seven others like it), both before and after the entity-identity-
    guidance fix. A correct Step B reconstruction of this same real data
    -- 7 logical rows (Trailblazer x 3 maturities, Pathfinder x 3
    maturities, Cave-in-Rock's one reported maturity) x 8 value columns (4
    measures x 2 sites) -- must produce 56 candidates through Step C's
    deterministic cross-product, not 1."""
    classification = daren_table2_classification()
    candidates = orchestrator._table_classification_to_candidates(classification, {})

    assert len(candidates) == 7 * 8 == 56
    assert len({c.candidate_id for c in candidates}) == 56  # every id genuinely unique
    assert all(c.anchors == ["b:0119"] for c in candidates)

    trailblazer_veg_total_ames = next(
        c for c in candidates
        if c.candidate_id == orchestrator._sanitize_candidate_id("total_yield_ames_trailblazer_vegetative")
    )
    assert "0.19" in trailblazer_veg_total_ames.description
    assert "Trailblazer" in trailblazer_veg_total_ames.description

    # Cave-in-Rock only reports the Vegetative maturity in this data ->
    # exactly 8 candidates (one per value column), not 24.
    cave_in_rock_candidates = [c for c in candidates if "cave_in_rock" in c.candidate_id]
    assert len(cave_in_rock_candidates) == 8


def test_daren_table2_treatment_projection_produces_the_real_expected_combinations():
    """Same real Table 2 reconstruction, projected as Treatment candidates
    instead of Observation ones: a Treatment is the (Population, Maturity,
    Site) COMBINATION, deduplicated across the 4 measures reported for
    each -- 7 logical rows x 2 sites = 14 distinct combinations, not 56
    (one per measure) and not 9/18 (free-form enumeration's real result,
    which treated Population and Maturity as separate, uncombined
    Treatments -- see TABLE_ENUMERATION_ENTITY_TYPES's own module comment
    for why that was wrong)."""
    classification = daren_table2_classification()
    candidates, covered = orchestrator._table_classifications_to_treatment_candidates([classification], {})

    assert len(candidates) == 7 * 2 == 14
    assert covered == {"b:0119"}

    # candidate_id is built from combo.items() sorted by KEY (alphabetical:
    # Maturity, Population, Site), so the values appear in that same order.
    trailblazer_veg_ames = next(
        c for c in candidates
        if c.candidate_id == orchestrator._sanitize_candidate_id("Vegetative_Trailblazer_Ames")
    )
    assert "Trailblazer" in trailblazer_veg_ames.description
    assert "Vegetative" in trailblazer_veg_ames.description
    assert "Ames" in trailblazer_veg_ames.description
    assert trailblazer_veg_ames.anchors == ["b:0119"]

    # Cave-in-Rock only reports Vegetative in this data -> exactly 2
    # combinations (one per site), not 6.
    cave_in_rock = [c for c in candidates if "cave_in_rock" in c.candidate_id.lower()]
    assert len(cave_in_rock) == 2


# --------------------------------------------------------------------- #
# 6. run_table_classification (Step B, mocked invoke, bounded retry)
# --------------------------------------------------------------------- #

def test_run_table_classification_succeeds_first_attempt(env):
    invoke = make_invoke_sequence([("extractor", _table_classification_inv(valid_classification_payload()))])
    result, error = orchestrator.run_table_classification(
        run_id="run1", paper_id=PAPER_ID, seed_table_anchor="b:0006",
        other_tables=[], model="test-model", invoke=invoke,
    )
    assert error is None
    assert result.applicable is True
    assert len(result.row_groups) == 3


def test_run_table_classification_retries_on_invalid_anchor_then_succeeds(env):
    bad = valid_classification_payload()
    bad["table_anchors"] = ["b:9999"]
    for rg in bad["row_groups"]:
        rg["source_table_anchor"] = "b:9999"
    invoke = make_invoke_sequence([
        ("extractor", _table_classification_inv(bad)),
        ("extractor", _table_classification_inv(valid_classification_payload())),
    ])
    result, error = orchestrator.run_table_classification(
        run_id="run1", paper_id=PAPER_ID, seed_table_anchor="b:0006",
        other_tables=[], model="test-model", invoke=invoke,
    )
    assert error is None
    assert result is not None
    assert result.table_anchors == ["b:0006"]


def test_run_table_classification_retries_on_sanity_check_failure_then_gives_up(env):
    dropped = valid_classification_payload()
    dropped["row_groups"] = dropped["row_groups"][:1]  # drops 2 of 3 real rows -> fails the sanity check
    invoke = make_invoke_sequence([
        ("extractor", _table_classification_inv(dropped)),
        ("extractor", _table_classification_inv(dropped)),
        ("extractor", _table_classification_inv(dropped)),
    ])
    result, error = orchestrator.run_table_classification(
        run_id="run1", paper_id=PAPER_ID, seed_table_anchor="b:0006",
        other_tables=[], model="test-model", invoke=invoke,
    )
    assert result is None
    assert error is not None


def test_run_table_classification_not_applicable_is_accepted_without_sanity_check(env):
    payload = {
        "applicable": False, "reason": "regression equation table, not raw values",
        "table_anchors": ["b:0006"], "value_columns": [], "row_groups": [],
    }
    invoke = make_invoke_sequence([("extractor", _table_classification_inv(payload))])
    result, error = orchestrator.run_table_classification(
        run_id="run1", paper_id=PAPER_ID, seed_table_anchor="b:0006",
        other_tables=[], model="test-model", invoke=invoke,
    )
    assert error is None
    assert result.applicable is False


def test_run_table_classification_retries_on_malformed_json(env):
    malformed = orchestrator.AgentInvocation(
        agent="extractor", model="test-model", prompt="prompt",
        returncode=0, stdout="not json", stderr="",
        final_text="not json", parsed_json=None, parse_error="no valid JSON object found",
    )
    invoke = make_invoke_sequence([
        ("extractor", malformed),
        ("extractor", _table_classification_inv(valid_classification_payload())),
    ])
    result, error = orchestrator.run_table_classification(
        run_id="run1", paper_id=PAPER_ID, seed_table_anchor="b:0006",
        other_tables=[], model="test-model", invoke=invoke,
    )
    assert error is None
    assert result is not None


def test_run_table_classification_second_call_uses_cache_no_new_invoke(env):
    # Entity-agnostic caching: Treatment's turn classifies a table, then
    # Observation's turn (same run_id, same table) must reuse it for free.
    first_invoke = make_invoke_sequence([("extractor", _table_classification_inv(valid_classification_payload()))])
    first_result, first_error = orchestrator.run_table_classification(
        run_id="run1", paper_id=PAPER_ID, seed_table_anchor="b:0006",
        other_tables=[], model="test-model", invoke=first_invoke,
    )
    assert first_error is None

    def _fail_if_called(agent, model, prompt, timeout=300):
        raise AssertionError("a cached table classification must never trigger a new invoke call")

    second_result, second_error = orchestrator.run_table_classification(
        run_id="run1", paper_id=PAPER_ID, seed_table_anchor="b:0006",
        other_tables=[], model="test-model", invoke=_fail_if_called,
    )
    assert second_error is None
    assert second_result.model_dump() == first_result.model_dump()


def test_run_table_classification_cache_is_scoped_per_run_id(env):
    invoke1 = make_invoke_sequence([("extractor", _table_classification_inv(valid_classification_payload()))])
    orchestrator.run_table_classification(
        run_id="run1", paper_id=PAPER_ID, seed_table_anchor="b:0006",
        other_tables=[], model="test-model", invoke=invoke1,
    )
    # A DIFFERENT run_id must not see run1's cache -- real invoke required.
    invoke2 = make_invoke_sequence([("extractor", _table_classification_inv(valid_classification_payload()))])
    result, error = orchestrator.run_table_classification(
        run_id="run2", paper_id=PAPER_ID, seed_table_anchor="b:0006",
        other_tables=[], model="test-model", invoke=invoke2,
    )
    assert error is None
    assert result is not None


def test_load_cached_table_classification_missing_file_returns_none(env):
    assert orchestrator._load_cached_table_classification("run1", "table_classification__b_9999") is None


# --------------------------------------------------------------------- #
# 7. run_table_classification_pass (Steps A + B, entity-agnostic, cached)
# --------------------------------------------------------------------- #

def test_run_table_classification_pass_returns_classifications_keyed_by_anchor(env):
    invoke = make_invoke_sequence([("extractor", _table_classification_inv(valid_classification_payload()))])
    classifications = orchestrator.run_table_classification_pass(
        run_id="run1", paper_id=PAPER_ID, model="test-model", invoke=invoke,
    )
    assert set(classifications.keys()) == {"b:0006"}
    assert len(classifications["b:0006"].row_groups) == 3


def test_run_table_classification_pass_second_call_reuses_cache(env):
    first_invoke = make_invoke_sequence([("extractor", _table_classification_inv(valid_classification_payload()))])
    orchestrator.run_table_classification_pass(run_id="run1", paper_id=PAPER_ID, model="test-model", invoke=first_invoke)

    def _fail_if_called(agent, model, prompt, timeout=300):
        raise AssertionError("a second pass within the same run_id must reuse the cache, not re-invoke")

    classifications = orchestrator.run_table_classification_pass(
        run_id="run1", paper_id=PAPER_ID, model="test-model", invoke=_fail_if_called,
    )
    assert set(classifications.keys()) == {"b:0006"}


# --------------------------------------------------------------------- #
# 7b. _table_classifications_to_treatment_candidates (Step C, Treatment
#     projection -- a Treatment is the full combination of factor levels,
#     not any single factor alone; see TABLE_ENUMERATION_ENTITY_TYPES's
#     own module comment for the real Daren-1997-Canopy evidence).
# --------------------------------------------------------------------- #

def test_treatment_candidates_single_factor_collapses_to_one_per_level():
    tc = TableClassification(
        applicable=True, table_anchors=["b:0001"],
        value_columns=[TableValueColumn(value_column_id="yield", variable_name_hint="yield")],
        row_groups=[
            TableRowGroup(row_group_id="control", source_table_anchor="b:0001",
                          factor_values={"Treatment": "control"}, cells={"yield": "3.2"}),
            TableRowGroup(row_group_id="n_fert", source_table_anchor="b:0001",
                          factor_values={"Treatment": "n_fert"}, cells={"yield": "4.1"}),
        ],
    )
    candidates, covered = orchestrator._table_classifications_to_treatment_candidates([tc], {})
    assert len(candidates) == 2
    assert covered == {"b:0001"}


def test_treatment_candidates_multi_factor_produces_full_combination():
    # Two factors (Population x Maturity) crossed with two value columns
    # (Ames/Mead sites) -- the real Daren shape, minimal version.
    tc = TableClassification(
        applicable=True, table_anchors=["b:0119"],
        value_columns=[
            TableValueColumn(value_column_id="yield_ames", variable_name_hint="yield", site_hint="Ames"),
            TableValueColumn(value_column_id="yield_mead", variable_name_hint="yield", site_hint="Mead"),
        ],
        row_groups=[
            TableRowGroup(row_group_id="trailblazer_veg", source_table_anchor="b:0119",
                          factor_values={"Population": "Trailblazer", "Maturity": "Vegetative"},
                          cells={"yield_ames": "0.19", "yield_mead": "0.30"}),
        ],
    )
    candidates, covered = orchestrator._table_classifications_to_treatment_candidates([tc], {})
    # One row x two site-columns -> two distinct (Population, Maturity, Site) combinations.
    assert len(candidates) == 2
    assert covered == {"b:0119"}


def test_treatment_candidates_deduplicates_across_value_columns_sharing_the_same_combo():
    # Four different measures reported for the SAME (Population, Site) --
    # must yield ONE Treatment candidate, not four.
    tc = TableClassification(
        applicable=True, table_anchors=["b:0119"],
        value_columns=[
            TableValueColumn(value_column_id="total_yield", variable_name_hint="total yield", site_hint="Ames"),
            TableValueColumn(value_column_id="leaf_blade", variable_name_hint="leaf blade dry wt", site_hint="Ames"),
            TableValueColumn(value_column_id="leaf_sheath", variable_name_hint="leaf sheath dry wt", site_hint="Ames"),
            TableValueColumn(value_column_id="stem", variable_name_hint="stem dry wt", site_hint="Ames"),
        ],
        row_groups=[
            TableRowGroup(row_group_id="trailblazer", source_table_anchor="b:0119",
                          factor_values={"Population": "Trailblazer"},
                          cells={"total_yield": "0.19", "leaf_blade": "0.13", "leaf_sheath": "0.06", "stem": "0"}),
        ],
    )
    candidates, covered = orchestrator._table_classifications_to_treatment_candidates([tc], {})
    assert len(candidates) == 1
    assert covered == {"b:0119"}


def test_treatment_candidates_links_site_id_deterministically():
    pool = {"site_id": [{"slug": "ames_ia", "record_id": "...", "name": "Ames"},
                         {"slug": "mead_ne", "record_id": "...", "name": "Mead"}]}
    tc = TableClassification(
        applicable=True, table_anchors=["b:0119"],
        value_columns=[TableValueColumn(value_column_id="yield_ames", variable_name_hint="yield", site_hint="Ames")],
        row_groups=[TableRowGroup(row_group_id="trailblazer", source_table_anchor="b:0119",
                                   factor_values={"Population": "Trailblazer"}, cells={"yield_ames": "0.19"})],
    )
    candidates, _covered = orchestrator._table_classifications_to_treatment_candidates([tc], pool)
    assert candidates[0].linked_candidates == {"site_id": "ames_ia"}


def test_treatment_candidates_not_applicable_classification_contributes_nothing():
    tc = TableClassification(applicable=False, reason="not treatment data", table_anchors=["b:0001"])
    candidates, covered = orchestrator._table_classifications_to_treatment_candidates([tc], {})
    assert candidates == []
    assert covered == set()


def test_treatment_candidates_blank_cell_produces_no_candidate():
    tc = TableClassification(
        applicable=True, table_anchors=["b:0001"],
        value_columns=[TableValueColumn(value_column_id="yield", variable_name_hint="yield")],
        row_groups=[TableRowGroup(row_group_id="r", source_table_anchor="b:0001",
                                   factor_values={"Population": "Trailblazer"}, cells={"yield": None})],
    )
    candidates, covered = orchestrator._table_classifications_to_treatment_candidates([tc], {})
    assert candidates == []
    assert covered == set()


# --------------------------------------------------------------------- #
# 7. run_table_enumeration (Steps A + B + C wired together)
# --------------------------------------------------------------------- #

def test_run_table_enumeration_discovers_and_classifies_the_one_table(env):
    invoke = make_invoke_sequence([("extractor", _table_classification_inv(valid_classification_payload()))])
    candidates, covered = orchestrator.run_table_enumeration(
        run_id="run1", paper_id=PAPER_ID, entity_type="Observation", model="test-model", invoke=invoke,
    )
    assert len(candidates) == 3
    assert covered == {"b:0006"}


def test_run_table_enumeration_not_applicable_table_is_not_covered(env):
    payload = {"applicable": False, "reason": "not raw values", "table_anchors": ["b:0006"],
               "value_columns": [], "row_groups": []}
    invoke = make_invoke_sequence([("extractor", _table_classification_inv(payload))])
    candidates, covered = orchestrator.run_table_enumeration(
        run_id="run1", paper_id=PAPER_ID, entity_type="Observation", model="test-model", invoke=invoke,
    )
    assert candidates == []
    assert covered == set()


def test_run_table_enumeration_failed_classification_is_not_covered(env):
    dropped = valid_classification_payload()
    dropped["row_groups"] = dropped["row_groups"][:1]
    invoke = make_invoke_sequence([("extractor", _table_classification_inv(dropped))] * 3)
    candidates, covered = orchestrator.run_table_enumeration(
        run_id="run1", paper_id=PAPER_ID, entity_type="Observation", model="test-model", invoke=invoke,
    )
    assert candidates == []
    assert covered == set()


def test_run_table_enumeration_no_table_blocks_returns_empty(env, tmp_path, monkeypatch):
    no_table_paper = "no_table_paper"
    pdir = tmp_path / "papers2" / no_table_paper
    pdir.mkdir(parents=True)
    (pdir / "content.md").write_text("Just text.\n⟦b:0001⟧\n", encoding="utf-8")
    (pdir / "provenance.json").write_text(
        json.dumps({"b:0001": {"block_type": "Text", "page_id": "page_0", "section_path": []}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("IR_PAPERS_ROOT", str(tmp_path / "papers2"))

    invoke = make_invoke_sequence([])  # must never be called
    candidates, covered = orchestrator.run_table_enumeration(
        run_id="run1", paper_id=no_table_paper, entity_type="Observation", model="test-model", invoke=invoke,
    )
    assert candidates == []
    assert covered == set()


def test_run_table_enumeration_treatment_dispatch_produces_deduplicated_combinations(env):
    invoke = make_invoke_sequence([("extractor", _table_classification_inv(valid_classification_payload()))])
    candidates, covered = orchestrator.run_table_enumeration(
        run_id="run1", paper_id=PAPER_ID, entity_type="Treatment", model="test-model", invoke=invoke,
    )
    # valid_classification_payload has 3 row_groups x 1 value_column -> 3
    # distinct single-factor Treatment combinations (no site_hint set).
    assert len(candidates) == 3
    assert covered == {"b:0006"}


def test_run_table_enumeration_treatment_and_observation_share_one_classification_pass(env):
    # The SAME table classified once, then projected two different ways --
    # confirms Treatment's turn and Observation's turn (same run_id) never
    # trigger a second real classification call for the same table.
    invoke = make_invoke_sequence([("extractor", _table_classification_inv(valid_classification_payload()))])
    treatment_candidates, _ = orchestrator.run_table_enumeration(
        run_id="run1", paper_id=PAPER_ID, entity_type="Treatment", model="test-model", invoke=invoke,
    )

    def _fail_if_called(agent, model, prompt, timeout=300):
        raise AssertionError("Observation's turn must reuse Treatment's already-cached classification")

    observation_candidates, _ = orchestrator.run_table_enumeration(
        run_id="run1", paper_id=PAPER_ID, entity_type="Observation", model="test-model", invoke=_fail_if_called,
    )
    assert len(treatment_candidates) == 3
    assert len(observation_candidates) == 3  # same 3 row_groups x 1 value_column, different projection shape


# --------------------------------------------------------------------- #
# 8. Step D dedup helper
# --------------------------------------------------------------------- #

def test_drop_candidates_covered_by_tables_removes_fully_covered_candidates():
    candidates = [
        EnumerationCandidate(candidate_id="a", description="a", anchors=["b:0006"], linked_candidates={}),
        EnumerationCandidate(candidate_id="b", description="b", anchors=["b:0007"], linked_candidates={}),
        EnumerationCandidate(candidate_id="c", description="c", anchors=["b:0006", "b:0007"], linked_candidates={}),
    ]
    kept = orchestrator._drop_candidates_covered_by_tables(candidates, {"b:0006"})
    kept_ids = {c.candidate_id for c in kept}
    # "a" is fully covered -> dropped. "b" and "c" each cite at least one
    # anchor OUTSIDE the covered set -> kept (may be genuinely new evidence).
    assert kept_ids == {"b", "c"}


def test_drop_candidates_covered_by_tables_noop_when_nothing_covered():
    candidates = [EnumerationCandidate(candidate_id="a", description="a", anchors=["b:0006"], linked_candidates={})]
    assert orchestrator._drop_candidates_covered_by_tables(candidates, set()) == candidates


# --------------------------------------------------------------------- #
# 8b. _dedupe_candidate_record_ids (Phase 1.2: candidate collision
#     detection) -- table-derived and free-form candidates are generated
#     by two independent passes and can legitimately choose the same
#     sanitized candidate_id; neither must ever silently overwrite the
#     other's run_record() results.
# --------------------------------------------------------------------- #

def test_dedupe_candidate_record_ids_noop_when_no_collision():
    candidates = [
        EnumerationCandidate(candidate_id="a", description="a", anchors=["b:0001"], linked_candidates={}),
        EnumerationCandidate(candidate_id="b", description="b", anchors=["b:0001"], linked_candidates={}),
    ]
    deduped, notes = orchestrator._dedupe_candidate_record_ids(candidates)
    assert [c.candidate_id for c in deduped] == ["a", "b"]
    assert notes == []


def test_dedupe_candidate_record_ids_disambiguates_exact_string_collision():
    table_candidate = EnumerationCandidate(
        candidate_id="ambient_co2", description="from table", anchors=["b:0001"], linked_candidates={},
    )
    freeform_candidate = EnumerationCandidate(
        candidate_id="ambient_co2", description="from prose", anchors=["b:0002"], linked_candidates={},
    )
    deduped, notes = orchestrator._dedupe_candidate_record_ids([table_candidate, freeform_candidate])
    # Neither candidate is dropped or merged -- both survive, distinguished
    # only by a disambiguated id, never a silent overwrite.
    assert [c.candidate_id for c in deduped] == ["ambient_co2", "ambient_co2_dup2"]
    assert deduped[0].description == "from table"
    assert deduped[1].description == "from prose"
    assert len(notes) == 1
    assert "ambient_co2" in notes[0]


def test_dedupe_candidate_record_ids_collision_detected_after_sanitization():
    # The collision that matters is on the record_id actually used, which
    # is built from the SANITIZED candidate_id -- two raw candidate_ids
    # that only collide once sanitized must be caught too.
    a = EnumerationCandidate(candidate_id="Ambient CO2!", description="a", anchors=["b:0001"], linked_candidates={})
    b = EnumerationCandidate(candidate_id="ambient_co2", description="b", anchors=["b:0002"], linked_candidates={})
    deduped, notes = orchestrator._dedupe_candidate_record_ids([a, b])
    assert orchestrator._sanitize_candidate_id(deduped[0].candidate_id) == "ambient_co2"
    assert orchestrator._sanitize_candidate_id(deduped[1].candidate_id) == "ambient_co2_dup2"
    assert len(notes) == 1


def test_dedupe_candidate_record_ids_handles_three_way_collision():
    candidates = [
        EnumerationCandidate(candidate_id="x", description=str(i), anchors=["b:0001"], linked_candidates={})
        for i in range(3)
    ]
    deduped, notes = orchestrator._dedupe_candidate_record_ids(candidates)
    assert [c.candidate_id for c in deduped] == ["x", "x_dup2", "x_dup3"]
    assert len(notes) == 2


def test_dedupe_candidate_record_ids_preserves_known_value():
    # model_copy(update=...) must only touch candidate_id -- every other
    # field (including the Phase 1.3 known_value) carries over unchanged.
    a = EnumerationCandidate(candidate_id="x", description="a", anchors=["b:0001"], linked_candidates={}, known_value="3.2")
    b = EnumerationCandidate(candidate_id="x", description="b", anchors=["b:0002"], linked_candidates={}, known_value="4.1")
    deduped, _ = orchestrator._dedupe_candidate_record_ids([a, b])
    assert deduped[0].known_value == "3.2"
    assert deduped[1].known_value == "4.1"


# --------------------------------------------------------------------- #
# 9. _run_multi_record_entity wiring (Step D end-to-end call shape) --
#    dependency resolution itself is unrelated to what this section
#    tests, so _resolve_known_refs is monkeypatched to always resolve,
#    isolating exactly the table-enumeration wiring/merge/dedup behavior.
# --------------------------------------------------------------------- #

def test_run_multi_record_entity_merges_table_and_freeform_candidates_for_observation(env, monkeypatch):
    monkeypatch.setattr(orchestrator, "_resolve_known_refs", lambda entity_type, records: ({}, None))
    monkeypatch.setattr(orchestrator, "_multi_record_link_pools", lambda *a, **k: {})

    table_candidate = EnumerationCandidate(
        candidate_id="table_one", description="from table", anchors=["b:0006"], linked_candidates={},
    )
    monkeypatch.setattr(
        orchestrator, "run_table_enumeration",
        lambda **kwargs: ([table_candidate], {"b:0006"}),
    )

    freeform_new = EnumerationCandidate(
        candidate_id="freeform_new", description="genuinely new", anchors=["b:0007"], linked_candidates={},
    )
    freeform_duplicate = EnumerationCandidate(
        candidate_id="freeform_dup", description="duplicate of table coverage",
        anchors=["b:0006"], linked_candidates={},
    )
    captured_excluded = {}

    def _fake_run_enumeration(*, excluded_table_anchors=None, **kwargs):
        captured_excluded["value"] = excluded_table_anchors
        return [freeform_new, freeform_duplicate], None

    monkeypatch.setattr(orchestrator, "run_enumeration", _fake_run_enumeration)

    seen_record_ids = []

    def _fake_apply_candidate_links(paper_id, entity_type, this_run_records, known_refs, candidate):
        return known_refs

    def _fake_run_record(*, entity_type, record_id, **kwargs):
        seen_record_ids.append(record_id)
        return orchestrator.RecordResult(
            status="ready", entity_type=entity_type, record_id=record_id,
            detail={"payload": {}, "ai_validation": None},
        )

    monkeypatch.setattr(orchestrator, "_apply_candidate_links", _fake_apply_candidate_links)
    monkeypatch.setattr(orchestrator, "run_record", _fake_run_record)

    record_infos = orchestrator._run_multi_record_entity(
        run_id="run1", paper_id=PAPER_ID, entity_type="Observation", model="test-model",
        client=None, invoke=make_invoke_sequence([]), enable_ai_validation=False, this_run_records={},
    )

    # The table candidate and the genuinely-new free-form candidate are
    # both processed; the free-form candidate whose only anchor is
    # already covered by the table pass is dropped (Step D dedup).
    assert len(record_infos) == 2
    processed_ids = {r["record_id"] for r in record_infos}
    assert any("table_one" in rid for rid in processed_ids)
    assert any("freeform_new" in rid for rid in processed_ids)
    assert not any("freeform_dup" in rid for rid in processed_ids)

    # run_enumeration (the free-form pass) was told what the table pass
    # already covered, so it can steer the model away from re-reporting it.
    assert captured_excluded["value"] == {"b:0006"}


def test_run_multi_record_entity_disambiguates_colliding_candidate_ids_instead_of_overwriting(env, monkeypatch):
    # Phase 1.2 (candidate collision detection): a table-derived candidate
    # and a free-form candidate independently choosing the SAME
    # candidate_id must both still be attempted, under distinct record_ids
    # -- never one silently overwriting the other's run_record() results.
    monkeypatch.setattr(orchestrator, "_resolve_known_refs", lambda entity_type, records: ({}, None))
    monkeypatch.setattr(orchestrator, "_multi_record_link_pools", lambda *a, **k: {})

    table_candidate = EnumerationCandidate(
        candidate_id="ambient_co2", description="from table", anchors=["b:0006"], linked_candidates={},
    )
    monkeypatch.setattr(orchestrator, "run_table_enumeration", lambda **kwargs: ([table_candidate], {"b:0006"}))

    freeform_candidate = EnumerationCandidate(
        candidate_id="ambient_co2", description="from prose", anchors=["b:0007"], linked_candidates={},
    )
    monkeypatch.setattr(orchestrator, "run_enumeration", lambda **kwargs: ([freeform_candidate], None))
    monkeypatch.setattr(
        orchestrator, "_apply_candidate_links",
        lambda paper_id, entity_type, this_run_records, known_refs, candidate: known_refs,
    )

    seen_record_ids = []

    def _fake_run_record(*, entity_type, record_id, **kwargs):
        seen_record_ids.append(record_id)
        return orchestrator.RecordResult(
            status="ready", entity_type=entity_type, record_id=record_id,
            detail={"payload": {}, "ai_validation": None},
        )

    monkeypatch.setattr(orchestrator, "run_record", _fake_run_record)

    record_infos = orchestrator._run_multi_record_entity(
        run_id="run_collide", paper_id=PAPER_ID, entity_type="Observation", model="test-model",
        client=None, invoke=make_invoke_sequence([]), enable_ai_validation=False, this_run_records={},
    )

    # Both candidates were attempted, each under its own distinct record_id.
    assert len(record_infos) == 2
    assert len(seen_record_ids) == 2
    assert len(set(seen_record_ids)) == 2

    # The collision itself is disclosed on disk, not just silently resolved.
    collision_artifact = run_store.load_json(
        run_store.record_dir("run_collide", "Observation__enumeration") / "candidate_collision" / "attempt1.json"
    )
    assert collision_artifact["collisions"]
    assert "ambient_co2" in collision_artifact["collisions"][0]


def test_run_multi_record_entity_skips_table_enumeration_for_non_table_entity_types(env, monkeypatch):
    # Treatment and Observation both use table enumeration now (see
    # TABLE_ENUMERATION_ENTITY_TYPES) -- Variable is the control case that
    # must NOT trigger it.
    monkeypatch.setattr(orchestrator, "_resolve_known_refs", lambda entity_type, records: ({}, None))
    monkeypatch.setattr(orchestrator, "_multi_record_link_pools", lambda *a, **k: {})

    def _fail_if_called(**kwargs):
        raise AssertionError("run_table_enumeration must not be called for a non-table-enumeration entity type")

    monkeypatch.setattr(orchestrator, "run_table_enumeration", _fail_if_called)
    monkeypatch.setattr(orchestrator, "run_enumeration", lambda **kwargs: ([], None))

    record_infos = orchestrator._run_multi_record_entity(
        run_id="run1", paper_id=PAPER_ID, entity_type="Variable", model="test-model",
        client=None, invoke=make_invoke_sequence([]), enable_ai_validation=False, this_run_records={},
    )
    assert record_infos == []
