"""Step B reconstructions checked against the raw provenance grid, main-effect tables whose marginal rows set more
than one factor, free-form Treatment dimensions reconciled with the tables, dated treatment factors, and design-statement
factor promotion. Synthetic tables use the shapes of Daren-1997-Canopy Tables 2 and 7 (packed cells, a row whose label
the rendering lost); the real ones are in fixtures/grid_checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import design, orchestrator
from pipeline.raw_schema import (
    CandidateDimension, EnumerationCandidate, TableClassification, TableFactor, TableRowGroup, TableValueColumn,
    TableVariable,
)

PAPER = "gridpaper"
COLUMNS = ["yield_a", "yield_b", "blade_a", "blade_b"]


def _grid_paper(tmp_path, monkeypatch, rows: list[list[str]], anchor: str = "b:0010"):
    """A paper whose provenance holds one raw table grid at `anchor`."""
    pdir = tmp_path / PAPER
    pdir.mkdir()
    prov = {anchor: {"block_type": "Table"}}
    for r, cells in enumerate(rows):
        for c, text in enumerate(cells):
            prov[f"{anchor}/c{r}_{c}"] = {"block_type": "TableCell", "parent_table_anchor": anchor,
                                          "row_index": r, "col_index": c, "cell_text": text}
    (pdir / "provenance.json").write_text(json.dumps(prov), encoding="utf-8")
    (pdir / "content.md").write_text(f"table\n⟦{anchor}⟧\n", encoding="utf-8")
    monkeypatch.setenv("IR_PAPERS_ROOT", str(tmp_path))


RAW = [
    ["Population", "Maturity", "A", "B", "A", "B"],
    ["P1", "Veg Elong Rep", "0.19 0.90 1.16", "0.30 1.26 1.09", "0.13 0.38 0.34", "0.18 0.41 0.30"],
    ["P2", "Veg", "0.22", "0.57", "0.12", "0.33"],
    ["P3", "Veg", "0.11", "0.18", "0.08", "0.12"],
    ["", "Rep", "0.87", "1.48", "0.22", "0.29"],
    ["", "Rep", "2.16", "2.20", "0.54", "0.53"],
]


def _classification(rows: dict[str, tuple[dict, list]]) -> TableClassification:
    return TableClassification(
        applicable=True, table_role="treatment_response", table_anchors=["b:0010"],
        factors=[TableFactor(name="Population", dimension="crop", encoding="rows"),
                 TableFactor(name="Maturity", dimension="treatment", encoding="rows"),
                 TableFactor(name="Site", dimension="site", encoding="columns")],
        variables=[TableVariable(label="Yield"), TableVariable(label="Blade")],
        value_columns=[TableValueColumn(value_column_id=c, variable="Yield" if c.startswith("yield") else "Blade")
                       for c in COLUMNS],
        row_groups=[TableRowGroup(row_group_id=rid, factor_values=levels, source_table_anchor="b:0010",
                                  cells=dict(zip(COLUMNS, values)))
                    for rid, (levels, values) in rows.items()],
    )


def _correct_rows() -> dict:
    return {
        "p1_veg": ({"Population": "P1", "Maturity": "Veg"}, ["0.19", "0.30", "0.13", "0.18"]),
        "p1_elong": ({"Population": "P1", "Maturity": "Elong"}, ["0.90", "1.26", "0.38", "0.41"]),
        "p1_rep": ({"Population": "P1", "Maturity": "Rep"}, ["1.16", "1.09", "0.34", "0.30"]),
        "p2_veg": ({"Population": "P2", "Maturity": "Veg"}, ["0.22", "0.57", "0.12", "0.33"]),
        "p3_veg": ({"Population": "P3", "Maturity": "Veg"}, ["0.11", "0.18", "0.08", "0.12"]),
        "p3_rep": ({"Population": "P3", "Maturity": "Rep"}, ["0.87", "1.48", "0.22", "0.29"]),
    }


# --------------------------------------------------------------------- #
# cell placement
# --------------------------------------------------------------------- #


def test_a_faithful_reconstruction_of_packed_cells_has_no_misplaced_values(tmp_path, monkeypatch):
    _grid_paper(tmp_path, monkeypatch, RAW)
    findings = orchestrator._cell_placement_findings(_classification(_correct_rows()), PAPER)
    assert [f for f in findings if f["kind"] == "misplaced"] == []


def test_a_value_taken_from_the_neighbouring_column_is_reported_with_the_source_value(tmp_path, monkeypatch):
    _grid_paper(tmp_path, monkeypatch, RAW)
    rows = _correct_rows()
    rows["p1_veg"] = (rows["p1_veg"][0], ["0.19", "0.30", "0.13", "0.13"])   # blade_b copied from blade_a
    findings = orchestrator._cell_placement_findings(_classification(rows), PAPER)
    assert [(f["row_group_id"], f["value_column_id"], f["value"], f["source_value"])
            for f in findings if f["kind"] == "misplaced"] == [("p1_veg", "blade_b", "0.13", "0.18")]


def test_a_raw_data_row_left_out_of_the_reconstruction_is_reported(tmp_path, monkeypatch):
    _grid_paper(tmp_path, monkeypatch, RAW)
    rows = _correct_rows()
    del rows["p2_veg"]
    findings = orchestrator._cell_placement_findings(_classification(rows), PAPER)
    assert any(f["kind"] == "unreconstructed_row" and "0.57" in f["source_row"] for f in findings)


def test_a_raw_row_rendered_one_column_off_is_skipped_not_reported(tmp_path, monkeypatch):
    shifted = RAW[:3] + [["P3", "0.11", "0.18", "0.08", "0.12", ""]] + RAW[4:]
    _grid_paper(tmp_path, monkeypatch, shifted)
    findings = orchestrator._cell_placement_findings(_classification(_correct_rows()), PAPER)
    assert not [f for f in findings if f.get("row_group_id") == "p3_veg"]


def test_a_mostly_disagreeing_pairing_is_not_trusted(tmp_path, monkeypatch):
    """When most values disagree with the left-to-right pairing, the pairing (not the values) is suspect: no finding."""
    _grid_paper(tmp_path, monkeypatch, RAW)
    rows = {rid: (levels, list(reversed(values))) for rid, (levels, values) in _correct_rows().items()}
    assert [f for f in orchestrator._cell_placement_findings(_classification(rows), PAPER) if f["kind"] == "misplaced"] == []


def test_misplaced_values_are_withheld_not_corrected():
    rows = _correct_rows()
    rows["p1_veg"] = (rows["p1_veg"][0], ["0.19", "0.30", "0.13", "0.13"])
    finding = {"kind": "misplaced", "row_group_id": "p1_veg", "value_column_id": "blade_b", "value": "0.13",
               "source_value": "0.18", "anchor": "b:0010"}
    withheld = orchestrator._withhold_cells(_classification(rows), [finding])
    cells = next(rg.cells for rg in withheld.row_groups if rg.row_group_id == "p1_veg")
    assert cells["blade_b"] is None and cells["blade_a"] == "0.13"


# --------------------------------------------------------------------- #
# duplicate factor levels
# --------------------------------------------------------------------- #


def test_a_later_row_claiming_the_same_levels_is_withheld():
    rows = _correct_rows()
    rows["p3_rep_2"] = ({"Population": "P3", "Maturity": "Rep"}, ["2.16", "2.20", "0.54", "0.53"])
    kept, withheld = orchestrator._withhold_duplicate_row_groups(_classification(rows))
    assert [w["row_group_id"] for w in withheld] == ["p3_rep_2"] and withheld[0]["same_levels_as"] == "p3_rep"
    assert "p3_rep" in {rg.row_group_id for rg in kept.row_groups}


def test_distinct_rows_are_all_kept():
    kept, withheld = orchestrator._withhold_duplicate_row_groups(_classification(_correct_rows()))
    assert withheld == [] and len(kept.row_groups) == 6


# --------------------------------------------------------------------- #
# main-effect layout with two-factor marginal rows
# --------------------------------------------------------------------- #


def _table7(extra_crossed_row: bool = False) -> TableClassification:
    rows = [TableRowGroup(row_group_id=f"l{i}", factor_values={"Location": loc, "Maturity": mat},
                          source_table_anchor="b:0761", cells={"lai": v})
            for i, (loc, mat, v) in enumerate([("A", "Veg", "2.8"), ("A", "Rep", "4.9"), ("B", "Veg", "3.4"), ("B", "Rep", "4.5")])]
    rows += [TableRowGroup(row_group_id=f"p{i}", factor_values={"Population": pop}, source_table_anchor="b:0761",
                           cells={"lai": v}) for i, (pop, v) in enumerate([("P1", "4.0"), ("P2", "4.7")])]
    if extra_crossed_row:
        rows.append(TableRowGroup(row_group_id="x", factor_values={"Location": "A", "Maturity": "Veg", "Population": "P1"},
                                  source_table_anchor="b:0761", cells={"lai": "3.0"}))
    return TableClassification(
        applicable=True, table_role="treatment_response", table_anchors=["b:0761"],
        factors=[TableFactor(name="Location", dimension="site", encoding="rows"),
                 TableFactor(name="Maturity", dimension="treatment", encoding="rows"),
                 TableFactor(name="Population", dimension="crop", encoding="rows")],
        variables=[TableVariable(label="LAI")], value_columns=[TableValueColumn(value_column_id="lai", variable="LAI")],
        row_groups=rows,
    )


def test_rows_setting_two_of_three_factors_and_rows_setting_one_are_a_main_effect_table():
    table = _table7()
    assert design.is_main_effect_layout(table)
    location_row, population_row = table.row_groups[0], table.row_groups[4]
    assert design.cell_pooling(table, location_row, "lai").pooled_over == ["Population"]
    pooled = design.cell_pooling(table, population_row, "lai")
    assert pooled.pooled_over == ["Location", "Maturity"]
    assert pooled.representation == design.BLOCKED_BY_REPRESENTATION   # averaged over the treatment: no Treatment


def test_a_fully_crossed_row_keeps_the_table_cell_level():
    assert not design.is_main_effect_layout(_table7(extra_crossed_row=True))


# --------------------------------------------------------------------- #
# free-form Treatment dimensions reconciled with the tables
# --------------------------------------------------------------------- #


def _freeform(cid: str, dims: list[tuple[str, str, str]]) -> EnumerationCandidate:
    return EnumerationCandidate(candidate_id=cid, description=cid, anchors=["b:0001"],
                                dimensions=[CandidateDimension(name=n, dimension=d, level=lv) for n, d, lv in dims])


def test_a_level_the_tables_declare_as_crop_is_not_a_treatment_whatever_the_model_declared():
    declared = design.declared_level_dimensions({"t": _classification(_correct_rows())})
    candidates = [_freeform("p1_a", [("Population", "treatment", "P1"), ("Site", "site", "A")]),
                  _freeform("veg_a", [("Maturity", "treatment", "Veg"), ("Site", "site", "A")])]
    kept, decisions = orchestrator._drop_freeform_treatments_covered_by_tables(candidates, [], None, {}, declared)
    assert {d["candidate_id"]: d["decision"] for d in decisions} == {
        "p1_a": "dropped_no_treatment_dimension", "veg_a": "kept_new"}
    assert [c.candidate_id for c in kept] == ["veg_a"]


def test_a_level_declared_with_two_different_dimensions_is_left_as_the_model_declared_it():
    other = _classification(_correct_rows()).model_copy(update={"factors": [
        TableFactor(name="Population", dimension="treatment", encoding="rows"),
        TableFactor(name="Maturity", dimension="time", encoding="rows"),
        TableFactor(name="Site", dimension="site", encoding="columns")]})
    declared = design.declared_level_dimensions({"t": _classification(_correct_rows()), "u": other})
    assert "p1" not in declared and "veg" not in declared


# --------------------------------------------------------------------- #
# a treatment factor may be dated
# --------------------------------------------------------------------- #


def test_time_levels_may_date_a_treatment_factor():
    data = _classification(_correct_rows()).model_dump()
    data["time_levels"] = [{"factor": "Maturity", "level": "Veg", "site": None, "date_text": "9 June",
                            "year_text": "1993", "anchors": ["b:0001"]}]
    assert TableClassification.model_validate(data).time_levels[0].level == "Veg"


def test_time_levels_still_refuse_a_crop_factor():
    data = _classification(_correct_rows()).model_dump()
    data["time_levels"] = [{"factor": "Population", "level": "P1", "site": None, "date_text": "9 June",
                            "year_text": None, "anchors": ["b:0001"]}]
    with pytest.raises(ValueError):
        TableClassification.model_validate(data)


# --------------------------------------------------------------------- #
# the real Daren-1997-Canopy Step B answers
# --------------------------------------------------------------------- #

GRID_FIXTURES = Path(__file__).parent / "fixtures" / "grid_checks"


@pytest.fixture()
def daren(monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(GRID_FIXTURES))
    answers = json.loads((GRID_FIXTURES / "daren_step_b_answers.json").read_text(encoding="utf-8"))["classifications"]
    return {k: TableClassification.model_validate(v) for k, v in answers.items()}


def test_daren_table_2_misplaced_values_are_exactly_the_seven_verified_by_hand(daren):
    findings = orchestrator._cell_placement_findings(daren["b_0119"], "Daren-1997-Canopy")
    assert sorted((f["row_group_id"], f["value_column_id"], f["value"], f["source_value"]) for f in findings) == sorted([
        ("trailblazer_vegetative", "leaf_blade_mead", "0.06", "0.18"),
        ("trailblazer_elongating", "leaf_blade_mead", "0.25", "0.41"),
        ("trailblazer_reproductive", "leaf_blade_mead", "0.22", "0.30"),
        ("trailblazer_reproductive", "leaf_sheath_ames", "0.34", "0.22"),
        ("pathfinder_vegetative", "leaf_blade_mead", "0.08", "0.17"),
        ("pathfinder_elongating", "leaf_blade_mead", "0.22", "0.38"),
        ("pathfinder_reproductive", "leaf_blade_mead", "0.25", "0.29"),
    ])


def test_daren_rows_that_lost_their_population_label_are_withheld(daren):
    _, table2 = orchestrator._withhold_duplicate_row_groups(daren["b_0119"])
    _, table6 = orchestrator._withhold_duplicate_row_groups(daren["b_0607"])
    assert [w["row_group_id"] for w in table2] == ["ey-ff_ldm_dc1_reproductive_2"]
    assert [w["row_group_id"] for w in table6] == ["r17", "r18"]


def test_daren_table_7_is_a_main_effect_table(daren):
    table7 = daren["b_0761"]
    assert design.is_main_effect_layout(table7)
    pooled = {tuple(rg.factor_values): design.cell_pooling(table7, rg, "lai").pooled_over for rg in table7.row_groups}
    assert pooled[("Location", "Maturity")] == ["Population"]
    assert pooled[("Population",)] == ["Location", "Maturity"]


def test_a_factor_the_tables_declare_as_crop_decides_a_level_spelled_differently():
    """The free-form pass may spell a population by its long name; the factor it names is still the tables' crop factor."""
    classifications = {"t": _classification(_correct_rows())}
    candidate = _freeform("p1_long_a", [("population", "treatment", "Population One (long name)"), ("Site", "site", "A")])
    kept, decisions = orchestrator._drop_freeform_treatments_covered_by_tables(
        [candidate], [], None, {}, design.declared_level_dimensions(classifications),
        design.declared_factor_dimensions(classifications))
    assert kept == [] and decisions[0]["decision"] == "dropped_no_treatment_dimension"


# --------------------------------------------------------------------- #
# a time factor the design statement names as a plot factor is a treatment
# --------------------------------------------------------------------- #


def _design_paper(tmp_path, monkeypatch, sentence: str):
    pdir = tmp_path / "papers" / PAPER
    pdir.mkdir(parents=True)
    (pdir / "content.md").write_text(f"{sentence}\n⟦b:0001⟧\n", encoding="utf-8")
    monkeypatch.setenv("IR_PAPERS_ROOT", str(tmp_path / "papers"))
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))


def _time_maturity() -> TableClassification:
    table = _classification(_correct_rows())
    return table.model_copy(update={"factors": [
        TableFactor(name="Population", dimension="crop", encoding="rows"),
        TableFactor(name="Maturity", dimension="time", encoding="rows"),
        TableFactor(name="Site", dimension="site", encoding="columns")]})


def test_a_subplot_factor_declared_time_becomes_a_treatment_in_the_stored_classification(tmp_path, monkeypatch):
    from pipeline import run_store
    _design_paper(tmp_path, monkeypatch, "Whole plots were cultivars and subplots were harvest maturities.")
    run_store.save_final("r1", "table_classification__b_0010", {"status": "success", "classification": _time_maturity().model_dump()})
    out = orchestrator._promote_design_factors("r1", PAPER, {"b:0010": _time_maturity()})
    dims = {f.name: f.dimension for f in out["b:0010"].factors}
    assert dims == {"Population": "crop", "Maturity": "treatment", "Site": "site"}
    stored = run_store.load_json(run_store.record_dir("r1", "table_classification__b_0010") / "final.json")
    assert stored["design_factor_promotions"][0]["evidence_anchor"] == "b:0001"
    assert {f["name"]: f["dimension"] for f in stored["classification"]["factors"]}["Maturity"] == "treatment"


@pytest.mark.parametrize("sentence", ["Subplots were harvested at each maturity.", "Samples were taken at each maturity."])
def test_an_action_or_a_non_design_sentence_promotes_nothing(tmp_path, monkeypatch, sentence):
    _design_paper(tmp_path, monkeypatch, sentence)
    out = orchestrator._promote_design_factors("r1", PAPER, {"b:0010": _time_maturity()})
    assert {f.name: f.dimension for f in out["b:0010"].factors}["Maturity"] == "time"
