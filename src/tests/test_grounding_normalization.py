"""Stage 1: grounding normalisation + coordinate transformation.

Real failures (replay baseline, tests/replay/baselines/stage0_baseline.json):
  - Philippe-2007-Six Site (run 20260925T132905): "45°42′ N" converted by the model to 45.7 / units "degrees" -- neither
    written in the block -- so the coordinates were dropped, the AI validator objected, and Site went unresolved,
    blocking Treatment/Observation/Coverage.
  - Kathryn-2020-Winter Site (run 20260923T095532): the source prints "36˚37´N" (ring above, acute accent); the model
    quoted "36°37′N" and grounding rejected the identical coordinate.
  - Philippe Variables: "Vcmax" / "Na" never grounded against "$V_{\\text{cmax}}$" / "$N_a$".
  - Philippe Table 3 dark respiration "–0.69" read as +0.69 (sign lost).
  - Table unit flags "m2" vs "(m 2 )", and "°" attached to a digit, falsely unsupported.

Every change only equates spellings of the SAME text, or tightens a check; the negative tests below pin that a
paraphrase, a wrong sign, a stray letter or a wrong decimal is still rejected.

Fixtures: tests/fixtures/stage1_grounding -- exact block texts of the real papers (trimmed content.md).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import coordinates, orchestrator, validators

FIXTURES = Path(__file__).parent / "fixtures" / "stage1_grounding"
PHILIPPE = "Philippe-2007-Six"
KATHRYN = "Kathryn-2020-Winter"


@pytest.fixture(autouse=True)
def papers(monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(FIXTURES))


def _blocks(paper: str) -> dict[str, str]:
    return validators._load_rendered_blocks(paper)


def _field(value, *anchors, label="EXTRACTED", paper=PHILIPPE):
    return {"value": value, "provenance_label": label, "source": {
        "source_document_id": paper, "page_number": 2, "section_path": [],
        "locators": [{"kind": "text", "block_anchor": a} for a in anchors]}}


def _codes(paper: str, payload: dict) -> list[str]:
    return [issue.code for issue in validators.validate_provenance(paper, payload)]


# --------------------------------------------------------------------------- #
# Coordinate parsing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text, decimal", [
    ("45°42′ N", 45.7),
    ("2°58′ E", 2 + 58 / 60),
    ("36˚37´N", 36 + 37 / 60),
    ("-121˚32´W", -(121 + 32 / 60)),          # a minus AND a W: negative once, never double-negated
    ("121°32'W", -(121 + 32 / 60)),
    ("45°42′30″ N", 45 + 42 / 60 + 30 / 3600),
    ("N 45°42′", 45.7),
    ("33.5° S", -33.5),
    ("121.5 W", -121.5),
])
def test_real_and_common_coordinate_spellings_parse_to_the_right_decimal(text, decimal):
    assert coordinates.parse_coordinate(text)["decimal"] == pytest.approx(decimal)


@pytest.mark.parametrize("text", [
    "45°60′ N",               # minutes >= 60
    "N 45°42′ S",             # two hemispheres
    "45°42′ N, 2°58′ E",      # two coordinates in one field
    "45.7",                   # a bare number is not recognisably a coordinate
    "about 45°42′ N",         # prose around it
    "",
])
def test_anything_that_is_not_one_well_formed_coordinate_is_not_parsed(text):
    assert coordinates.parse_coordinate(text) is None


def test_a_longitude_written_into_latitude_is_an_axis_problem():
    parsed = coordinates.parse_coordinate("2°58′ E")
    assert coordinates.axis_problem("lat", parsed) is not None
    assert coordinates.axis_problem("lon", parsed) is None


# --------------------------------------------------------------------------- #
# Deterministic transformation of a Site payload
# --------------------------------------------------------------------------- #

def _site(lat_text, lon_text=None, numeric=45.7, units="degrees"):
    payload = {"id": "s", "name": _field("Chaîne des Puys", "b:0028"),
               "latitude": _field({"reported_text": lat_text, "reported_numeric_value": numeric, "reported_units": units}, "b:0028")}
    if lon_text:
        payload["longitude"] = _field({"reported_text": lon_text, "reported_numeric_value": None, "reported_units": ""}, "b:0028")
    return payload


def test_a_dms_coordinate_gets_a_derived_decimal_and_keeps_its_literal():
    payload = coordinates.apply_coordinate_transformations("Site", _site("45°42′ N", "2°58′ E"))
    lat = payload["latitude"]["value"]
    assert lat["reported_text"] == "45°42′ N"               # the literal is untouched
    assert lat["reported_numeric_value"] is None             # no single number is written
    assert lat["reported_units"] == "°"                      # what the text actually carries
    assert lat["converted_value"] == pytest.approx(45.7)
    assert lat["converted_units"] == "decimal degrees"
    assert lat["conversion_formula"] and lat["conversion_rationale"]
    assert payload["longitude"]["value"]["converted_value"] == pytest.approx(2.966667, abs=1e-6)


def test_the_models_own_decimal_is_never_trusted():
    # The model put a wrong decimal in converted_value; the pipeline recomputes it from the text.
    payload = _site("45°42′ N")
    payload["latitude"]["value"].update(converted_value=45.42, converted_units="deg", conversion_formula="x", conversion_rationale="y")
    coordinates.apply_coordinate_transformations("Site", payload)
    assert payload["latitude"]["value"]["converted_value"] == pytest.approx(45.7)


def test_a_signed_decimal_already_written_as_the_number_needs_no_conversion():
    payload = _site("-121.53", numeric=-121.53, units="")
    payload["longitude"] = payload.pop("latitude")
    coordinates.apply_coordinate_transformations("Site", payload)
    assert "converted_value" not in payload["longitude"]["value"]
    assert payload["longitude"]["value"]["reported_numeric_value"] == -121.53


def test_unparseable_or_wrong_axis_text_and_other_entities_are_left_for_the_validators():
    garbled = _site("somewhere north")
    assert coordinates.apply_coordinate_transformations("Site", garbled)["latitude"]["value"]["reported_numeric_value"] == 45.7
    wrong_axis = _site("2°58′ E")
    assert "converted_value" not in coordinates.apply_coordinate_transformations("Site", wrong_axis)["latitude"]["value"]
    other = _site("45°42′ N")
    assert "converted_value" not in coordinates.apply_coordinate_transformations("Observation", other)["latitude"]["value"]


# --------------------------------------------------------------------------- #
# Validation of the real Philippe / Kathryn Site blocks
# --------------------------------------------------------------------------- #

def test_the_philippe_site_coordinates_now_ground_after_transformation():
    payload = coordinates.apply_coordinate_transformations("Site", _site("45°42′ N", "2°58′ E"))
    assert _codes(PHILIPPE, payload) == []


def test_the_philippe_run_payload_that_was_rejected_is_rejected_again_without_the_transformation():
    # Exactly what conversion attempt 5 of run 20260925T132905 proposed: the model's own decimal as the reported number.
    assert "provenance_numeric_mismatch" in _codes(PHILIPPE, _site("45°42′ N", numeric=45.7, units=""))


def test_a_converted_value_that_does_not_follow_from_the_text_is_rejected():
    payload = coordinates.apply_coordinate_transformations("Site", _site("45°42′ N"))
    payload["latitude"]["value"]["converted_value"] = 45.42
    assert "coordinate_conversion_mismatch" in _codes(PHILIPPE, payload)


def test_a_longitude_filed_as_latitude_is_rejected():
    payload = _site("2°58′ E", numeric=None, units="°")
    assert "coordinate_axis_mismatch" in _codes(PHILIPPE, payload)


def test_the_kathryn_coordinates_quoted_with_standard_symbols_now_ground():
    # The model wrote "36°37′N"; the source prints "36˚37´N".
    payload = {
        "id": "s", "name": _field("USDA-ARS", "b:0035", paper=KATHRYN),
        "latitude": _field({"reported_text": "36°37′N", "reported_units": ""}, "b:0035", paper=KATHRYN),
        "longitude": _field({"reported_text": "-121°32′W", "reported_units": ""}, "b:0035", paper=KATHRYN),
    }
    coordinates.apply_coordinate_transformations("Site", payload)
    assert _codes(KATHRYN, payload) == []
    assert payload["longitude"]["value"]["converted_value"] == pytest.approx(-121.533333, abs=1e-6)


def test_a_coordinate_the_block_does_not_state_is_still_rejected():
    payload = coordinates.apply_coordinate_transformations("Site", _site("45°24′ N"))
    assert "provenance_reported_text_mismatch" in _codes(PHILIPPE, payload)


# --------------------------------------------------------------------------- #
# LaTeX notation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value, anchor", [
    ("Vcmax", "b:0044"),          # "$V_{\text{cmax}}$"
    ("Vcmax", "b:0046"),          # "$V_{\rm cmax}$"
    ("Jmax", "b:0044"),           # "$J_{max}$"
    ("Na", "b:0044"),             # "$N_a$"
    ("Ci", "b:0044"),             # "$C_i$"
    ("N concentration on an area basis (Na)", "b:0044"),
])
def test_latex_rendered_symbols_ground_against_their_plain_spelling(value, anchor):
    assert validators._value_supported_by_text(value, _blocks(PHILIPPE)[anchor])


@pytest.mark.parametrize("value", [
    "maximum electron transport rate (Jmax)",               # source: "($J_{max}$; μmol ..." -- the ')' is not written
    "leaf nitrogen concentration on an area basis (Na)",    # source has no "leaf" here
    "Vcmin",
])
def test_paraphrases_are_still_rejected(value):
    assert not validators._value_supported_by_text(value, _blocks(PHILIPPE)["b:0044"])


def test_existing_latex_units_normalisation_is_unchanged():
    assert validators._normalize_typography("$4.6\\pm0.4~\\mathrm{Mg~ha^{-1}}$") == "4.6±0.4 Mg ha-1"
    assert validators._normalize_typography("$CO_2$") == "CO2"


# --------------------------------------------------------------------------- #
# Signed numbers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text, numbers", [
    ("–0.69 a", [-0.69]),            # Philippe Table 3 Rd: an en dash as a minus sign
    ("−25 °C", [-25.0]),
    ("[-1.2, 3]", [-1.2, 3.0]),
    ("0-0.1", [0.0, 0.1]),           # a range
    ("15 – 30 cm", [15.0, 30.0]),    # a spaced range
    ("g m -2", [2.0]),               # a unit exponent
    ("pH 6.0-7.0", [6.0, 7.0]),
])
def test_a_dash_is_a_sign_only_where_it_cannot_be_a_range_or_an_exponent(text, numbers):
    assert validators._numbers_in(text) == numbers


def test_a_negative_value_keeps_its_sign_through_grounding():
    rd = {"reported_text": "–0.69 a", "reported_numeric_value": -0.69, "reported_units": "µmol m–2 s–1"}
    cited = [("b:0193", _blocks(PHILIPPE)["b:0193"])]
    assert validators._nested_value_issues("value", rd, cited) == []
    sign_dropped = {**rd, "reported_numeric_value": 0.69}
    assert [i.code for i in validators._nested_value_issues("value", sign_dropped, cited)] == ["provenance_numeric_mismatch"]


# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("units, text", [
    ("°", "(45°42′ N, 2°58′ E)"),                    # a symbol unit between two digits
    ("m2", "Sapling total leaf area (m 2 )"),        # Marker's spaced superscript
    ("cm2", "Leaf area (cm 2 )"),
    ("°C", "at 25°C"),
    ("%", "reduced by 45%"),
    ("µmol m-2 s-1", "(μmol m–2 s–1)"),              # micro sign vs Greek mu
    ("g m‑2", "Na (g m–2)"),                          # non-breaking hyphen vs en dash
])
def test_units_written_differently_but_identically_are_supported(units, text):
    assert validators._unit_supported(units, [text])


@pytest.mark.parametrize("units, text", [
    ("g", "a big dog"),              # a stray letter of a word
    ("m2", "the m 2nd"),             # a digit that starts a word
    ("mm", "2 cm"),
])
def test_units_not_written_are_still_unsupported(units, text):
    assert not validators._unit_supported(units, [text])


def test_the_table_pass_unit_check_uses_the_same_rule():
    # Philippe Table 2 / Table 3 unit hints were falsely flagged "not found in the table" in run 20260925T132905.
    assert orchestrator._units_supported("m2", ["| Sapling total leaf area (m 2 ) |"])
    assert orchestrator._units_supported("g m‑2", ["| Na (g m–2) |"])
    assert not orchestrator._units_supported("kg ha-1", ["| Na (g m–2) |"])


# --------------------------------------------------------------------------- #
# Genus abbreviation, grounded in the same paper
# --------------------------------------------------------------------------- #

def test_a_full_binomial_grounds_against_its_abbreviation_when_the_paper_spells_it_out():
    blocks = _blocks(PHILIPPE)   # b:0003/b:0006 spell "Pinus sylvestris"; b:0028 writes "P. sylvestris"
    value = "25-year-old natural Pinus sylvestris stand"
    assert not validators._value_supported_by_text(value, blocks["b:0028"])
    assert any(validators._value_supported_by_text(v, blocks["b:0028"]) for v in validators._genus_abbreviation_variants(value, blocks))
    payload = {"id": "s", "name": _field("Chaîne des Puys", "b:0028"), "description": _field(value, "b:0028")}
    assert _codes(PHILIPPE, payload) == []


def test_a_binomial_the_paper_never_spells_out_is_not_expanded():
    blocks = {"b:0001": "a natural P. nigra stand"}
    assert validators._genus_abbreviation_variants("a natural Pinus nigra stand", blocks) == []


def test_unicode_subscript_digits_match_the_spaced_rendering():
    # Daren-1997-Canopy b:0028 renders "NH 4 NO 3"; a model writes "NH₄NO₃". The paraphrase around it is still judged.
    block = "All plots at both locations received 122 kg N ha -1 in the form of NH 4 NO 3 approximately 1 wk"
    assert validators._value_supported_by_text("NH₄NO₃", block)
    assert validators._value_supported_by_text("122 kg N ha⁻¹ in the form of NH₄NO₃", block)
    assert not validators._value_supported_by_text("122 kg N ha⁻¹ as NH₄NO₃", block)
