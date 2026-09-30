"""
IR schema — Pydantic models.

Based on the IR Architecture Specification v1.0, with a Study entity grouping citations. Holds data shape and
per-entity construction invariants; whole-graph invariants live in `validators.py`.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Generic, Literal, Optional, TypeVar, Union

from pydantic import BaseModel, Field, model_validator

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Support types (IR spec Section 6)
# ---------------------------------------------------------------------------


class ProvenanceLabel(str, Enum):
    """Section 5 / Table 3. The only enum in the IR support layer other than
    SourcePriorityTier -- everything else (variable_name, event_type,
    reported_effect_scope aside) is deliberately free text at this layer."""

    EXTRACTED = "EXTRACTED"
    INFERRED = "INFERRED"
    UNRESOLVED = "UNRESOLVED"


class SourcePriorityTier(str, Enum):
    """The Protocol's source-priority order."""

    ARCHIVED_DATA = "archived_data"
    SUPPLEMENT = "supplement"
    PUB_TABLE = "pub_table"
    PUB_TEXT = "pub_text"
    FIGURE = "figure"
    OTHER = "other"


class InferenceSource(str, Enum):
    """Which regime a `confidence` value came from (a raw LLM score is not calibrated)."""

    LLM = "llm"
    CURATOR = "curator"


class SourceLocator(BaseModel):
    """IR spec Section 6.2: TextLocator | TableLocator | FigureLocator |
    ObjectLocator. Modeled as one discriminated shape rather than four
    classes since the differences are which optional fields are populated,
    not different required shapes -- keeps `get_schema` simpler for the
    agent to consume. `kind` is the discriminant."""

    kind: Literal["text", "table", "figure", "object"]
    block_anchor: Optional[str] = Field(
        default=None,
        description="content.md anchor, e.g. 'b:0123', when the locator points at a specific rendered block.",
    )
    table_id: Optional[str] = None
    row: Optional[int] = None
    col: Optional[int] = None
    figure_id: Optional[str] = None
    panel_label: Optional[str] = Field(
        default=None,
        description="Free text pending Document Schema owner's source_panel decision (Section 5, Table 20 item 2).",
    )
    detail: Optional[str] = None

    @model_validator(mode="after")
    def _kind_matches_fields(self) -> "SourceLocator":
        if self.kind == "table" and self.table_id is None:
            raise ValueError("table locator requires table_id (row/col optional per spec Section 6.2)")
        if self.kind == "figure" and self.figure_id is None:
            raise ValueError("figure locator requires figure_id")
        return self


class ExtractionSource(BaseModel):
    """IR spec Section 6.2, plus `source_priority_tier`."""

    source_document_id: str
    # The 1-indexed PDF page of the FIRST cited block, filled deterministically from provenance.json by the orchestrator
    # (`_apply_source_pages`) -- never by a model. Optional because the honest value is "unknown" when provenance does
    # not give one; a required int made the models invent one (real runs: 160 of 160 values were 1 or 0, the true pages
    # were 2-6) or mark real fields UNRESOLVED for want of it ("page number not available").
    page_number: Optional[int] = None
    section_path: list[str] = Field(default_factory=list)
    locators: list[SourceLocator] = Field(min_length=1)
    raw_excerpt: Optional[str] = None
    notes: Optional[str] = None
    source_priority_tier: Optional[SourcePriorityTier] = None


class ExtractedField(BaseModel, Generic[T]):
    """IR spec Section 6.1, plus `inference_source` and a 0-100 int confidence.

    Construction-time invariants enforced here (Section 9.1, Table 18, row 1):
      - EXTRACTED/INFERRED -> value is not None
      - UNRESOLVED -> value is None and unresolved_reason is not None
      - INFERRED -> unresolved_reason populated (repurposed as an
        inference-basis note, per the spec's own field doc)
    """

    value: Optional[T] = None
    provenance_label: ProvenanceLabel
    unresolved_reason: Optional[str] = None
    confidence: Optional[int] = Field(default=None, ge=0, le=100)
    inference_source: Optional[InferenceSource] = None
    source: ExtractionSource

    @model_validator(mode="after")
    def _provenance_invariants(self) -> "ExtractedField":
        if self.provenance_label in (ProvenanceLabel.EXTRACTED, ProvenanceLabel.INFERRED):
            if self.value is None:
                raise ValueError(
                    f"provenance_label={self.provenance_label.value} requires a non-null value"
                )
        if self.provenance_label == ProvenanceLabel.UNRESOLVED:
            if self.value is not None:
                raise ValueError("provenance_label=UNRESOLVED requires value=None")
            if not self.unresolved_reason:
                raise ValueError("provenance_label=UNRESOLVED requires unresolved_reason")
        if self.provenance_label == ProvenanceLabel.INFERRED and not self.unresolved_reason:
            raise ValueError(
                "provenance_label=INFERRED requires unresolved_reason populated as an inference-basis note"
            )
        # EXTRACTED/INFERRED fields must carry at least one locator.
        if self.provenance_label != ProvenanceLabel.UNRESOLVED and not self.source.locators:
            raise ValueError("EXTRACTED/INFERRED field requires at least one source locator")
        return self


class QuantityValue(BaseModel):
    """IR spec Section 6.3."""

    reported_text: str
    reported_numeric_value: Optional[float] = None
    reported_units: str
    unit_basis_notes: Optional[str] = None
    converted_value: Optional[float] = None
    converted_units: Optional[str] = None
    conversion_formula: Optional[str] = None
    conversion_rationale: Optional[str] = None

    @model_validator(mode="after")
    def _conversion_completeness(self) -> "QuantityValue":
        if self.converted_value is not None:
            missing = [
                name
                for name, val in (
                    ("converted_units", self.converted_units),
                    ("conversion_formula", self.conversion_formula),
                    ("conversion_rationale", self.conversion_rationale),
                )
                if val is None
            ]
            if missing:
                raise ValueError(
                    f"converted_value is set but missing required companion field(s): {', '.join(missing)}"
                )
        return self


class DateRange(BaseModel):
    """IR spec Section 6.4. The invariant (exactly one of interval /
    relative_timing / neither-with-reason) can't be fully checked here alone
    -- the "neither set" branch requires `unresolved_reason` on the
    *enclosing* ExtractedField, which this model doesn't have access to.
    The interval/relative-timing mutual exclusion IS checkable here; the
    third-branch check is done by `validators.check_date_range_field`
    against the enclosing ExtractedField[DateRange]."""

    earliest: Optional[date] = None
    latest: Optional[date] = None
    reported_text: str
    relative_timing: Optional[str] = None
    relative_timing_days: Optional[int] = None

    @model_validator(mode="after")
    def _interval_xor_relative(self) -> "DateRange":
        has_interval = self.earliest is not None or self.latest is not None
        has_relative = self.relative_timing is not None
        if has_interval and has_relative:
            raise ValueError(
                "DateRange cannot have both an interval (earliest/latest) and relative_timing set"
            )
        if has_interval:
            if self.earliest is None or self.latest is None:
                raise ValueError("DateRange interval requires both earliest and latest set")
            if self.earliest > self.latest:
                raise ValueError("DateRange.earliest must be <= latest")
        return self


class StatisticalSummary(BaseModel):
    """IR spec Section 6.5. statistic_value is float|str per spec (e.g. a
    reported '<0.01' p-value is legitimately non-numeric text)."""

    statistic_name: str
    statistic_value: Union[float, str]


ExtractedReference = str  # text id pointer; never a nested copy (Section 10)

# ---------------------------------------------------------------------------
# Entities (IR spec Section 7, plus Study)
# ---------------------------------------------------------------------------


class Citation(BaseModel):
    id: str
    author: ExtractedField[str]
    year: ExtractedField[int]
    title: ExtractedField[str]
    persistent_identifier: ExtractedField[str]


class Study(BaseModel):
    """Identity/grouping entity: a real-world study that may be reported across several citations. Holds no
    scientific fields."""

    id: str
    citation_ids: list[ExtractedReference] = Field(min_length=1)


class Site(BaseModel):
    id: str
    name: ExtractedField[str]
    latitude: Optional[ExtractedField[QuantityValue]] = None
    longitude: Optional[ExtractedField[QuantityValue]] = None
    country: Optional[ExtractedField[str]] = None
    state_or_region: Optional[ExtractedField[str]] = None
    nearest_city: Optional[ExtractedField[str]] = None
    elevation: Optional[ExtractedField[QuantityValue]] = None
    soil_context: Optional[ExtractedField[str]] = None
    description: Optional[ExtractedField[str]] = None


class Species(BaseModel):
    id: str
    genus: ExtractedField[str]
    species_epithet: ExtractedField[str]
    scientific_name: ExtractedField[str]
    common_name: Optional[ExtractedField[str]] = None

    @model_validator(mode="after")
    def _scientific_name_consistency(self) -> "Species":
        # Advisory-strength structural check only when all three are
        # EXTRACTED/INFERRED (i.e. have real values) -- UNRESOLVED components
        # can't be cross-checked and shouldn't block on that basis.
        g, s, sn = self.genus, self.species_epithet, self.scientific_name
        if g.value and s.value and sn.value:
            expected_prefix = f"{g.value} {s.value}"
            if not sn.value.startswith(expected_prefix):
                raise ValueError(
                    f"scientific_name '{sn.value}' must equal genus + ' ' + species_epithet "
                    f"(expected it to start with '{expected_prefix}')"
                )
        return self


class Method(BaseModel):
    id: str
    citation_id: ExtractedReference
    name: ExtractedField[str]
    description: ExtractedField[str]


class Variable(BaseModel):
    """The datapackage's `variables` table. `Observation.variable_name` stays the source's verbatim name;
    `Observation.variable_id` is an optional link to this record."""

    id: str
    name: ExtractedField[str]
    description: Optional[ExtractedField[str]] = None
    units: Optional[ExtractedField[str]] = None
    notes: Optional[ExtractedField[str]] = None


class Crop(BaseModel):
    """The datapackage's `crops` table: the paper-specific crop (typically a cultivar), referencing Species for its
    taxonomy."""

    id: str
    citation_id: ExtractedReference
    species_id: ExtractedReference
    cultivar: Optional[ExtractedField[str]] = None
    common_name: Optional[ExtractedField[str]] = None
    notes: Optional[ExtractedField[str]] = None


class Treatment(BaseModel):
    id: str
    citation_id: ExtractedReference
    site_id: ExtractedReference
    study_id: ExtractedField[ExtractedReference]  # may be UNRESOLVED
    name: ExtractedField[str]
    definition: ExtractedField[str]
    control_status: Optional[ExtractedField[bool]] = None


class TreatmentPair(BaseModel):
    """A named treatment contrast; treatment_id_1 is the baseline (Protocol Section 7.3). An empty list is normal."""

    id: str
    citation_id: ExtractedReference
    treatment_id_1: ExtractedReference  # baseline, by convention (Protocol Section 7.3)
    treatment_id_2: ExtractedReference  # comparison
    comparison_factor: ExtractedField[str]
    comparison_label: ExtractedField[str]
    use_for_validation: Optional[ExtractedField[bool]] = None
    notes: Optional[str] = None  # curator-facing commentary, not itself a reported value

    @model_validator(mode="after")
    def _distinct_treatments(self) -> "TreatmentPair":
        if self.treatment_id_1 == self.treatment_id_2:
            raise ValueError(
                f"TreatmentPair.treatment_id_1 and treatment_id_2 must reference distinct "
                f"Treatments (got {self.treatment_id_1!r} for both)"
            )
        return self


class Management(BaseModel):
    id: str
    citation_id: ExtractedReference
    treatment_ids: Optional[ExtractedField[list[ExtractedReference]]] = None
    event_type: ExtractedField[str]
    date: ExtractedField[DateRange]
    amount: Optional[ExtractedField[QuantityValue]] = None

    @model_validator(mode="after")
    def _date_amount_never_inferred(self) -> "Management":
        # Table 18: Management.date and .amount are never INFERRED --
        # EXTRACTED or UNRESOLVED only. event_type occurrence MAY be
        # INFERRED (that's the "occurrence" the Protocol means, e.g. "a
        # tillage event happened" without a date).
        if self.date.provenance_label == ProvenanceLabel.INFERRED:
            raise ValueError("Management.date must never be INFERRED (EXTRACTED or UNRESOLVED only)")
        if self.amount is not None and self.amount.provenance_label == ProvenanceLabel.INFERRED:
            raise ValueError("Management.amount must never be INFERRED (EXTRACTED or UNRESOLVED only)")
        return self


ReportedEffectScope = Literal["treatment_mean", "aggregated_mean"]


class Observation(BaseModel):
    id: str
    dataset_id: str
    citation_id: ExtractedReference
    site_id: ExtractedReference
    treatment_id: ExtractedReference  # singular, per BETYdb constraint
    species_id: Optional[ExtractedReference] = None
    crop_id: Optional[ExtractedReference] = None  # optional link to Crop
    method_id: ExtractedReference
    replicate_id: Optional[ExtractedField[str]] = None
    variable_name: ExtractedField[str]
    variable_id: Optional[ExtractedReference] = None  # optional link to Variable
    value: ExtractedField[QuantityValue]
    statistical_encoding: Optional[ExtractedField[StatisticalSummary]] = None
    reported_effect_scope: ExtractedField[ReportedEffectScope]
    aggregated_over_factors: Optional[ExtractedField[list[str]]] = None
    temporal_info: ExtractedField[DateRange]
    notes: Optional[ExtractedField[str]] = None
    is_raw_replicate_level: ExtractedField[bool]

    @model_validator(mode="after")
    def _effect_scope_aggregation_rule(self) -> "Observation":
        # Table 16 / Section 7.7.1.
        scope = self.reported_effect_scope.value
        if scope == "treatment_mean":
            if self.aggregated_over_factors is None:
                raise ValueError(
                    "reported_effect_scope=treatment_mean requires aggregated_over_factors to be "
                    "present as EXTRACTED with an empty list (not applicable != absent)"
                )
            f = self.aggregated_over_factors
            if f.provenance_label != ProvenanceLabel.EXTRACTED or f.value != []:
                raise ValueError(
                    "reported_effect_scope=treatment_mean requires aggregated_over_factors = "
                    "EXTRACTED with an empty list"
                )
        elif scope == "aggregated_mean":
            if self.aggregated_over_factors is None:
                raise ValueError(
                    "reported_effect_scope=aggregated_mean requires aggregated_over_factors "
                    "(EXTRACTED with factor names, or UNRESOLVED with a reason) -- never absent"
                )
            f = self.aggregated_over_factors
            if f.provenance_label == ProvenanceLabel.EXTRACTED and not f.value:
                raise ValueError(
                    "reported_effect_scope=aggregated_mean with EXTRACTED aggregated_over_factors "
                    "requires a non-empty factor list"
                )
        return self


class Coverage(BaseModel):
    """The datapackage's `coverage` table: how much curated data exists per site/variable. A curation rollup, not
    a claim from the text, so no field is an ExtractedField."""

    id: str
    citation_id: ExtractedReference
    site_id: ExtractedReference
    variable_id: Optional[ExtractedReference] = None
    system: Optional[str] = None
    treatment_contrast: Optional[str] = None
    daily_rows: Optional[int] = Field(default=None, ge=0)
    annual_rows: Optional[int] = Field(default=None, ge=0)
    seasonal_rows: Optional[int] = Field(default=None, ge=0)
    static_observation_rows: Optional[int] = Field(default=None, ge=0)
    weather_rows: Optional[int] = Field(default=None, ge=0)
    initial_data_sources: Optional[str] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Root aggregate (IR spec Section 3)
# ---------------------------------------------------------------------------


class IRDataset(BaseModel):
    dataset_id: str
    citations: list[Citation] = Field(default_factory=list)  # CHANGED: was singular primary_citation
    studies: list[Study] = Field(default_factory=list)
    sites: list[Site] = Field(default_factory=list)
    species: list[Species] = Field(default_factory=list)
    crops: list[Crop] = Field(default_factory=list)
    methods: list[Method] = Field(default_factory=list)
    treatments: list[Treatment] = Field(default_factory=list)
    treatment_pairs: list[TreatmentPair] = Field(default_factory=list)
    variables: list[Variable] = Field(default_factory=list)
    managements: list[Management] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    coverages: list[Coverage] = Field(default_factory=list)


ENTITY_MODELS: dict[str, type[BaseModel]] = {
    "Citation": Citation,
    "Study": Study,
    "Site": Site,
    "Species": Species,
    "Crop": Crop,
    "Method": Method,
    "Treatment": Treatment,
    "TreatmentPair": TreatmentPair,
    "Variable": Variable,
    "Management": Management,
    "Observation": Observation,
    "Coverage": Coverage,
}
