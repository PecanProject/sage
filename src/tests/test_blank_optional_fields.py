"""Step B: a blank optional field (`units: ""`) means "not provided", never a reason to reject the whole table.

Real evidence (Smulker-2012-Assessment, run 20260924T063634_43cf5bc7): for a unitless variable -- "C:N ratio" in table
b:0043, "pH" in table b:0239 -- the model wrote `units: ""` (and `units_hint: ""`). `TableVariable.units` has
`min_length=1`, so the classification was rejected at the field check -- which also hid a second, separate defect (see
"the two real answers" below) -- and both tables were given up on. Now a blank or whitespace-only OPTIONAL free-text
field is None. Required fields keep `min_length=1`, a non-empty value is validated exactly as before, and no units are
ever supplied.

Fixtures (real): tests/fixtures/blank_units/ -- the two rejected answers exactly as the model sent them, and the trimmed
Smulker blocks and provenance they cite.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline import orchestrator
from pipeline.raw_schema import TableClassification, TableValueColumn, TableVariable, TimeLevel

FIXTURES = Path(__file__).parent / "fixtures" / "blank_units"
PAPER = "Smulker-2012-Assessment"
ANSWERS = json.loads((FIXTURES / "smulker_step_b_blank_units_answers_20260924T063634_43cf5bc7.json").read_text())


def _answer(key: str) -> dict:
    return json.loads(json.dumps(ANSWERS[key]))


@pytest.fixture()
def paper(tmp_path, monkeypatch):
    shutil.copytree(FIXTURES / PAPER, tmp_path / "papers" / PAPER)
    monkeypatch.setenv("IR_PAPERS_ROOT", str(tmp_path / "papers"))
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))
    return tmp_path


# --------------------------------------------------------------------- #
# the two real answers
# --------------------------------------------------------------------- #
# Finding while writing these tests: the blank units were NOT the answers' only defect. Pydantic runs field checks before
# the model-level checks, so `units: ""` hid a second, independent error: the model pointed value columns at each
# variable's `variable_name` ("EC", "no3_n") instead of its `label` ("EC (µS cm–1)", "NO₃‑N"). That is a separate Step B
# issue, now repaired deterministically before any retry.


def _declared_labels(raw: dict) -> dict:
    """The derived copy used to isolate the units rule: every column -> variable reference pointed at the declared LABEL
    of the variable it names (by `variable_name`). Nothing else changes -- units included."""
    by_name = {v["variable_name"]: v["label"] for v in raw["variables"]}
    for column in raw["value_columns"]:
        column["variable"] = by_name.get(column.get("variable"), column.get("variable"))
    return raw


@pytest.mark.parametrize("key, unitless", [("b_0043", "C:N ratio"), ("b_0239", "pH")])
def test_the_real_answer_no_longer_fails_on_the_blank_units_only_on_its_other_defect(key, unitless):
    raw = _answer(key)
    assert next(v for v in raw["variables"] if v["label"] == unitless)["units"] == ""        # what the model sent
    with pytest.raises(ValidationError) as caught:
        TableClassification.model_validate(raw)
    message = str(caught.value)
    assert "at least 1 character" not in message                                             # the units error is gone
    assert "which is not declared in variables" in message                                   # the separate, remaining one


@pytest.mark.parametrize("key, unitless", [("b_0043", "C:N ratio"), ("b_0239", "pH")])
def test_blank_units_alone_no_longer_block_the_table(key, unitless):
    classification = TableClassification.model_validate(_declared_labels(_answer(key)))
    variable = next(v for v in classification.variables if v.label == unitless)
    assert variable.units is None                                                            # not provided -- never invented
    others = [v for v in classification.variables if v.label != unitless]
    assert others and all(v.units for v in others)                                           # real units untouched


def _step_b(answer: dict):
    prompts: list[str] = []

    def invoke(agent, model, prompt, timeout=300):
        prompts.append(prompt)
        return orchestrator.AgentInvocation(agent=agent, model=model, prompt=prompt, returncode=0, stdout="", stderr="",
                                            final_text=json.dumps(answer), parsed_json=answer, parse_error=None)
    return invoke, prompts


@pytest.mark.parametrize("key", ["b_0043", "b_0239"])
def test_step_b_repairs_the_variable_name_slip_deterministically_and_accepts_the_real_answer(paper, key):
    """The separate defect this file found: a column naming its variable by `variable_name` ("EC") instead of
    the declared `label` ("EC (µS cm–1)") is repaired without a model call -- exactly one declared variable carries
    that name -- and recorded. Before, the table spent every attempt re-classifying from scratch on it."""
    invoke, prompts = _step_b(_answer(key))
    classification, error = orchestrator.run_table_classification(
        run_id="r1", paper_id=PAPER, seed_table_anchor=key.replace("_", ":"), other_tables=[], model="m", invoke=invoke)
    assert error is None and classification is not None and len(prompts) == 1
    from pipeline.raw_schema import _variable_key
    labels = {_variable_key(v.label) for v in classification.variables}
    assert all(_variable_key(c.variable) in labels for c in classification.value_columns if c.variable)
    final = json.loads((paper / "runs" / "r1" / "records" / f"table_classification__{key}" / "final.json").read_text())
    assert final["deterministic_repairs"] and all(r["field"] == "variable" for r in final["deterministic_repairs"])


@pytest.mark.parametrize("key", ["b_0043", "b_0239"])
def test_with_blank_units_as_its_only_difference_step_b_accepts_the_real_table(paper, key):
    """End to end through run_table_classification (anchor checks, reconstruction sanity check, unit-hint flags)."""
    invoke, prompts = _step_b(_declared_labels(_answer(key)))
    classification, error = orchestrator.run_table_classification(
        run_id="r1", paper_id=PAPER, seed_table_anchor=key.replace("_", ":"), other_tables=[], model="m", invoke=invoke)
    assert error is None and classification is not None and classification.row_groups and len(prompts) == 1
    unitless = [v for v in classification.variables if v.units is None]
    assert len(unitless) == 1
    # a unitless variable is not an unsupported units hint: nothing to flag, nothing to withhold
    assert not any(f.key == unitless[0].label for f in classification.unit_hint_flags)


# --------------------------------------------------------------------- #
# what must NOT change
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_every_optional_free_text_field_treats_blank_as_not_provided(blank):
    variable = TableVariable(label="pH", variable_name=blank, units=blank, method_hint=blank)
    assert (variable.variable_name, variable.units, variable.method_hint) == (None, None, None)
    column = TableValueColumn(value_column_id="v", variable_name_hint="pH", variable=blank, units_hint=blank,
                              site_hint=blank, method_hint=blank, treatment_level_hint=blank)
    assert (column.variable, column.units_hint, column.site_hint, column.method_hint, column.treatment_level_hint) == (None,) * 5
    assert TimeLevel(factor="DAP", level="35", date_text="9 June", year_text=blank, site=blank, anchors=["b:0001"]).year_text is None


def test_required_fields_still_reject_blank():
    with pytest.raises(ValidationError):
        TableVariable(label="", units="g m-2")
    with pytest.raises(ValidationError):
        TableValueColumn(value_column_id="", variable_name_hint="x")


def test_a_non_empty_value_is_kept_exactly_and_a_non_string_is_still_rejected():
    assert TableVariable(label="Biomass", units=" g m-2 ").units == " g m-2 "                    # not trimmed, not rewritten
    with pytest.raises(ValidationError):
        TableVariable(label="Biomass", units=["g", "m-2"])                                         # genuinely invalid
    # the other defects of the real attempts stay defects (b:0043 attempt 2: non-string `variable`)
    raw = _answer("b_0043")
    raw["value_columns"][0]["variable"] = {"label": "Total C"}
    with pytest.raises(ValidationError):
        TableClassification.model_validate(raw)


def test_a_time_level_with_only_blank_dates_still_fails_its_own_rule():
    """Blank date and year become None, and a time level that states no date is still rejected (it gives neither)."""
    with pytest.raises(ValidationError, match="gives neither date_text nor year_text"):
        TimeLevel(factor="DAP", level="35", date_text=" ", year_text="", anchors=["b:0001"])


def test_a_blank_column_variable_does_not_bypass_the_identify_your_variable_check():
    with pytest.raises(ValidationError, match="does not identify its variable"):
        TableClassification.model_validate({
            "table_role": "treatment_response", "table_anchors": ["b:0001"],
            "value_columns": [{"value_column_id": "v", "variable": "", "variable_name_hint": "  "}],
            "row_groups": [{"row_group_id": "r", "factor_values": {}, "source_table_anchor": "b:0001", "cells": {"v": "1"}}],
        })
