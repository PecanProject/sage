"""Fixes from the Philippe live run 20260926T190955_435c16fa.

1. Binding by elimination: 2 of 3 PAR Treatments failed readiness; `_resolve_known_refs` bound Observation.treatment_id
   to the ONLY ready one, and all 30 ready Observations (PAR 0-0.1 rows, 0.1-0.2 rows, Year rows) were committed with
   PAR 0.2-0.35. A multi-record prerequisite is bound by default / offered as an allowed set only when every record of
   that type the run attempted is referenceable; otherwise only the candidate's own link counts.
2. A table-classification answer whose reason says the table was not read ("its content and caption were not
   accessed") was accepted as non_enumerable, and Table 1 was lost (also Paul b:0458, Daren b:0119, Berntson b:0059).
3. Rows with no factor level in a main-effect table (the P-value rows) were counted as 33 blocked "measurements";
   "–" cells ("not reported") were counted too.
4. The hint "counted after 3-D digitising of each sapling (Methods b:0038)" linked leaf number to gap-fraction
   photography by the one shared word "sapling" (in 44 of the paper's 160 blocks).
5. The Treatment packet held the design sentence defining the PAR classes, yet the extraction reported Table 3's
   response values as the Treatment's facts and no definition.
"""

from __future__ import annotations

import json

import pytest

from pipeline import causes, design, method_map as mm, orchestrator
from pipeline.document_map import build_document_map
from pipeline.evidence_index import build_evidence_index
from pipeline.raw_schema import (EnumerationCandidate, TableClassification, TableFactor, TableRowGroup,
                                 TableValueColumn, TableVariable)
from test_document_map import _write_paper
from test_orchestrator import env  # noqa: F401 (fixture)

P = "p"


def _rec(entity, slug, status="ready"):
    return {"entity_type": entity, "record_id": f"{P}_{entity.lower()}_{slug}", "status": status, "detail": {}}


def _records(treatments, methods=(("m1", "ready"),)):
    return {
        "Citation": _rec("Citation", "c"), "Site": [_rec("Site", "s")],
        "Treatment": [_rec("Treatment", slug, status) for slug, status in treatments],
        "Method": [_rec("Method", slug, status) for slug, status in methods],
    }


def _candidate(**links):
    return EnumerationCandidate(candidate_id="obs", description="d", anchors=["b:0001"], linked_candidates=links)


# --------------------------------------------------------------------------- #
# 1. No binding by elimination
# --------------------------------------------------------------------------- #

def test_the_only_ready_treatment_is_not_bound_when_its_siblings_failed():
    records = _records([("low", "unresolved"), ("mid", "unresolved"), ("high", "ready")])
    known, blocked = orchestrator._resolve_known_refs("Observation", records)
    assert blocked is None and "treatment_id" not in known
    assert known["method_id"] == f"{P}_method_m1" and known["site_id"] == f"{P}_site_s"     # complete pools still bind


def test_a_candidate_is_refused_not_bound_to_the_survivor():
    records = _records([("low", "unresolved"), ("mid", "unresolved"), ("high", "ready")])
    known, _ = orchestrator._resolve_known_refs("Observation", records)
    resolved = orchestrator._apply_candidate_links(P, "Observation", records, known, _candidate())
    assert "treatment_id" not in resolved
    refusal = orchestrator._link_refusals(P, "Observation", records, resolved, _candidate())
    assert [(r["field"], r["cause"]) for r in refusal] == [("treatment_id", causes.AMBIGUOUS)]
    assert "choice by elimination" in refusal[0]["message"]


def test_a_link_naming_a_failed_treatment_is_blocked_by_that_prerequisite():
    records = _records([("low", "unresolved"), ("high", "ready")])
    resolved = orchestrator._apply_candidate_links(P, "Observation", records, {}, _candidate(treatment_id="low"))
    refusal = orchestrator._link_refusals(P, "Observation", records, resolved, _candidate(treatment_id="low"))
    assert refusal[0]["cause"] == causes.BLOCKED_PREREQUISITE and f"{P}_treatment_low" in refusal[0]["message"]


def test_the_candidates_own_link_to_a_ready_treatment_still_binds():
    records = _records([("low", "unresolved"), ("high", "ready")])
    resolved = orchestrator._apply_candidate_links(P, "Observation", records, {}, _candidate(treatment_id="high"))
    assert resolved["treatment_id"] == f"{P}_treatment_high"
    assert orchestrator._link_refusals(P, "Observation", records, resolved, _candidate(treatment_id="high")) == []


def test_a_one_treatment_paper_and_a_complete_pool_behave_as_before():
    one = _records([("only", "ready")])
    assert orchestrator._resolve_known_refs("Observation", one)[0]["treatment_id"] == f"{P}_treatment_only"
    complete = _records([("low", "ready"), ("high", "ready")])
    resolved = orchestrator._apply_candidate_links(P, "Observation", complete, {}, _candidate())
    assert resolved["treatment_id"] == [f"{P}_treatment_high", f"{P}_treatment_low"]           # allowed set, unchanged


def test_one_method_surviving_of_several_is_not_bound_either():
    records = _records([("only", "ready")], methods=(("caliper", "ready"), ("gas", "unresolved")))
    known, _ = orchestrator._resolve_known_refs("Observation", records)
    assert "method_id" not in known


def test_the_candidate_loop_refuses_without_attempting_the_record(env, monkeypatch):
    records = _records([("low", "unresolved"), ("high", "ready")])
    monkeypatch.setattr(orchestrator, "run_enumeration",
                        lambda **k: ([_candidate()], None))
    monkeypatch.setattr(orchestrator, "run_table_classification_pass", lambda **k: {})
    attempted = []
    monkeypatch.setattr(orchestrator, "run_record", lambda **k: attempted.append(k["record_id"]))
    out = orchestrator._run_multi_record_entity(
        run_id="r", paper_id=P, entity_type="Observation", model="m", client=None,
        invoke=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model call expected")),
        enable_ai_validation=False, this_run_records=records)
    assert attempted == []
    assert [(o["status"], o["detail"]["unresolved_cause"]) for o in out] == [("unresolved", causes.AMBIGUOUS)]


# --------------------------------------------------------------------------- #
# 2. "I did not read the table" is not a classification
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("reason, rejected", [
    ("Unable to reconstruct the table at anchor b:0053 because its content and caption were not accessed; therefore "
     "the table structure, factors, variables, and values cannot be determined.", True),
    ("Unable to reconstruct the table structure without accessing the table content; no table data was read.", True),
    ("Unable to reconstruct the table content because the table cells were not read; no data available.", True),
    ("could not be reconstructed from the available source information; its structure and contents are ambiguous or "
     "inaccessible.", True),
    ("The table reports ANOVA F-statistics and significance for each effect rather than measured values.", False),
    ("The table at anchor b:0053 is severely garbled: header cells are split and values run together.", False),
])
def test_only_an_answer_that_says_it_did_not_read_the_table_is_rejected(reason, rejected):
    assert bool(orchestrator._NOT_READ_RE.search(reason)) is rejected


def _not_applicable(reason):
    return {"table_role": "non_enumerable", "applicable": False, "reason": reason, "table_anchors": ["b:0002"]}


def _invoke(answers):
    prompts = []

    def invoke(agent, model, prompt, timeout=300):
        answer = answers[min(len(prompts), len(answers) - 1)]
        prompts.append(prompt)
        return orchestrator.AgentInvocation(agent=agent, model=model, prompt=prompt, returncode=0, stdout="", stderr="",
                                            final_text=json.dumps(answer), parsed_json=answer, parse_error=None)
    return invoke, prompts


@pytest.fixture
def table_paper(tmp_path, monkeypatch):
    _write_paper(tmp_path / "papers", P, [("b:0001", "Caption", "Table 1. ANOVA.", 0),
                                          ("b:0002", "Table", "| Effect | F |\n|---|---|\n| Year | 12.1 |", 0)])
    monkeypatch.setenv("IR_PAPERS_ROOT", str(tmp_path / "papers"))
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))


def test_a_not_read_answer_is_retried_and_the_real_verdict_accepted(table_paper):
    invoke, prompts = _invoke([_not_applicable("the table cells were not read"),
                               _not_applicable("The table reports ANOVA F statistics, not measured values.")])
    table, error = orchestrator.run_table_classification(
        run_id="r", paper_id=P, seed_table_anchor="b:0002", other_tables=[], model="m", invoke=invoke)
    assert error is None and table.reason.startswith("The table reports ANOVA") and len(prompts) == 2
    assert "read it with read_table" in prompts[1] and "PREVIOUS ANSWER" not in prompts[1]


def test_a_table_the_model_never_reads_is_a_failure_not_non_enumerable(table_paper):
    invoke, prompts = _invoke([_not_applicable("its content was not accessed")])
    table, error = orchestrator.run_table_classification(
        run_id="r", paper_id=P, seed_table_anchor="b:0002", other_tables=[], model="m", invoke=invoke)
    assert table is None and "did not read the table" in error
    assert len(prompts) == orchestrator.MAX_TABLE_CLASSIFICATION_ATTEMPTS


# --------------------------------------------------------------------------- #
# 3. Unidentified rows and "–" cells are not blocked measurements
# --------------------------------------------------------------------------- #

def _main_effect_table():
    def row(rid, levels, cells):
        return TableRowGroup(row_group_id=rid, factor_values=levels, source_table_anchor="b:0193", cells=cells)
    return TableClassification(
        applicable=True, table_role="treatment_response", table_anchors=["b:0193"],
        factors=[TableFactor(name="PARt", dimension="treatment", encoding="rows"),
                 TableFactor(name="Year", dimension="time", encoding="rows")],
        variables=[TableVariable(label="Nm (g g-1)")],
        value_columns=[TableValueColumn(value_column_id="nm", variable="Nm (g g-1)")],
        row_groups=[row("r1", {"PARt": "0-0.1"}, {"nm": "2.33 a"}), row("r2", {"PARt": "0.1-0.2"}, {"nm": "2.59 b"}),
                    row("y1", {"Year": "2001"}, {"nm": "–"}), row("y2", {"Year": "2002"}, {"nm": "2.43"}),
                    row("p1", {}, {"nm": "0.0006"})])


def test_a_row_with_no_factor_level_is_unidentified_never_blocked_and_a_dash_is_not_a_value():
    table = _main_effect_table()
    assert design.cell_pooling(table, table.row_groups[4], "nm").representation == design.UNIDENTIFIED_ROW
    sink: list[dict] = []
    candidates = orchestrator._table_classification_to_candidates(table, {}, None, sink)
    assert sorted(c.candidate_id for c in candidates) == ["nm_r1", "nm_r2"]
    assert [(c["row"], c["status"]) for c in sink] == [("y2", design.BLOCKED_BY_REPRESENTATION),
                                                       ("p1", design.UNIDENTIFIED_ROW)]


# --------------------------------------------------------------------------- #
# 4. Method hints: never one common word; a cited block narrows the Methods
# --------------------------------------------------------------------------- #

@pytest.fixture
def hint_index(tmp_path):
    blocks = [(f"b:{i:04d}", "Text", f"Each sapling grew under the pine stand in plot {i}.", 0) for i in range(1, 20)]
    blocks += [("b:0038", "Text", "Leaves were recorded with a 3D-digitizing technique; leaf area was LA = kLW.", 0),
               ("b:0042", "Text", "Gap fraction was computed from a fisheye photograph taken above each sapling.", 0)]
    _write_paper(tmp_path, "h", blocks)
    return build_evidence_index(build_document_map("h", tmp_path))


METHODS = {
    "gap": {"slug": "gap_fraction_photography", "name": "Gap fraction", "anchors": ["b:0042"],
            "description": "computed from a fisheye photograph taken above each sapling"},
    "dig": {"slug": "three_d_digitizing", "name": "3D-digitizing technique", "anchors": ["b:0038"],
            "description": "position and orientation of each leaf recorded"},
    "la": {"slug": "leaf_area_estimation", "name": "leaf surface area", "anchors": ["b:0038"], "description": "LA = kLW"},
}


def _hint(text):
    return mm.VariableEntry(key="x", label="Leaf number", name=None, hints=[text])


def test_a_word_common_across_the_paper_links_nothing(hint_index):
    assert "sapling" in mm.common_words(hint_index)
    assert mm.hint_tier(_hint("counted on each sapling"), METHODS, hint_index).status == mm.NONE


def test_the_real_hint_links_the_digitizing_method_among_its_cited_blocks_methods(hint_index):
    link = mm.hint_tier(_hint("counted after 3‑D digitising of each sapling (Methods b:0038)."), METHODS, hint_index)
    assert (link.status, link.method_slug) == (mm.LINKED, "three_d_digitizing") and "b:0038" in link.reason


def test_a_cited_block_shared_by_two_methods_without_a_distinctive_word_is_ambiguous(hint_index):
    link = mm.hint_tier(_hint("measured from leaf outlines (Methods b:0038)."), METHODS, hint_index)
    assert link.status == mm.AMBIGUOUS and set(link.candidates) == {"three_d_digitizing", "leaf_area_estimation"}


# --------------------------------------------------------------------------- #
# 5. The Treatment extraction is pointed at the design text defining its factor
# --------------------------------------------------------------------------- #

def test_the_treatment_note_cites_the_design_statements_of_its_factor(tmp_path, monkeypatch):
    _write_paper(tmp_path, "t", [
        ("b:0001", "SectionHeader", "## Materials and methods", 0),
        ("b:0002", "Text", "The stand was thinned to obtain a gradient of PAR t.", 0),
        ("b:0003", "Text", "The PAR t was divided into three classes (0-0.1, 0.1-0.2 and 0.2-0.37).", 0),
        ("b:0004", "Text", "Leaves were dried and weighed.", 0),
    ])
    monkeypatch.setenv("IR_PAPERS_ROOT", str(tmp_path))
    candidate = EnumerationCandidate(candidate_id="0_0_1", description="Experimental condition: PARt=0–0.1",
                                     anchors=["b:0193"], dimensions=[{"name": "PARt", "dimension": "treatment", "level": "0–0.1"}])
    note = orchestrator._treatment_definition_note("t", candidate)
    assert "b:0002, b:0003" in note and "b:0004" not in note
    assert "are Observations, not facts of a Treatment" in note and "never reconcile" in note
    plain = EnumerationCandidate(candidate_id="x", description="d", anchors=["b:0002"])
    assert orchestrator._treatment_definition_note("t", plain) == ""


def test_one_ready_record_of_an_incomplete_pool_is_offered_for_the_candidates_own_link():
    """With the default binding withheld, the table row that NAMES the surviving Treatment must still reach it (the
    Philippe 0.2-0.35 cells); a complete single-record pool needs no link pool -- `_resolve_known_refs` binds it."""
    incomplete = _records([("low", "unresolved"), ("high", "ready")])
    pool = orchestrator._multi_record_link_pools(P, "Observation", incomplete)
    assert [item["slug"] for item in pool["treatment_id"]] == ["high"]
    assert "treatment_id" not in orchestrator._multi_record_link_pools(P, "Observation", _records([("only", "ready")]))
