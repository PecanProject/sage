"""Stage 4: ContextBundle (evidence packet) + coverage audit + unresolved-cause taxonomy.

The packet is deterministic, anchored and verbatim; it states per concept whether the evidence was FOUND, present but
NOT_RETRIEVED, NOT_FOUND_AFTER_FULL_SEARCH (not proof of absence) or ABSENT_BY_PATTERN (proven by a whole-document scan).
Real-paper tests (skipped when the prepared papers are not on disk) pin the evidence each regression failure lacked.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from pipeline import causes
from pipeline.context_bundle import (
    ABSENT_BY_PATTERN, FOUND, NOT_FOUND_AFTER_FULL_SEARCH, NOT_RETRIEVED, build_context_bundle,
)
from test_document_map import _write_paper

PAPERS = Path(__file__).resolve().parents[1] / "paper"


@pytest.fixture
def paper(tmp_path):
    _write_paper(tmp_path, "toy", [
        ("b:0001", "SectionHeader", "# Beech saplings under pine", 0),
        ("b:0002", "SectionHeader", "## Material and methods", 1),
        ("b:0003", "Text", "Study site", 1),
        ("b:0004", "Text", "Measurements were performed in a pine stand near Clermont, France (45°42′ N, 2°58′ E), "
                           "on an Andisol with a mean annual rainfall of 820 mm.", 1),
        ("b:0005", "Text", "Saplings were planted in November 2000 in a stand that had been thinned to 500 stem ha –1.", 1),
        ("b:0006", "Text", "Vcmax was measured with a LI-6400 gas-exchange analyzer (Li-Cor, Lincoln, NE).", 1),
        ("b:0007", "Caption", "*Table 1. Mean Vcmax (µmol m–2 s–1) by light class.*", 2),
        ("b:0008", "Table", "| Class | Vcmax (µmol m–2 s–1) |\n|---|---|\n| low | 27.6 a |", 2),
        ("b:0009", "SectionHeader", "## Results", 2),
        ("b:0010", "Text", "Vcmax increased with light (Table 1).", 2),
    ])
    return tmp_path


def _status(bundle, concept):
    return next((c.status for c in bundle.coverage if c.concept == concept), None)


def _bundle(root, entity_type, task="extraction", **kw):
    return build_context_bundle("toy", entity_type, task, papers_root=root, **kw)


# --------------------------------------------------------------------------- #
# Ladder, provenance, immutability
# --------------------------------------------------------------------------- #

def test_the_seed_is_primary_evidence_and_its_neighbours_are_level_one(paper):
    bundle = _bundle(paper, "Site", seed_anchors=["b:0004"])
    levels = {i.anchor: (i.level, i.evidence_type) for i in bundle.items}
    assert levels["b:0004"] == (0, "primary")
    assert levels["b:0003"][0] == 1 and levels["b:0005"][0] == 1
    assert all(i.reasons for i in bundle.items)                       # every item says why it was taken


def test_items_are_verbatim_blocks_with_their_anchor_page_and_region(paper):
    bundle = _bundle(paper, "Site", seed_anchors=["b:0004"])
    item = next(i for i in bundle.items if i.anchor == "b:0004")
    assert item.text.startswith("Measurements were performed in a pine stand") and item.region == "methods"
    assert item.page == 2 and item.section == "Study site"


def test_a_bundle_is_immutable_and_its_id_is_deterministic(paper):
    a = _bundle(paper, "Site", seed_anchors=["b:0004"])
    b = _bundle(paper, "Site", seed_anchors=["b:0004"])
    assert a.bundle_id == b.bundle_id
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.target = "x"
    assert _bundle(paper, "Site", seed_anchors=["b:0006"]).bundle_id != a.bundle_id
    json.dumps(a.to_dict())                                             # serialisable as a run artifact


def test_linked_tables_come_with_caption_header_and_the_prose_that_refers_to_them(paper):
    bundle = _bundle(paper, "Method", target="Vcmax")
    types = {i.anchor: (i.level, i.evidence_type) for i in bundle.items}
    assert types["b:0006"][1] == "supporting"
    assert types["b:0007"][1] == "caption" and types["b:0007"][0] <= 3    # a caption keeps its role at any level
    assert types["b:0008"] == (3, "table_header")
    assert types["b:0010"] == (4, "cross_reference")


# --------------------------------------------------------------------------- #
# Coverage audit
# --------------------------------------------------------------------------- #

def test_coverage_states_found_absent_by_pattern_and_not_found(paper):
    site = _bundle(paper, "Site", seed_anchors=["b:0004"])
    assert _status(site, "coordinates") == FOUND and _status(site, "soil") == FOUND
    assert _status(site, "elevation") == ABSENT_BY_PATTERN          # a whole-document scan fired nowhere
    species = _bundle(paper, "Species", task="enumeration")
    assert _status(species, "scientific_names") == ABSENT_BY_PATTERN
    crop = _bundle(paper, "Crop", task="enumeration")
    assert _status(crop, "cultivars") == NOT_FOUND_AFTER_FULL_SEARCH  # a phrase concept: absence is never "proven"


def test_evidence_cut_by_the_budget_is_not_retrieved_never_absent(paper):
    bundle = _bundle(paper, "Site", seed_anchors=["b:0006"], max_chars=150)
    assert _status(bundle, "coordinates") == NOT_RETRIEVED
    assert "b:0004" in next(c.anchors for c in bundle.coverage if c.concept == "coordinates")


def test_a_target_conditioned_concept_needs_the_target_in_the_same_block(paper):
    bundle = _bundle(paper, "Variable", target="Vcmax")
    units = next(c for c in bundle.coverage if c.concept == "units")
    assert units.status == FOUND and "b:0004" not in units.anchors  # "820 mm" is a unit, but not Vcmax's


def test_the_rendered_packet_states_absence_only_when_it_is_proven(paper):
    text = _bundle(paper, "Site", seed_anchors=["b:0004"]).render()
    assert "[b:0004]" in text and "45°42′ N" in text
    assert "elevation: NOT STATED anywhere in the paper" in text
    crop = _bundle(paper, "Crop", task="enumeration").render()
    assert "NOT STATED" not in crop and "not found by the pipeline's search" in crop


def test_a_figure_reference_next_to_the_evidence_links_no_table(tmp_path):
    # Kathryn-2020-Winter: prose citing a figure crashed table linking (a figure has no table anchors).
    _write_paper(tmp_path, "toy", [
        ("b:0001", "SectionHeader", "## Methods", 0),
        ("b:0002", "Text", "Leaf area was measured with a scanner (Fig. 1).", 0),
        ("b:0003", "Figure", "*[Figure — image data captured separately; not rendered as text. Caption: Figure 1. Leaf area.]*", 0),
    ])
    bundle = build_context_bundle("toy", "Method", "extraction", seed_anchors=["b:0002"], papers_root=tmp_path)
    assert "b:0002" in bundle.anchors and not any(i.evidence_type == "table_header" for i in bundle.items)


def test_entity_types_without_a_strategy_get_no_packet(paper):
    assert _bundle(paper, "Citation") is None and _bundle(paper, "Observation") is None


# --------------------------------------------------------------------------- #
# Real papers
# --------------------------------------------------------------------------- #

def _real(paper_id, entity_type, task, **kw):
    if not (PAPERS / paper_id / "content.md").is_file():
        pytest.skip(f"{paper_id} is not prepared on this machine")
    return build_context_bundle(paper_id, entity_type, task, papers_root=PAPERS, **kw)


def test_philippe_management_packet_contains_the_thinning_event():
    bundle = _real("Philippe-2007-Six", "Management", "enumeration")
    assert "b:0030" in bundle.anchors and _status(bundle, "thinning") == FOUND


def test_variable_enumeration_is_table_first():
    bundle = _real("Philippe-2007-Six", "Variable", "enumeration")
    headers = {i.anchor for i in bundle.items if i.evidence_type == "table_header"}
    assert headers == {"b:0053", "b:0115", "b:0193"}                  # Table 3 carries Ma, missed twice before
    assert "b:0177" in _real("Kathryn-2020-Winter", "Variable", "enumeration").anchors


def test_the_method_packet_for_vcmax_leads_with_its_procedure_and_instrument():
    bundle = _real("Philippe-2007-Six", "Method", "extraction", target="Vcmax")
    assert _status(bundle, "instrument") == FOUND and "b:0044" in bundle.anchors


def test_coordinates_found_or_proven_absent_across_the_corpus():
    assert _status(_real("Kathryn-2020-Winter", "Site", "enumeration"), "coordinates") == FOUND
    assert _status(_real("Daren-1997-Canopy", "Site", "enumeration"), "coordinates") == ABSENT_BY_PATTERN


def test_felipe_species_packet_contains_the_tomato_binomial():
    assert "b:0026" in _real("Felipe-2010-Cultivar", "Species", "enumeration").anchors


# --------------------------------------------------------------------------- #
# Unresolved-cause taxonomy
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("status, detail, reason, coverage, entity_type, expected", [
    ("ready", {}, None, None, "Site", None),
    ("blocked", {}, "required prerequisite Site is not 'ready'", None, "Treatment", causes.BLOCKED_PREREQUISITE),
    ("blocked", {}, "L1 (current-IR limitation, not an extraction failure): ...", None, "Observation", causes.SCHEMA_LIMITATION),
    ("error", {"failure_kind": "provider", "message": "x"}, None, None, "Site", causes.PROVIDER_FAILURE),
    ("error", {"message": "attempt 4: raw evidence grounding failed: [...]"}, None, None, "Variable", causes.GROUNDING_FAILURE),
    ("error", {"message": "attempt 4: RawExtraction shape validation failed"}, None, None, "Species", causes.VALIDATION_FAILURE),
    ("unresolved", {"last_errors": [{"message": "method_id is ambiguous among 8 equally-valid candidates"}]}, None, None,
     "Observation", causes.AMBIGUOUS),
    ("unresolved", {"unresolved_by": "ai_validation", "last_errors": [{"message": "AI Validator concern: x"}]}, None, None,
     "Citation", causes.AI_CONCERN),
    ("unresolved", {"last_errors": [{"message": "latitude: reported_text '35°03' N' is not found in the cited block(s)"}]},
     None, None, "Site", causes.GROUNDING_FAILURE),
    ("unresolved", {"readiness_issues": [{"code": "variable_name_unresolved", "message": "name UNRESOLVED"}]}, None,
     {"definition": "NOT_RETRIEVED"}, "Variable", causes.NOT_RETRIEVED),
    ("unresolved", {"readiness_issues": [{"code": "variable_name_unresolved", "message": "name UNRESOLVED"}]}, None,
     {"definition": "FOUND"}, "Variable", causes.AMBIGUOUS),
    ("unresolved", {"readiness_issues": [{"code": "observation_value_unresolved",
                                          "message": "reports a directional difference without providing a numeric value"}]},
     None, None, "Observation", causes.ABSENT),
])
def test_every_non_ready_outcome_gets_one_cause(status, detail, reason, coverage, entity_type, expected):
    assert causes.classify(status, detail, reason, coverage, entity_type) == expected
