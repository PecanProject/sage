"""Recorded semantic decision (Felipe-2010-Cultivar): a DESIGNED multi-cultivar mixture level
(1-cv, 3-cv, 5-cv) is a `treatment`-dimension level; each individual cultivar stays `crop`.

Evidence: the paper calls the mixtures "subplot treatments" (b:0030) and states the
mixture-vs-monoculture contrast as its central question (b:0020); protocol Section 6.3 defines
a treatment as an experimental condition or system contrast and keeps cultivar information out of
Treatments. Two live gpt-oss-120b validations classified the same mixtures as `crop` and as
`treatment` because the guidance never mentioned mixtures. Q2 is unchanged: Treatments exist only
at the granularity the extracted evidence supports (Table 1 pools over the mixtures), so the six
Fallow/Mustard x mixture combinations are never manufactured.

Real fixtures: tests/fixtures/item8/ (live gpt-oss outputs) and tests/fixtures/pooling/. Tests
marked SYNTHETIC use invented text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import orchestrator, pooling_evidence as pe, validators
from pipeline.raw_schema import (
    DESIGN_FACTOR_RULE, MIXTURE_LEVEL_RULE, CandidateDimension, EnumerationCandidate, TableClassification, TableFactor,
)

ITEM8 = Path(__file__).parent / "fixtures" / "item8"
POOLING = Path(__file__).parent / "fixtures" / "pooling"
FELIPE = "Felipe-2010-Cultivar"
CULTIVARS = ["AB-2", "CXD-179", "H-2601", "H-8892", "Red Spring"]
CROP_POOL = [{"slug": f"crop_{i}", "name": name} for i, name in enumerate(CULTIVARS)]
PAPER_SPECIFIC = ["daren", "felipe", "tomato", "mustard", "ames", "mead", "trailblazer", "switchgrass", "1-cv", "3-cv", "5-cv", "ab-2"]


def _freeform(name: str) -> list[EnumerationCandidate]:
    return [EnumerationCandidate.model_validate(c) for c in json.loads((ITEM8 / name).read_text())["candidates"]]


def _table1() -> TableClassification:
    return TableClassification.model_validate(json.loads((ITEM8 / "live_gpt_oss_felipe_table1_classification.json").read_text()))


def _dim(name, dimension, level):
    return CandidateDimension(name=name, dimension=dimension, level=level)


def _dedup(freeform, tables, dimension_pools=None):
    return orchestrator._drop_freeform_treatments_covered_by_tables(freeform, tables, None, dimension_pools)


def _table_treatments():
    return orchestrator._table_classifications_to_treatment_candidates([_table1()], {})[0]


# --------------------------------------------------------------------- #
# the guidance: one shared rule, in Step B, free-form and the schema
# --------------------------------------------------------------------- #


def test_step_b_free_form_and_schema_state_the_same_mixture_rule():
    step_b = orchestrator._table_classification_prompt("p", "b:0001", [])
    free_form = orchestrator._enumeration_prompt("p", "Treatment", covered_conditions=[], declare_dimensions=True)
    assert MIXTURE_LEVEL_RULE in step_b and MIXTURE_LEVEL_RULE in free_form
    assert MIXTURE_LEVEL_RULE in TableFactor.model_fields["dimension"].description
    assert "an individual cultivar" in step_b and "an individual cultivar" in free_form
    assert "never infer a treatment merely because the word 'mixture' appears" in MIXTURE_LEVEL_RULE
    assert "when the paper defines it as an experimental treatment or factor" in MIXTURE_LEVEL_RULE


def test_the_free_form_treatment_guidance_carries_the_rule_and_the_q2_granularity_limit():
    guidance = orchestrator._ENTITY_IDENTITY_GUIDANCE["Treatment"]
    assert "designed mixture or composition level of several cultivars" in guidance
    assert "only at the granularity the paper actually reports values or comparisons for" in guidance
    assert "never multiply it with another factor's levels into combinations the paper does not report" in guidance
    assert "mixture" not in orchestrator._ENTITY_IDENTITY_GUIDANCE["Observation"]  # nothing else was touched


def test_the_rules_are_generic_and_the_step_b_dimension_wording_states_the_design_factor_rule():
    for rule in (MIXTURE_LEVEL_RULE, DESIGN_FACTOR_RULE):
        assert not [w for w in PAPER_SPECIFIC if w in rule.lower()]
    step_b = orchestrator._table_classification_prompt("p", "b:0001", [])
    role_section = step_b[step_b.index("- table_role:"): step_b.index("- table_anchors:")].lower()
    assert not [w for w in PAPER_SPECIFIC if w in role_section]
    for expected in ("a location is 'site' -- neither is a 'treatment'", DESIGN_FACTOR_RULE,
                     "'time' (a sampling date, growth stage, year, season or day after planting at which values were measured)"):
        assert expected in step_b


# --------------------------------------------------------------------- #
# free-form Treatment enumeration: mixture levels vs individual cultivars
# --------------------------------------------------------------------- #


def test_real_run_mixture_levels_declared_treatment_are_kept_and_never_converted_to_crop():
    """The live variant run declared 1-cv/3-cv/5-cv as `treatment`; even with the five cultivars as ready
    Crop records, no mixture level is reconciled to crop (only an EXACT cultivar match ever is)."""
    freeform = _freeform("live_gpt_oss_felipe_freeform_variant_run.json")
    mixtures = [c for c in freeform if c.dimensions and c.dimensions[0].name == "cultivar mixture"]
    assert {c.dimensions[0].level for c in mixtures} == {"1-cv", "3-cv", "5-cv"}
    for c in mixtures:
        assert [d.dimension for d in orchestrator._reconcile_candidate_dimensions(c, {"crop": CROP_POOL, "site": []})] == ["treatment"]
    kept, decisions = _dedup(freeform, _table_treatments(), {"crop": CROP_POOL, "site": []})
    by_id = {d["candidate_id"]: d["decision"] for d in decisions}
    assert {by_id["cultivar_1cv"], by_id["cultivar_3cv"], by_id["cultivar_5cv"]} == {"kept_new"}
    assert by_id["fallow"] == "dropped_covered"  # the table's own Treatment, still deduplicated as before
    assert all(c.candidate_id.startswith("cultivar_") or c.candidate_id == "mustard_cover_crop" for c in kept)


def test_individual_cultivars_stay_crop_and_are_never_a_treatment():
    named = _dim("Cultivar", "crop", "AB-2")
    declared_treatment = _dim("Cultivar", "treatment", "AB-2")  # a model that files a single cultivar under `treatment`
    as_crop = EnumerationCandidate(candidate_id="a", description="AB-2 plots", anchors=["b:0030"], dimensions=[named])
    as_treatment = EnumerationCandidate(candidate_id="b", description="AB-2 plots", anchors=["b:0030"], dimensions=[declared_treatment])
    kept, decisions = _dedup([as_crop, as_treatment], _table_treatments(), {"crop": CROP_POOL, "site": []})
    assert kept == []
    assert {d["decision"] for d in decisions} == {"dropped_no_treatment_dimension"}  # exact ready-Crop match -> crop -> not a Treatment
    for name in CULTIVARS:  # every individual cultivar of the paper reconciles to crop
        c = EnumerationCandidate(candidate_id="x", description=name, anchors=["b:0030"], dimensions=[_dim("Cultivar", "treatment", name)])
        assert orchestrator._reconcile_candidate_dimensions(c, {"crop": CROP_POOL, "site": []})[0].dimension == "crop"


def test_a_model_that_declares_the_mixtures_crop_is_not_overridden_by_code():
    """The production live run declared the mixtures `crop`. The guidance change targets that model behaviour;
    code never re-labels a declaration by guessing (the level is not an exact Crop record either way)."""
    freeform = _freeform("live_gpt_oss_felipe_freeform_production_run.json")
    kept, decisions = _dedup(freeform, _table_treatments(), {"crop": CROP_POOL, "site": []})
    assert kept == [] and {d["decision"] for d in decisions} == {"dropped_no_treatment_dimension"}


# --------------------------------------------------------------------- #
# Q2 is unchanged: no manufactured combinations from Table 1's pooled evidence
# --------------------------------------------------------------------- #


def test_table_1_pooled_over_the_mixture_still_gives_only_fallow_and_mustard_and_no_manufactured_observations(monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(POOLING))
    table = _table1()
    merged, records = orchestrator._apply_pooling_evidence(table, FELIPE, validators._load_rendered_blocks(FELIPE))
    [pooled] = merged.pooled_factors
    assert (pooled.name, pooled.dimension, pooled.origin) == ("cultivar mixture", "treatment", "deterministic")
    assert records and records[0]["applied"] is True

    treatments, covered = orchestrator._table_classifications_to_treatment_candidates([merged], {})
    assert sorted(t.description for t in treatments) == ["Experimental condition: Treatment=Fallow", "Experimental condition: Treatment=Mustard"]
    assert covered == {"b:0069"}  # the pooled treatment-dimension factor is never a Treatment identity
    observations = orchestrator._table_classification_to_candidates(merged, {})
    reported_cells = sum(1 for row in merged.row_groups for cell in row.cells.values() if cell and cell.strip())
    assert len(observations) == reported_cells  # one per REPORTED cell -- nothing for unreported combinations
    for candidate in observations:
        assert "cv" not in candidate.description.lower().replace("cover", "")
        assert candidate.context["reported_effect_scope"] == "aggregated_mean"
        assert candidate.context["aggregated_over_factors"] == ["cultivar mixture"]
    assert orchestrator._pooled_representability(merged) == (True, None)


# --------------------------------------------------------------------- #
# pooling evidence follows the same rule
# --------------------------------------------------------------------- #


def test_real_felipe_note_is_a_treatment_dimension_pooled_factor():
    [record] = pe.detect_pooling_evidence(FELIPE, ["b:0069"], POOLING)
    assert (record["factor"], record["dimension"]) == ("cultivar mixture", "treatment")  # the source itself says "treatments"


@pytest.mark.parametrize("sentence, expected", [
    ("Data are means for all cultivar mixture treatments.", "treatment"),   # the source calls them treatments
    ("Data are means for all cultivar mixtures.", "other"),                 # undetermined: not crop, not inferred treatment
    ("Values are means across all mixtures of cultivars.", "other"),        # cultivars are named, but it is a mixture
    ("Data are means across all blends.", "other"),
    ("Data are means for all cultivars.", "crop"),                          # individual cultivars stay crop
])
def test_synthetic_pooled_mixture_names_are_never_labelled_crop(sentence, expected):
    accepted = [r for r in pe.scan_text(sentence) if r["status"] == "accepted"]
    assert accepted and {r["dimension"] for r in accepted} == {expected}
