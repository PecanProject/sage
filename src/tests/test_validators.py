from ir_schema import (
    Citation,
    Coverage,
    Crop,
    DateRange,
    ExtractedField,
    ExtractionSource,
    IRDataset,
    Management,
    Observation,
    ProvenanceLabel,
    QuantityValue,
    Site,
    SourceLocator,
    Species,
    Study,
    Treatment,
    TreatmentPair,
    Variable,
)
from validators import _normalize_typography, _value_supported_by_text, validate_dataset


def src(anchor="b:0001"):
    return ExtractionSource(
        source_document_id="paperA",
        page_number=1,
        section_path=[],
        locators=[SourceLocator(kind="text", block_anchor=anchor)],
    )


def ef(value, label=ProvenanceLabel.EXTRACTED, reason=None):
    return ExtractedField(value=value, provenance_label=label, unresolved_reason=reason, source=src())


def base_citation(cid="paperA"):
    return Citation(
        id=cid,
        author=ef("Smukler S"),
        year=ef(2012),
        title=ef("Nutrient cycling study"),
        persistent_identifier=ef("10.1234/example"),
    )


def base_site(sid="site_1"):
    return Site(id=sid, name=ef("Yolo County"))


def resolved_study_ref(study_id):
    return ef(study_id)


def unresolved_study_ref():
    return ExtractedField(value=None, provenance_label=ProvenanceLabel.UNRESOLVED, unresolved_reason="not yet assigned", source=src())


def make_treatment(tid, citation_id, site_id, study_field, name, control=None):
    return Treatment(
        id=tid,
        citation_id=citation_id,
        site_id=site_id,
        study_id=study_field,
        name=ef(name),
        definition=ef(f"{name} definition"),
        control_status=ef(control) if control is not None else None,
    )


def test_clean_dataset_with_studies_passes():
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA"), base_citation("paperB")],
        studies=[Study(id="study_1", citation_ids=["paperA", "paperB"])],
        sites=[base_site("site_1")],
        treatments=[
            make_treatment("control", "paperA", "site_1", resolved_study_ref("study_1"), "control", control=True),
            make_treatment("n_fert", "paperB", "site_1", resolved_study_ref("study_1"), "n_fert"),
        ],
    )
    issues = validate_dataset(ds)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []


def test_treatment_name_unique_within_study_not_citation():
    # Two treatments named "control", same study, different citations ->
    # should now conflict (Option B: scope is study_id, not citation_id).
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA"), base_citation("paperB")],
        studies=[Study(id="study_1", citation_ids=["paperA", "paperB"])],
        sites=[base_site("site_1")],
        treatments=[
            make_treatment("control_a", "paperA", "site_1", resolved_study_ref("study_1"), "control"),
            make_treatment("control_b", "paperB", "site_1", resolved_study_ref("study_1"), "control"),
        ],
    )
    issues = validate_dataset(ds)
    codes = [i.code for i in issues if i.severity == "error"]
    assert "duplicate_treatment_name" in codes


def test_treatment_name_same_across_different_studies_is_fine():
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA"), base_citation("paperB")],
        studies=[Study(id="study_1", citation_ids=["paperA"]), Study(id="study_2", citation_ids=["paperB"])],
        sites=[base_site("site_1")],
        treatments=[
            make_treatment("control_a", "paperA", "site_1", resolved_study_ref("study_1"), "control"),
            make_treatment("control_b", "paperB", "site_1", resolved_study_ref("study_2"), "control"),
        ],
    )
    issues = validate_dataset(ds)
    codes = [i.code for i in issues if i.severity == "error"]
    assert "duplicate_treatment_name" not in codes


def test_unresolved_study_id_falls_back_to_citation_scope():
    # Two treatments, same citation, both study_id UNRESOLVED, same name ->
    # falls back to citation_id scoping -> still a conflict.
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA")],
        studies=[],
        sites=[base_site("site_1")],
        treatments=[
            make_treatment("control_1", "paperA", "site_1", unresolved_study_ref(), "control"),
            make_treatment("control_2", "paperA", "site_1", unresolved_study_ref(), "control"),
        ],
    )
    issues = validate_dataset(ds)
    codes = [i.code for i in issues if i.severity == "error"]
    assert "duplicate_treatment_name" in codes
    # also expect the warning that study_id is unresolved
    warn_codes = [i.code for i in issues if i.severity == "warning"]
    assert "treatment_study_unresolved" in warn_codes


def test_dangling_study_ref_is_error():
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA")],
        studies=[],  # no Study defined at all
        sites=[base_site("site_1")],
        treatments=[
            make_treatment("control", "paperA", "site_1", resolved_study_ref("study_ghost"), "control"),
        ],
    )
    issues = validate_dataset(ds)
    codes = [i.code for i in issues if i.severity == "error"]
    assert "dangling_study_ref" in codes


def test_study_dangling_citation_ref_is_error():
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA")],
        studies=[Study(id="study_1", citation_ids=["paperA", "paper_ghost"])],
    )
    issues = validate_dataset(ds)
    codes = [i.code for i in issues if i.severity == "error"]
    assert "dangling_citation_ref" in codes


def test_observation_citation_id_may_differ_from_treatment_citation_id():
    # Option B: Observation.citation_id no longer required to equal
    # Treatment.citation_id -- this must NOT be flagged as an error.
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA"), base_citation("paperB")],
        studies=[Study(id="study_1", citation_ids=["paperA", "paperB"])],
        sites=[base_site("site_1")],
        treatments=[make_treatment("control", "paperA", "site_1", resolved_study_ref("study_1"), "control")],
        observations=[
            Observation(
                id="obs_1",
                dataset_id="ds1",
                citation_id="paperB",  # deliberately different citation than the Treatment
                site_id="site_1",
                treatment_id="control",
                method_id="method_1",
                variable_name=ef("yield"),
                value=ExtractedField(
                    value=QuantityValue(reported_text="3.2", reported_units="Mg/ha"),
                    provenance_label=ProvenanceLabel.EXTRACTED,
                    source=src(),
                ),
                reported_effect_scope=ef("treatment_mean"),
                aggregated_over_factors=ExtractedField(value=[], provenance_label=ProvenanceLabel.EXTRACTED, source=src()),
                temporal_info=ExtractedField(
                    value=DateRange(reported_text="2007", relative_timing="at_harvest"),
                    provenance_label=ProvenanceLabel.EXTRACTED,
                    source=src(),
                ),
                is_raw_replicate_level=ef(True),
            )
        ],
    )
    issues = validate_dataset(ds)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == [], f"unexpected errors: {errors}"


def test_observation_site_id_mismatch_still_an_error():
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA")],
        studies=[Study(id="study_1", citation_ids=["paperA"])],
        sites=[base_site("site_1"), base_site("site_2")],
        treatments=[make_treatment("control", "paperA", "site_1", resolved_study_ref("study_1"), "control")],
        observations=[
            Observation(
                id="obs_1",
                dataset_id="ds1",
                citation_id="paperA",
                site_id="site_2",  # mismatched vs. Treatment's site_1
                treatment_id="control",
                method_id="method_1",
                variable_name=ef("yield"),
                value=ExtractedField(
                    value=QuantityValue(reported_text="3.2", reported_units="Mg/ha"),
                    provenance_label=ProvenanceLabel.EXTRACTED,
                    source=src(),
                ),
                reported_effect_scope=ef("treatment_mean"),
                aggregated_over_factors=ExtractedField(value=[], provenance_label=ProvenanceLabel.EXTRACTED, source=src()),
                temporal_info=ExtractedField(
                    value=DateRange(reported_text="2007", relative_timing="at_harvest"),
                    provenance_label=ProvenanceLabel.EXTRACTED,
                    source=src(),
                ),
                is_raw_replicate_level=ef(True),
            )
        ],
    )
    issues = validate_dataset(ds)
    codes = [i.code for i in issues if i.severity == "error"]
    assert "site_id_mismatch" in codes


def test_management_cannot_span_multiple_studies():
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA"), base_citation("paperB")],
        studies=[Study(id="study_1", citation_ids=["paperA"]), Study(id="study_2", citation_ids=["paperB"])],
        sites=[base_site("site_1")],
        treatments=[
            make_treatment("t1", "paperA", "site_1", resolved_study_ref("study_1"), "t1"),
            make_treatment("t2", "paperB", "site_1", resolved_study_ref("study_2"), "t2"),
        ],
        managements=[
            Management(
                id="mgmt_1",
                citation_id="paperA",
                treatment_ids=ExtractedField(value=["t1", "t2"], provenance_label=ProvenanceLabel.EXTRACTED, source=src()),
                event_type=ef("tillage"),
                date=ExtractedField(
                    value=DateRange(reported_text="2007-05-01"),
                    provenance_label=ProvenanceLabel.EXTRACTED,
                    source=src(),
                ),
            )
        ],
    )
    issues = validate_dataset(ds)
    codes = [i.code for i in issues if i.severity == "error"]
    assert "management_spans_multiple_studies" in codes


def test_at_most_one_control_per_study_site():
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA"), base_citation("paperB")],
        studies=[Study(id="study_1", citation_ids=["paperA", "paperB"])],
        sites=[base_site("site_1")],
        treatments=[
            make_treatment("control_a", "paperA", "site_1", resolved_study_ref("study_1"), "control_a", control=True),
            make_treatment("control_b", "paperB", "site_1", resolved_study_ref("study_1"), "control_b", control=True),
        ],
    )
    issues = validate_dataset(ds)
    codes = [i.code for i in issues if i.severity == "error"]
    assert "multiple_control_treatments" in codes


def test_dangling_citation_ref_on_treatment():
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA")],
        studies=[Study(id="study_1", citation_ids=["paperA"])],
        sites=[base_site("site_1")],
        treatments=[make_treatment("t1", "paper_ghost", "site_1", resolved_study_ref("study_1"), "t1")],
    )
    issues = validate_dataset(ds)
    codes = [i.code for i in issues if i.severity == "error"]
    assert "dangling_citation_ref" in codes


def test_duplicate_site_name_is_error():
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA")],
        sites=[base_site("site_1"), base_site("site_2")],  # both default to name "Yolo County"
    )
    issues = validate_dataset(ds)
    codes = [i.code for i in issues if i.severity == "error"]
    assert "duplicate_site_name" in codes


def test_distinct_site_names_pass():
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA")],
        sites=[base_site("site_1"), Site(id="site_2", name=ef("Auvergne"))],
    )
    issues = validate_dataset(ds)
    codes = [i.code for i in issues if i.severity == "error"]
    assert "duplicate_site_name" not in codes


# --------------------------------------------------------------------- #
# Variable, Crop, TreatmentPair, Coverage whole-graph checks (added this sprint)
# --------------------------------------------------------------------- #


def base_species(sid="sp_1"):
    return Species(id=sid, genus=ef("Panicum"), species_epithet=ef("virgatum"), scientific_name=ef("Panicum virgatum"))


def test_crop_dangling_species_ref_is_error():
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA")],
        species=[base_species("sp_1")],
        crops=[Crop(id="crop1", citation_id="paperA", species_id="no_such_species")],
    )
    codes = [i.code for i in validate_dataset(ds) if i.severity == "error"]
    assert "dangling_species_ref" in codes


def test_crop_valid_species_ref_passes():
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA")],
        species=[base_species("sp_1")],
        crops=[Crop(id="crop1", citation_id="paperA", species_id="sp_1")],
    )
    codes = [i.code for i in validate_dataset(ds) if i.severity == "error"]
    assert "dangling_species_ref" not in codes


def test_treatment_pair_dangling_treatment_ref_is_error():
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA")],
        sites=[base_site("site_1")],
        studies=[Study(id="study_1", citation_ids=["paperA"])],
        treatments=[make_treatment("control", "paperA", "site_1", resolved_study_ref("study_1"), "control")],
        treatment_pairs=[TreatmentPair(
            id="pair1", citation_id="paperA", treatment_id_1="control", treatment_id_2="ghost",
            comparison_factor=ef("compost"), comparison_label=ef("x"),
        )],
    )
    codes = [i.code for i in validate_dataset(ds) if i.severity == "error"]
    assert "dangling_treatment_ref" in codes


def test_treatment_pair_citation_scope_mismatch_is_error():
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA"), base_citation("paperB")],
        sites=[base_site("site_1")],
        studies=[Study(id="study_1", citation_ids=["paperA", "paperB"])],
        treatments=[
            make_treatment("control", "paperA", "site_1", resolved_study_ref("study_1"), "control"),
            make_treatment("compost", "paperB", "site_1", resolved_study_ref("study_1"), "compost"),
        ],
        treatment_pairs=[TreatmentPair(
            id="pair1", citation_id="paperA", treatment_id_1="control", treatment_id_2="compost",
            comparison_factor=ef("compost"), comparison_label=ef("x"),
        )],
    )
    codes = [i.code for i in validate_dataset(ds) if i.severity == "error"]
    assert "treatment_pair_citation_scope_mismatch" in codes


def test_treatment_pair_valid_passes_clean():
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA")],
        sites=[base_site("site_1")],
        studies=[Study(id="study_1", citation_ids=["paperA"])],
        treatments=[
            make_treatment("control", "paperA", "site_1", resolved_study_ref("study_1"), "control"),
            make_treatment("compost", "paperA", "site_1", resolved_study_ref("study_1"), "compost"),
        ],
        treatment_pairs=[TreatmentPair(
            id="pair1", citation_id="paperA", treatment_id_1="control", treatment_id_2="compost",
            comparison_factor=ef("compost"), comparison_label=ef("x"),
        )],
    )
    codes = [i.code for i in validate_dataset(ds) if i.severity == "error"]
    assert codes == []


def test_duplicate_variable_name_is_error():
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA")],
        variables=[Variable(id="v1", name=ef("SOC")), Variable(id="v2", name=ef("SOC"))],
    )
    codes = [i.code for i in validate_dataset(ds) if i.severity == "error"]
    assert "duplicate_variable_name" in codes


def test_observation_variable_id_dangling_is_error():
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA")],
        sites=[base_site("site_1")],
        studies=[Study(id="study_1", citation_ids=["paperA"])],
        treatments=[make_treatment("control", "paperA", "site_1", resolved_study_ref("study_1"), "control")],
        observations=[Observation(
            id="obs1", dataset_id="ds1", citation_id="paperA", site_id="site_1", treatment_id="control",
            method_id="m1", variable_id="no_such_variable",
            variable_name=ef("LAI"),
            value=ef(QuantityValue(reported_text="3.2", reported_units="m2/m2")),
            reported_effect_scope=ef("treatment_mean"),
            aggregated_over_factors=ef([]),
            temporal_info=ef(DateRange(reported_text="2012")),
            is_raw_replicate_level=ef(True),
        )],
    )
    codes = [i.code for i in validate_dataset(ds) if i.severity == "error"]
    assert "dangling_variable_ref" in codes


def test_coverage_dangling_site_ref_is_error():
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA")],
        coverages=[Coverage(id="cov1", citation_id="paperA", site_id="no_such_site")],
    )
    codes = [i.code for i in validate_dataset(ds) if i.severity == "error"]
    assert "dangling_site_ref" in codes


def test_coverage_valid_site_ref_passes():
    ds = IRDataset(
        dataset_id="ds1",
        citations=[base_citation("paperA")],
        sites=[base_site("site_1")],
        coverages=[Coverage(id="cov1", citation_id="paperA", site_id="site_1", annual_rows=5)],
    )
    codes = [i.code for i in validate_dataset(ds) if i.severity == "error"]
    assert codes == []


# ---------------------------------------------------------------------------
# _value_supported_by_text -- typographic normalization (Fix 2, design-review
# session). Real confirmed cases from the Daren-1997-Canopy paper audit: a
# non-breaking hyphen the model reproduced vs. content.md's plain ASCII
# hyphen for "fine-loamy" (Site.soil_context), and "Ey × FF" rendered as
# literal Unicode "×" in three places and LaTeX "$\times$" in a fourth
# within the SAME source paragraph (Crop.notes) -- both were real,
# genuinely-supported values rejected as provenance_value_mismatch purely
# because of the encoding difference, not a real grounding failure.
# ---------------------------------------------------------------------------

def test_normalize_typography_collapses_hyphen_variants_to_ascii():
    assert _normalize_typography("fine‑loamy") == "fine-loamy"
    assert _normalize_typography("fine-loamy") == "fine-loamy"


def test_normalize_typography_collapses_latex_times_to_unicode():
    assert _normalize_typography("Ey $\\times$ FF") == "Ey × FF"
    assert _normalize_typography("Ey × FF") == "Ey × FF"


def test_value_supported_by_text_accepts_non_breaking_hyphen_against_ascii_source():
    # Real Daren-1997-Canopy case: model value has U+2011, content.md has U+002D.
    value = "Webster silty clay loam (fine‑loamy, mixed, mesic Typic Endoaquolls)"
    source_text = "on a Webster silty clay loam (fine-loamy, mixed, mesic Typic Endoaquolls)."
    assert _value_supported_by_text(value, source_text) is True


def test_value_supported_by_text_accepts_unicode_times_against_latex_source():
    # Real Daren-1997-Canopy case: model value uses literal "×", content.md
    # renders the identical phrase as LaTeX "$\times$" at this exact spot.
    value = "Ey × FF High IVDMD Cycle 3 population was selected from the same base population as Trailblazer"
    source_text = "The Ey $\\times$ FF High IVDMD Cycle 3 population was selected from the same base population as Trailblazer using three cycles"
    assert _value_supported_by_text(value, source_text) is True


def test_value_supported_by_text_still_rejects_genuinely_unsupported_values():
    # The normalization must never turn into a fuzzy match -- an actually
    # wrong value must still fail.
    assert _value_supported_by_text("elevated CO2", "the plots received ambient CO2 only") is False


def test_value_supported_by_text_em_and_en_dash_normalize_too():
    assert _value_supported_by_text("well–watered", "the plants were well-watered throughout") is True
    assert _value_supported_by_text("well—watered", "the plants were well-watered throughout") is True


# ---------------------------------------------------------------------------
# _value_supported_by_text -- Management.event_type / Observation.variable_name
# free-text exemption (Fix 1, design-review session). Both fields are named
# by pipeline/vocab.py's own docstring and ir_schema.py's ProvenanceLabel
# docstring as "deliberately free text at this layer" (Playbook Section 5),
# alongside the already-exempted reported_effect_scope -- the literal-match
# requirement made them structurally unable to pass whenever the paper
# describes an event/quantity in prose rather than naming it. Real cases
# below are taken directly from the three paper audits.
# ---------------------------------------------------------------------------

def test_event_type_accepts_a_reasonable_label_not_a_literal_quote():
    # Real Daren-1997-Canopy case (Management/fertilizer_nitrogen, b:0028).
    source_text = (
        "All plots at both locations received 122 kg N ha-1 in the form of "
        "NH4NO3 approximately 1 wk following initiation of spring growth."
    )
    assert _value_supported_by_text("fertilizer application", source_text, field_name="event_type") is True


def test_event_type_accepts_another_real_paraphrase_case():
    # Real Oceologia-1998 case (Management/seasonal_relocation, b:0030).
    source_text = (
        "Tubs were brought back into the glasshouses in trenches so that "
        "their soil surface was even with the external soil surface."
    )
    assert _value_supported_by_text("tubs moved to an outside garden", source_text, field_name="event_type") is True


def test_variable_name_accepts_a_descriptive_label_not_a_literal_quote():
    source_text = "Total soil C was determined on all air-dried ground (<0.5 mm) soil samples by combustion."
    assert _value_supported_by_text("soil organic carbon", source_text, field_name="variable_name") is True


def test_event_type_and_variable_name_still_require_a_real_non_empty_anchor():
    # The exemption removes the literal-text requirement, not the
    # requirement that the cited block actually contain something.
    assert _value_supported_by_text("fertilizer application", "", field_name="event_type") is False
    assert _value_supported_by_text("soil organic carbon", "   ", field_name="variable_name") is False


def test_fields_outside_the_exemption_list_still_require_literal_grounding():
    # A field with the exact same "label" flavor but NOT in the exemption
    # set (e.g. Treatment.name) must be unaffected by this change.
    assert _value_supported_by_text("fertilizer application", "the plots received nitrogen", field_name="name") is False
