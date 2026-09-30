"""The always-on free-form Treatment guidance (`_ENTITY_IDENTITY_GUIDANCE["Treatment"]`) must agree with
decision Q1: a population/cultivar/genotype is CROP, a growth stage/maturity/date is TIME, a location is
SITE -- none becomes a Treatment merely because they are combined in a phrase. It used to teach the
opposite with a Daren example ("'Trailblazer at Ames' and 'Trailblazer at Mead' are two candidates"),
which is what pushed free-form enumeration to mint population x site Treatments in papers where no
table provides Treatment coverage. Valid Treatment examples (ambient/elevated CO2, cover-cropping
systems, a treatment level applied at several sites, mixture levels under Q2) must remain.
"""

from __future__ import annotations

from pipeline import orchestrator
from pipeline.raw_schema import DESIGN_FACTOR_RULE, MIXTURE_LEVEL_RULE

GUIDANCE = orchestrator._ENTITY_IDENTITY_GUIDANCE["Treatment"]
# the prompt a Treatment enumeration gets when NO table provides Treatment coverage
NO_TABLE_PROMPT = orchestrator._enumeration_prompt("p", "Treatment")


def test_the_old_population_by_site_treatment_example_is_gone():
    for stale in ("Trailblazer", "switchgrass", "Daren", "Ames", "Mead", "six switchgrass populations"):
        assert stale not in GUIDANCE
    assert "'Trailblazer at Ames' and 'Trailblazer at Mead' are two candidates" not in NO_TABLE_PROMPT


def test_the_guidance_states_the_q1_dimension_rule_and_the_replacement_example():
    assert "A cultivar, variety, population or genotype is a CROP" in GUIDANCE
    assert "a growth stage, maturity, date or year is TIME" in GUIDANCE
    assert "a location is a SITE" in GUIDANCE
    assert "none of them is a Treatment, and naming them together in one phrase never makes them one" in GUIDANCE
    # the replacement example: the same crop at two sites is one Crop in two Site contexts
    assert "'Population P at Site A' and 'Population P at Site B' are NOT two Treatments" in GUIDANCE
    assert "the same crop (one Crop record) grown in two Site contexts" in GUIDANCE
    assert "'Population P at maturity M' is that same crop at a point in time" in GUIDANCE
    assert "with no experimental treatment level applied -- report no Treatment candidates for them" in GUIDANCE


def test_the_no_table_free_form_prompt_carries_the_corrected_guidance():
    """No table coverage -> no dimension-declaration block; the always-on guidance is all the model gets."""
    assert "dimensions" not in NO_TABLE_PROMPT and GUIDANCE in NO_TABLE_PROMPT
    assert "A cultivar, variety, population or genotype is a CROP" in NO_TABLE_PROMPT
    # other entity types are untouched
    assert "NOT two Treatments" not in orchestrator._enumeration_prompt("p", "Observation")
    assert "NOT two Treatments" not in orchestrator._enumeration_prompt("p", "Variable")


def test_valid_treatment_examples_remain():
    assert "'ambient (375 ppm) and elevated (700 ppm) atmospheric CO2'" in GUIDANCE
    assert "'quadrennial vs. annual cover cropping'" in GUIDANCE
    assert "EACH NAMED LEVEL is a separate Treatment candidate" in GUIDANCE
    # a genuine treatment level applied at several sites is still split per site (Treatment.site_id is required)
    assert "'no-till' applied at both Site A and Site B" in GUIDANCE
    assert "'no-till at Site A' and 'no-till at Site B' are two candidates, not one" in GUIDANCE
    assert "linked_candidates['site_id']" in GUIDANCE and "only correct when the paper genuinely never applies that level" in GUIDANCE
    assert "This applies ONLY to experimental treatment levels" in GUIDANCE


def test_the_approved_mixture_and_q2_granularity_rules_are_unchanged():
    assert "A designed mixture or composition level of several cultivars grown together" in GUIDANCE
    assert "only at the granularity the paper actually reports values or comparisons for" in GUIDANCE
    assert "never multiply it with another factor's levels into combinations the paper does not report" in GUIDANCE
    assert "an individual cultivar" in orchestrator._enumeration_prompt("p", "Treatment", covered_conditions=[], declare_dimensions=True)
    assert MIXTURE_LEVEL_RULE in orchestrator._enumeration_prompt("p", "Treatment", covered_conditions=[], declare_dimensions=True)


def test_the_always_on_guidance_and_the_dimension_block_agree():
    """Both must say that crop/site are not Treatments and time is one only when assigned as a design factor."""
    with_block = orchestrator._enumeration_prompt("p", "Treatment", covered_conditions=[], declare_dimensions=True)
    assert with_block.startswith(NO_TABLE_PROMPT)
    assert "neither is a 'treatment'" in with_block and DESIGN_FACTOR_RULE in with_block
    assert "none of them is a Treatment" in GUIDANCE and "unless the paper assigns it to plots as a design factor" in GUIDANCE
