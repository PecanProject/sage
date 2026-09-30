"""Nested value-bearing fields are grounded (and LaTeX-rendered units no longer falsely fail).

Real evidence (Felipe-2010-Cultivar, run felipe_smoke_20260920T132343). `validators._value_supported_by_text` returned True
for any dict, so the QuantityValue / DateRange inside an ExtractedField was only ever checked for being a dict:
  - Observation `fruit_hue_mustard` was committed READY with `reported_text` "39.8 degrees hue angle" and
    `reported_units` "degrees"; the cited block says "39.8 hue". (The AI validator flagged it; the deterministic gate did not.)
  - Management `seedling_transplanting` was committed with date text "May 18-19 2006" and earliest 2006-05-18; the block says
    "May 18 and 19" and gives no year (protocol: never supply one).
The opposite failure is also real: Marker renders "$4.6\\pm0.4~\\mathrm{Mg~ha^{-1}}$" and "$CO_2$", so a correct
"Mg ha⁻¹" units string and a correct "CO2" note were REJECTED -- the second sank the whole mustard-CO2 Observation.

Fix: LaTeX/unicode math NOTATION is canonicalised on both sides (no number, unit symbol or word changes), and the nested
reported_text / reported_units / numeric value / date text / date year must be supported by the blocks the field cites.

Fixtures (real): tests/fixtures/pass2 -- trimmed Felipe content, felipe_ready_payloads.json, felipe_mustard_co2_payload.json.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline import validators

FIXTURES = Path(__file__).parent / "fixtures" / "pass2"
PAPER = "Felipe-2010-Cultivar"


@pytest.fixture(autouse=True)
def felipe(monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(FIXTURES))


def _blocks() -> dict[str, str]:
    return validators._load_rendered_blocks(PAPER)


def _real(key: str) -> dict:
    return copy.deepcopy(json.loads((FIXTURES / "felipe_ready_payloads.json").read_text())[key])


def _field(value, *anchors, label="EXTRACTED"):
    return {"value": value, "provenance_label": label, "source": {
        "source_document_id": PAPER, "page_number": 1, "section_path": [],
        "locators": [{"kind": "text", "block_anchor": a} for a in anchors]}}


def _codes(payload) -> list[str]:
    return sorted(i.code for i in validators.validate_provenance(PAPER, payload))


def _quantity(text, numeric, units, *anchors):
    return {"q": _field({"reported_text": text, "reported_numeric_value": numeric, "reported_units": units}, *anchors)}


def _date(text, earliest, latest, *anchors):
    return {"d": _field({"reported_text": text, "earliest": earliest, "latest": latest, "relative_timing": None}, *anchors)}


# --------------------------------------------------------------------- #
# LaTeX / unicode notation: valid renderings are no longer falsely rejected
# --------------------------------------------------------------------- #


def test_the_real_latex_block_matches_the_unicode_units_a_correct_model_writes():
    block = _blocks()["b:0056"]
    assert "\\mathrm{Mg~ha^{-1}}" in block                       # what Marker actually rendered
    assert validators._value_supported_by_text("Mg ha⁻¹", block)   # the real Variable `aboveground_plant_biomass` failure
    assert validators._value_supported_by_text("kg N ha-1", block) and validators._value_supported_by_text("Mg ha-1", block)


def test_a_real_latex_units_payload_is_accepted():
    """Real `plant_total_nitrogen_fallow`: units 'kg N ha^{-1}' and the literal LaTeX reported_text, both in b:0056."""
    assert _codes({"value": _real("Observation::plant_total_nitrogen_fallow")["value"]}) == []


def test_the_real_mustard_co2_notes_is_grounded_once_the_latex_subscript_is_normalised():
    """This was the 'optional field sank a valid Observation' case: `notes` says CO2, the block says $CO_2$."""
    payload = json.loads((FIXTURES / "felipe_mustard_co2_payload.json").read_text())
    block = _blocks()["b:0055"]
    assert "$CO_2$" in block and "CO2" not in block
    assert validators.validate_provenance(PAPER, {"notes": payload["notes"]}) == []


def test_normalisation_changes_notation_only_never_numbers_or_words():
    n = validators._normalize_typography
    assert n("$4.6\\pm0.4~\\mathrm{mg~ha^{-1}}$") == "4.6±0.4 mg ha-1"
    assert n("mg ha⁻¹") == "mg ha-1" and n("$10 {\\pm} 0.4$") == "10 ± 0.4" and n("co_2 emissions") == "co2 emissions"
    assert n("g n m -2") == "g n m -2"
    assert "4.6" in n("$4.6$") and "39.8 hue" in n("39.8 hue")   # digits and words untouched


def test_normalisation_does_not_make_a_wrong_value_match():
    block = _blocks()["b:0056"]
    assert not validators._value_supported_by_text("Mg ha-2", block) and not validators._value_supported_by_text("t ha-1", block)


# --------------------------------------------------------------------- #
# unsupported units / reported text / numeric value (real fruit_hue_mustard)
# --------------------------------------------------------------------- #


def test_the_real_fruit_hue_record_is_now_rejected_for_units_and_reported_text():
    payload = {"value": _real("Observation::fruit_hue_mustard")["value"]}
    assert payload["value"]["value"]["reported_units"] == "degrees" and "degrees" not in _blocks()["b:0058"]
    assert _codes(payload) == ["provenance_reported_text_mismatch", "provenance_units_mismatch"]


def test_the_source_words_pass_and_nothing_is_supplied_beyond_them():
    assert _codes(_quantity("39.8 hue", 39.8, "hue", "b:0058")) == []           # as the paper writes it
    assert _codes(_quantity("39.8 hue", 39.8, "unitless", "b:0058")) == []      # dimensionless: nothing to ground


def test_unsupported_reported_text_is_rejected_even_with_supported_units():
    assert "provenance_reported_text_mismatch" in _codes(_quantity("39.9 hue", 39.9, "hue", "b:0058"))
    assert "provenance_reported_text_mismatch" in _codes(_quantity("about 39.8 hue", 39.8, "hue", "b:0058"))


def test_the_numeric_value_must_be_the_number_in_reported_text():
    assert _codes(_quantity("39.8 hue", 40.0, "hue", "b:0058")) == ["provenance_numeric_mismatch"]
    assert _codes(_quantity("352±15.4 a", 352, "unitless", "b:0069")) == []          # a real Table 1 cell


def test_a_short_unit_must_be_a_whole_token_not_a_stray_letter():
    assert "provenance_units_mismatch" in _codes(_quantity("39.8 hue", 39.8, "g", "b:0058"))
    assert _codes(_quantity("88±22 mm", 88, "mm", "b:0030")) == []                # 'mm' is a real token in b:0030


def test_units_may_be_supported_by_any_cited_block():
    assert _codes(_quantity("39.8 hue", 39.8, "hue", "b:0026", "b:0058")) == []


# --------------------------------------------------------------------- #
# unsupported date text / year (real seedling_transplanting)
# --------------------------------------------------------------------- #


def test_the_real_transplanting_date_is_rejected_for_text_and_an_invented_year():
    payload = {"date": _real("Management::seedling_transplanting")["date"]}
    value = payload["date"]["value"]
    assert value["reported_text"] == "May 18-19 2006" and value["earliest"] == "2006-05-18"
    assert "2006" not in _blocks()["b:0030"]
    assert _codes(payload) == ["provenance_date_text_mismatch", "provenance_date_year_unsupported"]


def test_the_literal_date_text_without_a_supplied_year_is_accepted():
    """What the protocol asks for: keep the source's own words, leave the interval null when the year is not stated."""
    assert _codes(_date("May 18 and 19", None, None, "b:0030")) == []


def test_a_year_is_accepted_when_another_cited_block_states_it():
    assert "2006" in _blocks()["b:0026"]
    assert _codes(_date("May 18 and 19", "2006-05-18", "2006-05-19", "b:0030", "b:0026")) == []
    assert _codes(_date("May 18 and 19", "2006-05-18", "2006-05-19", "b:0030")) == ["provenance_date_year_unsupported"]


def test_a_reformatted_date_text_that_adds_something_the_citation_lacks_is_rejected():
    assert _codes(_date("18 May 2006", None, None, "b:0030")) == ["provenance_date_text_mismatch"]   # 2006 is not in b:0030


def test_a_date_assembled_from_two_cited_blocks_is_accepted_when_every_word_is_in_them():
    """The temporal-context flow: date_text ('May 18') and year_text ('2006') come from different blocks and are joined."""
    assert _codes(_date("May 18 2006", None, None, "b:0030", "b:0026")) == []
    assert _codes(_date("May 18 2006", "2006-05-18", "2006-05-18", "b:0030", "b:0026")) == []


def test_an_assembled_date_with_a_word_no_cited_block_contains_is_rejected():
    assert _codes(_date("May 18 2007", None, None, "b:0030", "b:0026")) == ["provenance_date_text_mismatch"]
    assert _codes(_date("June 18 2006", None, None, "b:0030", "b:0026")) == ["provenance_date_text_mismatch"]


def test_relative_timing_tokens_are_not_treated_as_source_text():
    value = {"reported_text": "May 18 and 19", "earliest": None, "latest": None, "relative_timing": "at_planting"}
    assert _codes({"d": _field(value, "b:0030")}) == []


# --------------------------------------------------------------------- #
# what the nested check does not do
# --------------------------------------------------------------------- #


def test_scalar_fields_and_unlabelled_dicts_are_unaffected():
    assert _codes({"n": _field("Tehama silt loam", "b:0026")}) == []
    assert _codes({"n": _field({"statistic_name": "SE", "statistic_value": 1.0}, "b:0026")}) == []


def test_a_literal_but_misattributed_date_is_outside_grounding_and_left_to_review():
    """Documented boundary: the real weeding/sulfur events carry the harvest date "September 7 and 8". That text IS in the
    block (the sentence says the work happened *between planting and harvest on* those dates), so a grounding check
    cannot call it wrong; attributing it to the right event is a semantic judgement for the AI validator / a reviewer."""
    payload = {"date": _real("Management::sulfur_application")["date"]}
    assert _codes(payload) == []
