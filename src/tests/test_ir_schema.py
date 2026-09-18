import pytest
from pydantic import ValidationError

from ir_schema import (
    Citation,
    Coverage,
    Crop,
    DateRange,
    ExtractedField,
    ExtractionSource,
    Management,
    Observation,
    ProvenanceLabel,
    QuantityValue,
    Site,
    SourceLocator,
    Study,
    Treatment,
    TreatmentPair,
    Variable,
)


def make_source(anchor="b:0001"):
    return ExtractionSource(
        source_document_id="paperA",
        page_number=1,
        section_path=["Methods"],
        locators=[SourceLocator(kind="text", block_anchor=anchor)],
    )


def test_extracted_field_extracted_requires_value():
    with pytest.raises(ValidationError):
        ExtractedField[str](value=None, provenance_label=ProvenanceLabel.EXTRACTED, source=make_source())


def test_extracted_field_unresolved_requires_none_value_and_reason():
    with pytest.raises(ValidationError):
        ExtractedField[str](value="x", provenance_label=ProvenanceLabel.UNRESOLVED, source=make_source())
    with pytest.raises(ValidationError):
        ExtractedField[str](value=None, provenance_label=ProvenanceLabel.UNRESOLVED, source=make_source())
    # valid
    f = ExtractedField[str](
        value=None,
        provenance_label=ProvenanceLabel.UNRESOLVED,
        unresolved_reason="source never mentions this",
        source=make_source(),
    )
    assert f.value is None


def test_extracted_field_inferred_requires_reason():
    with pytest.raises(ValidationError):
        ExtractedField[str](value="x", provenance_label=ProvenanceLabel.INFERRED, source=make_source())
    f = ExtractedField[str](
        value="harvest occurred",
        provenance_label=ProvenanceLabel.INFERRED,
        unresolved_reason="yields are reported so a harvest must have occurred",
        source=make_source(),
    )
    assert f.provenance_label == ProvenanceLabel.INFERRED


def test_extracted_field_requires_locator_unless_unresolved():
    src_no_locator_dict = {
        "source_document_id": "paperA",
        "page_number": 1,
        "section_path": [],
        "locators": [],
    }
    with pytest.raises(ValidationError):
        ExtractionSource.model_validate(src_no_locator_dict)  # min_length=1 catches it here already


def test_quantity_value_conversion_completeness():
    with pytest.raises(ValidationError):
        QuantityValue(reported_text="3.2", reported_units="Mg/ha", converted_value=3200.0)
    q = QuantityValue(
        reported_text="3.2",
        reported_units="Mg/ha",
        converted_value=3200.0,
        converted_units="kg/ha",
        conversion_formula="x*1000",
        conversion_rationale="unit harmonization",
    )
    assert q.converted_value == 3200.0


def test_date_range_interval_and_relative_mutually_exclusive():
    with pytest.raises(ValidationError):
        DateRange(
            earliest="2007-06-01",
            latest="2007-08-01",
            reported_text="June-August 2007, before planting",
            relative_timing="before_planting",
        )


def test_date_range_earliest_before_latest():
    with pytest.raises(ValidationError):
        DateRange(earliest="2007-08-01", latest="2007-06-01", reported_text="backwards")


def test_management_date_never_inferred():
    date_field = ExtractedField[DateRange](
        value=DateRange(reported_text="unclear", relative_timing="before_planting"),
        provenance_label=ProvenanceLabel.INFERRED,
        unresolved_reason="inferred from context",
        source=make_source(),
    )
    with pytest.raises(ValidationError):
        Management(
            id="mgmt_1",
            citation_id="paperA",
            event_type=ExtractedField[str](
                value="tillage", provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()
            ),
            date=date_field,
        )


def test_management_date_unresolved_is_allowed():
    date_field = ExtractedField[DateRange](
        value=None,
        provenance_label=ProvenanceLabel.UNRESOLVED,
        unresolved_reason="no date given in source",
        source=make_source(),
    )
    m = Management(
        id="mgmt_1",
        citation_id="paperA",
        event_type=ExtractedField[str](
            value="tillage",
            provenance_label=ProvenanceLabel.INFERRED,
            unresolved_reason="a following planting event implies tillage occurred",
            source=make_source(),
        ),
        date=date_field,
    )
    assert m.date.value is None


def test_observation_treatment_mean_requires_empty_extracted_list():
    with pytest.raises(ValidationError):
        _build_observation(scope="treatment_mean", agg_field=None)

    obs = _build_observation(
        scope="treatment_mean",
        agg_field=ExtractedField[list](
            value=[], provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()
        ),
    )
    assert obs.aggregated_over_factors.value == []


def test_observation_aggregated_mean_requires_nonempty_or_unresolved():
    with pytest.raises(ValidationError):
        _build_observation(scope="aggregated_mean", agg_field=None)

    with pytest.raises(ValidationError):
        _build_observation(
            scope="aggregated_mean",
            agg_field=ExtractedField[list](
                value=[], provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()
            ),
        )

    obs = _build_observation(
        scope="aggregated_mean",
        agg_field=ExtractedField[list](
            value=["block"], provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()
        ),
    )
    assert obs.aggregated_over_factors.value == ["block"]


def _build_observation(scope, agg_field):
    return Observation(
        id="obs_1",
        dataset_id="ds_1",
        citation_id="paperA",
        site_id="site_1",
        treatment_id="treat_1",
        method_id="method_1",
        variable_name=ExtractedField[str](value="yield", provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
        value=ExtractedField[QuantityValue](
            value=QuantityValue(reported_text="3.2", reported_units="Mg/ha"),
            provenance_label=ProvenanceLabel.EXTRACTED,
            source=make_source(),
        ),
        reported_effect_scope=ExtractedField[str](value=scope, provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
        aggregated_over_factors=agg_field,
        temporal_info=ExtractedField[DateRange](
            value=DateRange(reported_text="2007", relative_timing="at_harvest"),
            provenance_label=ProvenanceLabel.EXTRACTED,
            source=make_source(),
        ),
        is_raw_replicate_level=ExtractedField[bool](value=True, provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
    )


def test_study_requires_nonempty_citation_ids():
    with pytest.raises(ValidationError):
        Study(id="study_1", citation_ids=[])
    s = Study(id="study_1", citation_ids=["paperA"])
    assert s.citation_ids == ["paperA"]


def test_treatment_study_id_may_be_unresolved():
    t = Treatment(
        id="control",
        citation_id="paperA",
        site_id="site_1",
        study_id=ExtractedField[str](
            value=None,
            provenance_label=ProvenanceLabel.UNRESOLVED,
            unresolved_reason="not yet assigned to a Study",
            source=make_source(),
        ),
        name=ExtractedField[str](value="control", provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
        definition=ExtractedField[str](value="no treatment applied", provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
    )
    assert t.study_id.provenance_label == ProvenanceLabel.UNRESOLVED


def _citation_fields(persistent_identifier=None):
    fields = dict(
        id="paperA",
        author=ExtractedField[str](value="A. Author", provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
        year=ExtractedField[int](value=2012, provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
        title=ExtractedField[str](value="A Title", provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
    )
    if persistent_identifier is not None:
        fields["persistent_identifier"] = persistent_identifier
    return fields


def test_citation_persistent_identifier_required_cannot_be_omitted():
    # Citation contract (finalized): persistent_identifier must always be
    # structurally present, the same as author/year/title -- never omitted.
    with pytest.raises(ValidationError):
        Citation(**_citation_fields())


def test_citation_persistent_identifier_extracted():
    c = Citation(
        **_citation_fields(
            persistent_identifier=ExtractedField[str](
                value="10.1234/example", provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()
            )
        )
    )
    assert c.persistent_identifier.value == "10.1234/example"


def test_citation_persistent_identifier_unresolved_when_no_doi_in_source():
    c = Citation(
        **_citation_fields(
            persistent_identifier=ExtractedField[str](
                value=None,
                provenance_label=ProvenanceLabel.UNRESOLVED,
                unresolved_reason="No DOI or other persistent identifier appears in the visible content.",
                source=make_source(),
            )
        )
    )
    assert c.persistent_identifier.provenance_label == ProvenanceLabel.UNRESOLVED
    assert c.persistent_identifier.value is None


def test_site_requires_id_and_name():
    with pytest.raises(ValidationError):
        Site(name=ExtractedField[str](value="Yolo County", provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()))
    with pytest.raises(ValidationError):
        Site(id="site_1")


def test_site_optional_fields_may_be_omitted():
    # Unlike Citation.persistent_identifier (finalized as required), Site's
    # other fields are genuinely optional -- confirm omitting all of them
    # still constructs cleanly, since Optional[ExtractedField[...]] fields
    # have shown a real, distinct Pydantic footgun elsewhere in this codebase.
    s = Site(id="site_1", name=ExtractedField[str](value="Yolo County", provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()))
    assert s.latitude is None
    assert s.country is None
    assert s.soil_context is None


def test_site_optional_field_extracted_with_quantity_value():
    s = Site(
        id="site_1",
        name=ExtractedField[str](value="Chaîne des Puys", provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
        elevation=ExtractedField[QuantityValue](
            value=QuantityValue(reported_text="900 m a.s.l.", reported_numeric_value=900.0, reported_units="m"),
            provenance_label=ProvenanceLabel.EXTRACTED,
            source=make_source(),
        ),
    )
    assert s.elevation.value.reported_numeric_value == 900.0


def test_site_optional_field_unresolved_when_not_stated():
    s = Site(
        id="site_1",
        name=ExtractedField[str](value="Chaîne des Puys", provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
        country=ExtractedField[str](
            value=None,
            provenance_label=ProvenanceLabel.UNRESOLVED,
            unresolved_reason="No country name appears in the visible content; only a region name is given.",
            source=make_source(),
        ),
    )
    assert s.country.provenance_label == ProvenanceLabel.UNRESOLVED
    assert s.country.value is None


# --------------------------------------------------------------------- #
# Variable, Crop, TreatmentPair, Coverage (added this sprint)
# --------------------------------------------------------------------- #


def test_variable_minimal_construction():
    v = Variable(
        id="soc_concentration",
        name=ExtractedField[str](value="soil organic carbon concentration", provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
    )
    assert v.units is None
    assert v.name.value == "soil organic carbon concentration"


def test_crop_references_species_by_bare_id_not_duplicating_fields():
    # Crop deliberately does NOT carry genus/species_epithet/scientific_name
    # itself -- it references Species by id (ir_schema.Crop's docstring).
    c = Crop(
        id="switchgrass_trailblazer",
        citation_id="paperA",
        species_id="panicum_virgatum",
        cultivar=ExtractedField[str](value="Trailblazer", provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
    )
    assert c.species_id == "panicum_virgatum"
    assert not hasattr(c, "genus")


def test_treatment_pair_valid_construction():
    tp = TreatmentPair(
        id="control_vs_compost",
        citation_id="paperA",
        treatment_id_1="control",
        treatment_id_2="compost",
        comparison_factor=ExtractedField[str](value="compost", provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
        comparison_label=ExtractedField[str](value="compost vs no compost", provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
    )
    assert tp.treatment_id_1 == "control"
    assert tp.use_for_validation is None


def test_treatment_pair_rejects_identical_treatments():
    with pytest.raises(ValidationError):
        TreatmentPair(
            id="bad_pair",
            citation_id="paperA",
            treatment_id_1="control",
            treatment_id_2="control",
            comparison_factor=ExtractedField[str](value="x", provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
            comparison_label=ExtractedField[str](value="y", provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
        )


def test_coverage_fields_are_plain_not_extracted_field_wrapped():
    # Coverage is deliberately the one entity (besides Study) with no
    # ExtractedField content -- row counts aren't claims from paper text.
    cov = Coverage(
        id="paperA_site1_soc",
        citation_id="paperA",
        site_id="site_1",
        annual_rows=4,
        notes="curator rollup",
    )
    assert cov.annual_rows == 4
    assert isinstance(cov.notes, str)


def test_coverage_row_counts_reject_negative():
    with pytest.raises(ValidationError):
        Coverage(id="bad_cov", citation_id="paperA", site_id="site_1", annual_rows=-1)


def test_observation_variable_id_and_crop_id_are_optional_and_additive():
    obs_kwargs = dict(
        id="obs1", dataset_id="ds1", citation_id="paperA", site_id="site_1", treatment_id="t1", method_id="m1",
        variable_name=ExtractedField[str](value="LAI", provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
        value=ExtractedField[QuantityValue](
            value=QuantityValue(reported_text="3.2", reported_numeric_value=3.2, reported_units="m2/m2"),
            provenance_label=ProvenanceLabel.EXTRACTED, source=make_source(),
        ),
        reported_effect_scope=ExtractedField[str](value="treatment_mean", provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
        aggregated_over_factors=ExtractedField[list[str]](value=[], provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
        temporal_info=ExtractedField[DateRange](
            value=DateRange(reported_text="2012"), provenance_label=ProvenanceLabel.EXTRACTED, source=make_source(),
        ),
        is_raw_replicate_level=ExtractedField[bool](value=True, provenance_label=ProvenanceLabel.EXTRACTED, source=make_source()),
    )
    # Without variable_id/crop_id at all -- must still construct (backward compatible).
    obs_without = Observation(**obs_kwargs)
    assert obs_without.variable_id is None
    assert obs_without.crop_id is None

    # With them set -- additive, variable_name is untouched.
    obs_with = Observation(**obs_kwargs, variable_id="soc_concentration", crop_id="switchgrass_trailblazer")
    assert obs_with.variable_id == "soc_concentration"
    assert obs_with.crop_id == "switchgrass_trailblazer"
    assert obs_with.variable_name.value == "LAI"
