"""Stage 2: identity-based prerequisites + field-level settling of AI-validator concerns.

Real failures (replay baseline, tests/replay/baselines/stage0_baseline.json): a Site unresolved over its coordinates
blocked every Treatment/Observation/Coverage of Philippe-2007-Six and Kathryn-2020-Winter; a Species flagged over its
epithet blocked every Crop of Daren-1997-Canopy. The rules pinned here: a CONTEXT prerequisite (Citation, Site,
Species) is referenceable once its identity is established; a SCIENTIFIC link (Treatment, Method) must still be ready;
an identity that is itself in doubt, or a payload that never passed validation, is never referenceable.
"""

from __future__ import annotations

from pipeline import orchestrator
from pipeline.orchestrator import (
    _demote_concerned_fields, _entity_result_file, _prerequisite_notes, _referenceable_records, _resolve_known_refs,
)
from pipeline.validators import readiness_issues


def _f(value, label="EXTRACTED", reason=None):
    field = {"value": value, "provenance_label": label,
             "source": {"source_document_id": "P", "locators": [{"kind": "text", "block_anchor": "b:0028"}]}}
    if reason:
        field["unresolved_reason"] = reason
    return field


def _record(entity_type, record_id, status, payload=None, ai_issues=(), last_errors=()):
    detail = {"status": status, "last_errors": list(last_errors)}
    if payload is not None:
        detail["payload"] = payload
    if ai_issues:
        detail["ai_validation"] = {"verdict": "suspicious", "issues": list(ai_issues)}
    return {"entity_type": entity_type, "record_id": record_id, "status": status, "detail": detail}


# The Philippe Site as run 20260925T132905 left it: valid payload, name UNRESOLVED, description grounded, the only open
# concern about the (missing) coordinates.
PHILIPPE_SITE = _record(
    "Site", "P_site_chaine", "unresolved",
    payload={"id": "P_site_chaine", "name": _f(None, "UNRESOLVED", "not stated"),
             "description": _f("25-year-old natural P. sylvestris stand in the Chaîne des Puys"),
             "country": _f("France")},
    ai_issues=[{"field": "latitude", "concern": "coordinates are in the source"},
               {"field": "longitude", "concern": "coordinates are in the source"}],
)
CITATION = {"entity_type": "Citation", "record_id": "P", "status": "ready", "detail": {"payload": {}}}


# --------------------------------------------------------------------------- #
# Which records are referenceable
# --------------------------------------------------------------------------- #

def test_a_site_unresolved_only_over_its_coordinates_is_referenceable():
    assert _referenceable_records({"Site": [PHILIPPE_SITE]}, "Site") == [PHILIPPE_SITE]


def test_a_site_whose_payload_never_passed_validation_is_not_referenceable():
    failed = _record("Site", "s", "unresolved", payload=None)   # _finalize_unresolved keeps no `payload`
    failed["detail"]["last_candidate_payload"] = {"name": _f("Somewhere")}
    assert _referenceable_records({"Site": [failed]}, "Site") == []


def test_a_site_whose_identity_is_in_doubt_is_not_referenceable():
    doubted = _record("Site", "s", "unresolved", payload={"name": _f("Somewhere")},
                      ai_issues=[{"field": "name.value", "concern": "not the site's name"}])
    assert _referenceable_records({"Site": [doubted]}, "Site") == []


def test_a_site_with_no_grounded_identity_field_is_not_referenceable():
    hollow = _record("Site", "s", "unresolved", payload={"name": _f(None, "UNRESOLVED", "x"), "country": _f("France")})
    assert _referenceable_records({"Site": [hollow]}, "Site") == []


def test_treatment_and_method_must_still_be_ready():
    treatment = _record("Treatment", "t", "unresolved", payload={"name": _f("Fallow"), "definition": _f("winter fallow")})
    method = _record("Method", "m", "unresolved", payload={"name": _f("LI-6400"), "description": _f("gas exchange")})
    assert _referenceable_records({"Treatment": [treatment]}, "Treatment") == []
    assert _referenceable_records({"Method": [method]}, "Method") == []


def test_blocked_and_error_records_are_never_referenceable():
    records = [{"record_id": "a", "status": "blocked", "reason": "x"}, {"record_id": "b", "status": "error", "detail": {}}]
    assert _referenceable_records({"Site": records}, "Site") == []


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def test_treatment_is_no_longer_blocked_by_a_site_missing_only_coordinates():
    known_refs, blocked = _resolve_known_refs("Treatment", {"Citation": CITATION, "Site": [PHILIPPE_SITE]})
    assert blocked is None
    assert known_refs["site_id"] == "P_site_chaine"


def test_observation_is_still_blocked_without_a_ready_treatment():
    unresolved_treatment = _record("Treatment", "t", "unresolved", payload={"name": _f("Fallow")})
    records = {"Citation": CITATION, "Site": [PHILIPPE_SITE], "Treatment": [unresolved_treatment],
               "Method": [_record("Method", "m", "ready", payload={"name": _f("x")})]}
    _refs, blocked = _resolve_known_refs("Observation", records)
    assert blocked is not None and "Treatment" in blocked


def test_crop_is_no_longer_blocked_by_a_species_flagged_only_over_a_non_identity_field():
    species = _record("Species", "sp", "unresolved",
                      payload={"scientific_name": _f("Panicum virgatum L."), "genus": _f("Panicum"),
                               "species_epithet": _f("virgatum")},
                      ai_issues=[{"field": "common_name", "concern": "switchgrass is written in the text"}])
    _refs, blocked = _resolve_known_refs("Crop", {"Citation": CITATION, "Species": [species]})
    assert blocked is None


def test_a_site_that_is_not_identified_still_blocks():
    failed = _record("Site", "s", "unresolved", payload=None)
    _refs, blocked = _resolve_known_refs("Treatment", {"Citation": CITATION, "Site": [failed]})
    assert blocked is not None and "Site" in blocked


def test_every_dependent_records_which_prerequisite_is_not_ready():
    notes = _prerequisite_notes("Treatment", {"Citation": CITATION, "Site": [PHILIPPE_SITE]})
    assert notes == [{"field": "site_id", "prerequisite": "Site", "record_id": "P_site_chaine", "status": "unresolved",
                      "open_fields": ["latitude", "longitude"]}]
    result = _entity_result_file("P", "Treatment", "run", "t", {"status": "ready", "detail": {"payload": {}},
                                                                   "prerequisite_notes": notes})
    assert result["prerequisite_notes"] == notes


# --------------------------------------------------------------------------- #
# Field-level settling of an unanswered AI-validator concern
# --------------------------------------------------------------------------- #

def test_a_concern_about_a_present_value_withdraws_that_value_only():
    payload = {"id": "v", "name": _f("maximum electron transport rate (Jmax"), "units": _f("µmol m -2 s -1")}
    demoted, demotions, open_concerns = _demote_concerned_fields(
        "Variable", payload, {"issues": [{"field": "name.value", "concern": "missing closing parenthesis"}]})
    assert demoted["name"]["value"] is None and demoted["name"]["provenance_label"] == "UNRESOLVED"
    assert "missing closing parenthesis" in demoted["name"]["unresolved_reason"]
    assert demoted["name"]["source"] == payload["name"]["source"]      # the evidence pointer is kept for review
    assert demoted["units"] == payload["units"]                          # untouched
    assert demotions[0]["value"] == "maximum electron transport rate (Jmax" and open_concerns == []
    # ...and a Variable without a name is not ready: readiness still decides.
    assert readiness_issues("Variable", demoted)


def test_an_omission_of_a_descriptive_field_is_kept_as_an_open_concern():
    payload = {"id": "s", "name": _f("Chaîne des Puys")}
    demoted, demotions, open_concerns = _demote_concerned_fields(
        "Site", payload, {"issues": [{"field": "latitude", "concern": "45°42′ N is in the block"}]})
    assert demoted == payload and demotions == []
    assert open_concerns == [{"field": "latitude", "concern": "45°42′ N is in the block", "rule": "omission_tolerated"}]


def test_an_omission_of_an_observations_time_is_never_tolerated():
    # Felipe run 20260923T132453: Observations committed ready with temporal_info UNRESOLVED although the row states DAP.
    payload = {"id": "o", "value": _f({"reported_text": "20", "reported_units": "%"}),
               "temporal_info": _f(None, "UNRESOLVED", "no date")}
    assert _demote_concerned_fields(
        "Observation", payload, {"issues": [{"field": "temporal_info", "concern": "the row states 35 DAP"}]}) is None


def test_record_level_reference_and_empty_concerns_are_not_settled_field_by_field():
    payload = {"id": "t", "site_id": "P_site", "name": _f("Fallow")}
    assert _demote_concerned_fields("Treatment", payload, {"issues": [{"field": None, "concern": "overall"}]}) is None
    assert _demote_concerned_fields("Treatment", payload, {"issues": [{"field": "site_id", "concern": "wrong site"}]}) is None
    assert _demote_concerned_fields("Treatment", payload, {"issues": [{"field": "name", "concern": ""}]}) is None
    assert _demote_concerned_fields("Treatment", payload, {"issues": []}) is None


def test_an_observation_value_concern_withdraws_the_value_and_the_record_is_not_ready():
    payload = {"id": "o", "variable_name": _f("PAR intercepted"),
               "value": _f({"reported_text": "20", "reported_numeric_value": 20, "reported_units": "%"})}
    demoted, _d, _o = _demote_concerned_fields(
        "Observation", payload, {"issues": [{"field": "value", "concern": "this is the SE, not the mean"}]})
    assert demoted["value"]["provenance_label"] == "UNRESOLVED"
    assert [i.code for i in readiness_issues("Observation", demoted)] == ["observation_value_unresolved"]


def test_the_tolerated_omission_table_never_lists_identity_value_or_time_fields():
    forbidden = {"name", "value", "variable_name", "temporal_info", "date", "event_type", "scientific_name", "title",
                 "cultivar", "definition"}
    for entity_type, fields in orchestrator.OMISSION_TOLERATED_FIELDS.items():
        assert not (fields & forbidden), entity_type


def test_a_concern_about_the_label_only_downgrades_extracted_to_inferred_and_keeps_the_value():
    # Daren-1997-Canopy Species (run 20260925T162701): "genus 'Panicum' ... is inferred from the scientific name".
    payload = {"id": "sp", "genus": _f("Panicum"), "species_epithet": _f("virgatum"), "scientific_name": _f("Panicum virgatum L.")}
    demoted, demotions, _o = _demote_concerned_fields("Species", payload, {"issues": [
        {"field": "genus.provenance_label", "concern": "inferred from the scientific name"}]})
    assert (demoted["genus"]["value"], demoted["genus"]["provenance_label"]) == ("Panicum", "INFERRED")
    assert "inferred from the scientific name" in demoted["genus"]["unresolved_reason"]
    assert demotions[0]["rule"] == "ai_concern_label_downgraded"
    # An already-INFERRED field is the weaker claim already: a concern about its label or basis note (Felipe
    # bed_preparation: "should be EXTRACTED rather than INFERRED") never strengthens it and never withdraws it.
    payload["genus"] = _f("Panicum", "INFERRED", "from the binomial")
    demoted, demotions, open_concerns = _demote_concerned_fields("Species", payload, {"issues": [
        {"field": "genus.provenance_label", "concern": "should be EXTRACTED"},
        {"field": "genus.unresolved_reason", "concern": "a reason on a resolved value"}]})
    assert (demoted["genus"]["value"], demoted["genus"]["provenance_label"]) == ("Panicum", "INFERRED")
    assert demotions == [] and [c["rule"] for c in open_concerns] == ["label_concern_kept_weaker"] * 2
