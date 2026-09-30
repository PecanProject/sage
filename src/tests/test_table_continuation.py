"""Deterministic page-split table continuation (content_reader.table_continuation_map)
and its use by the table-classification pass (Step B).

Real evidence behind the rule: Daren-1997-Canopy's Table 2 spans a page break
(anchors b:0119 -> b:0178). Asked to judge continuation on its own, the model
read b:0178 as a separate LSD table and Table 2 was reconstructed from 7 of its
18 rows (56 of an expected 144 Observation candidates); the numeric-token
sanity check could not notice, because it only compared against the anchors the
model itself chose to list.

The rule was verified against real Marker output for 9 distinct papers (22
consecutive table pairs): it yields exactly Daren's three continuations and
nothing else. The negative cases below are REAL papers where a looser rule
(e.g. "the Marker TableGroup has no Caption sibling") merges separate tables.
Fixtures: tests/fixtures/continuation/ (see its README).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import content_reader as cr
from pipeline import orchestrator, run_store
from pipeline.raw_schema import TableClassification, TableRowGroup, TableValueColumn

FIXTURES = Path(__file__).parent / "fixtures" / "continuation"


# --------------------------------------------------------------------- #
# Real papers
# --------------------------------------------------------------------- #


def test_daren_real_structure_yields_exactly_the_three_known_chains():
    assert cr.table_continuation_chains("Daren-1997-Canopy", FIXTURES) == [
        ["b:0119", "b:0178"], ["b:0350", "b:0367"], ["b:0607", "b:0656"],
    ]


def test_daren_list_tables_marks_continuations_and_only_those():
    tables = {t["table_anchor"]: t["continuation_of"] for t in cr.list_tables("Daren-1997-Canopy", FIXTURES)["tables"]}
    assert tables == {
        "b:0048": None, "b:0119": None, "b:0178": "b:0119", "b:0297": None, "b:0350": None,
        "b:0367": "b:0350", "b:0475": None, "b:0607": None, "b:0656": "b:0607", "b:0761": None,
    }


@pytest.mark.parametrize("paper", ["Berntson-1997-Regenerating", "Nutrient-cycling", "Kathryn-2020-Winter"])
def test_real_papers_with_look_alike_neighbouring_tables_are_never_merged(paper):
    # Berntson b:0053->b:0059: consecutive pages, same width, DIFFERENT tables (own caption outside the Marker group).
    # Nutrient-cycling b:0081->b:0206->b:0210: `#### Table N` SectionHeader captions.
    # Kathryn b:0044->b:0101: no label between them, but 17 substantive blocks.
    assert cr.table_continuation_chains(paper, FIXTURES) == []
    assert all(t["continuation_of"] is None for t in cr.list_tables(paper, FIXTURES)["tables"])


# --------------------------------------------------------------------- #
# Synthetic: each guard of the rule, broken in isolation
# --------------------------------------------------------------------- #

TABLE_A = "| Pop | Yield | SE |\n|---|---|---|\n| a | 1.0 | 0.1 |\n| b | 2.0 | 0.2 |"
TABLE_B = "| c | 3.0 | 0.3 |\n| d | 4.0 | 0.4 |\n| e | 5.0 | 0.5 |"  # same width, no header of its own
TABLE_WIDE = "| Pop | Yield | SE | N |\n|---|---|---|---|\n| c | 3.0 | 0.3 | 8 |"


def _paper(tmp_path, blocks):
    """blocks: list of (block_type, text, page_number). Anchors b:0001.. in order."""
    pdir = tmp_path / "syn"
    pdir.mkdir()
    content, prov = [], {}
    for i, (btype, text, page) in enumerate(blocks, start=1):
        anchor = f"b:{i:04d}"
        content.append(f"{text}\n⟦{anchor}⟧\n")
        prov[anchor] = {"block_type": btype, "page_id": f"page_{page}", "section_path": []}
    (pdir / "content.md").write_text("\n".join(content), encoding="utf-8")
    (pdir / "provenance.json").write_text(json.dumps(prov), encoding="utf-8")
    return tmp_path


def _chains(tmp_path, blocks):
    return cr.table_continuation_chains("syn", _paper(tmp_path, blocks))


def test_adjacent_same_width_next_page_with_only_a_footnote_between_is_a_chain(tmp_path):
    blocks = [("Caption", "*Table 1. Yield.*", 1), ("Table", TABLE_A, 1), ("Footnote", "† footnote", 2), ("Table", TABLE_B, 2)]
    assert _chains(tmp_path, blocks) == [["b:0002", "b:0004"]]


def test_same_page_adjacent_tables_can_chain(tmp_path):
    assert _chains(tmp_path, [("Table", TABLE_A, 3), ("Table", TABLE_B, 3)]) == [["b:0001", "b:0002"]]


def test_a_three_block_chain_is_one_chain(tmp_path):
    blocks = [("Table", TABLE_A, 1), ("Table", TABLE_B, 2), ("Table", TABLE_B, 3)]
    assert _chains(tmp_path, blocks) == [["b:0001", "b:0002", "b:0003"]]


def test_different_column_count_is_not_a_continuation(tmp_path):
    assert _chains(tmp_path, [("Table", TABLE_A, 1), ("Table", TABLE_WIDE, 2)]) == []


def test_tables_more_than_one_page_apart_are_not_a_continuation(tmp_path):
    assert _chains(tmp_path, [("Table", TABLE_A, 1), ("Table", TABLE_B, 3)]) == []


@pytest.mark.parametrize("btype, text", [
    ("Caption", "*Table 2. A new caption.*"),
    ("SectionHeader", "#### Table 3"),
    ("Text", "Table 4 Summary of soil properties"),
    ("Caption", "TABLE II. Results"),
])
def test_a_table_label_between_the_tables_means_a_new_table(tmp_path, btype, text):
    assert _chains(tmp_path, [("Table", TABLE_A, 1), (btype, text, 2), ("Table", TABLE_B, 2)]) == []


def test_any_substantive_block_between_the_tables_means_not_a_continuation(tmp_path):
    blocks = [("Table", TABLE_A, 1), ("Text", "An ordinary paragraph of discussion.", 2), ("Table", TABLE_B, 2)]
    assert _chains(tmp_path, blocks) == []


def test_page_furniture_between_the_tables_is_ignored(tmp_path):
    blocks = [("Table", TABLE_A, 1), ("PageFooter", "12", 1), ("PageHeader", "Journal", 2), ("Table", TABLE_B, 2)]
    assert _chains(tmp_path, blocks) == [["b:0001", "b:0004"]]


def test_a_table_flattened_to_prose_has_no_column_count_and_is_never_chained(tmp_path):
    blocks = [("Table", TABLE_A, 1), ("Table", "Just some flattened cell text 1 2 3", 2)]
    assert _chains(tmp_path, blocks) == []


def test_missing_provenance_or_content_yields_no_chains(tmp_path):
    (tmp_path / "empty").mkdir()
    assert cr.table_continuation_chains("empty", tmp_path) == []
    assert cr.table_continuation_map("nonexistent_paper", tmp_path) == {}


# --------------------------------------------------------------------- #
# Step B prompt / validation / pass
# --------------------------------------------------------------------- #


def _inv(payload):
    return orchestrator.AgentInvocation(
        agent="extractor", model="m", prompt="p", returncode=0, stdout="{}", stderr="",
        final_text=json.dumps(payload), parsed_json=payload, parse_error=None,
    )


def test_prompt_is_byte_identical_for_a_table_with_no_chain_and_names_the_chain_otherwise():
    others = [{"table_anchor": "b:0300", "page": "page_5", "section_path": ["R"]}]
    base = orchestrator._table_classification_prompt("p", "b:0119", others)
    assert orchestrator._table_classification_prompt("p", "b:0119", others, None, None) == base
    assert orchestrator._table_classification_prompt("p", "b:0119", others, None, ["b:0119"]) == base
    chained = orchestrator._table_classification_prompt(
        "p", "b:0119", others + [{"table_anchor": "b:0178", "page": "page_5", "section_path": ["R"]}], None,
        ["b:0119", "b:0178"],
    )
    assert "CONFIRMED CONTINUATION" in chained and "['b:0178']" in chained
    assert "  - b:0300" in chained            # a genuinely separate table is still listed for reference
    assert "  - b:0178" not in chained        # the confirmed member is not offered as an "other" table
    assert chained.count("Output ONLY") == 1  # the chain note is inserted, nothing else is duplicated


@pytest.fixture()
def chain_env(tmp_path, monkeypatch):
    papers = tmp_path / "papers"
    pdir = papers / "syn"
    pdir.mkdir(parents=True)
    content = (
        f"{TABLE_A}\n⟦b:0001⟧\n\n{TABLE_B}\n⟦b:0002⟧\n\n"
        "A separate paragraph.\n⟦b:0003⟧\n\n*Table 2. Another.*\n⟦b:0004⟧\n\n| x | 9.0 |\n|---|---|\n| y | 8.0 |\n⟦b:0005⟧\n"
    )
    prov = {
        "b:0001": {"block_type": "Table", "page_id": "page_1", "section_path": []},
        "b:0002": {"block_type": "Table", "page_id": "page_2", "section_path": []},
        "b:0003": {"block_type": "Text", "page_id": "page_2", "section_path": []},
        "b:0004": {"block_type": "Caption", "page_id": "page_2", "section_path": []},
        "b:0005": {"block_type": "Table", "page_id": "page_2", "section_path": []},
    }
    cells = [("b:0001", 1.0), ("b:0001", 0.1), ("b:0001", 2.0), ("b:0001", 0.2), ("b:0002", 3.0), ("b:0002", 4.0), ("b:0002", 5.0)]
    for n, (parent, val) in enumerate(cells):
        prov[f"b:9{n:03d}"] = {"block_type": "TableCell", "page_id": "page_1", "section_path": [], "parent_table_anchor": parent,
                               "row_index": n, "col_index": 0, "cell_text": str(val)}
    (pdir / "content.md").write_text(content, encoding="utf-8")
    (pdir / "provenance.json").write_text(json.dumps(prov), encoding="utf-8")
    monkeypatch.setenv("IR_PAPERS_ROOT", str(papers))
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))
    return papers


def _classification(anchors, cells_by_row):
    return {
        "applicable": True, "table_role": None, "reason": None, "table_anchors": anchors,
        "value_columns": [{"value_column_id": "v", "variable_name_hint": "yield"}],
        "row_groups": [
            {"row_group_id": f"r{i}", "factor_values": {"Pop": f"p{i}"}, "source_table_anchor": anchors[0] if i < 2 else anchors[-1],
             "cells": {"v": c}} for i, c in enumerate(cells_by_row)
        ],
    }


def test_a_classification_that_omits_a_confirmed_chain_member_is_rejected_with_feedback_then_retried(chain_env):
    partial = _classification(["b:0001"], ["1.0", "0.1", "2.0", "0.2"])
    full = _classification(["b:0001", "b:0002"], ["1.0", "0.1", "2.0", "0.2", "3.0", "4.0", "5.0"])
    prompts = []

    def invoke(agent, model, prompt, timeout=300):
        prompts.append(prompt)
        return _inv(partial if len(prompts) == 1 else full)

    result, error = orchestrator.run_table_classification(
        run_id="r1", paper_id="syn", seed_table_anchor="b:0001", other_tables=[], model="m", invoke=invoke,
        chain_anchors=["b:0001", "b:0002"],
    )
    assert error is None and result.table_anchors == ["b:0001", "b:0002"]
    assert len(prompts) == 2 and "omits ['b:0002']" in prompts[1]  # the retry carries the specific feedback


def test_without_a_chain_the_same_partial_classification_is_still_accepted(chain_env):
    partial = _classification(["b:0001"], ["1.0", "0.1", "2.0", "0.2"])
    result, error = orchestrator.run_table_classification(
        run_id="r1", paper_id="syn", seed_table_anchor="b:0001", other_tables=[], model="m",
        invoke=lambda a, m, p, timeout=300: _inv(partial),
    )
    assert error is None  # existing behavior unchanged when no continuation was confirmed


def test_a_chain_that_is_never_fully_covered_fails_after_the_attempt_budget(chain_env):
    partial = _classification(["b:0001"], ["1.0", "0.1", "2.0", "0.2"])
    calls = []

    def invoke(agent, model, prompt, timeout=300):
        calls.append(1)
        return _inv(partial)

    result, error = orchestrator.run_table_classification(
        run_id="r1", paper_id="syn", seed_table_anchor="b:0001", other_tables=[], model="m", invoke=invoke,
        chain_anchors=["b:0001", "b:0002"],
    )
    assert result is None and "omitted confirmed continuation" in error
    assert len(calls) == orchestrator.MAX_TABLE_CLASSIFICATION_ATTEMPTS


def test_the_pass_classifies_a_chain_once_and_never_seeds_its_continuation_block(chain_env):
    seeds = []
    full = _classification(["b:0001", "b:0002"], ["1.0", "0.1", "2.0", "0.2", "3.0", "4.0", "5.0"])
    other = {
        "applicable": True, "reason": None, "table_anchors": ["b:0005"],
        "value_columns": [{"value_column_id": "v", "variable_name_hint": "y"}],
        "row_groups": [{"row_group_id": "x", "factor_values": {"k": "x"}, "source_table_anchor": "b:0005", "cells": {"v": "9.0"}},
                       {"row_group_id": "y", "factor_values": {"k": "y"}, "source_table_anchor": "b:0005", "cells": {"v": "8.0"}}],
    }

    def invoke(agent, model, prompt, timeout=300):
        seed = "b:0001" if "table at content.md anchor 'b:0001'" in prompt else "b:0005"
        seeds.append(seed)
        return _inv(full if seed == "b:0001" else other)

    out = orchestrator.run_table_classification_pass(run_id="r1", paper_id="syn", model="m", invoke=invoke)
    assert seeds == ["b:0001", "b:0005"]            # b:0002 was never a seed of its own
    assert sorted(out) == ["b:0001", "b:0005"] and out["b:0001"].table_anchors == ["b:0001", "b:0002"]


def test_the_pass_does_not_reseed_the_continuation_when_the_chain_head_fails(chain_env):
    seeds = []

    def invoke(agent, model, prompt, timeout=300):
        seeds.append("b:0001" if "table at content.md anchor 'b:0001'" in prompt else "other")
        return orchestrator.AgentInvocation(
            agent="extractor", model="m", prompt="p", returncode=0, stdout="", stderr="",
            final_text=None, parsed_json=None, parse_error="no final assistant text found in agent output",
        )

    orchestrator.run_table_classification_pass(run_id="r1", paper_id="syn", model="m", invoke=invoke)
    # an empty provider response is a provider failure (Item 9): bounded by its own budget, not the numbered attempts
    assert "b:0002" not in "".join(seeds) and seeds.count("b:0001") == orchestrator.MAX_PROVIDER_FAILURE_ROUNDS


# --------------------------------------------------------------------- #
# The real blind spot: Table 2 reconstructed from its first block only
# --------------------------------------------------------------------- #


def test_the_sanity_check_now_sees_the_whole_real_daren_table_2(monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(FIXTURES))
    first_block_cells = [c for c in cr.raw_table_cells("Daren-1997-Canopy", ["b:0119"], papers_root=FIXTURES)
                         if any(ch.isdigit() for ch in c)]
    rows = [TableRowGroup(row_group_id=f"r{i}", factor_values={"k": str(i)}, source_table_anchor="b:0119", cells={"v": text})
            for i, text in enumerate(first_block_cells)]

    def classify(anchors):
        return TableClassification(
            applicable=True, table_anchors=anchors,
            value_columns=[TableValueColumn(value_column_id="v", variable_name_hint="yield")], row_groups=rows,
        )

    # Old blind spot: judged against b:0119 alone, a first-block-only reconstruction looks complete.
    assert orchestrator._table_classification_sanity_check(classify(["b:0119"]), "Daren-1997-Canopy") is None
    # Judged against the whole chain (b:0119 + b:0178), the same rows are flagged as dropping real data.
    message = orchestrator._table_classification_sanity_check(classify(["b:0119", "b:0178"]), "Daren-1997-Canopy")
    assert message and "accounts for only" in message
