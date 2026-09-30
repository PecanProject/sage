"""Stage 3: the Canonical Document Map and the Evidence Index.

Synthetic fixtures pin the rules; the real-paper tests (skipped when the prepared papers are not on disk -- they are
not in git) pin the behaviour on the regression corpus, each for a failure seen there:
  - Philippe's thinning (b:0030) was never enumerated as a Management event -> the management detector finds it;
  - Kathryn's "36˚37´N" / Paul's "35°3' N" coordinates -> the coordinate detector finds every spelling;
  - Smukler's "#### Table 3" header / bare "*Table 7*" label and Kathryn's caption inside a Picture block -> caption links;
  - Berntson's Table 1 caption sits five prose blocks before its (flattened) table -> still linked;
  - "Summary and Conclusions" is the conclusions, not the abstract (Smukler).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.document_map import build_document_map
from pipeline.evidence_index import build_evidence_index, normalize

PAPERS = Path(__file__).resolve().parents[1] / "paper"


def _write_paper(root: Path, paper_id: str, blocks: list[tuple[str, str, str, int]]) -> None:
    """blocks: (anchor, block_type, text, page_index)."""
    d = root / paper_id
    d.mkdir(parents=True)
    (d / "content.md").write_text("".join(f"{text}\n⟦{anchor}⟧\n\n" for anchor, _t, text, _p in blocks), encoding="utf-8")
    (d / "provenance.json").write_text(json.dumps({
        anchor: {"block_type": btype, "page_id": f"page_{page}", "section_path": []} for anchor, btype, _x, page in blocks
    }), encoding="utf-8")


@pytest.fixture
def toy(tmp_path):
    _write_paper(tmp_path, "toy", [
        ("b:0001", "SectionHeader", "# A study of beech saplings", 0),
        ("b:0002", "Text", "A. Author and B. Author", 0),
        ("b:0003", "SectionHeader", "## Introduction", 0),
        ("b:0004", "Text", "Leaf area index (LAI) matters. Abbreviations: N a, leaf nitrogen per unit area; DM, dry matter.", 0),
        ("b:0005", "SectionHeader", "## Material and methods", 1),
        ("b:0006", "Text", "Study site", 1),
        ("b:0007", "Text", "The stand (45°42′ N, 2°58′ E) was thinned to 500 stems ha –1 and saplings were planted in November 2000.", 1),
        ("b:0008", "Text", "LAI was measured with a LI-COR LAI-2000 leaf area analyzer (LI-COR, Lincoln, NE).", 1),
        ("b:0009", "SectionHeader", "#### Table 3", 2),
        ("b:0010", "Text", "Mean values by class, averaged across years.", 2),
        ("b:0011", "Table", "| Class | LAI |\n|---|---|\n| A | 2.8 |", 2),
        ("b:0012", "SectionHeader", "## Results", 2),
        ("b:0013", "Text", "LAI differed between classes (Table 3; Fig. 1).", 2),
        ("b:0014", "Figure", "*[Figure — image data captured separately; not rendered as text. Caption: Figure 1. LAI over time.]*", 3),
        ("b:0015", "SectionHeader", "#### Summary and Conclusions", 3),
        ("b:0016", "Text", "Thinning raised LAI.", 3),
        ("b:0017", "SectionHeader", "## References", 3),
        ("b:0018", "ListItem", "Smith, J. 1990. Leaf area index of forests (Table 1).", 3),
    ])
    return build_document_map("toy", tmp_path)


# --------------------------------------------------------------------------- #
# Document Map rules
# --------------------------------------------------------------------------- #

def test_regions_follow_the_headings_in_reading_order(toy):
    region = {b.anchor: b.region for b in toy.blocks}
    assert region["b:0002"] == "front_matter"
    assert region["b:0004"] == "introduction"
    assert region["b:0007"] == region["b:0008"] == "methods"
    assert region["b:0013"] == "results"
    assert region["b:0016"] == "conclusions"        # "Summary and Conclusions" is not the abstract
    assert region["b:0018"] == "references"


def test_a_short_run_in_heading_names_the_section_but_does_not_change_the_region(toy):
    site = toy.block("b:0007")
    assert (site.section_title, site.section_anchor, site.region) == ("Study site", "b:0006", "methods")


def test_a_bare_table_header_takes_the_following_text_as_its_caption(toy):
    [table] = toy.tables
    assert table.label == "Table 3" and table.anchors == ("b:0011",)
    assert table.caption_anchors == ("b:0009", "b:0010")
    assert "averaged across years" in table.caption_text


def test_cross_references_resolve_to_their_table_and_figure_and_skip_the_reference_list(toy):
    refs = {(r.from_anchor, r.label): r.target_anchors for r in toy.cross_refs}
    assert refs[("b:0013", "Table 3")] == ("b:0011",)
    assert refs[("b:0013", "Figure 1")] == ("b:0014",)
    assert not any(r.from_anchor == "b:0018" for r in toy.cross_refs)


def test_neighbors_skip_non_prose_blocks(toy):
    assert [b.anchor for b in toy.neighbors("b:0007", before=1, after=1)] == ["b:0006", "b:0008"]


# --------------------------------------------------------------------------- #
# Evidence Index rules
# --------------------------------------------------------------------------- #

def test_typed_detectors_find_coordinates_management_and_instruments(toy):
    index = build_evidence_index(toy)
    assert index.signal_hits("coordinate") == ["b:0007"]
    assert set(index.signals["b:0007"]["management_verb"]) >= {"thinned", "planted"}
    assert "b:0008" in index.signal_hits("instrument")
    assert index.signal_hits("doi") == []


def test_search_is_tiered_and_every_hit_says_why(toy):
    index = build_evidence_index(toy)
    hits = index.search(phrases=["LAI-2000 leaf area analyzer"], signals=["instrument"], regions=["methods"])
    assert hits[0].anchor == "b:0008" and hits[0].tier == 1
    assert any(r.startswith("phrase:") for r in hits[0].reasons) and "region:methods" in hits[0].reasons
    assert all(h.tier >= hits[0].tier for h in hits)


def test_the_reference_list_is_excluded_by_default(toy):
    index = build_evidence_index(toy)
    assert "b:0018" not in {h.anchor for h in index.search(phrases=["leaf area index"])}


def test_terminology_links_abbreviations_both_ways(toy):
    index = build_evidence_index(toy)
    assert "leaf area index" in index.aliases("LAI")
    assert "lai" in index.aliases("leaf area index")
    assert "leaf nitrogen per unit area" in index.aliases("Na")     # "N a" (a rendered $N_a$) keyed as "na"
    assert "dry matter" in index.aliases("DM")


def test_search_uses_the_grounding_notation_normalisation():
    assert normalize("$V_{\\text{cmax}}$") == normalize("Vcmax")
    assert normalize("36˚37´N") == normalize("36°37′N")


# --------------------------------------------------------------------------- #
# Real papers (skipped when not prepared on this machine)
# --------------------------------------------------------------------------- #

def _real(paper_id: str):
    if not (PAPERS / paper_id / "content.md").is_file():
        pytest.skip(f"{paper_id} is not prepared on this machine")
    return build_document_map(paper_id, PAPERS)


def test_philippe_tables_captions_and_the_thinning_sentence():
    dmap = _real("Philippe-2007-Six")
    assert [(t.label, t.anchors, t.caption_anchors) for t in dmap.tables] == [
        ("Table 1", ("b:0053",), ("b:0052",)), ("Table 2", ("b:0115",), ("b:0114",)), ("Table 3", ("b:0193",), ("b:0192",))]
    index = build_evidence_index(dmap)
    assert "b:0030" in {h.anchor for h in index.search(phrases=["thinned"])}
    assert index.signal_hits("coordinate") == ["b:0028"]
    assert "maximum leaf carboxylation rate" in index.aliases("Vcmax")


@pytest.mark.parametrize("paper_id, anchor", [("Kathryn-2020-Winter", "b:0035"), ("Paul-1998-Foliar", "b:0023")])
def test_every_coordinate_spelling_is_detected(paper_id, anchor):
    assert build_evidence_index(_real(paper_id)).signal_hits("coordinate") == [anchor]


def test_every_smukler_table_caption_shape_is_linked():
    dmap = _real("Smulker-2012-Assessment")
    captions = {t.label: t.caption_anchors for t in dmap.tables}
    assert captions["Table 3"] == ("b:0204", "b:0205")      # "#### Table 3" + its text
    assert captions["Table 7"] == ("b:0601", "b:0602")      # "*Table 7*" + its text
    assert dmap.block("b:0610").region == "conclusions"      # "#### Summary and Conclusions"


def test_a_caption_inside_a_picture_and_a_distant_caption_are_linked():
    assert _real("Kathryn-2020-Winter").table_by_label("Table 2").caption_anchors == ("b:0099",)
    assert _real("Berntson-1997-Regenerating").table_by_label("Table 1").caption_anchors == ("b:0047",)


def test_page_split_tables_are_one_logical_table():
    tables = {t.label: t.anchors for t in _real("Daren-1997-Canopy").tables}
    assert tables["Table 2"] == ("b:0119", "b:0178") and tables["Table 6"] == ("b:0607", "b:0656")
