"""
IR schema — Pydantic models.

Source of truth: `IR Architecture Specification v1.0` (the base spec) PLUS every
amendment agreed in Playbook Section 5 and the Option B Study design agreed in
Section 5.1 (approved for implementation this sprint, per explicit instruction
-- Option A was the fallback if fixture review found no multi-citation study;
this session was told to build Option B directly rather than wait on that
gate).

This module holds ONLY data shape (Pydantic models + enums). Construction-time
invariants that Pydantic can express as field/model validators live here too
(Section 9.1, IR spec) since they're inseparable from "what does a valid
instance look like." Whole-graph invariants (Section 9.2 / Table 19, Option B
diff) that require seeing the whole IRDataset live in `validators.py`, not
here -- a single entity's own fields being locally consistent is a different
question from whether it's consistent with the rest of the graph.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Generic, Literal, Optional, TypeVar, Union
from typing import Optional

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
    """Playbook Section 5 amendment: added to ExtractionSource, matching the
    Protocol's source-priority order."""

    ARCHIVED_DATA = "archived_data"
    SUPPLEMENT = "supplement"
    PUB_TABLE = "pub_table"
    PUB_TEXT = "pub_text"
    FIGURE = "figure"
    OTHER = "other"


class InferenceSource(str, Enum):
    """Playbook Section 5 amendment: added alongside `confidence` on
    ExtractedField. A raw LLM confidence score is not treated as calibrated
    -- this field exists so a reviewer/validator can tell which regime a
    confidence value came from."""

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
    """IR spec Section 6.2 + Playbook `source_priority_tier` amendment."""

    source_document_id: str
    page_number: int
    section_path: list[str] = Field(default_factory=list)
    locators: list[SourceLocator] = Field(min_length=1)
    raw_excerpt: Optional[str] = None
    notes: Optional[str] = None
    source_priority_tier: Optional[SourcePriorityTier] = None


class ExtractedField(BaseModel, Generic[T]):
    """IR spec Section 6.1 + Playbook amendments (`inference_source`,
    confidence 0-100 int).

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
        # P4/propose_record rule (Playbook Section 6): EXTRACTED/INFERRED
        # fields must carry at least one real locator -- an empty locators
        # list already fails ExtractionSource's min_length=1, so this is a
        # belt-and-suspenders re-check, not new logic.
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
# Entities (IR spec Section 7, + Study per Playbook Section 5.1 Option B)
# ---------------------------------------------------------------------------


class Citation(BaseModel):
    id: str
    author: ExtractedField[str]
    year: ExtractedField[int]
    title: ExtractedField[str]
    persistent_identifier: ExtractedField[str]


class Study(BaseModel):
    """NEW under Option B (Playbook Section 5.1). Pure identity/grouping
    entity: a real-world study that may be reported across multiple
    citations. Holds no scientific fields itself yet (design_type /
    experimental_unit / replicate_unit are future work per Section 5.1's
    Option A tradeoff discussion -- out of scope for this sprint)."""

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
    """Added this sprint: promotes the calibration/validation datapackage's
    `variables` table (name, description, units, notes; primary key `name`)
    to a first-class IR entity, adapted from the earlier `src2/` design
    (`src2/betydb_extraction/ir/entities/variable.py`) to this schema's bare
    `ExtractedReference` convention.

    This does NOT replace `Observation.variable_name` -- that field is
    unchanged and remains the verbatim, capture-first text of what the
    source actually calls the measured quantity (the free-text-at-the-IR-
    layer decision this module previously documented under "Playbook
    Section 5" applied to *that* field, not to whether a Variable registry
    entity could also exist). `Observation.variable_id` (below) is the new,
    optional link from an observation to a normalized Variable record,
    additive to variable_name, not a replacement for it."""

    id: str
    name: ExtractedField[str]
    description: Optional[ExtractedField[str]] = None
    units: Optional[ExtractedField[str]] = None
    notes: Optional[ExtractedField[str]] = None


class Crop(BaseModel):
    """Added this sprint: promotes the calibration/validation datapackage's
    `crops` table to a first-class IR entity, deliberately NOT a duplicate
    of Species. Species (above) is the pure taxonomic identity (genus /
    species_epithet / scientific_name) and is reusable across any number of
    papers and cultivars. Crop is the paper-specific agricultural identity
    actually used in one experiment -- typically a named cultivar/variety --
    and references Species for its taxonomy rather than restating it, the
    same way Treatment references Site rather than embedding site fields."""

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
    study_id: ExtractedField[ExtractedReference]  # NEW under Option B; may be UNRESOLVED
    name: ExtractedField[str]
    definition: ExtractedField[str]
    control_status: Optional[ExtractedField[bool]] = None


class TreatmentPair(BaseModel):
    """Added this sprint: a previously-confirmed-then-dropped entity.
    `src2/betydb_extraction/ir/entities/treatment_pair.py` (a separate,
    never-wired-in implementation elsewhere in this repo) already built this
    once, with the note "Added post-freeze per PROJECT_STATE_HANDOFF.md
    ('Confirmed gap to fix')" -- that handoff document no longer exists
    anywhere in the repository, but the entity it justified is reinstated
    here, adapted to this schema's bare-ExtractedReference convention
    (src2's version wrapped every reference in its own provenance-carrying
    model; this codebase's `ExtractedReference = str` choice, Section 10,
    stays consistent with every other entity here).

    Represents an explicit named treatment-contrast comparison -- the one
    Treatment<->Treatment relationship this schema didn't previously have
    any way to express. Calibration/Validation Protocol Section 7.3: "By
    convention, treatment_id_1 is the baseline treatment," and the table is
    "required when the task depends on named treatment comparisons... not
    required for every curated dataset" -- so, like Management and Study,
    an empty treatment_pairs list is the normal case, not an error."""

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
    crop_id: Optional[ExtractedReference] = None  # NEW: optional link to Crop, same pattern as species_id
    method_id: ExtractedReference
    replicate_id: Optional[ExtractedField[str]] = None
    variable_name: ExtractedField[str]
    variable_id: Optional[ExtractedReference] = None  # NEW: optional link to Variable; variable_name is unchanged
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
    """Added this sprint: promotes the calibration/validation datapackage's
    `coverage` table (a planning matrix of how much curated data exists per
    site/variable, used to decide where more curation effort is needed) to
    a first-class IR entity.

    Deliberately different in kind from every other entity above: a row
    count or a magnitude-target label is not something a paper's text
    states with a citable anchor -- it's a rollup computed from what has
    already been curated, or a planning judgment a curator records. None of
    its fields are wrapped in ExtractedField for that reason (the same
    reasoning that already applies to Study, which also carries zero
    ExtractedField content: not every entity in this schema represents a
    claim extracted from text). It is still scoped per-paper (citation_id)
    and lives in IRDataset as a list, like every other entity, since a
    single paper can report on several site/variable combinations."""

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
# Root aggregate (IR spec Section 3, Option B shape per Section 5.1)
# ---------------------------------------------------------------------------


class IRDataset(BaseModel):
    dataset_id: str
    citations: list[Citation] = Field(default_factory=list)  # CHANGED: was singular primary_citation
    studies: list[Study] = Field(default_factory=list)  # NEW
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
