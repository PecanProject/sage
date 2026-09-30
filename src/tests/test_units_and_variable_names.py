"""Item 12: units and canonical variable name (plan section 10).

Evidence (real run 20260919T085532_98015a8a, Daren-1997-Canopy): ready Observations carried the units `kg DI`
(Marker's split of "kg DM m-2": the real table block b:0119 renders "kg DI | M m -2") and the literal placeholder
`unknown`; one variable appeared under five spellings ("SC", "Mean stage by count (SC)", "Mean Stage by Count (MSC)",
...); and the `units_hint` Step B produced never reached Extraction -- the candidate only had the variable name
inside its description text.

Item 12: the variable name and the units hint travel as sealed, UNVERIFIED context. The name follows the hint (a
naming judgment) and every spelling that resolves to one Variable record shares that record's name; units come from
the source text (datapackage: `reported_units` = "units as reported by the source") and the hint is used only where
it agrees. A hint the source text does not support is flagged and withheld -- never silently adopted, never silently
"corrected" (the garbled `kg DI` stays what the source says, and the model's `kg DM` is flagged). Placeholder units
and disagreements are flagged on the committed record, advisory only.

Fixtures: tests/fixtures/item12/ (real). Tests marked SYNTHETIC use invented text.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pipeline import orchestrator, run_store
from pipeline.raw_schema import TableClassification, TableFactor, TableRowGroup, TableValueColumn, TableVariable, UnitHintFlag
from pipeline.validators import _load_rendered_blocks
from test_orchestrator import RAW_EXTRACTION, env as ir_env, make_invoke_sequence, valid_citation_payload, _inv, PAPER_ID  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures" / "item12"


def _pool() -> list[dict]:
    return [{**v, "record_id": f"p_variable_{v['slug']}"} for v in json.loads((FIXTURES / "085532_variable_pool.json").read_text())]


def _labels() -> dict[str, list]:
    return {v["label"]: v["units_hints"] for v in json.loads((FIXTURES / "085532_column_variables.json").read_text())}


def _row(rid, cells, anchor="b:0119", **fv):
    return TableRowGroup(row_group_id=rid, factor_values=fv, source_table_anchor=anchor, cells=cells)


def _tc(columns, anchors=("b:0119",), **kw) -> TableClassification:
    rows = [_row("r", {c.value_column_id: "1.0" for c in columns}, anchor=anchors[0])]
    return TableClassification(table_role="treatment_response", table_anchors=list(anchors), value_columns=columns, row_groups=rows, **kw)


# --------------------------------------------------------------------- #
# units: comparison and support
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("a, b", [("g m -2 ", "g m⁻²"), ("g m-2", "g m −2"), ("Mg ha-1", "Mg ha -1"), ("kg DM m-2", "kg DM m⁻²")])
def test_units_spellings_that_differ_only_in_spacing_or_superscripts_compare_equal(a, b):
    assert orchestrator._units_key(a) == orchestrator._units_key(b)


@pytest.mark.parametrize("a, b", [("kg DM", "kg DI"), ("g m-2", "g m-1"), ("kg ha-1", "Mg ha-1"), ("%", "g")])
def test_different_units_never_compare_equal(a, b):
    assert orchestrator._units_key(a) != orchestrator._units_key(b)


def test_a_very_short_hint_needs_a_whole_token_not_a_stray_letter():
    assert orchestrator._units_supported("g", ["Shoot biomass (g m -2 )"]) is True
    assert orchestrator._units_supported("g", ["Plant density varied by location"]) is False  # 'g' only inside words
    assert orchestrator._units_supported("%", ["PAR intercepted (%)"]) is True
    assert orchestrator._units_supported("m", ["measurements were made"]) is False


def test_real_table_2_the_source_says_kg_DI_so_the_hint_kg_DM_is_not_supported(monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(FIXTURES))
    blocks = _load_rendered_blocks("Daren-1997-Canopy")
    assert "kg DI | M m -2" in blocks["b:0119"]                                   # what Marker rendered
    texts = [blocks[a] for a in ("b:0119", "b:0178", "b:0118", "b:0289")]
    assert orchestrator._units_supported("kg DI", texts) is True                  # what the source says: supported, uncorrected
    assert orchestrator._units_supported("kg DM", texts) is False                 # the model's reading: not the source's
    assert orchestrator._units_supported("kg DM m-2", texts) is False


def test_step_b_flags_and_withholds_a_units_hint_the_source_does_not_support(monkeypatch, tmp_path):
    papers = tmp_path / "papers"
    shutil.copytree(FIXTURES / "Daren-1997-Canopy", papers / "Daren-1997-Canopy")
    monkeypatch.setenv("IR_PAPERS_ROOT", str(papers))
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))
    payload = {
        "table_role": "treatment_response", "table_anchors": ["b:0119", "b:0178"],
        "variables": [{"label": "Leaf blade dry wt.", "variable_name": "leaf blade dry weight", "units": "kg DM"}],
        "value_columns": [
            {"value_column_id": "total", "variable_name_hint": "Total yield", "units_hint": "kg DM"},          # the model's reading
            {"value_column_id": "stem", "variable_name_hint": "Stem dry wt.", "units_hint": "kg DI"},          # the source's own text
            {"value_column_id": "blade", "variable": "Leaf blade dry wt."},
        ],
        "row_groups": [{"row_group_id": "r", "factor_values": {"k": "a"}, "source_table_anchor": "b:0119",
                        "cells": {"total": "0.19", "stem": "0.30", "blade": "0.4"}}],
        # a model can never assert its own hints are supported
        "unit_hint_flags": [],
    }
    invoke = lambda agent, model, prompt, timeout=300: orchestrator.AgentInvocation(  # noqa: E731
        agent=agent, model=model, prompt=prompt, returncode=0, stdout="{}", stderr="",
        final_text=json.dumps(payload), parsed_json=payload, parse_error=None)
    result, error = orchestrator.run_table_classification(run_id="r1", paper_id="Daren-1997-Canopy", seed_table_anchor="b:0119",
                                                          other_tables=[], model="m", invoke=invoke, chain_anchors=None)
    assert error is None
    assert sorted((f.scope, f.key, f.units_hint) for f in result.unit_hint_flags) == [
        ("column", "total", "kg DM"), ("variable", "Leaf blade dry wt.", "kg DM")]
    candidates = {c.candidate_id.split("_")[0]: c for c in orchestrator._table_classification_to_candidates(result, {})}
    assert candidates["total"].units_hint is None            # flagged -> withheld, and NOT replaced by 'kg DI'
    assert candidates["stem"].units_hint == "kg DI"          # supported by the source text (garbled as it is) -> carried, uncorrected
    assert candidates["blade"].units_hint is None            # the variable's own flagged units
    summary = orchestrator.summarize_table_pass("r1", "Daren-1997-Canopy", {})
    assert {(f["scope"], f["key"]) for f in summary["tables"][0]["unit_hint_flags"]} == {("column", "total"), ("variable", "Leaf blade dry wt.")}


def test_a_hint_flag_supplied_by_the_model_is_discarded_and_recomputed(monkeypatch, tmp_path):
    papers = tmp_path / "papers"
    shutil.copytree(FIXTURES / "Daren-1997-Canopy", papers / "Daren-1997-Canopy")
    monkeypatch.setenv("IR_PAPERS_ROOT", str(papers))
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))
    payload = {"table_role": "treatment_response", "table_anchors": ["b:0119"],
               "unit_hint_flags": [{"scope": "column", "key": "total", "units_hint": "kg DI", "reason": "made up"}],
               "value_columns": [{"value_column_id": "total", "variable_name_hint": "Total yield", "units_hint": "kg DI"}],
               "row_groups": [{"row_group_id": "r", "factor_values": {"k": "a"}, "source_table_anchor": "b:0119", "cells": {"total": "0.19"}}]}
    invoke = lambda agent, model, prompt, timeout=300: orchestrator.AgentInvocation(  # noqa: E731
        agent=agent, model=model, prompt=prompt, returncode=0, stdout="{}", stderr="",
        final_text=json.dumps(payload), parsed_json=payload, parse_error=None)
    result, _ = orchestrator.run_table_classification(run_id="r1", paper_id="Daren-1997-Canopy", seed_table_anchor="b:0119",
                                                      other_tables=[], model="m", invoke=invoke)
    assert result.unit_hint_flags == []  # 'kg DI' IS in the source text


def test_units_hints_only_reach_the_candidate_from_the_variable_first_then_the_column():
    tc = _tc([TableValueColumn(value_column_id="a", variable="Yield", units_hint="column units"),
              TableValueColumn(value_column_id="b", variable_name_hint="Other", units_hint="Mg ha-1")],
             variables=[TableVariable(label="Yield", units="g m-2")])
    by = {c.candidate_id.split("_")[0]: c for c in orchestrator._table_classification_to_candidates(tc, {})}
    assert (by["a"].units_hint, by["b"].units_hint) == ("g m-2", "Mg ha-1")


# --------------------------------------------------------------------- #
# canonical variable name: spellings collapse through the Variable record
# --------------------------------------------------------------------- #


def test_real_run_spellings_of_one_variable_share_the_variable_record_and_its_name():
    pool = _pool()
    resolved = {}
    for label in _labels():
        record = orchestrator._match_variable_pool(label, None, pool)
        if record:
            resolved.setdefault(record["slug"], []).append(label)
    # the four 'Tiller weight – <stage>' spellings are one variable ...
    assert sorted(resolved["tiller_weight"]) == sorted(l for l in _labels() if l.startswith("Tiller weight"))
    assert len(resolved["tiller_weight"]) == 4
    # ... and each other resolution is a single-record collapse of a case/wording variant
    assert {"leaf blade length", "Leaf blade length"} == set(resolved["leaf_blade_length"])
    assert {"stem internode length", "Stem internode length"} == set(resolved["stem_internode_length"])
    assert resolved["mean_stage_by_count"] == ["Mean stage by count (MSC)"]


def test_real_run_labels_with_no_variable_record_stay_unresolved_never_guessed():
    pool = _pool()
    unresolved = [l for l in _labels() if orchestrator._match_variable_pool(l, None, pool) is None]
    assert {"Total yield", "Leaf blade width", "leaf blade width", "Stem dry weight", "Precipitation"} <= set(unresolved)


def test_the_candidates_of_one_variable_carry_the_same_name_and_link():
    """Each spelling in its own column of a real-shaped table; the Variable pool is the run's real one."""
    columns = [TableValueColumn(value_column_id=f"c{i}", variable_name_hint=label) for i, label in enumerate(
        ["Tiller weight – Vegetative stage", "Tiller weight – Elongating stage", "Tiller weight – Reproductive stage", "Total yield"])]
    tc = _tc(columns)
    candidates = orchestrator._table_classification_to_candidates(tc, {"variable_id": _pool()})
    by = {c.candidate_id.split("_")[0]: c for c in candidates}
    tiller = [by["c0"], by["c1"], by["c2"]]
    assert {c.linked_candidates["variable_id"] for c in tiller} == {"tiller_weight"}
    assert {c.variable_name_hint for c in tiller} == {"Tüler weight"}    # ONE spelling: the paper's own Variable record's name
    assert "variable_id" not in by["c3"].linked_candidates and by["c3"].variable_name_hint == "Total yield"


def test_synthetic_a_label_and_a_canonical_name_that_disagree_are_refused():
    pool = [{"slug": "a", "name": "Root mass"}, {"slug": "b", "name": "Shoot mass"}]
    assert orchestrator._match_variable_pool("Root mass", "Shoot mass", pool) is None     # two different variables: no merge
    assert orchestrator._match_variable_pool("Root mass", None, pool)["slug"] == "a"
    assert orchestrator._match_variable_pool("Root mass (RM)", "root mass", pool)["slug"] == "a"   # agreeing spellings collapse


def test_synthetic_the_variable_link_rests_on_the_variables_own_label_not_other_row_values():
    """Before item 12 the generic matcher scored EVERY row value against the Variable pool, so a row factor level that
    happened to equal a Variable's name could link an unrelated measurement to it."""
    pool = [{"slug": "precip", "name": "Precipitation"}]
    tc = TableClassification(
        table_role="treatment_response", table_anchors=["b:0119"],
        factors=[TableFactor(name="Note", dimension="other", encoding="rows")],
        value_columns=[TableValueColumn(value_column_id="t", variable_name_hint="Temperature")],
        row_groups=[_row("r", {"t": "12"}, Note="Precipitation")],
    )
    [candidate] = orchestrator._table_classification_to_candidates(tc, {"variable_id": pool})
    assert "variable_id" not in candidate.linked_candidates


# --------------------------------------------------------------------- #
# prompts: the hints travel, marked unverified; units come from the source
# --------------------------------------------------------------------- #


def test_the_extraction_note_asks_for_the_sources_own_wording():
    note = orchestrator._hint_extraction_note({"variable_name_hint": "Tüler weight", "units_hint": "g"})
    assert "unverified hints" in note and "'Tüler weight'" in note and "'g'" in note
    assert "as the SOURCE writes them" in note and "report the source's" in note


def test_the_conversion_prompt_carries_the_naming_and_units_rules_only_with_hints():
    raw = {"paper_id": "p", "entity_type": "Observation", "record_id": "r", "facts": []}
    with_hints = orchestrator._conversion_prompt("p", "Observation", "r", raw, None, None, {"variable_name_hint": "leaf area index", "units_hint": "m2 m-2"})
    assert "use `variable_name_hint` exactly" in with_hints and "use `units_hint` only where it agrees" in with_hints
    assert "Never write a placeholder such as 'unknown'" in with_hints and "UNRESOLVED with a real reason" in with_hints
    assert "unit_basis_notes" in with_hints
    other_context = orchestrator._conversion_prompt("p", "Observation", "r", raw, None, None, {"aggregated_over_factors": ["x"]})
    assert "use `variable_name_hint` exactly" not in other_context and "Never write a placeholder" not in other_context
    assert orchestrator._conversion_prompt("p", "Observation", "r", raw, None) == orchestrator._conversion_prompt("p", "Observation", "r", raw, None, None, None)


def test_the_pipeline_hands_the_hints_to_extraction_and_conversion_context(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(FIXTURES))
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(orchestrator, "_resolve_known_refs", lambda entity_type, records: ({}, None))
    monkeypatch.setattr(orchestrator, "_multi_record_link_pools", lambda *a, **k: {})
    monkeypatch.setattr(orchestrator, "_apply_candidate_links", lambda paper_id, entity_type, this_run_records, known_refs, candidate: known_refs)
    tc = _tc([TableValueColumn(value_column_id="c", variable_name_hint="Total yield", units_hint="kg DI")])
    monkeypatch.setattr(orchestrator, "run_table_classification_pass", lambda **k: {"b_0119": tc})
    monkeypatch.setattr(orchestrator, "run_enumeration", lambda **k: ([], None))
    seen = []
    monkeypatch.setattr(orchestrator, "run_record", lambda *, entity_type, record_id, extraction_context=None, candidate_context=None, **k: seen.append(
        (extraction_context, candidate_context)) or orchestrator.RecordResult(status="ready", entity_type=entity_type, record_id=record_id,
                                                                            detail={"payload": {}, "ai_validation": None}))
    orchestrator._run_multi_record_entity(run_id="r", paper_id="Daren-1997-Canopy", entity_type="Observation", model="m", client=None,
                                          invoke=None, enable_ai_validation=False, this_run_records={})
    [(context, candidate_context)] = seen
    # The hints reach Conversion unchanged. Phase D adds the cell identity and the composed experimental context
    # (which carries the same header units, still unverified -- the value's units must match the source).
    assert {k: candidate_context[k] for k in ("variable_name_hint", "units_hint")} == {
        "variable_name_hint": "Total yield", "units_hint": "kg DI"}
    assert candidate_context["experimental_context"]["variable"]["header_units"] == "kg DI"
    assert set(candidate_context) == {"variable_name_hint", "units_hint", "cell", "experimental_context"}
    assert "unverified hints" in context and "'kg DI'" in context


# --------------------------------------------------------------------- #
# record-level advisory flags
# --------------------------------------------------------------------- #


def _payload(units="g m-2", name="Shoot biomass", value_label="EXTRACTED"):
    value = {"value": {"reported_text": "70", "reported_units": units}, "provenance_label": value_label} if value_label != "UNRESOLVED" \
        else {"value": None, "provenance_label": "UNRESOLVED", "unresolved_reason": "no units stated"}
    return {"value": value, "variable_name": {"value": name, "provenance_label": "EXTRACTED"}}


def test_a_placeholder_units_string_is_flagged():
    for placeholder in ("unknown", "N/A", "not reported", "-"):
        flags = orchestrator._hint_consistency_flags(_payload(units=placeholder), None)
        assert [f["flag"] for f in flags] == ["units_placeholder"] and flags[0]["reported_units"] == placeholder


def test_units_that_differ_from_the_hint_are_flagged_and_equivalent_spellings_are_not():
    hints = {"units_hint": "kg DM m-2"}
    [flag] = orchestrator._hint_consistency_flags(_payload(units="kg DI"), hints)
    assert flag == {"flag": "units_differ_from_hint", "reported_units": "kg DI", "units_hint": "kg DM m-2"}
    assert orchestrator._hint_consistency_flags(_payload(units="kg DM m⁻²"), hints) == []
    assert orchestrator._hint_consistency_flags(_payload(units="kg DM m -2 "), hints) == []


def test_a_variable_name_other_than_the_canonical_one_is_flagged_case_and_punctuation_aside():
    hints = {"variable_name_hint": "Mean stage by count (MSC)"}
    assert orchestrator._hint_consistency_flags(_payload(name="mean stage by count msc"), hints) == []
    [flag] = orchestrator._hint_consistency_flags(_payload(name="SC"), hints)
    assert flag["flag"] == "variable_name_differs_from_hint" and flag["variable_name"] == "SC"


def test_unresolved_or_absent_fields_raise_no_flag_and_the_payload_is_never_edited():
    unresolved = _payload(value_label="UNRESOLVED")
    assert orchestrator._hint_consistency_flags(unresolved, {"units_hint": "g", "variable_name_hint": "x"}) == [
        {"flag": "variable_name_differs_from_hint", "variable_name": "Shoot biomass", "variable_name_hint": "x"}]
    payload = _payload(units="unknown")
    before = json.dumps(payload, sort_keys=True)
    orchestrator._hint_consistency_flags(payload, {"units_hint": "g m-2"})
    assert json.dumps(payload, sort_keys=True) == before
    assert orchestrator._hint_consistency_flags({}, {"units_hint": "g"}) == []


def test_the_committed_record_carries_its_hint_flags(ir_env, monkeypatch):
    """run_record stores advisory flags next to the payload without changing it (the record is a Citation here only to
    exercise the commit path; the flag function is stubbed)."""
    def run(flags):
        monkeypatch.setattr(orchestrator, "_hint_consistency_flags", lambda payload, ctx: flags)
        invoke = make_invoke_sequence([("extractor", _inv("extractor", RAW_EXTRACTION)), ("converter", _inv("converter", valid_citation_payload())),
                                       ("ir-validator", _inv("ir-validator", {"verdict": "plausible", "issues": []}))])
        return orchestrator.run_record(run_id="run1" if flags else "run2", paper_id=PAPER_ID, entity_type="Citation", record_id=PAPER_ID,
                                       model="m", client=ir_env["client"], invoke=invoke, enable_ai_validation=True,
                                       candidate_context={"units_hint": "g"})
    flagged = run([{"flag": "units_placeholder", "reported_units": "unknown"}])
    assert flagged.status == "ready" and flagged.detail["hint_flags"] == [{"flag": "units_placeholder", "reported_units": "unknown"}]
    assert "hint_flags" not in run([]).detail


def test_synthetic_a_units_hint_stated_only_in_the_caption_or_a_note_is_supported(tmp_path, monkeypatch):
    """The units are often in the caption ('... (Mg ha-1)') or a footnote, not in the table body."""
    root = tmp_path / "papers"
    (root / "syn").mkdir(parents=True)
    blocks = [("b:0001", "Caption", "*Table 1. Grain yield (Mg ha-1) by tillage.*"), ("b:0002", "Table", "| Tillage | Yield | Biomass |\n|---|---|---|\n| till | 4.1 | 9 |"),
              ("b:0003", "Footnote", "[^footnote] a Biomass in g m -2 ."), ("b:0004", "Text", "Body text with kg ha-1 inside a later paragraph."),
              ("b:0005", "Text", "More text.")]
    (root / "syn" / "content.md").write_text("".join(f"{t}\n⟦{a}⟧\n\n" for a, _, t in blocks), encoding="utf-8")
    (root / "syn" / "provenance.json").write_text(json.dumps({a: {"block_type": b, "page_id": "page_1", "section_path": [], "rendered_in_content_md": True} for a, b, _ in blocks}))
    monkeypatch.setenv("IR_PAPERS_ROOT", str(root))
    tc = _tc([TableValueColumn(value_column_id="y", variable_name_hint="Yield", units_hint="Mg ha-1"),
              TableValueColumn(value_column_id="b", variable_name_hint="Biomass", units_hint="g m-2"),
              TableValueColumn(value_column_id="z", variable_name_hint="Other", units_hint="kg ha-1")], anchors=("b:0002",))
    flags = orchestrator._unit_hint_flags(tc, _load_rendered_blocks("syn"), "syn")
    # caption and footnote support their hints; an unrelated later paragraph (outside the eligible window) does not
    assert [(f.scope, f.key) for f in flags] == [("column", "z")]
