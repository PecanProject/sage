from __future__ import annotations

from components import record_row


def _field(value, kind="extracted_field", provenance_label="EXTRACTED"):
    return {"kind": kind, "effective_value": value, "provenance_label": provenance_label}


def _record(record_id, fields):
    return {"record_id": record_id, "status": "ready", "fields": fields}


def test_citation_summary_includes_author_year_title():
    record = _record("pecan", {
        "author": _field("S.M. Smukler"),
        "year": _field(2012),
        "title": _field("Assessment of best management practices"),
    })
    summary = record_row.summarize("Citation", record, {})
    assert "S.M. Smukler" in summary
    assert "2012" in summary
    assert "Assessment of best management practices" in summary


def test_species_summary_prefers_scientific_and_common_name():
    record = _record("sp1", {
        "genus": _field("Acer"),
        "species_epithet": _field("rubrum"),
        "scientific_name": _field("Acer rubrum"),
        "common_name": _field("red maple"),
    })
    assert record_row.summarize("Species", record, {}) == "Acer rubrum · red maple"


def test_species_summary_falls_back_without_common_name():
    record = _record("sp2", {"scientific_name": _field("Quercus rubra"), "common_name": None})
    record["fields"] = {"scientific_name": _field("Quercus rubra")}
    assert record_row.summarize("Species", record, {}) == "Quercus rubra"


def test_variable_summary_combines_name_and_units():
    record = _record("v1", {"name": _field("Soil nitrogen"), "units": _field("mg kg-1")})
    assert record_row.summarize("Variable", record, {}) == "Soil nitrogen · mg kg-1"


def test_management_summary_combines_event_and_date_reported_text():
    record = _record("m1", {
        "event_type": _field("Fertilization"),
        "date": _field({"reported_text": "1998-06-12", "earliest": None, "latest": None}),
    })
    assert record_row.summarize("Management", record, {}) == "Fertilization · 1998-06-12"


def test_observation_summary_shows_quantity_reported_text():
    record = _record("o1", {
        "variable_name": _field("C/N ratio"),
        "value": _field({"reported_text": "24.6 mg kg-1"}),
    })
    assert record_row.summarize("Observation", record, {}) == "C/N ratio · 24.6 mg kg-1"


def test_observation_summary_is_honest_about_unresolved_value():
    record = _record("o2", {
        "variable_name": _field("C/N ratio"),
        "value": {"kind": "extracted_field", "effective_value": None, "provenance_label": "UNRESOLVED"},
    })
    assert record_row.summarize("Observation", record, {}) == "C/N ratio · value unresolved"


def test_treatmentpair_summary_resolves_treatment_names_not_raw_ids():
    all_data = {
        "Treatment": [
            {"record_id": "t1", "fields": {"name": _field("ambient CO2")}},
            {"record_id": "t2", "fields": {"name": _field("elevated CO2")}},
        ],
    }
    record = _record("tp1", {
        "treatment_id_1": _field("t1", kind="reference", provenance_label=None),
        "treatment_id_2": _field("t2", kind="reference", provenance_label=None),
    })
    assert record_row.summarize("TreatmentPair", record, all_data) == "ambient CO2 ↔ elevated CO2"


def test_summary_falls_back_to_record_id_when_nothing_resolves():
    record = _record("Daren-1997-Canopy_crop_mystery", {})
    assert record_row.summarize("Crop", record, {}) == "Daren-1997-Canopy_crop_mystery"


def test_blocked_and_error_records_are_not_fabricated_a_summary():
    record = {"record_id": "x", "status": "error", "reason": "no final assistant text found", "fields": {}}
    assert record["status"] in ("blocked", "error")
    assert record["reason"] == "no final assistant text found"
