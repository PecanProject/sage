"""Item 10: Method representation and matching (decisions Q5 and Q6).

Evidence (real run 20260919T085532_98015a8a, Daren-1997-Canopy): Step B produced 11 distinct method
hints; against the run's 7 ready Methods the matcher resolved 4 and left 7 unresolved -- among them
"LI-COR LAI-2000 leaf area analyzer", although a Method whose description says exactly that existed
(the matcher only looked at a Method's short NAME). And a table whose variable is named by ROW labels
(Felipe Table 1) forced the model to invent a placeholder `variable_name_hint` ("measurement") because
the column schema required one, with nowhere to put a per-variable method.

Item 10: a structured `TableVariable {label, variable_name, units, method_hint}` (column -> variable and
row -> variable, legacy column hints preserved); a description tier in the Method matcher (strictly after
the existing tiers, only for a distinctive, unique match); and Method candidates seeded from distinct,
GROUNDED hints. Nothing is invented: a hint no prose block states creates no Method.

Fixtures: tests/fixtures/item10/ (real). Tests marked SYNTHETIC use invented text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline import orchestrator, run_store
from pipeline.raw_schema import (
    EnumerationCandidate, TableClassification, TableFactor, TableRowGroup, TableValueColumn, TableVariable,
)

FIXTURES = Path(__file__).parent / "fixtures" / "item10"
ITEM8 = Path(__file__).parent / "fixtures" / "item8"


def _pool() -> list[dict]:
    return json.loads((FIXTURES / "085532_method_pool.json").read_text())


def _hints() -> dict[str, list[str]]:
    return {h["hint"]: h["columns"] for h in json.loads((FIXTURES / "085532_method_hints.json").read_text())}


def _resolve(hint: str, pool=None):
    return orchestrator._match_method_hint({"Method": hint}, hint, _pool() if pool is None else pool)


def _tc(**kw) -> TableClassification:
    kwargs = dict(table_role="treatment_response", table_anchors=["b:0001"])
    kwargs.update(kw)
    return TableClassification(**kwargs)


def _row(rid, cells, **factor_values):
    return TableRowGroup(row_group_id=rid, factor_values=factor_values, source_table_anchor="b:0001", cells=cells)


# --------------------------------------------------------------------- #
# schema: TableVariable, column -> variable, row -> variable, legacy
# --------------------------------------------------------------------- #


def test_a_row_named_variable_needs_no_invented_placeholder_hint():
    """FM5: Felipe-shaped -- the variables live in the row labels, so the columns name none."""
    tc = _tc(
        factors=[TableFactor(name="Variable", dimension="variable", encoding="rows"),
                 TableFactor(name="Cover crop", dimension="treatment", encoding="columns")],
        variables=[TableVariable(label="PAR intercepted (%)", variable_name="PAR intercepted", units="%"),
                   TableVariable(label="Shoot biomass (g m -2 )", variable_name="shoot biomass", units="g m-2")],
        value_columns=[TableValueColumn(value_column_id="f", factor_levels={"Cover crop": "Fallow"}),
                       TableValueColumn(value_column_id="m", factor_levels={"Cover crop": "Mustard"})],
        row_groups=[_row("r1", {"f": "20", "m": "15"}, Variable="PAR intercepted (%)")],
    )
    assert all(c.variable_name_hint is None for c in tc.value_columns)


def test_a_column_that_identifies_no_variable_at_all_is_rejected():
    with pytest.raises(ValidationError, match="does not identify its variable"):
        _tc(value_columns=[TableValueColumn(value_column_id="v")], row_groups=[_row("r", {"v": "1"})])


def test_a_column_may_point_at_a_declared_variable_and_an_undeclared_one_is_rejected():
    ok = _tc(variables=[TableVariable(label="Yield", units="Mg/ha")],
             value_columns=[TableValueColumn(value_column_id="v", variable="yield")], row_groups=[_row("r", {"v": "1"})])
    assert ok.value_columns[0].variable == "yield"  # matched ignoring case/punctuation
    with pytest.raises(ValidationError, match="not declared in variables"):
        _tc(variables=[TableVariable(label="Yield")], value_columns=[TableValueColumn(value_column_id="v", variable="Biomass")],
            row_groups=[_row("r", {"v": "1"})])


def test_variable_labels_must_be_unique_and_legacy_classifications_are_unchanged():
    with pytest.raises(ValidationError, match="unique labels"):
        _tc(variables=[TableVariable(label="Yield"), TableVariable(label="yield!")],
            value_columns=[TableValueColumn(value_column_id="v", variable_name_hint="y")], row_groups=[_row("r", {"v": "1"})])
    legacy = _tc(value_columns=[TableValueColumn(value_column_id="v", variable_name_hint="tiller weight", method_hint="oven-dried")],
                 row_groups=[_row("r", {"v": "1"})])
    assert legacy.variables == [] and legacy.value_columns[0].variable_name_hint == "tiller weight"


# --------------------------------------------------------------------- #
# resolution: which variable, which method hint
# --------------------------------------------------------------------- #


def test_a_column_resolves_to_its_variable_and_the_canonical_name_is_used():
    tc = _tc(variables=[TableVariable(label="Total yield", variable_name="total dry matter yield", method_hint="hand-clipping harvest")],
             value_columns=[TableValueColumn(value_column_id="v", variable="Total yield")], row_groups=[_row("r", {"v": "1"})])
    label, variable = orchestrator._effective_variable(tc, tc.row_groups[0], tc.value_columns[0])
    assert (label, variable.method_hint) == ("total dry matter yield", "hand-clipping harvest")
    [candidate] = orchestrator._table_classification_to_candidates(tc, {})
    assert candidate.description.startswith("total dry matter yield")


def test_a_row_named_variable_resolves_through_the_rows_level_so_one_variable_has_one_method():
    """A variable repeated across time points must not disagree with itself (why the descriptor was chosen over
    a per-row hint): both DAP rows of 'Shoot biomass' resolve to the SAME variable and the same method hint."""
    tc = _tc(
        factors=[TableFactor(name="DAP", dimension="time", encoding="rows"), TableFactor(name="Variable", dimension="variable", encoding="rows")],
        variables=[TableVariable(label="Shoot biomass", variable_name="shoot biomass", method_hint="oven-dried tomato shoots"),
                   TableVariable(label="PAR intercepted (%)", variable_name="PAR interception")],
        value_columns=[TableValueColumn(value_column_id="v")],
        row_groups=[_row("a", {"v": "70"}, DAP="39", Variable="Shoot biomass"), _row("b", {"v": "246"}, DAP="75", Variable="Shoot biomass"),
                    _row("c", {"v": "20"}, DAP="35", Variable="PAR intercepted (%)")],
    )
    hints = [orchestrator._effective_method_hint(orchestrator._effective_variable(tc, r, tc.value_columns[0])[1], tc.value_columns[0])
             for r in tc.row_groups]
    assert hints == ["oven-dried tomato shoots", "oven-dried tomato shoots", None]  # PAR: no hint stated -> none invented
    labels = [orchestrator._effective_variable(tc, r, tc.value_columns[0])[0] for r in tc.row_groups]
    assert labels == ["shoot biomass", "shoot biomass", "PAR interception"]


def test_an_undeclared_row_level_still_names_the_variable_but_carries_no_method():
    tc = _tc(factors=[TableFactor(name="Variable", dimension="variable", encoding="rows")],
             value_columns=[TableValueColumn(value_column_id="v")], row_groups=[_row("r", {"v": "1"}, Variable="Harvest index")])
    label, variable = orchestrator._effective_variable(tc, tc.row_groups[0], tc.value_columns[0])
    assert (label, variable) == ("Harvest index", None)
    assert orchestrator._effective_method_hint(variable, tc.value_columns[0]) is None


def test_the_legacy_column_hint_still_stands_and_a_variables_own_hint_takes_precedence():
    legacy = TableValueColumn(value_column_id="v", variable_name_hint="LAI", method_hint="LI-COR LAI-2000")
    assert orchestrator._effective_method_hint(None, legacy) == "LI-COR LAI-2000"
    variable = TableVariable(label="LAI", method_hint="the variable's own hint")
    assert orchestrator._effective_method_hint(variable, legacy) == "the variable's own hint"
    no_hint = TableVariable(label="LAI")
    assert orchestrator._effective_method_hint(no_hint, legacy) == "LI-COR LAI-2000"  # legacy fallback


def test_real_felipe_table_1_shape_unresolved_method_is_left_unresolved():
    """The real live classification declares no method for any variable (the paper gives none for these): the
    Observation candidates carry no method link and nothing is fabricated."""
    tc = TableClassification.model_validate(json.loads((ITEM8 / "live_gpt_oss_felipe_table1_classification.json").read_text()))
    pool = [{"slug": "sas_glm", "name": "General Linear Model (GLM)", "description": "Data were analyzed with the GLM procedure of SAS."}]
    candidates = orchestrator._table_classification_to_candidates(tc, {"method_id": pool})
    assert candidates and all("method_id" not in c.linked_candidates for c in candidates)


# --------------------------------------------------------------------- #
# matcher: the description tier, on the real hints and Methods
# --------------------------------------------------------------------- #


def test_real_hints_the_description_tier_resolves_li_cor_and_changes_nothing_else():
    results = {h: _resolve(h) for h in _hints()}
    before = {h: orchestrator._match_row_group_to_pool({"Method": h}, _pool()) for h in _hints()}
    assert sum(1 for v in before.values() if v) == 4 and sum(1 for v in results.values() if v) == 5  # 4 before; +1 now
    changed = {h for h in results if results[h] != before[h]}
    assert changed == {"LI-COR LAI-2000 leaf area analyzer"}
    assert results["LI-COR LAI-2000 leaf area analyzer"] == "li_cor_lai_mta"
    for h in results:  # every earlier resolution is untouched
        if before[h]:
            assert results[h] == before[h]


def test_real_hints_that_are_not_distinctive_or_not_in_the_pool_stay_unresolved():
    """The hand/ruler measurement hints mention 'ruler', 'freshly harvested', ... which no Method states, and the
    only Method about leaf dimensions names 'length and width' together: refused, not approximated."""
    results = {h: _resolve(h) for h in _hints()}
    ruler = [h for h in results if "ruler" in h or "hand" in h and "measured" in h]
    assert len(ruler) == 6 and all(results[h] is None for h in ruler)


def test_synthetic_a_single_generic_or_two_token_hint_never_matches_by_description():
    pool = [{"slug": "m", "name": "Sampling", "description": "Samples were collected weekly from each plot using a standard procedure."}]
    assert _resolve("standard procedure", pool) is None          # two significant tokens: below the minimum
    assert _resolve("measurement method used", pool) is None     # generic tokens only
    assert _resolve("collected weekly standard", pool) == "m"    # three distinctive tokens, all present, unique


def test_synthetic_an_ambiguous_description_is_left_unresolved():
    pool = [{"slug": "a", "name": "Method A", "description": "Roots were washed, dried in a forced-draft oven and weighed."},
            {"slug": "b", "name": "Method B", "description": "Shoots were washed, dried in a forced-draft oven and weighed."}]
    assert _resolve("washed dried forced-draft oven weighed", pool) is None  # two winners
    assert _resolve("roots washed dried forced-draft oven", pool) == "a"


def test_synthetic_the_description_tier_is_consulted_only_when_the_earlier_tiers_hit_nothing():
    """hand-clipping still resolves through its NAME even when two Methods share an identical description; and a
    tie in the earlier tiers is refused rather than broken by the description."""
    shared = "Plants were harvested from a randomly placed 0.1 square-meter quadrat in each plot."
    pool = [{"slug": "hand_clipping", "name": "hand-clipping", "description": shared},
            {"slug": "quadrat_survey", "name": "quadrat survey", "description": shared}]
    assert _resolve("hand-clipping harvest", pool) == "hand_clipping"            # tier 2 (name), description never consulted
    assert _resolve("randomly placed quadrat plants harvested", pool) is None     # identical descriptions: two winners
    tied = [{"slug": "x", "name": "oven drying", "description": "Tillers were weighed after forced-draft drying."},
            {"slug": "y", "name": "oven drying", "description": "Unrelated procedure."}]
    hint = "oven drying tillers forced-draft weighed"  # the description alone would single out x ...
    assert orchestrator._description_candidates(hint, tied) == ["x"]
    assert orchestrator._match_method_hint({"Method": hint}, hint, tied) is None  # ... but the earlier tiers tie, so: refused


def test_synthetic_pools_without_descriptions_and_other_entity_pools_are_unaffected():
    site_pool = [{"slug": "ames_ia", "name": "Ames, IA", "record_id": "x"}]
    assert orchestrator._match_row_group_to_pool({"Site": "Ames"}, site_pool) == "ames_ia"
    assert _resolve("forced-draft oven weighing scale", [{"slug": "m", "name": "oven", "description": None}]) == "m"  # name tier, no description needed


def test_method_pool_items_carry_the_methods_description():
    def record(rid, name, desc):
        return {"entity_type": "Method", "record_id": rid, "status": "ready",
                "detail": {"payload": {"name": {"value": name}, "description": {"value": desc}}}}
    records = {"Method": [record("p_method_a", "A", "desc a"), record("p_method_b", "B", "desc b")]}
    pools = orchestrator._multi_record_link_pools("p", "Observation", records)
    assert {i["slug"]: i["description"] for i in pools["method_id"]} == {"a": "desc a", "b": "desc b"}


def test_the_link_reaches_the_observation_candidate_through_the_description_tier():
    tc = _tc(value_columns=[TableValueColumn(value_column_id="lai", variable_name_hint="Leaf area index", method_hint="LI-COR LAI-2000 leaf area analyzer")],
             row_groups=[_row("r", {"lai": "3.1"})])
    [candidate] = orchestrator._table_classification_to_candidates(tc, {"method_id": _pool()})
    assert candidate.linked_candidates == {"method_id": "li_cor_lai_mta"}
    unrelated = _tc(value_columns=[TableValueColumn(value_column_id="w", variable_name_hint="Precipitation", method_hint="weather station rain gauge")],
                    row_groups=[_row("r", {"w": "3"})])
    assert orchestrator._table_classification_to_candidates(unrelated, {"method_id": _pool()})[0].linked_candidates == {}


# --------------------------------------------------------------------- #
# seeding Method candidates from grounded hints (Q6)
# --------------------------------------------------------------------- #


def _paper(tmp_path, blocks):
    root = tmp_path / "papers"
    (root / "syn").mkdir(parents=True)
    prov, content = {}, ""
    for anchor, block_type, text in blocks:
        prov[anchor] = {"block_type": block_type, "page_id": "page_1", "section_path": [], "rendered_in_content_md": True}
        content += f"{text}\n⟦{anchor}⟧\n\n"
    (root / "syn" / "content.md").write_text(content, encoding="utf-8")
    (root / "syn" / "provenance.json").write_text(json.dumps(prov), encoding="utf-8")
    return root


def _seeds(monkeypatch, root, hints, freeform=(), paper="syn"):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(root))
    columns = [TableValueColumn(value_column_id=f"c{i}", variable_name_hint=f"v{i}", method_hint=h) for i, h in enumerate(hints)]
    tc = _tc(value_columns=columns, row_groups=[_row("r", {c.value_column_id: "1" for c in columns})])
    monkeypatch.setattr(orchestrator, "run_table_classification_pass", lambda **k: {"b_0001": tc})
    return orchestrator._method_hint_seeds(run_id="r", paper_id=paper, model="m", invoke=None, freeform_candidates=list(freeform))


def test_synthetic_a_grounded_distinct_hint_seeds_a_method_candidate_anchored_in_the_prose(tmp_path, monkeypatch):
    root = _paper(tmp_path, [("b:0001", "Table", "| a | 1 |"), ("b:0002", "Text", "Soil cores were air-dried, sieved through a 2-mm mesh and weighed."),
                             ("b:0003", "Text", "Unrelated text.")])
    seeds, decisions = _seeds(monkeypatch, root, ["cores air-dried sieved mesh weighed"])
    [seed] = seeds
    assert seed.anchors == ["b:0002"] and seed.description == "Measurement method described as: cores air-dried sieved mesh weighed"
    assert decisions == [{"hint": "cores air-dried sieved mesh weighed", "decision": "seeded", "candidate_id": seed.candidate_id, "anchors": ["b:0002"]}]


def test_synthetic_a_hint_no_prose_block_states_is_never_seeded(tmp_path, monkeypatch):
    """Felipe-style: a method the paper never describes must stay unresolved, not become a Method."""
    root = _paper(tmp_path, [("b:0001", "Table", "| a | 1 |"), ("b:0002", "Text", "Plots were irrigated weekly.")])
    seeds, decisions = _seeds(monkeypatch, root, ["gravimetric drying oven protocol"])
    assert seeds == [] and "not grounded" in decisions[0]["reason"]


def test_synthetic_table_text_is_not_grounding(tmp_path, monkeypatch):
    root = _paper(tmp_path, [("b:0001", "Table", "| method | gravimetric drying oven protocol |")])
    assert _seeds(monkeypatch, root, ["gravimetric drying oven protocol"])[0] == []


def test_synthetic_method_sounding_but_undistinctive_phrases_never_seed(tmp_path, monkeypatch):
    root = _paper(tmp_path, [("b:0001", "Table", "| a | 1 |"), ("b:0002", "Text", "The standard method was used for the measurement.")])
    seeds, decisions = _seeds(monkeypatch, root, ["standard method measurement", "measured"])
    assert seeds == [] and all("too few distinctive tokens" in d["reason"] for d in decisions)


def test_synthetic_a_hint_the_free_form_pass_already_produced_is_not_seeded_again(tmp_path, monkeypatch):
    root = _paper(tmp_path, [("b:0001", "Table", "| a | 1 |"), ("b:0002", "Text", "Cores were air-dried, sieved and weighed on a balance.")])
    free = EnumerationCandidate(candidate_id="soil_processing", description="Cores were air-dried, sieved and weighed on a balance.", anchors=["b:0002"])
    seeds, decisions = _seeds(monkeypatch, root, ["cores air-dried sieved weighed"], freeform=[free])
    assert seeds == [] and "already covered" in decisions[0]["reason"] and "soil_processing" in decisions[0]["reason"]


def test_synthetic_duplicate_and_repeated_hints_seed_once(tmp_path, monkeypatch):
    root = _paper(tmp_path, [("b:0001", "Table", "| a | 1 |"), ("b:0002", "Text", "Cores were air-dried, sieved and weighed on a balance.")])
    seeds, _ = _seeds(monkeypatch, root, ["cores air-dried sieved weighed", "Cores air-dried, sieved, weighed", "cores air-dried sieved weighed"])
    assert len(seeds) == 1


def test_real_daren_methods_prose_grounds_the_li_cor_hint_and_a_covered_hint_is_skipped(monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(FIXTURES))
    hint = "LI-COR LAI-2000 leaf area analyzer"
    tc = _tc(value_columns=[TableValueColumn(value_column_id="lai", variable_name_hint="LAI", method_hint=hint)], row_groups=[_row("r", {"lai": "3"})])
    monkeypatch.setattr(orchestrator, "run_table_classification_pass", lambda **k: {"b_0001": tc})
    seeds, decisions = orchestrator._method_hint_seeds(run_id="r", paper_id="Daren-1997-Canopy", model="m", invoke=None, freeform_candidates=[])
    [seed] = seeds
    assert seed.anchors == ["b:0035"] and decisions[0]["decision"] == "seeded"  # the real Methods block
    covered = EnumerationCandidate(candidate_id="li_cor", description="LI-COR LAI-2000 leaf area analyzer for LAI and MTA", anchors=["b:0035"])
    assert orchestrator._method_hint_seeds(run_id="r", paper_id="Daren-1997-Canopy", model="m", invoke=None, freeform_candidates=[covered])[0] == []


def test_the_pipeline_hands_seeded_methods_to_extraction_and_leaves_papers_without_tables_untouched(tmp_path, monkeypatch):
    root = _paper(tmp_path, [("b:0001", "Table", "| a | 1 |"), ("b:0002", "Text", "Cores were air-dried, sieved and weighed on a balance.")])
    monkeypatch.setenv("IR_PAPERS_ROOT", str(root))
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(orchestrator, "_resolve_known_refs", lambda entity_type, records: ({}, None))
    monkeypatch.setattr(orchestrator, "_multi_record_link_pools", lambda *a, **k: {})
    monkeypatch.setattr(orchestrator, "_apply_candidate_links", lambda paper_id, entity_type, this_run_records, known_refs, candidate: known_refs)
    handed = []
    monkeypatch.setattr(orchestrator, "run_record", lambda *, entity_type, record_id, **k: handed.append(record_id) or orchestrator.RecordResult(
        status="ready", entity_type=entity_type, record_id=record_id, detail={"payload": {}, "ai_validation": None}))
    free = EnumerationCandidate(candidate_id="other_method", description="Irrigation was applied weekly.", anchors=["b:0002"])
    monkeypatch.setattr(orchestrator, "run_enumeration", lambda **k: ([free], None))
    tc = _tc(value_columns=[TableValueColumn(value_column_id="v", variable_name_hint="v", method_hint="cores air-dried sieved weighed")], row_groups=[_row("r", {"v": "1"})])
    monkeypatch.setattr(orchestrator, "run_table_classification_pass", lambda **k: {"b_0001": tc})
    orchestrator._run_multi_record_entity(run_id="run1", paper_id="syn", entity_type="Method", model="m", client=None, invoke=None,
                                          enable_ai_validation=False, this_run_records={})
    assert sorted(handed) == ["syn_method_method_hint_cores_air_dried_sieved_weighed", "syn_method_other_method"]
    log = json.loads((run_store.record_dir("run1", "Method__enumeration") / "method_hint_seeds" / "attempt1.json").read_text())
    assert [d["decision"] for d in log["decisions"]] == ["seeded"]
    # no classified table -> no seeds, no log, free-form only
    handed.clear()
    monkeypatch.setattr(orchestrator, "run_table_classification_pass", lambda **k: {})
    orchestrator._run_multi_record_entity(run_id="run2", paper_id="syn", entity_type="Method", model="m", client=None, invoke=None,
                                          enable_ai_validation=False, this_run_records={})
    assert handed == ["syn_method_other_method"] and not (run_store.record_dir("run2", "Method__enumeration") / "method_hint_seeds").exists()


def test_the_step_b_prompt_asks_for_variables_and_no_placeholder_hints():
    prompt = orchestrator._table_classification_prompt("p", "b:0001", [])
    assert "variables, value_columns, row_groups" in prompt
    assert "- variables: one entry per measured variable" in prompt and "never invent a method" in prompt
    assert "leave variable_name_hint out of the value columns" in prompt and "set that column's `variable`" in prompt
