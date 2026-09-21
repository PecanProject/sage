"""Canonical Treatment identity for tables with declared factors.

Real evidence: the same real treatment was generated several times under
different candidate ids because identity was built from the raw text of each
table's key names and values -- `Location` vs `Site`, "Ames, IA" vs "Ames" -- so
one run produced 131 Treatment records for a paper whose distinct conditions
were a fraction of that (63 were redundant copies).

Identity now comes from canonical semantic content: the normalized treatment
levels (key names ignored) plus the site resolved to a ready Site record. When
identity cannot be resolved it stays AMBIGUOUS -- kept separate and flagged --
never silently merged. Nothing here targets a count; the expected numbers follow
from the synthetic tables' structure.
"""

from __future__ import annotations

from pipeline import orchestrator
from pipeline.raw_schema import TableClassification, TableFactor, TableRowGroup, TableValueColumn

SITE_POOL = [{"slug": "ames_ia", "name": "Ames, IA", "record_id": "p_site_ames_ia"},
             {"slug": "mead_ne", "name": "Mead, NE", "record_id": "p_site_mead_ne"}]
POOLS = {"site_id": SITE_POOL}


def _table_a(location="Ames, IA", anchor="b:0001") -> TableClassification:
    """Tillage as a row factor, Location as a row factor."""
    return TableClassification(
        table_role="treatment_response", table_anchors=[anchor],
        factors=[TableFactor(name="Tillage", dimension="treatment", encoding="rows"),
                 TableFactor(name="Location", dimension="site", encoding="rows")],
        value_columns=[TableValueColumn(value_column_id="y", variable_name_hint="yield")],
        row_groups=[
            TableRowGroup(row_group_id="r1", factor_values={"Tillage": "No-till", "Location": location}, source_table_anchor=anchor, cells={"y": "1"}),
            TableRowGroup(row_group_id="r2", factor_values={"Tillage": "Conventional", "Location": location}, source_table_anchor=anchor, cells={"y": "2"}),
        ],
    )


def _table_b(site="Ames", anchor="b:0002") -> TableClassification:
    """The SAME conditions, spelled differently: the treatment factor is 'Practice'
    and lives in the COLUMNS, the site factor is 'Site' and lives in the table context."""
    return TableClassification(
        table_role="treatment_response", table_anchors=[anchor],
        factors=[TableFactor(name="Practice", dimension="treatment", encoding="columns"),
                 TableFactor(name="Site", dimension="site", encoding="context"),
                 TableFactor(name="Variable", dimension="variable", encoding="rows")],
        context_levels={"Site": site},
        value_columns=[
            TableValueColumn(value_column_id="nt", variable_name_hint="v", factor_levels={"Practice": "no till"}),
            TableValueColumn(value_column_id="cv", variable_name_hint="v", factor_levels={"Practice": "CONVENTIONAL"}),
        ],
        row_groups=[TableRowGroup(row_group_id="r", factor_values={"Variable": "biomass"}, source_table_anchor=anchor, cells={"nt": "3", "cv": "4"})],
    )


def _candidates(tables, pools=POOLS):
    notes: list[dict] = []
    candidates, covered = orchestrator._table_classifications_to_treatment_candidates(tables, pools, notes)
    return candidates, covered, notes


def test_the_same_treatment_spelled_differently_by_two_tables_is_one_candidate():
    candidates, covered, notes = _candidates([_table_a(), _table_b()])
    assert sorted(c.candidate_id for c in candidates) == ["conventional_ames_ia", "no_till_ames_ia"]
    assert notes == []
    assert covered == {"b:0001", "b:0002"}
    assert all(c.linked_candidates == {"site_id": "ames_ia"} for c in candidates)


def test_key_names_and_level_capitalisation_do_not_change_identity():
    a = orchestrator._canonical_treatment_identity({"Tillage": "No-Till"}, {"Location": "Ames, IA"}, SITE_POOL)
    b = orchestrator._canonical_treatment_identity({"Practice": "no till"}, {"Site": "Ames"}, SITE_POOL)
    assert a == b and a[2] is False


def test_identity_is_independent_of_the_order_of_treatment_factors():
    a = orchestrator._canonical_treatment_identity({"Cover": "mustard", "Tillage": "no-till"}, {}, None)
    b = orchestrator._canonical_treatment_identity({"Tillage": "no-till", "Cover": "mustard"}, {}, None)
    assert a == b


def test_different_sites_stay_different_treatments():
    candidates, _, _ = _candidates([_table_a("Ames, IA"), _table_a("Mead, NE", anchor="b:0003")])
    assert len(candidates) == 4
    assert {c.linked_candidates["site_id"] for c in candidates} == {"ames_ia", "mead_ne"}


def test_a_site_that_cannot_be_resolved_against_the_pool_is_ambiguous_and_never_merged():
    candidates, _, notes = _candidates([_table_a("Ames, IA"), _table_a("Somewhere Else", anchor="b:0003")])
    # the resolvable Ames rows are 2 candidates; the unresolvable site's rows are kept apart (2 more)
    assert len(candidates) == 4
    assert [n["kind"] for n in notes] == ["identity_ambiguous_site"] * 2
    assert all("not merged" in n["detail"] for n in notes)
    unresolved = [c for c in candidates if not c.linked_candidates]
    assert len(unresolved) == 2  # and they are not linked to any Site either -- no guess


def test_an_unresolvable_site_is_never_merged_even_when_two_spellings_normalize_identically():
    candidates, _, notes = _candidates([_table_a("Site X"), _table_a("site x!", anchor="b:0003")], pools=POOLS)
    # neither spelling resolves to a real Site, so each table's cells stay separate candidates (their raw
    # spelling is part of the identity) and every affected cell is disclosed -- nothing is merged on a guess
    assert len(candidates) == 4
    assert len(notes) == 4 and all(n["kind"] == "identity_ambiguous_site" for n in notes)
    # they share a normalized candidate_id text, which the existing collision detector then disambiguates
    deduped, collision_notes = orchestrator._dedupe_candidate_record_ids(candidates)
    assert len({c.candidate_id for c in deduped}) == 4 and len(collision_notes) == 2


def test_with_no_site_pool_a_single_site_table_ignores_its_site_text_and_unifies_across_tables():
    candidates, _, notes = _candidates([_table_a("Ames, IA"), _table_b("Ames")], pools={})
    assert len(candidates) == 2 and notes == []  # one site in play: its spelling is irrelevant to identity


def test_with_no_site_pool_a_table_that_distinguishes_several_sites_keeps_them_apart_and_flags_it():
    two_sites = TableClassification(
        table_role="treatment_response", table_anchors=["b:0009"],
        factors=[TableFactor(name="Tillage", dimension="treatment", encoding="rows"),
                 TableFactor(name="Site", dimension="site", encoding="columns")],
        value_columns=[TableValueColumn(value_column_id="a", variable_name_hint="y", factor_levels={"Site": "Ames"}),
                       TableValueColumn(value_column_id="m", variable_name_hint="y", factor_levels={"Site": "Mead"})],
        row_groups=[TableRowGroup(row_group_id="r", factor_values={"Tillage": "no-till"}, source_table_anchor="b:0009", cells={"a": "1", "m": "2"})],
    )
    candidates, _, notes = _candidates([two_sites], pools={})
    assert len(candidates) == 2  # Ames and Mead are not collapsed into one Treatment
    assert len(notes) == 2 and all(n["kind"] == "identity_ambiguous_site" for n in notes)


def test_the_same_level_string_under_two_treatment_factors_is_not_collapsed():
    tc = TableClassification(
        table_role="treatment_response", table_anchors=["b:0004"],
        factors=[TableFactor(name="Cover", dimension="treatment", encoding="rows"),
                 TableFactor(name="Tillage", dimension="treatment", encoding="rows")],
        value_columns=[TableValueColumn(value_column_id="y", variable_name_hint="yield")],
        row_groups=[
            TableRowGroup(row_group_id="r1", factor_values={"Cover": "none", "Tillage": "till"}, source_table_anchor="b:0004", cells={"y": "1"}),
            TableRowGroup(row_group_id="r2", factor_values={"Cover": "till", "Tillage": "none"}, source_table_anchor="b:0004", cells={"y": "2"}),
        ],
    )
    candidates, _, _ = _candidates([tc], pools={})
    assert len(candidates) == 2  # {none, till} as a bare set would have merged these two distinct conditions


def test_candidate_ids_are_deterministic_and_filename_safe():
    candidates, _, _ = _candidates([_table_a(), _table_b()])
    for c in candidates:
        assert c.candidate_id == orchestrator._sanitize_candidate_id(c.candidate_id)
    again, _, _ = _candidates([_table_b(), _table_a()])
    assert sorted(c.candidate_id for c in candidates) == sorted(c.candidate_id for c in again)


def test_legacy_classifications_keep_their_exact_previous_identity_and_ids():
    legacy = TableClassification(
        applicable=True, table_anchors=["b:0005"],
        value_columns=[TableValueColumn(value_column_id="y", variable_name_hint="yield", site_hint="Ames")],
        row_groups=[TableRowGroup(row_group_id="r", factor_values={"Population": "P", "Maturity": "V"}, source_table_anchor="b:0005", cells={"y": "1"})],
    )
    candidates, _, notes = _candidates([legacy])
    assert [c.candidate_id for c in candidates] == ["v_p_ames"] and notes == []  # sorted-key values, exactly as before
