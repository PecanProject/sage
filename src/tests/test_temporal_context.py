"""Item 11: temporal propagation (plan section 9).

Evidence (real run 20260919T085532_98015a8a, Daren-1997-Canopy): of the ready Observation records, 11 have
temporal_info UNRESOLVED with "No explicit sampling date is stated anywhere near this reported value" and one is
EXTRACTED as the bare year "1993" with no interval. Yet the paper states the dates -- b:0028: "Harvest date and
growth stage of subplots were 9 June (vegetative), 19 July (elongating), and 27 August (reproductive) at Ames and
10 June (vegetative), 27 July (elongating), and 26 August (reproductive) at Mead" -- and the year in the Table 1
caption b:0047. A table-derived candidate only carries "Maturity=Vegetative"; Extraction reads the table's own
anchor and Conversion is sealed, so the Methods dates never arrived. Also, the date reconstruction tool defaulted a
missing end day to 28 and could not read a written date with a year.

Item 11: Step B records the dates the paper gives for a table's time levels (`time_levels`, grounded literal text);
Step C attaches the single matching, unambiguous entry to each cell's candidate as sealed context; Extraction is
told to cite the dating block as a fact; Conversion may only use what RAW_EVIDENCE carries; the reconstruction
handles written dates and the month end. Nothing is computed or guessed: no year, no site match, no date -> UNRESOLVED.

Fixtures: tests/fixtures/item11/ (real blocks) and tests/fixtures/item8/ (the real live Table 6 classification).
Tests marked SYNTHETIC use invented text.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline import orchestrator, run_store
from pipeline.raw_schema import TableClassification, TableFactor, TableRowGroup, TableValueColumn, TimeLevel
from pipeline.reconstruction import reconstruct_date_mapping
from pipeline.validators import _load_rendered_blocks

FIXTURES = Path(__file__).parent / "fixtures" / "item11"
ITEM8 = Path(__file__).parent / "fixtures" / "item8"
TABLE = "b:0048"  # a real Table block of the fixture paper (Table 1); the real Table 6 classification is re-anchored to it


def _dates(**kw):
    r = reconstruct_date_mapping(kw.pop("text"))
    return r["earliest"], r["latest"]


# --------------------------------------------------------------------- #
# reconstruction: written dates with a year, and the month end
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("text, expected", [
    ("9 June 1993", ("1993-06-09", "1993-06-09")),
    ("June 9, 1993", ("1993-06-09", "1993-06-09")),
    ("27 August 1993", ("1993-08-27", "1993-08-27")),
    ("Sept. 5, 1993", ("1993-09-05", "1993-09-05")),
    ("15-24 June 1981", ("1981-06-15", "1981-06-24")),
    ("9 June to 19 July 1993", ("1993-06-09", "1993-07-19")),
    ("harvested on 10 June 1993 at Mead", ("1993-06-10", "1993-06-10")),
    ("June 1993", ("1993-06-01", "1993-06-30")),                       # a month alone spans the month (protocol 10.1)
    ("2007-06-15", ("2007-06-15", "2007-06-15")),                      # existing behaviour unchanged
])
def test_written_dates_with_a_year_are_parsed(text, expected):
    assert _dates(text=text) == expected


@pytest.mark.parametrize("text, expected_latest", [
    ("2007-06 to 2007-08", "2007-08-31"),      # this used to be 2007-08-28
    ("2007-01 to 2007-04", "2007-04-30"),
    ("2007-01 to 2007-02", "2007-02-28"),
    ("2008-01 to 2008-02", "2008-02-29"),      # a leap year
    ("February 2024", "2024-02-29"),
    ("2007-06-15 to 2007-08-20", "2007-08-20"),  # an explicit end day is kept
])
def test_the_month_end_is_the_real_last_day_not_28(text, expected_latest):
    assert _dates(text=text)[1] == expected_latest


@pytest.mark.parametrize("text", [
    "9 June",                                          # no year: never completed with a guessed one
    "9 June (vegetative), 19 July (elongating), and 27 August (reproductive)",  # several dates: ambiguous
    "9 June 1993 (vegetative) and 19 July 1993 (elongating)",                    # several dated spans: never collapsed into one
    "9 June 1993, 10 June 1993",
    "31 September 1993", "31 June 1993",               # impossible dates
    "sometime in the summer", "1993",
])
def test_undated_ambiguous_or_impossible_texts_stay_unparsed(text):
    result = reconstruct_date_mapping(text)
    assert (result["earliest"], result["latest"], result["relative_timing"]) == (None, None, None) and result["reported_text"] == text


def test_relative_timing_is_unchanged():
    assert reconstruct_date_mapping("before planting")["relative_timing"] == "before_planting"


# --------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------- #


def _tc(**kw) -> TableClassification:
    base = dict(
        table_role="treatment_response", table_anchors=[TABLE],
        factors=[TableFactor(name="Maturity", dimension="time", encoding="rows"), TableFactor(name="Site", dimension="site", encoding="columns")],
        value_columns=[TableValueColumn(value_column_id="a", variable_name_hint="yield", factor_levels={"Site": "Ames"}),
                       TableValueColumn(value_column_id="m", variable_name_hint="yield", factor_levels={"Site": "Mead"})],
        row_groups=[TableRowGroup(row_group_id="v", factor_values={"Maturity": "Vegetative"}, source_table_anchor=TABLE, cells={"a": "1", "m": "2"})],
    )
    base.update(kw)
    return TableClassification(**base)


def test_a_time_level_needs_a_date_or_a_year_and_an_anchor():
    with pytest.raises(ValidationError, match="neither date_text nor year_text"):
        TimeLevel(factor="Maturity", level="Vegetative", anchors=["b:0028"])
    with pytest.raises(ValidationError):
        TimeLevel(factor="Maturity", level="Vegetative", date_text="9 June", anchors=[])


def test_a_time_level_must_name_a_declared_time_factor_and_a_level_the_table_reports():
    good = TimeLevel(factor="Maturity", level="Vegetative", date_text="9 June", anchors=["b:0028"])
    assert _tc(time_levels=[good]).time_levels == [good]
    with pytest.raises(ValidationError, match="not a declared factor with dimension 'time'"):
        _tc(time_levels=[TimeLevel(factor="Site", level="Ames", date_text="9 June", anchors=["b:0028"])])  # a site is not a time factor
    with pytest.raises(ValidationError, match="not a level this table reports"):
        _tc(time_levels=[TimeLevel(factor="Maturity", level="Flowering", date_text="9 June", anchors=["b:0028"])])
    with pytest.raises(ValidationError, match="given more than once"):
        _tc(time_levels=[good, good])
    # per-site entries for the same level are distinct
    _tc(time_levels=[good.model_copy(update={"site": "Ames"}), good.model_copy(update={"site": "Mead"})])


def test_a_legacy_classification_without_time_levels_is_unchanged():
    assert _tc().time_levels == []


# --------------------------------------------------------------------- #
# grounding (real b:0028 / b:0047)
# --------------------------------------------------------------------- #


def _level(level, date_text, site=None, year_text="1993", anchors=("b:0028", "b:0047")):
    return TimeLevel(factor="Maturity", level=level, site=site, date_text=date_text, year_text=year_text, anchors=list(anchors))


@pytest.fixture()
def blocks(monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(FIXTURES))
    return _load_rendered_blocks("Daren-1997-Canopy")


def test_real_methods_dates_and_the_captions_year_are_grounded(blocks):
    assert "9 June (vegetative)" in blocks["b:0028"] and "1993" in blocks["b:0047"]
    tc = _tc(time_levels=[_level("Vegetative", "9 June", site="Ames"), _level("Vegetative", "10 June", site="Mead")])
    assert orchestrator._time_level_grounding_errors(tc, blocks) == []


@pytest.mark.parametrize("kwargs, part", [
    (dict(date_text="June 9"), "date_text 'June 9' is not found"),                 # a paraphrase
    (dict(date_text="1993-06-09"), "computed date"),                                 # a computed ISO date
    (dict(date_text="12 June"), "date_text '12 June' is not found"),                 # an invented date
    (dict(date_text="9 June", year_text="1994"), "year_text '1994' is not found"),
    (dict(date_text="9 June", site="Lincoln"), "site 'Lincoln' is not named"),
    (dict(date_text="9 June", anchors=("b:9999",)), "does not exist in content.md"),
])
def test_an_ungrounded_time_level_is_rejected(blocks, kwargs, part):
    tc = _tc(time_levels=[_level("Vegetative", **{"date_text": "9 June", **kwargs})])
    errors = orchestrator._time_level_grounding_errors(tc, blocks)
    assert errors and any(part in e["message"] for e in errors)


def test_a_date_supported_only_by_a_block_the_level_does_not_cite_is_still_rejected(blocks):
    """The date text is in b:0028, but the entry cites only the year block: grounding is per cited anchor."""
    tc = _tc(time_levels=[_level("Vegetative", "9 June", anchors=("b:0047",))])
    assert orchestrator._time_level_grounding_errors(tc, blocks)


# --------------------------------------------------------------------- #
# Step B end to end
# --------------------------------------------------------------------- #


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """The real Daren Methods blocks; the real live Table 6 classification re-anchored to the paper's real Table block."""
    papers = tmp_path / "papers"
    shutil.copytree(FIXTURES / "Daren-1997-Canopy", papers / "Daren-1997-Canopy")
    monkeypatch.setenv("IR_PAPERS_ROOT", str(papers))
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))
    return tmp_path


def _live_table6(**extra) -> dict:
    text = (ITEM8 / "live_gpt_oss_table6_declared_classification.json").read_text().replace("b:0607", TABLE).replace("b:0656", TABLE)
    payload = json.loads(text)
    payload["table_anchors"] = [TABLE]
    payload.update(extra)
    return payload


def _time_levels_payload(**override):
    dates = {("Vegetative", "Ames"): "9 June", ("Elongating", "Ames"): "19 July", ("Reproductive", "Ames"): "27 August",
             ("Vegetative", "Mead"): "10 June", ("Elongating", "Mead"): "27 July", ("Reproductive", "Mead"): "26 August"}
    return [{"factor": "Maturity", "level": lv, "site": site, "date_text": d, "year_text": "1993", "anchors": ["b:0028", "b:0047"]}
            for (lv, site), d in dates.items()] if not override else override["levels"]


def _classify(payload):
    def invoke(agent, model, prompt, timeout=300):
        return orchestrator.AgentInvocation(agent=agent, model=model, prompt=prompt, returncode=0, stdout="{}", stderr="",
                                            final_text=json.dumps(payload), parsed_json=payload, parse_error=None)
    return orchestrator.run_table_classification(run_id="r1", paper_id="Daren-1997-Canopy", seed_table_anchor=TABLE,
                                                 other_tables=[], model="m", invoke=invoke)


def test_step_b_accepts_grounded_time_levels_and_records_them(env):
    result, error = _classify(_live_table6(time_levels=_time_levels_payload()))
    assert error is None and len(result.time_levels) == 6
    assert {(tl.level, tl.site, tl.date_text) for tl in result.time_levels} >= {("Vegetative", "Ames", "9 June"), ("Vegetative", "Mead", "10 June")}
    summary = orchestrator.summarize_table_pass("r1", "Daren-1997-Canopy", {})
    assert len(summary["tables"][0]["time_levels"]) == 6  # disclosed in the manifest


def test_step_b_rejects_an_ungrounded_date_with_feedback_and_a_grounded_retry_succeeds(env):
    invented = [{**_time_levels_payload()[0], "date_text": "12 June"}]  # a date the paper never states
    prompts, payloads = [], iter([_live_table6(time_levels=invented), _live_table6(time_levels=_time_levels_payload())])

    def invoke(agent, model, prompt, timeout=300):
        prompts.append(prompt)
        payload = next(payloads)
        return orchestrator.AgentInvocation(agent=agent, model=model, prompt=prompt, returncode=0, stdout="{}", stderr="",
                                            final_text=json.dumps(payload), parsed_json=payload, parse_error=None)
    result, error = orchestrator.run_table_classification(run_id="r1", paper_id="Daren-1997-Canopy", seed_table_anchor=TABLE,
                                                          other_tables=[], model="m", invoke=invoke)
    assert error is None and len(prompts) == 2
    assert "date_text '12 June' is not found" in prompts[1] and "failed validation" in prompts[1]
    artifact = json.loads((run_store.record_dir("r1", f"table_classification__{TABLE.replace(':', '_')}") / "table_classification" / "attempt1.json").read_text())
    assert artifact["failure_class"] == "validation_failure"  # an extraction failure class, never a provider one


def test_step_b_gives_up_on_a_time_level_that_is_never_grounded(env):
    result, error = _classify(_live_table6(time_levels=[{**_time_levels_payload()[0], "date_text": "12 June"}]))
    assert result is None and "ungrounded time_levels" in error
    assert json.loads((run_store.record_dir("r1", f"table_classification__{TABLE.replace(':', '_')}") / "final.json").read_text())["failure_kind"] == "extraction"


def test_the_step_b_prompt_asks_for_grounded_literal_dates_only():
    prompt = orchestrator._table_classification_prompt("p", "b:0001", [])
    assert "time_levels, variables, value_columns, row_groups" in prompt
    assert "- time_levels: ONLY for a table with a 'time'-dimension factor" in prompt
    assert "Copy the text verbatim -- never compute, convert or guess a date" in prompt


# --------------------------------------------------------------------- #
# Step C: per-cell context, with refuse-to-guess
# --------------------------------------------------------------------- #


def _real_table6() -> TableClassification:
    return TableClassification.model_validate(_live_table6(time_levels=_time_levels_payload()))


def test_real_table_6_cells_carry_the_single_matching_dated_level():
    candidates = orchestrator._table_classification_to_candidates(_real_table6(), {})
    by = {}
    for c in candidates:
        [entry] = c.context["temporal_context"]
        # the candidate description names its own level and site, so the attached date can be checked independently
        by.setdefault((entry["level"], entry["site"]), set()).add(entry["date_text"])
        assert entry["year_text"] == "1993" and entry["anchors"] == ["b:0028", "b:0047"]
        assert entry["level"] in c.description and (f"at {entry['site']}" in c.description)
    assert by[("Vegetative", "Ames")] == {"9 June"} and by[("Vegetative", "Mead")] == {"10 June"}
    assert by[("Reproductive", "Ames")] == {"27 August"} and by[("Reproductive", "Mead")] == {"26 August"}
    assert all(len(v) == 1 for v in by.values())  # one date per (level, site)


def test_a_per_site_entry_is_not_attached_when_the_cell_names_no_site():
    """SYNTHETIC: the paper dates Vegetative differently at each site, but this table's cell has no site -> ambiguous."""
    tc = _tc(factors=[TableFactor(name="Maturity", dimension="time", encoding="rows")],
             value_columns=[TableValueColumn(value_column_id="y", variable_name_hint="yield")],
             time_levels=[TimeLevel(factor="Maturity", level="Vegetative", site="Ames", date_text="9 June", anchors=["b:0028"]),
                          TimeLevel(factor="Maturity", level="Vegetative", site="Mead", date_text="10 June", anchors=["b:0028"])],
             row_groups=[TableRowGroup(row_group_id="v", factor_values={"Maturity": "Vegetative"}, source_table_anchor=TABLE, cells={"y": "1"})])
    [candidate] = orchestrator._table_classification_to_candidates(tc, {})
    assert "temporal_context" not in candidate.context


def test_two_entries_that_both_apply_to_a_cell_are_ambiguous_and_attach_nothing():
    """SYNTHETIC: a level dated for every site AND separately for Ames -> the Ames cell has two candidate dates."""
    tc = _tc(time_levels=[TimeLevel(factor="Maturity", level="Vegetative", year_text="1993", anchors=["b:0047"]),
                          TimeLevel(factor="Maturity", level="Vegetative", site="Ames", date_text="9 June", anchors=["b:0028"])])
    got = {c.candidate_id: "temporal_context" in c.context for c in orchestrator._table_classification_to_candidates(tc, {})}
    assert got == {"a_v": False, "m_v": True}  # Mead has exactly one applicable entry; Ames has two -> nothing attached


def test_a_level_dated_for_every_site_applies_to_every_cell_and_an_unlisted_level_gets_nothing():
    tc = _tc(time_levels=[TimeLevel(factor="Maturity", level="Vegetative", year_text="1993", anchors=["b:0047"])],
             row_groups=[TableRowGroup(row_group_id="v", factor_values={"Maturity": "Vegetative"}, source_table_anchor=TABLE, cells={"a": "1", "m": "2"}),
                         TableRowGroup(row_group_id="e", factor_values={"Maturity": "Elongating"}, source_table_anchor=TABLE, cells={"a": "3"})])
    got = {c.candidate_id: "temporal_context" in c.context for c in orchestrator._table_classification_to_candidates(tc, {})}
    assert got == {"a_v": True, "m_v": True, "a_e": False}


def test_a_table_with_no_time_factor_or_no_time_levels_gets_no_context():
    no_levels = _tc()
    no_factor = _tc(factors=[TableFactor(name="Site", dimension="site", encoding="columns")],
                    row_groups=[TableRowGroup(row_group_id="v", source_table_anchor=TABLE, cells={"a": "1"})])
    for tc in (no_levels, no_factor):
        assert all("temporal_context" not in c.context for c in orchestrator._table_classification_to_candidates(tc, {}))


def test_temporal_context_coexists_with_pooling_context():
    tc = _tc(time_levels=[TimeLevel(factor="Maturity", level="Vegetative", year_text="1993", anchors=["b:0047"])])
    pooled = tc.model_copy(update={"pooled_factors": []})
    ctx = {**orchestrator._pooled_context(pooled), **orchestrator._temporal_context(orchestrator._time_levels_for_cell(tc, tc.row_groups[0], tc.value_columns[0]))}
    assert list(ctx) == ["temporal_context"]


# --------------------------------------------------------------------- #
# prompts: Extraction cites the dating block; Conversion may use only what RAW_EVIDENCE has
# --------------------------------------------------------------------- #


def test_the_extraction_note_names_the_date_and_asks_for_a_cited_fact():
    note = orchestrator._temporal_extraction_note([{"factor": "Maturity", "level": "Vegetative", "site": "Ames", "date_text": "9 June",
                                                    "year_text": "1993", "anchors": ["b:0028", "b:0047"]}])
    assert "Maturity=Vegetative at Ames is dated '9 June 1993' (block(s) b:0028, b:0047)" in note
    assert "field_name 'sampling_date'" in note and "If the block does not actually say it, report nothing" in note


def test_the_conversion_prompt_carries_the_temporal_instruction_only_with_temporal_context():
    raw = {"paper_id": "p", "entity_type": "Observation", "record_id": "r", "facts": []}
    ctx = {"temporal_context": [{"factor": "Maturity", "level": "Vegetative", "site": None, "date_text": "9 June", "year_text": "1993", "anchors": ["b:0028"]}]}
    with_ctx = orchestrator._conversion_prompt("p", "Observation", "r", raw, None, None, ctx)
    assert "call apply_reconstruction with kind `date_mapping`" in with_ctx and "never compute or assume a date or a year" in with_ctx
    assert "UNRESOLVED with a real reason" in with_ctx
    without = orchestrator._conversion_prompt("p", "Observation", "r", raw, None, None, {"aggregated_over_factors": ["x"]})
    assert "date_mapping" not in without
    assert orchestrator._conversion_prompt("p", "Observation", "r", raw, None) == orchestrator._conversion_prompt("p", "Observation", "r", raw, None, None, None)


def test_the_pipeline_hands_the_dating_note_to_extraction_for_a_dated_cell(env, monkeypatch):
    monkeypatch.setattr(orchestrator, "_resolve_known_refs", lambda entity_type, records: ({}, None))
    monkeypatch.setattr(orchestrator, "_multi_record_link_pools", lambda *a, **k: {})
    monkeypatch.setattr(orchestrator, "_apply_candidate_links", lambda paper_id, entity_type, this_run_records, known_refs, candidate: known_refs)
    monkeypatch.setattr(orchestrator, "run_table_classification_pass", lambda **k: {TABLE: _real_table6()})
    monkeypatch.setattr(orchestrator, "run_enumeration", lambda **k: ([], None))
    seen = []

    def fake_run_record(*, entity_type, record_id, extraction_context=None, candidate_context=None, **kw):
        seen.append((extraction_context, candidate_context))
        return orchestrator.RecordResult(status="ready", entity_type=entity_type, record_id=record_id, detail={"payload": {}, "ai_validation": None})

    monkeypatch.setattr(orchestrator, "run_record", fake_run_record)
    orchestrator._run_multi_record_entity(run_id="run1", paper_id="Daren-1997-Canopy", entity_type="Observation", model="m", client=None,
                                          invoke=None, enable_ai_validation=False, this_run_records={})
    assert seen and all("is dated" in ctx and "sampling_date" in ctx and "temporal_context" in cc for ctx, cc in seen)
    assert any("Vegetative at Ames is dated '9 June 1993'" in ctx for ctx, _ in seen)
    assert any("Vegetative at Mead is dated '10 June 1993'" in ctx for ctx, _ in seen)
