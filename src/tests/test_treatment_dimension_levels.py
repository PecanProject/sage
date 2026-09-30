"""The free-form Treatment dimension check tolerates a decorated level, and stays grounded.

Real evidence (run felipe_smoke_20260920T132343, Felipe-2010-Cultivar): with Table 1 covering Treatment, the free-form
enumeration was asked to declare each candidate's `dimensions`. Its first answer was semantically right -- three
mixture-level Treatments, which the paper defines as "cultivar mixtures (subplot treatments) ... (1-cv) ... (3-cv) ...
(5-cv)" -- but each level was written with a decoration: "1-cv (choice cultivar only)", "3-cv (choice cultivar + CXD-179 +
H-8892)", "5-cv (all five cultivars)". `_candidate_dimension_errors` demanded the WHOLE string in the text, rejected all
three, and on the retry the model returned no candidates at all: the mixture Treatments were lost, and the mixture-level
Observations (fruit hue by 1/3/5-cv) were later refused for want of a Treatment.

Now a level is supported when it, or the level WITHOUT its qualifier, is in the description or a cited block. Nothing is
loosened otherwise: the level must still be in the text, a level of only generic words ("mixture") is never supported, and
no word of the level itself is dropped.

Fixtures (real): tests/fixtures/pass2/felipe_treatment_enumeration_attempt1.json (the real rejected answer) and the
trimmed real Felipe content.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import orchestrator
from pipeline.raw_schema import CandidateDimension, EnumerationCandidate, EnumerationResult
from pipeline.validators import _load_rendered_blocks

FIXTURES = Path(__file__).parent / "fixtures" / "pass2"
PAPER = "Felipe-2010-Cultivar"


@pytest.fixture()
def blocks(monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(FIXTURES))
    return _load_rendered_blocks(PAPER)


def _real_candidates() -> list[EnumerationCandidate]:
    real = json.loads((FIXTURES / "felipe_treatment_enumeration_attempt1.json").read_text())
    return EnumerationResult.model_validate(real["parsed_json"]).candidates


def _candidate(level: str, description: str = "A mixture treatment.", anchors=("b:0030",)) -> EnumerationCandidate:
    return EnumerationCandidate(
        candidate_id="c", description=description, anchors=list(anchors),
        dimensions=[CandidateDimension(name="Cultivar mixture", dimension="treatment", level=level)],
    )


def test_the_real_rejected_answer_was_rejected_by_the_old_whole_string_rule(blocks):
    real = json.loads((FIXTURES / "felipe_treatment_enumeration_attempt1.json").read_text())
    assert [e["field"] for e in real["validation_errors"]] == ["dimensions"] * 3   # what the run recorded


def test_the_real_felipe_mixture_levels_now_validate_against_the_real_source(blocks):
    candidates = _real_candidates()
    assert [c.dimensions[0].level for c in candidates] == [
        "1-cv (choice cultivar only)", "3-cv (choice cultivar + CXD‑179 + H‑8892)", "5-cv (all five cultivars)"]
    errors, bad = orchestrator._candidate_dimension_errors(candidates, blocks)
    assert errors == [] and bad == set()
    assert "(1-cv)" in blocks["b:0030"] and "(3-cv)" in blocks["b:0030"] and "(5-cv)" in blocks["b:0030"]   # grounded in the source


@pytest.mark.parametrize("level", ["1-cv", "1-CV", "1 cv", "1-cv (choice cultivar only)", "1-cv: the monoculture", "1-cv, one cultivar"])
def test_a_level_the_source_writes_is_accepted_however_it_is_decorated(blocks, level):
    assert orchestrator._candidate_dimension_errors([_candidate(level)], blocks)[0] == []


@pytest.mark.parametrize("level", [
    "2-cv (two cultivars)",          # a level the paper never defines: the decoration does not make it real
    "7-cv",
    "1-cv-plus",                     # a different token, not a decorated 1-cv
    "monoculture cv",                # words scattered from the text are not a level of it
])
def test_a_level_the_source_does_not_write_is_still_rejected(blocks, level):
    errors, bad = orchestrator._candidate_dimension_errors([_candidate(level)], blocks)
    assert bad == {"c"} and "appears neither" in errors[0]["message"]


@pytest.mark.parametrize("level", ["mixture", "Mixture (1-cv)", "treatment", "cultivars (all five)"])
def test_the_word_mixture_alone_is_never_a_supported_level(blocks, level):
    """Never infer a Treatment merely from the word 'mixture': a level made only of generic words is not supported --
    even though 'mixture' occurs in the very block that is cited."""
    assert "mixture" in blocks["b:0030"].lower()
    if level == "Mixture (1-cv)":        # ...but a decorated SPECIFIC level keeps its specific part
        assert orchestrator._candidate_dimension_errors([_candidate(level)], blocks)[0] != []   # 'mixture 1 cv' is not in the text
        return
    errors, bad = orchestrator._candidate_dimension_errors([_candidate(level)], blocks)
    assert bad == {"c"}


def test_the_level_may_be_supported_by_the_candidates_own_description(blocks):
    c = _candidate("5-cv (all five cultivars)", description="The 5-cv treatment includes all five cultivars.", anchors=("b:0001",))
    assert orchestrator._candidate_dimension_errors([c], blocks)[0] == []


def test_a_level_is_not_supported_by_a_block_that_is_not_cited(blocks):
    assert "(5-cv)" in blocks["b:0030"]
    assert orchestrator._candidate_dimension_errors([_candidate("5-cv (x)", anchors=("b:0001",))], blocks)[1] == {"c"}


def test_missing_dimensions_are_still_an_error(blocks):
    no_dims = EnumerationCandidate(candidate_id="c", description="d", anchors=["b:0030"], dimensions=[])
    errors, bad = orchestrator._candidate_dimension_errors([no_dims], blocks)
    assert bad == {"c"} and "declares no dimensions" in errors[0]["message"]


def test_the_helper_forms_remove_only_the_decoration():
    assert orchestrator._level_forms("1-cv (choice cultivar only)") == ["1 cv choice cultivar only", "1 cv"]
    assert orchestrator._level_forms("Fallow") == ["fallow"]
    assert orchestrator._level_forms("mixture") == []
    assert orchestrator._level_forms("3-cv (choice cultivar + CXD‑179 + H‑8892)")[-1] == "3 cv"
