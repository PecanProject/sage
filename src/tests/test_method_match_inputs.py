"""Method matching sees only Method-relevant values (the Treatment level is not a Method signal).

Real evidence (Felipe-2010-Cultivar, run felipe_regress_20260920T163059, Table 1): 10 of 22 candidates linked to the WRONG
Method. All 10 were Fallow cells. Step C built ONE values dict per cell -- row levels, the column's Treatment level,
Site, the Method hint -- and handed all of it to the Method matcher. The matcher's substring tier then let the Treatment
level "Fallow" score against the name of `cover_crop_aboveground_biomass_sampling_oven_dry` ("...sampled in both MCC and
fallow treatments at -32 DAP."), which made it a unique winner and the grounded hint was never consulted. So PAR, shoot
biomass, total fruit, harvestable fruit and harvest index of the Fallow column all became "cover-crop biomass" records
(8 of them ready), while the same rows of the Mustard column resolved correctly from their hints.

The matcher itself behaved as designed. The fix is at input construction: `_method_match_values` gives the Method matcher
the variable the cell reports and the method the source names, nothing else. The matcher and its tiers are untouched.

Fixtures (real): tests/fixtures/pass2/felipe_table1_classification_regress.json (that run's Step B result for Table 1)
and felipe_method_pool_regress.json (the 11 READY Methods of that run, the pool Step C actually matched against).
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline import orchestrator
from pipeline.raw_schema import TableClassification, TableFactor, TableRowGroup, TableValueColumn, TableVariable

FIXTURES = Path(__file__).parent / "fixtures" / "pass2"
BIOMASS = "cover_crop_aboveground_biomass_sampling_oven_dry"
PAR = "canopy_light_interception_par_solarimeter"
NITROGEN = "tomato_total_nitrogen_leco_gas_analyzer"


def _pool() -> list[dict]:
    return json.loads((FIXTURES / "felipe_method_pool_regress.json").read_text())


def _felipe() -> TableClassification:
    return TableClassification.model_validate(json.loads((FIXTURES / "felipe_table1_classification_regress.json").read_text()))


def _links(classification: TableClassification, pool: list[dict]) -> dict[str, str | None]:
    return {
        c.candidate_id: c.linked_candidates.get("method_id")
        for c in orchestrator._table_classification_to_candidates(classification, {"method_id": pool})
    }


# --------------------------------------------------------------------- #
# the real Felipe Table 1
# --------------------------------------------------------------------- #


def test_the_real_pool_contains_the_word_fallow_in_exactly_the_method_that_won_by_it():
    """The precondition of the failure: a Method whose text mentions the Treatment level."""
    mentioning = [m["slug"] for m in _pool() if "fallow" in f"{m['name']} {m['description']}".lower()]
    assert mentioning == [BIOMASS]


def test_no_fallow_cell_links_to_a_method_merely_because_its_text_mentions_fallow():
    links = _links(_felipe(), _pool())
    assert BIOMASS not in links.values()
    assert len(links) == 22


def test_the_grounded_hints_resolve_the_same_method_for_both_treatment_columns():
    """PAR and aboveground N carry a grounded hint; their Fallow and Mustard cells must agree."""
    links = _links(_felipe(), _pool())
    for row in ("row1", "row2", "row3"):
        assert links[f"fallow_{row}"] == links[f"mustard_{row}"] == PAR
    assert links["fallow_row10"] == links["mustard_row10"] == NITROGEN


def test_the_linking_of_a_cell_does_not_depend_on_which_treatment_column_it_is_in():
    links = _links(_felipe(), _pool())
    for row in (f"row{n}" for n in range(1, 12)):
        assert links[f"fallow_{row}"] == links[f"mustard_{row}"], row


def test_variables_without_a_supported_method_stay_unresolved_not_forced():
    """Shoot biomass ('oven dried at 60°C' is too few distinctive tokens), total fruit, harvestable fruit and harvest
    index carry no usable hint, and the pool has no Method for the tomato plant sampling: refuse to guess."""
    links = _links(_felipe(), _pool())
    unresolved = {f"{col}_{row}" for col in ("fallow", "mustard") for row in ("row4", "row5", "row6", "row7", "row8", "row9", "row11")}
    assert all(links[c] is None for c in unresolved)
    assert sum(v is not None for v in links.values()) == 8


# --------------------------------------------------------------------- #
# what the matcher is given
# --------------------------------------------------------------------- #


def _factored() -> TableClassification:
    return TableClassification(
        table_role="treatment_response", table_anchors=["b:0001"],
        factors=[TableFactor(name="DAP", dimension="time", encoding="rows"),
                 TableFactor(name="Variable", dimension="variable", encoding="rows"),
                 TableFactor(name="Site", dimension="site", encoding="rows"),
                 TableFactor(name="Cultivar", dimension="crop", encoding="rows"),
                 TableFactor(name="Block", dimension="replicate", encoding="rows"),
                 TableFactor(name="Other", dimension="other", encoding="rows")],
        variables=[TableVariable(label="Shoot biomass", variable_name="shoot biomass", method_hint="clipped shoots oven dried weighed")],
        value_columns=[TableValueColumn(value_column_id="fallow", treatment_level_hint="Fallow", site_hint="Davis")],
        row_groups=[TableRowGroup(row_group_id="r1", source_table_anchor="b:0001", cells={"fallow": "70"},
                                  factor_values={"DAP": "111", "Variable": "Shoot biomass", "Site": "Davis", "Cultivar": "AB-2",
                                                 "Block": "3", "Other": "x"})],
    )


def test_the_method_matcher_is_given_the_variable_and_the_method_hint_and_nothing_else():
    tc = _factored()
    row, column = tc.row_groups[0], tc.value_columns[0]
    values = orchestrator._method_match_values(tc, row, column, "clipped shoots oven dried weighed")
    assert values == {"Variable": "Shoot biomass", "Method": "clipped shoots oven dried weighed"}


def test_treatment_site_time_crop_replicate_and_other_levels_are_never_method_signals():
    tc = _factored()
    values = orchestrator._method_match_values(tc, tc.row_groups[0], tc.value_columns[0], None)
    assert values == {"Variable": "Shoot biomass"}
    assert not {"Treatment", "Site", "DAP", "Cultivar", "Block", "Other"} & set(values)


def test_other_link_pools_still_receive_the_treatment_and_site_levels():
    """Only the Method pool changed: Treatment and Site linking still see their own levels."""
    tc = _factored()
    treatment_pool = [{"slug": "fallow", "record_id": "p_treatment_fallow", "name": "Fallow"}]
    site_pool = [{"slug": "davis", "record_id": "p_site_davis", "name": "Davis, CA"}]
    [candidate] = orchestrator._table_classification_to_candidates(tc, {"treatment_id": treatment_pool, "site_id": site_pool})
    assert candidate.linked_candidates == {"treatment_id": "fallow", "site_id": "davis"}


# --------------------------------------------------------------------- #
# synthetic: the matcher's guarantees still hold
# --------------------------------------------------------------------- #

SYNTHETIC_POOL = [
    {"slug": "oven_weighing", "name": "Plants were oven dried and weighed in both fallow and cover crop plots",
     "description": "Shoots of each plot were clipped, oven dried at 60 C and weighed."},
    {"slug": "solarimeter", "name": "Canopy light interception", "description": "Measured with a portable-tube solarimeter."},
]


def _synthetic(hint: str | None, treatment: str = "Fallow") -> TableClassification:
    return TableClassification(
        table_role="treatment_response", table_anchors=["b:0001"],
        factors=[TableFactor(name="Variable", dimension="variable", encoding="rows")],
        variables=[TableVariable(label="PAR fraction", variable_name="par fraction", method_hint=hint)],
        value_columns=[TableValueColumn(value_column_id="c", treatment_level_hint=treatment)],
        row_groups=[TableRowGroup(row_group_id="r", source_table_anchor="b:0001", cells={"c": "1"}, factor_values={"Variable": "PAR fraction"})],
    )


def _synthetic_link(hint: str | None, treatment: str = "Fallow", pool=None):
    [candidate] = orchestrator._table_classification_to_candidates(
        _synthetic(hint, treatment), {"method_id": SYNTHETIC_POOL if pool is None else pool})
    return candidate.linked_candidates.get("method_id")


def test_a_treatment_level_named_in_a_method_description_cannot_make_it_win_without_a_hint():
    assert _synthetic_link(None, "Fallow") is None
    assert _synthetic_link(None, "Fallow treatments") is None


def test_a_real_grounded_hint_still_resolves_the_right_method_whatever_the_treatment():
    assert _synthetic_link("portable-tube solarimeter", "Fallow") == "solarimeter"
    assert _synthetic_link("portable-tube solarimeter", "Mustard") == "solarimeter"


def test_a_hint_that_two_methods_satisfy_is_still_refused():
    twin = [{"slug": "method_a", "name": "Canopy measurement A", "description": "Measured with a portable-tube solarimeter."},
            {"slug": "method_b", "name": "Canopy measurement B", "description": "Also read with a portable-tube solarimeter."},
            SYNTHETIC_POOL[0]]
    assert _synthetic_link("portable-tube solarimeter", "Fallow", pool=twin) is None


def test_a_hint_no_method_supports_stays_unresolved():
    assert _synthetic_link("Kjeldahl digestion titration", "Fallow") is None


def test_a_legacy_row_factor_that_is_not_a_method_is_not_matched_against_methods():
    """A classification with no declared factors: its row levels (here a population named like a Method) are not
    Method signals; only its Method hint is."""
    pool = [{"slug": "oven_weighing", "name": "oven drying", "description": None}]
    tc = TableClassification(
        table_role="treatment_response", table_anchors=["b:0001"],
        value_columns=[TableValueColumn(value_column_id="v", variable_name_hint="yield")],
        row_groups=[TableRowGroup(row_group_id="r", source_table_anchor="b:0001", cells={"v": "1"}, factor_values={"Population": "oven drying"})],
    )
    [candidate] = orchestrator._table_classification_to_candidates(tc, {"method_id": pool})
    assert "method_id" not in candidate.linked_candidates
