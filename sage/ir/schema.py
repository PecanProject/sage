"""The IR: twelve entities whose scientific values are `Field[T]`, each carrying its basis and evidence.

Entity shapes and invariants follow the IR Architecture Specification v1.0, plus the Study entity and the fields the
PEcAn curation workbook needs (`n`, `depth`, the study period, the management crop and material); see
docs/pecan/workbook.md. Whole-dataset rules (references, duplicates, coverage) live in `sage.pipeline.checker`.
Status (verified, unresolved, flagged) is attached by settle and never set here.
"""

from datetime import date
from typing import Annotated, Literal, Self

import pydantic as pd
from pydantic import BaseModel, ConfigDict, model_validator

Basis = Literal["stated", "derived", "inferred"]
Ref = str
"""An id pointer to another record, never a nested copy."""


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(Model):
    anchor: str = pd.Field(description='Where in the paper view, e.g. "p6.t1.r1.c2" or "p3.b4".')
    quote: str = pd.Field(description="Verbatim text at that anchor that states the value.")


class Field[T](Model):
    value: T | None = pd.Field(description="The value, or null when the paper does not give it.")
    basis: Basis = pd.Field(
        description="stated: the quote says it. derived: computed from stated inputs (code recomputes it). "
        "inferred: a judgement the paper implies; say why in note."
    )
    evidence: list[Evidence] = pd.Field(default_factory=list)
    note: str | None = pd.Field(default=None, description="Why inferred, why null, or a conflict worth flagging.")


class QuantityValue(Model):
    reported_text: str
    reported_numeric_value: float | None = None
    reported_units: str
    unit_basis_notes: str | None = None
    converted_value: float | None = None
    converted_units: str | None = None
    conversion_formula: str | None = None
    conversion_rationale: str | None = None

    @model_validator(mode="after")
    def _conversion_complete(self) -> Self:
        if self.converted_value is not None:
            missing = [
                name
                for name in ("converted_units", "conversion_formula", "conversion_rationale")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"converted_value is set, so also set {', '.join(missing)}")
        return self


class DateRange(Model):
    reported_text: str
    earliest: date | None = None
    latest: date | None = None
    relative_timing: str | None = None
    relative_timing_days: int | None = None

    @model_validator(mode="after")
    def _interval_or_relative(self) -> Self:
        has_interval = self.earliest is not None or self.latest is not None
        if has_interval and self.relative_timing is not None:
            raise ValueError("a DateRange is an interval (earliest/latest) or relative_timing, never both")
        if has_interval:
            if self.earliest is None or self.latest is None:
                raise ValueError("a DateRange interval needs both earliest and latest; use the same date twice")
            if self.earliest > self.latest:
                raise ValueError("DateRange earliest is after latest")
        return self


class DepthRange(Model):
    """A soil sampling depth as printed. Code reads `top_cm` and `bottom_cm` from `reported_text`."""

    reported_text: str = pd.Field(description='The depth as printed, with its unit, e.g. "0-15 cm" or "30 cm".')
    top_cm: float | None = None
    bottom_cm: float | None = None

    @model_validator(mode="after")
    def _interval(self) -> Self:
        if (self.top_cm is None) != (self.bottom_cm is None):
            raise ValueError("a DepthRange sets both top_cm and bottom_cm, or neither")
        if self.top_cm is not None and self.bottom_cm is not None and self.top_cm > self.bottom_cm:
            raise ValueError("DepthRange top_cm is below bottom_cm")
        return self


class StatisticalSummary(Model):
    statistic_name: str = pd.Field(description='As the paper names it, e.g. "SE", "SD", "LSD (0.05)", "p".')
    statistic_value: float | str


class Citation(Model):
    type: Literal["Citation"] = "Citation"
    id: str
    author: Field[str]
    year: Field[int]
    title: Field[str]
    persistent_identifier: Field[str]


class Study(Model):
    type: Literal["Study"] = "Study"
    id: str
    citation_ids: list[Ref] = pd.Field(min_length=1)
    design: Field[str] | None = pd.Field(
        default=None, description="Experimental design as stated, e.g. RCBD split-plot."
    )
    period: Field[DateRange] | None = pd.Field(
        default=None, description='When the experiment ran, as printed, e.g. "2003-2011" or "1993 to 2012".'
    )


class Site(Model):
    type: Literal["Site"] = "Site"
    id: str
    name: Field[str]
    latitude: Field[QuantityValue] | None = None
    longitude: Field[QuantityValue] | None = None
    country: Field[str] | None = None
    state_or_region: Field[str] | None = None
    nearest_city: Field[str] | None = None
    elevation: Field[QuantityValue] | None = None
    soil_context: Field[str] | None = None
    description: Field[str] | None = None


class Species(Model):
    type: Literal["Species"] = "Species"
    id: str
    genus: Field[str]
    species_epithet: Field[str]
    scientific_name: Field[str]
    common_name: Field[str] | None = None

    @model_validator(mode="after")
    def _scientific_name_matches(self) -> Self:
        g, s, name = self.genus.value, self.species_epithet.value, self.scientific_name.value
        if g is not None and s is not None and name is not None and not name.startswith(f"{g} {s}"):
            raise ValueError(f"scientific_name '{name}' must start with genus + species_epithet ('{g} {s}')")
        return self


class Crop(Model):
    type: Literal["Crop"] = "Crop"
    id: str
    citation_id: Ref
    species_id: Ref
    cultivar: Field[str] | None = None
    common_name: Field[str] | None = None
    cover_crop: Field[bool] | None = pd.Field(default=None, description="True when it is grown as a cover crop.")
    notes: Field[str] | None = None


class Method(Model):
    type: Literal["Method"] = "Method"
    id: str
    citation_id: Ref
    name: Field[str]
    description: Field[str]


class Variable(Model):
    type: Literal["Variable"] = "Variable"
    id: str
    name: Field[str]
    description: Field[str] | None = None
    units: Field[str] | None = None
    notes: Field[str] | None = None


class Treatment(Model):
    type: Literal["Treatment"] = "Treatment"
    id: str
    citation_id: Ref
    site_id: Ref
    study_id: Field[Ref]
    name: Field[str]
    definition: Field[str]
    control_status: Field[bool] | None = None
    factor_levels: dict[str, str] = pd.Field(
        default_factory=dict,
        description='The factor level this treatment sets, per factor, e.g. {"cultivar mixture": "3-cv"}.',
    )


class TreatmentPair(Model):
    type: Literal["TreatmentPair"] = "TreatmentPair"
    id: str
    citation_id: Ref
    treatment_id_1: Ref = pd.Field(description="The baseline.")
    treatment_id_2: Ref = pd.Field(description="The comparison.")
    comparison_factor: Field[str]
    comparison_label: Field[str]
    use_for_validation: Field[bool] | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if self.treatment_id_1 == self.treatment_id_2:
            raise ValueError(f"a TreatmentPair needs two distinct treatments, got '{self.treatment_id_1}' twice")
        return self


class Management(Model):
    type: Literal["Management"] = "Management"
    id: str
    citation_id: Ref
    treatment_ids: Field[list[Ref]] | None = pd.Field(
        default=None, description="The treatments the event applies to; leave it out when it applies to all."
    )
    crop_id: Ref | None = pd.Field(default=None, description="The Crop planted or harvested.")
    event_type: Field[str]
    date: Field[DateRange]
    amount: Field[QuantityValue] | None = None
    material: Field[str] | None = pd.Field(
        default=None, description='What was applied, as printed, e.g. "urea" or "composted green waste".'
    )
    notes: Field[str] | None = None

    @model_validator(mode="after")
    def _date_and_amount_never_inferred(self) -> Self:
        if self.date.basis == "inferred":
            raise ValueError("Management.date is never inferred: cite the stated date or set value null with a note")
        if self.amount is not None and self.amount.basis == "inferred":
            raise ValueError("Management.amount is never inferred: cite the stated amount or set value null")
        return self


EffectScope = Literal["treatment_mean", "aggregated_mean"]


class Observation(Model):
    type: Literal["Observation"] = "Observation"
    id: str
    citation_id: Ref
    site_id: Ref
    treatment_id: Ref
    species_id: Ref | None = None
    crop_id: Ref | None = None
    method_id: Ref | None = None
    variable_id: Ref | None = None
    variable_name: Field[str]
    value: Field[QuantityValue]
    statistical_encoding: Field[StatisticalSummary] | None = None
    n: Field[int] | None = pd.Field(
        default=None, description="How many replicates the value summarises; 1 for a single replicate's value."
    )
    reported_effect_scope: Field[EffectScope]
    aggregated_over_factors: Field[list[str]] | None = None
    temporal_info: Field[DateRange]
    depth: Field[DepthRange] | None = pd.Field(default=None, description="The soil depth sampled, when there is one.")
    replicate_id: Field[str] | None = None
    is_raw_replicate_level: Field[bool]
    notes: Field[str] | None = None

    @model_validator(mode="after")
    def _effect_scope_rule(self) -> Self:
        scope = self.reported_effect_scope.value
        agg = self.aggregated_over_factors
        if scope == "treatment_mean" and agg is not None and agg.value is not None and agg.value != []:
            raise ValueError(
                f"aggregated_over_factors is {agg.value}, but a treatment_mean is averaged over no factor: leave "
                "aggregated_over_factors out (code sets it to [] from reported_effect_scope), or use aggregated_mean "
                "if the paper says the values are averaged over those factors"
            )
        if scope == "aggregated_mean":
            if agg is None:
                raise ValueError(
                    "reported_effect_scope=aggregated_mean needs aggregated_over_factors: the factor names, or "
                    "value null with a note"
                )
            if agg.value is not None and not agg.value:
                raise ValueError("reported_effect_scope=aggregated_mean needs a non-empty aggregated_over_factors")
            if agg.value is None and agg.note is None:
                raise ValueError("aggregated_over_factors is null, so say why in its note")
        return self


class Coverage(Model):
    """A curation rollup, not a claim from the text, so no field is a Field."""

    type: Literal["Coverage"] = "Coverage"
    id: str
    citation_id: Ref
    site_id: Ref
    variable_id: Ref | None = None
    system: str | None = None
    treatment_contrast: str | None = None
    daily_rows: int | None = pd.Field(default=None, ge=0)
    annual_rows: int | None = pd.Field(default=None, ge=0)
    seasonal_rows: int | None = pd.Field(default=None, ge=0)
    static_observation_rows: int | None = pd.Field(default=None, ge=0)
    weather_rows: int | None = pd.Field(default=None, ge=0)
    initial_data_sources: str | None = None
    notes: str | None = None


type Entity = (
    Citation
    | Study
    | Site
    | Species
    | Crop
    | Method
    | Variable
    | Treatment
    | TreatmentPair
    | Management
    | Observation
    | Coverage
)
Record = Annotated[Entity, pd.Field(discriminator="type")]
RecordAdapter: pd.TypeAdapter[Entity] = pd.TypeAdapter(Record)

REFERENCE_FIELDS: dict[str, dict[str, str]] = {
    "Study": {"citation_ids": "Citation"},
    "Crop": {"citation_id": "Citation", "species_id": "Species"},
    "Method": {"citation_id": "Citation"},
    "Treatment": {"citation_id": "Citation", "site_id": "Site"},
    "TreatmentPair": {"citation_id": "Citation", "treatment_id_1": "Treatment", "treatment_id_2": "Treatment"},
    "Management": {"citation_id": "Citation", "crop_id": "Crop"},
    "Observation": {
        "citation_id": "Citation",
        "site_id": "Site",
        "treatment_id": "Treatment",
        "species_id": "Species",
        "crop_id": "Crop",
        "method_id": "Method",
        "variable_id": "Variable",
    },
    "Coverage": {"citation_id": "Citation", "site_id": "Site", "variable_id": "Variable"},
}
"""Plain reference attributes per entity and the entity type each points at."""

FIELD_REFERENCES: dict[str, dict[str, str]] = {
    "Treatment": {"study_id": "Study"},
    "Management": {"treatment_ids": "Treatment"},
}
"""Field-valued references per entity: a judgement with a basis and evidence, but its value is ids, not text."""


def fields_of(record: Model) -> dict[str, Field[object]]:
    return {name: v for name in type(record).model_fields if isinstance(v := getattr(record, name), Field)}
