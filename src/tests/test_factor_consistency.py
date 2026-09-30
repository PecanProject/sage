"""A table's factor structure is repaired, not regenerated, and checked against the paper's other tables before
any Treatment or Observation is derived from it.

Real failure (Philippe-2007-Six, run 20260926T162813_56de01a6): Table 3 (b:0193) was classified CORRECTLY on the first
attempt (PARt class = treatment, Year = time, P-value rows = "Statistic") but rejected because one value column named
its variable by `variable_name` ("leaf nitrogen concentration per unit area") instead of its label ("Na (g m-2)"). The
retry prompt did not include the previous answer, so the model re-classified from scratch into ONE factor "Condition"
(dimension treatment) holding PAR classes, years and P-values. Trusted as is: "Year 2004"/"Year 2006" became ready
Treatments, the PAR classes were minted twice ("PARt 0-0.1" vs Table 1's "0-0.1"), the PAR rows were labelled
treatment means although pooled over Year, and STAR sky (Table 2) was "aggregated over [Year, Statistic]".

Fixture (real): fixtures/factor_consistency/ -- the run's accepted classifications of b:0053, b:0115, b:0193 and the
rejected first attempt of b:0193 with its validation error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import design, orchestrator
from pipeline.raw_schema import TableClassification, TableFactor, TableRowGroup, TableValueColumn, TableVariable
from test_document_map import _write_paper

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "factor_consistency"
                      / "philippe_table_pass_20260926T162813_56de01a6.json").read_text(encoding="utf-8"))
PAPERS = Path(__file__).resolve().parents[1] / "paper"


def _accepted(key: str) -> TableClassification:
    return TableClassification.model_validate(FIXTURE["accepted"][key])


def _attempt1_repaired() -> TableClassification:
    repaired, repairs = orchestrator._repair_table_classification(json.loads(json.dumps(FIXTURE["b_0193_attempt1"])))
    assert repairs
    return TableClassification.model_validate(repaired)


def _real(table3: TableClassification) -> dict[str, TableClassification]:
    return {"b_0053": _accepted("b_0053"), "b_0115": _accepted("b_0115"), "b_0193": table3}


# --------------------------------------------------------------------------- #
# repair, not regenerate
# --------------------------------------------------------------------------- #

def test_the_real_first_attempt_is_repaired_without_a_model_call():
    raw = json.loads(json.dumps(FIXTURE["b_0193_attempt1"]))
    assert "which is not declared in variables" in FIXTURE["b_0193_attempt1_errors"][0]["message"]
    repaired, repairs = orchestrator._repair_table_classification(raw)
    assert repairs[0]["from"] == "leaf nitrogen concentration per unit area" and repairs[0]["to"] == "Na (g m–2)"
    assert len(repairs) == 6                                      # every column had the same slip; the error showed one
    table = TableClassification.model_validate(repaired)
    assert orchestrator._factor_structure(table) == [("PARt class", "treatment"), ("Statistic", "other"), ("Year", "time")]


def test_a_name_shared_by_two_variables_is_never_repaired():
    answer = {"variables": [{"label": "A (g)", "variable_name": "mass"}, {"label": "B (g)", "variable_name": "mass"}],
              "value_columns": [{"value_column_id": "c", "variable": "mass"}]}
    assert orchestrator._repair_table_classification(answer) == (answer, [])


def _table_paper(tmp_path, monkeypatch):
    """Philippe Table 3 as its own content.md block, so the anchor and reconstruction checks run for real."""
    content = (PAPERS / "Philippe-2007-Six" / "content.md")
    if not content.is_file():
        pytest.skip("Philippe-2007-Six is not prepared on this machine")
    text = content.read_text(encoding="utf-8")
    start = text.index("⟦b:0192⟧") + len("⟦b:0192⟧")
    table = text[start:text.index("⟦b:0193⟧")].strip()
    _write_paper(tmp_path / "papers", "Philippe-2007-Six", [
        ("b:0191", "Caption", "Table 3. Leaf traits by PARt class and year.", 0), ("b:0193", "Table", table, 0)])
    monkeypatch.setenv("IR_PAPERS_ROOT", str(tmp_path / "papers"))
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))


def _invoke(answers):
    prompts: list[str] = []

    def invoke(agent, model, prompt, timeout=300):
        answer = answers[min(len(prompts), len(answers) - 1)]
        prompts.append(prompt)
        return orchestrator.AgentInvocation(agent=agent, model=model, prompt=prompt, returncode=0, stdout="", stderr="",
                                            final_text=json.dumps(answer), parsed_json=answer, parse_error=None)
    return invoke, prompts


def test_step_b_accepts_the_real_first_attempt_after_repair(tmp_path, monkeypatch):
    _table_paper(tmp_path, monkeypatch)
    invoke, prompts = _invoke([json.loads(json.dumps(FIXTURE["b_0193_attempt1"]))])
    table, error = orchestrator.run_table_classification(
        run_id="r", paper_id="Philippe-2007-Six", seed_table_anchor="b:0193", other_tables=[], model="m", invoke=invoke)
    assert error is None and len(prompts) == 1
    assert {f.name: f.dimension for f in table.factors} == {"PARt class": "treatment", "Year": "time", "Statistic": "other"}
    final = json.loads((tmp_path / "runs" / "r" / "records" / "table_classification__b_0193" / "final.json").read_text())
    assert final["deterministic_repairs"][0]["to"] == "Na (g m–2)"


def test_a_retry_is_shown_its_previous_answer_and_a_changed_structure_is_flagged(tmp_path, monkeypatch):
    _table_paper(tmp_path, monkeypatch)
    broken = json.loads(json.dumps(FIXTURE["b_0193_attempt1"]))
    broken["aggregation_scope"] = "per_cell"                      # a slip no deterministic repair touches
    collapsed = FIXTURE["accepted"]["b_0193"]                     # what the real retry returned
    invoke, prompts = _invoke([broken, collapsed])
    table, error = orchestrator.run_table_classification(
        run_id="r", paper_id="Philippe-2007-Six", seed_table_anchor="b:0193", other_tables=[], model="m", invoke=invoke)
    assert error is None and len(prompts) == 2
    assert "PREVIOUS ANSWER:" in prompts[1] and '"PARt class"' in prompts[1] and "aggregation_scope" in prompts[1]
    final = json.loads((tmp_path / "runs" / "r" / "records" / "table_classification__b_0193" / "final.json").read_text())
    change = final["structure_changed_on_retry"]
    assert change["before"] == [["PARt class", "treatment"], ["Statistic", "other"], ["Year", "time"]]
    assert change["after"] == [["Condition", "treatment"]]


def test_a_first_prompt_never_carries_a_previous_answer():
    assert "PREVIOUS ANSWER" not in orchestrator._table_classification_prompt("p", "b:0001", [], prior_answer={"x": 1})


# --------------------------------------------------------------------------- #
# cross-table factor consistency
# --------------------------------------------------------------------------- #

def test_the_collapsed_table_is_withheld_as_a_mixed_factor_citing_the_other_tables():
    result = design.factor_consistency(_real(_accepted("b_0193")))
    finding = result.withheld["b_0193"]
    assert finding["kind"] == "mixed_factor" and finding["factor"] == "Condition"
    assert set(finding["non_treatment_levels"]) == {"Year 2001", "Year 2002", "Year 2003", "Year 2004", "Year 2006"}
    assert "b_0053, b_0115" in finding["non_treatment_levels"]["Year 2004"]["evidence"]
    assert set(result.withheld) == {"b_0193"}                     # Tables 1-2 are consistent


def test_the_real_first_attempt_is_consistent_and_yields_the_three_real_treatments():
    kept, consistency = orchestrator._apply_factor_consistency(_real(_attempt1_repaired()))
    assert not consistency.withheld
    candidates, _ = orchestrator._table_classifications_to_treatment_candidates(list(kept.values()), {})
    assert len(candidates) == 3                                   # Table 1's "0-0.1" == Table 3's "0–0.1"


def test_no_year_treatment_and_no_duplicate_from_the_collapsed_table():
    kept, _ = orchestrator._apply_factor_consistency(_real(_accepted("b_0193")))
    assert set(kept) == {"b_0053", "b_0115"}
    candidates, _ = orchestrator._table_classifications_to_treatment_candidates(list(kept.values()), {})
    assert len(candidates) == 3 and not any("2004" in c.description for c in candidates)


def _one_factor_table(levels, name="Light", dimension="treatment", anchor="b:0100"):
    return TableClassification(
        applicable=True, table_role="treatment_response", table_anchors=[anchor],
        factors=[TableFactor(name=name, dimension=dimension, encoding="rows")],
        variables=[TableVariable(label="Y (g)")], value_columns=[TableValueColumn(value_column_id="y", variable="Y (g)")],
        row_groups=[TableRowGroup(row_group_id=f"r{i}", factor_values={name: lv}, source_table_anchor=anchor,
                                  cells={"y": str(i + 1)}) for i, lv in enumerate(levels)])


def test_the_same_treatment_factor_spelled_by_another_table_is_one_treatment():
    tables = {"t1": _one_factor_table(["0-0.1", "0.1-0.2"], name="PAR t"),
              "t3": _one_factor_table(["PARt 0–0.1", "PARt 0.1–0.2"], name="Condition", anchor="b:0200")}
    kept, consistency = orchestrator._apply_factor_consistency(tables)
    assert consistency.level_aliases["t3"]["Condition"] == {"PARt 0–0.1": "0-0.1", "PARt 0.1–0.2": "0.1-0.2"}
    candidates, _ = orchestrator._table_classifications_to_treatment_candidates(list(kept.values()), {})
    assert len(candidates) == 2


def test_a_calendar_year_is_never_a_treatment_level_even_with_no_other_table():
    result = design.factor_consistency({"t": _one_factor_table(["Year 2004", "Year 2006"])})
    assert result.withheld["t"]["kind"] == "dimension_conflict"


@pytest.mark.parametrize("levels", [["Nitrate", "Ammonium"], ["N100 kg", "control"], ["2004 cohort", "2006 cohort"]])
def test_a_factor_name_or_year_inside_a_real_level_is_not_a_reading(levels):
    tables = {"n": _one_factor_table(["0", "100"], name="N"), "t": _one_factor_table(levels, name="Form", anchor="b:0200"),
              "y": _one_factor_table(["2004", "2006"], name="Year", dimension="time", anchor="b:0300")}
    result = design.factor_consistency(tables)
    assert "t" not in result.withheld and "t" not in result.level_aliases


def test_a_withheld_tables_anchors_are_accounted_for_so_free_form_does_not_re_derive_it(monkeypatch):
    tables = {"table_classification__b_0053": _accepted("b_0053"), "table_classification__b_0193": _accepted("b_0193")}
    monkeypatch.setattr(orchestrator, "run_table_classification_pass", lambda **kw: dict(tables))
    monkeypatch.setattr(orchestrator.run_store, "save_stage_attempt", lambda *a, **k: None)
    candidates, covered = orchestrator.run_table_enumeration(run_id="r", paper_id="p", entity_type="Treatment", model="m")
    assert "b:0193" in covered and len(candidates) == 3


# --------------------------------------------------------------------------- #
# statistic rows are not design factors
# --------------------------------------------------------------------------- #

def test_star_sky_is_pooled_over_year_only_never_over_the_statistic_rows():
    table2 = _accepted("b_0115")
    row = next(r for r in table2.row_groups if any("0.1" in v and "0.2" in v for v in r.factor_values.values()))
    column = next(c.value_column_id for c in table2.value_columns if "star" in c.value_column_id.lower())
    pooling = design.cell_pooling(table2, row, column)
    assert pooling.pooled_over == ["Year"]
    summary = design.design_summary({"b_0115": table2}, _EmptyIndex())
    assert "Statistic" not in summary["factors"]


class _EmptyIndex:
    class dmap:
        blocks: list = []
    signals: dict = {}


# --------------------------------------------------------------------------- #
# the Treatment validator sees the design
# --------------------------------------------------------------------------- #

def test_the_treatment_validator_is_told_year_is_time(monkeypatch):
    tables = {"table_classification__b_0053": _accepted("b_0053")}
    monkeypatch.setattr(orchestrator, "_cached_table_classifications", lambda run_id: tables)
    context = orchestrator._treatment_design_context("r")
    year = next(f for f in context["factors"] if f["name"] == "Year")
    assert year["dimensions"] == ["time"] and "2004" in year["levels"]
    prompt = orchestrator._ai_validation_prompt("Treatment", {"name": "Year 2004"}, {}, {}, context)
    assert "DESIGN_CONTEXT" in prompt and "never a Treatment" in prompt
    assert "DESIGN_CONTEXT" not in orchestrator._ai_validation_prompt("Observation", {}, {}, {})
