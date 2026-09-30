"""Step B: undeclared row-encoded variable levels are a recoverable quality failure.

Real evidence (Felipe-2010-Cultivar Table 1). The identical 20,176-character Step B prompt, Methods text included, was
answered with all six variables declared in three runs and with `variables=[]` in a fourth (run
felipe_methodfix3_20260921T025636: one tool call, 1,164 output tokens against 1,450-1,620). All 22 cells were still
reconstructed correctly, so nothing rejected it -- yet a variable named by ROW labels can carry its units, canonical
name and method hint only in `variables`, so every Table 1 candidate lost its method hint and all 22 were unresolved.

Now: a `variable`-dimension rows factor whose levels are not all declared is rejected with feedback (a numbered
attempt: it is the model's own omission), and on the last attempt -- when nothing else is wrong -- accepted and flagged.
Only a label is needed to declare a variable; method hints, units and names stay optional and are never demanded.

Fixtures (real): tests/fixtures/item8/live_gpt_oss_felipe_table1_classification.json (an earlier live answer with
`variables=[]` and a variable rows factor) and tests/fixtures/pass2 (trimmed Felipe content; the complete Table 1 answer).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import orchestrator, run_store
from pipeline.raw_schema import TableClassification, TableFactor, TableRowGroup, TableValueColumn, TableVariable

TESTS = Path(__file__).parent / "fixtures"
PASS2 = TESTS / "pass2"
PAPER = "Felipe-2010-Cultivar"


def _real(name: str, folder: Path = PASS2) -> dict:
    return json.loads((folder / name).read_text())


def _tc(**kw) -> TableClassification:
    kwargs = dict(table_role="treatment_response", table_anchors=["b:0001"])
    kwargs.update(kw)
    return TableClassification(**kwargs)


def _row(rid: str, **factor_values: str) -> TableRowGroup:
    return TableRowGroup(row_group_id=rid, factor_values=factor_values, source_table_anchor="b:0001", cells={"v": "1"})


VARIABLE_ROWS = [TableFactor(name="Variable", dimension="variable", encoding="rows")]
COLUMN = [TableValueColumn(value_column_id="v")]


# --------------------------------------------------------------------- #
# the detector
# --------------------------------------------------------------------- #


def test_the_real_variables_empty_answer_leaves_all_six_levels_undeclared():
    tc = TableClassification.model_validate(_real("live_gpt_oss_felipe_table1_classification.json", TESTS / "item8"))
    assert tc.variables == [] and any(f.dimension == "variable" and f.encoding == "rows" for f in tc.factors)
    assert orchestrator._undeclared_variable_levels(tc) == [
        "PAR intercepted (%)", "Shoot biomass (g m -2 )", "Total fruit (g m -2 )",
        "Harvestable fruit (g m -2 )", "Aboveground N (g N m -2 )", "Harvest index"]


def test_the_real_complete_answer_has_nothing_undeclared():
    tc = TableClassification.model_validate(_real("felipe_table1_classification.json"))
    assert len(tc.variables) == 6 and orchestrator._undeclared_variable_levels(tc) == []


def test_a_partial_declaration_names_only_the_missing_levels_once_in_row_order():
    tc = _tc(factors=VARIABLE_ROWS, value_columns=COLUMN,
             variables=[TableVariable(label="Shoot biomass")],
             row_groups=[_row("a", Variable="Shoot biomass"), _row("b", Variable="Harvest index"),
                         _row("c", Variable="Shoot biomass"), _row("d", Variable="Harvest index"), _row("e", Variable="LAI")])
    assert orchestrator._undeclared_variable_levels(tc) == ["Harvest index", "LAI"]


def test_a_label_alone_declares_a_variable_and_case_and_punctuation_do_not_matter():
    tc = _tc(factors=VARIABLE_ROWS, value_columns=COLUMN,
             variables=[TableVariable(label="par INTERCEPTED"), TableVariable(label="Aboveground N g N m-2")],
             row_groups=[_row("a", Variable="PAR intercepted (%)"), _row("b", Variable="Aboveground N (g N m -2 )")])
    assert orchestrator._undeclared_variable_levels(tc) == []


def test_blank_levels_are_ignored():
    tc = _tc(factors=VARIABLE_ROWS, value_columns=COLUMN, variables=[TableVariable(label="LAI")],
             row_groups=[_row("a", Variable="LAI"), _row("b", Variable="  "), _row("c")])
    assert orchestrator._undeclared_variable_levels(tc) == []


def test_a_table_whose_variables_are_named_by_column_headers_is_never_affected():
    """The legitimate `variables=[]` shape: no variable rows factor, each column carries its own name (the Daren shape)."""
    tc = _tc(factors=[TableFactor(name="Population", dimension="crop", encoding="rows")],
             value_columns=[TableValueColumn(value_column_id="v", variable_name_hint="total yield")],
             row_groups=[_row("a", Population="Trailblazer")])
    assert tc.variables == [] and orchestrator._undeclared_variable_levels(tc) == []
    legacy = _tc(value_columns=[TableValueColumn(value_column_id="v", variable_name_hint="LAI")], row_groups=[_row("a", Population="X")])
    assert orchestrator._undeclared_variable_levels(legacy) == []


def test_tables_that_feed_no_candidates_are_never_affected():
    undeclared = dict(factors=VARIABLE_ROWS, value_columns=COLUMN, row_groups=[_row("a", Variable="LAI")])
    assert orchestrator._undeclared_variable_levels(_tc(**undeclared)) == ["LAI"]
    assert orchestrator._undeclared_variable_levels(_tc(table_role="aggregated_summary", reason="pooled", **undeclared)) == []
    assert orchestrator._undeclared_variable_levels(_tc(table_role="non_enumerable", reason="an ANOVA table", **undeclared)) == []


def test_a_method_hint_is_never_required_to_declare_a_variable():
    """Only the label is demanded: a declared variable without a hint stays without one (nothing is invented)."""
    tc = _tc(factors=VARIABLE_ROWS, value_columns=COLUMN, variables=[TableVariable(label="LAI")], row_groups=[_row("a", Variable="LAI")])
    assert orchestrator._undeclared_variable_levels(tc) == [] and tc.variables[0].method_hint is None


# --------------------------------------------------------------------- #
# the Step B loop: retry, then accept-and-flag
# --------------------------------------------------------------------- #


@pytest.fixture()
def step_b(monkeypatch, tmp_path):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(PASS2))
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(orchestrator.time, "sleep", lambda seconds: None)
    orchestrator._PROVIDER_FAILURE_LOG.clear()   # process-global: never let another test's rounds leak in


def _answer(declare: bool) -> dict:
    data = _real("felipe_table1_classification.json")
    if not declare:
        data["variables"] = []
    return data


def _run(answers: list[dict], run_id: str = "r1"):
    prompts: list[str] = []
    queue = list(answers)

    def invoke(agent, model, prompt, timeout=300):
        prompts.append(prompt)
        data = queue.pop(0)
        return orchestrator.AgentInvocation(agent=agent, model=model, prompt=prompt, returncode=0, stdout="", stderr="",
                                            final_text=json.dumps(data), parsed_json=data)

    result = orchestrator.run_table_classification(
        run_id=run_id, paper_id=PAPER, seed_table_anchor="b:0069", other_tables=[], model="m", invoke=invoke)
    return result, prompts


def _final(run_id: str = "r1") -> dict:
    return run_store.load_json(run_store.record_dir(run_id, "table_classification__b_0069") / "final.json")


def test_an_empty_variables_answer_is_retried_with_feedback_and_the_complete_one_is_accepted(step_b):
    (classification, error), prompts = _run([_answer(False), _answer(True)])
    assert error is None and len(prompts) == 2
    assert len(classification.variables) == 6
    assert "variable_declaration_flags" not in _final()                       # recovered: nothing to disclose
    # the retry prompt names the undeclared levels and asks for labels only, never for invented hints
    assert "no `variables` entry declares" in prompts[1] and "Harvest index" in prompts[1]
    assert "never invent one" in prompts[1] and "no `variables` entry declares" not in prompts[0]
    first = run_store.load_json(run_store.record_dir("r1", "table_classification__b_0069") / "table_classification" / "attempt1.json")
    assert first["failure_class"] == "validation_failure" and first["failure_kind"] == "extraction" and first["numbered_attempt"] == 1


def test_a_complete_first_answer_is_accepted_at_once(step_b):
    (classification, error), prompts = _run([_answer(True)])
    assert error is None and len(prompts) == 1 and len(classification.variables) == 6


def test_the_retry_consumes_a_numbered_attempt_not_the_provider_budget(step_b):
    _run([_answer(False), _answer(True)])
    attempts = [run_store.load_json(p) for p in sorted((run_store.record_dir("r1", "table_classification__b_0069") / "table_classification").glob("attempt*.json"))]
    assert [a["numbered_attempt"] for a in attempts] == [1, 2]
    assert orchestrator.summarize_provider_failures("r1")["total_rounds"] == 0


def test_when_every_attempt_leaves_variables_undeclared_the_last_is_accepted_and_flagged(step_b):
    n = orchestrator.MAX_TABLE_CLASSIFICATION_ATTEMPTS
    (classification, error), prompts = _run([_answer(False)] * n)
    assert error is None and len(prompts) == n
    assert classification is not None and classification.variables == [] and len(classification.row_groups) == 11   # the table survives
    flags = _final()["variable_declaration_flags"]
    assert len(flags) == 1 and len(flags[0]["undeclared_levels"]) == 6 and f"after {n} attempts" in flags[0]["reason"]
    last = run_store.load_json(run_store.record_dir("r1", "table_classification__b_0069") / "table_classification" / f"attempt{n}.json")
    assert last["failure_class"] is None and last["variable_declaration_flags"] == flags


def test_the_flag_is_disclosed_in_the_run_summary_and_absent_when_nothing_was_flagged(step_b):
    n = orchestrator.MAX_TABLE_CLASSIFICATION_ATTEMPTS
    _run([_answer(False)] * n, run_id="flagged")
    _run([_answer(True)], run_id="clean")
    flagged = orchestrator.summarize_table_pass("flagged", PAPER, {})["tables"][0]
    clean = orchestrator.summarize_table_pass("clean", PAPER, {})["tables"][0]
    assert len(flagged["variable_declaration_flags"]) == 1
    assert len(flagged["variable_declaration_flags"][0]["undeclared_levels"]) == 6
    assert clean["variable_declaration_flags"] == []


def test_the_accepted_classification_is_cached_so_a_second_pass_costs_no_call(step_b):
    n = orchestrator.MAX_TABLE_CLASSIFICATION_ATTEMPTS
    _run([_answer(False)] * n)
    (classification, error), prompts = _run([], run_id="r1")
    assert error is None and prompts == [] and classification.variables == []


def test_other_validation_failures_are_unchanged_and_take_precedence(step_b):
    """A classification with a different defect is still rejected for that defect, never accepted-and-flagged."""
    broken = _answer(False)
    broken["table_anchors"] = ["b:9999"]   # an anchor that does not exist (rows cite it too, so the shape stays valid)
    for row in broken["row_groups"]:
        row["source_table_anchor"] = "b:9999"
    (classification, error), prompts = _run([broken] * orchestrator.MAX_TABLE_CLASSIFICATION_ATTEMPTS)
    assert classification is None and "invalid anchors" in error and "undeclared variable" not in error
