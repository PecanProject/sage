"""The paper-level Variable -> Method map.

Real failure: every Observation of Philippe (118/118), Kathryn (12/12), Felipe (23/24) and Smukler (198/198) was
unresolved because `method_id` was ambiguous. The map links a variable to its Method once, with evidence, and refuses
otherwise. Each negative test is a wrong link measured on the corpus while this was built (so it can never come back):
  - Kathryn: the hint "TruSpec CN analyzer" matched the SHIMADZU analyzer on the generic words "C/N analyzer";
  - Smukler: "Volume" was linked to bulk density because "...rings of 345 cm3 volume" contains the word;
  - Smukler: the year "2005" was treated as a variable;
  - Felipe / Kathryn: a positional tie-break in a shared paragraph linked "Total fruit" to the nitrogen analysis.
"""

from __future__ import annotations

import pytest

from pipeline import method_map as mm
from pipeline.document_map import build_document_map
from pipeline.evidence_index import build_evidence_index
from pipeline.raw_schema import TableClassification, TableRowGroup, TableValueColumn, TableVariable
from test_document_map import _write_paper


@pytest.fixture
def index(tmp_path):
    _write_paper(tmp_path, "p", [
        ("b:0001", "SectionHeader", "## Methods", 0),
        ("b:0002", "Text", "Stem basal diameter was measured with a calliper rule at the end of each season.", 0),
        ("b:0003", "Text", "Leaf C and N were determined by combustion on a TruSpec CN analyzer (LECO Corp.). Soil extracts "
                           "were analyzed on a Shimadzu TOC/VCPH-TN C/N analyzer.", 0),
        ("b:0004", "Text", "Bulk density was determined using rings of 345 cm 3 volume to remove intact soil cores.", 0),
        ("b:0005", "Text", "Vcmax and Jmax were derived from A/Ci curves measured with a LI-6400 gas-exchange analyzer. "
                           "Leaves were then dried and N was quantified with an elemental micro-analyzer.", 0),
        ("b:0006", "Text", "Compost for the 2005 crop was applied before the experiment started.", 0),
    ])
    return build_evidence_index(build_document_map("p", tmp_path))


def _methods(**specs):
    return {rid: {"slug": rid, "name": name, "description": desc, "anchors": anchors}
            for rid, (name, desc, anchors) in specs.items()}


METHODS = _methods(
    caliper=("Stem basal diameter", "measured with a calliper rule", ["b:0002"]),
    combustion=("Combustion analysis", "determined by combustion on a TruSpec CN analyzer", ["b:0003"]),
    shimadzu=("Shimadzu TOC/VCPH-TN C/N analyzer", "Soil extracts were analyzed", ["b:0003"]),
    rings=("Soil bulk density", "rings of 345 cm3", ["b:0004"]),
    gas_exchange=("LI-6400 gas-exchange analyzer", "A/Ci curves", ["b:0005"]),
    nitrogen=("Elemental analysis", "N was quantified with an elemental micro-analyzer", ["b:0005"]),
)


def _entry(label, name=None, hints=()):
    return mm.VariableEntry(key=mm.variable_key(label), label=label, name=name, hints=list(hints))


# --------------------------------------------------------------------------- #
# Tier 1 -- hints by distinctive words
# --------------------------------------------------------------------------- #

def test_a_hint_links_by_its_distinctive_words_never_the_generic_ones():
    link = mm.hint_tier(_entry("C:N", hints=["TruSpec CN analyzer"]), METHODS)
    assert (link.status, link.method_slug) == (mm.LINKED, "combustion")      # not "shimadzu" ("C/N analyzer")


def test_a_hint_of_generic_words_links_nothing():
    assert mm.hint_tier(_entry("x", hints=["measured with an analyzer"]), METHODS).status == mm.NONE


# --------------------------------------------------------------------------- #
# Tier 2 -- the variable is what a measurement verb applies to, in the Method's own evidence
# --------------------------------------------------------------------------- #

def test_a_sentence_stating_the_variable_was_measured_links_it(index):
    link = mm.evidence_tier(_entry("Stem basal diameter (mm)"), METHODS, index)
    assert (link.status, link.method_slug, link.tier) == (mm.LINKED, "caliper", "evidence")
    assert "was measured with a calliper" in link.reason


def test_a_word_that_merely_occurs_in_the_sentence_is_not_evidence(index):
    assert mm.evidence_tier(_entry("Volume (mm event-1)"), METHODS, index).status == mm.NONE


def test_several_methods_sharing_the_evidence_is_ambiguous_never_a_positional_guess(index):
    link = mm.evidence_tier(_entry("Vcmax (µmol m-2 s-1)"), METHODS, index)
    assert link.status == mm.AMBIGUOUS and set(link.candidates) == {"gas_exchange", "nitrogen"}


def test_a_year_or_a_level_is_not_a_variable(index):
    links = mm.build_map([_entry("2005"), _entry("2006 – Fallow")], METHODS, index, None)
    assert all(l.status == mm.NONE and "not a variable" in l.reason for l in links.values())


# --------------------------------------------------------------------------- #
# Tier 3 -- the model's links are verified
# --------------------------------------------------------------------------- #

def _ask(links):
    calls = []

    def ask(prompt):
        calls.append(prompt)
        return {"links": links}

    return ask, calls


def test_a_verified_model_link_is_used(index):
    ask, calls = _ask([{"variable": "Vcmax (µmol m-2 s-1)", "status": "linked", "method": "gas_exchange", "anchor": "b:0005"}])
    links = mm.build_map([_entry("Vcmax (µmol m-2 s-1)")], METHODS, index, None, ask)
    link = links[mm.variable_key("Vcmax")]
    assert (link.status, link.method_slug, link.tier) == (mm.LINKED, "gas_exchange", "model") and len(calls) == 1


@pytest.mark.parametrize("proposal, why", [
    ({"method": "gas_exchange", "anchor": "b:0002"}, "does not mention"),          # block does not name the variable
    ({"method": "unknown", "anchor": "b:0005"}, "unknown method"),
    ({"method": "caliper", "anchor": "b:0005"}, "neither"),                        # not the method's block, not its name
    ({"method": "gas_exchange", "anchor": "b:9999"}, "no real block"),
])
def test_an_unverifiable_model_link_is_ambiguous_never_used(index, proposal, why):
    ask, _ = _ask([{"variable": "Vcmax (µmol m-2 s-1)", "status": "linked", **proposal}])
    link = mm.build_map([_entry("Vcmax (µmol m-2 s-1)")], METHODS, index, None, ask)[mm.variable_key("Vcmax")]
    assert link.status == mm.AMBIGUOUS and why in link.reason


def test_the_model_is_not_asked_when_nothing_is_left_to_decide(index):
    ask, calls = _ask([])
    mm.build_map([_entry("Stem basal diameter (mm)")], METHODS, index, None, ask)
    assert calls == []


def test_the_prompt_carries_the_papers_aliases(tmp_path):
    _write_paper(tmp_path, "q", [("b:0001", "Text", "Abbreviations: N a, leaf nitrogen concentration per unit area.", 0)])
    index = build_evidence_index(build_document_map("q", tmp_path))
    prompt = mm.map_prompt([_entry("Na (g m-2)")], METHODS, None, index)
    assert "leaf nitrogen concentration per unit area" in prompt


# --------------------------------------------------------------------------- #
# Integration: a table cell inherits its variable's link
# --------------------------------------------------------------------------- #

def test_a_cell_the_matcher_cannot_resolve_inherits_the_maps_link():
    from pipeline import orchestrator

    classification = TableClassification(
        applicable=True, table_role="treatment_response", table_anchors=["b:0010"],
        variables=[TableVariable(label="Vcmax (µmol m-2 s-1)", variable_name="maximum carboxylation rate")],
        value_columns=[TableValueColumn(value_column_id="v", variable="Vcmax (µmol m-2 s-1)")],
        row_groups=[TableRowGroup(row_group_id="r1", factor_values={"PAR": "low"}, source_table_anchor="b:0010", cells={"v": "27.6"})],
    )
    pool = {"method_id": [{"slug": "gas_exchange", "record_id": "m1", "name": "LI-6400", "description": "A/Ci"},
                          {"slug": "nitrogen", "record_id": "m2", "name": "Elemental analysis", "description": "N"}]}
    without = orchestrator._table_classification_to_candidates(classification, pool)
    assert "method_id" not in without[0].linked_candidates
    links = {mm.variable_key("Vcmax"): mm.MethodLink(mm.LINKED, "model", "gas_exchange", "m1")}
    with_map = orchestrator._table_classification_to_candidates(classification, pool, links)
    assert with_map[0].linked_candidates["method_id"] == "gas_exchange"


def test_a_hint_that_cites_its_block_is_grounded_by_that_blocks_distinctive_words(tmp_path, monkeypatch):
    from pipeline import orchestrator

    _write_paper(tmp_path, "h", [("b:0038", "Text", "Leaves were recorded with a 3D-digitizing technique and software Pol95.", 0)])
    monkeypatch.setenv("IR_PAPERS_ROOT", str(tmp_path))
    assert orchestrator._hint_grounded_in_cited_block("3D‑digitizing technique (Pol95 software) – see b:0038", "h")
    assert not orchestrator._hint_grounded_in_cited_block("measured with an analyzer – see b:0038", "h")
    assert not orchestrator._hint_grounded_in_cited_block("3D-digitizing technique (Pol95 software)", "h")   # cites nothing
