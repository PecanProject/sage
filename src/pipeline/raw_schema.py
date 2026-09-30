"""The sealed raw-evidence format the Extraction AI produces: the only view of the paper the Conversion AI gets.

Every fact cites at least one content.md anchor it was read from; provenance labels are assigned later, by Conversion.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


def _blank_to_none(value: Any) -> Any:
    """An optional free-text field given as "" or whitespace means "not provided" (None); required fields keep
    min_length=1."""
    return None if isinstance(value, str) and not value.strip() else value


class RawFact(BaseModel):
    field_name: str
    # Optional, not required: a fact can legitimately report "I looked at
    # this anchor and the field is not stated" (raw_value=None, with `notes`
    # explaining what was checked) rather than being forced to either
    # silently omit the fact or fabricate a placeholder. Observed directly
    # from a real gpt-oss-120b run reporting Citation.persistent_identifier
    # this way -- a more informative evidence record than omission, and
    # consistent with the calibration/validation protocol's own "make
    # uncertainty explicit" principle, not a loosening of what counts as
    # grounded evidence (anchors are still required either way).
    raw_value: Optional[str] = Field(default=None, description="The value as read from the source, or null if examined and not found.")
    raw_text_excerpt: str = Field(description="The literal source text this value was read from (or the passage that was checked).")
    anchors: list[str] = Field(min_length=1, description="content.md block anchors (b:NNNN) actually read.")
    notes: Optional[str] = None


class RawExtraction(BaseModel):
    paper_id: str
    entity_type: str
    record_id: str
    facts: list[RawFact] = Field(default_factory=list)
    extraction_notes: Optional[str] = Field(
        default=None,
        description="What was looked for and not found, and which sections/anchors were checked.",
    )


FactorDimension = Literal["treatment", "crop", "time", "site", "variable", "replicate", "other"]
FactorEncoding = Literal["rows", "columns", "context"]


class CandidateDimension(BaseModel):
    """One factor level that distinguishes an enumeration candidate: `name` is
    the factor as the source calls it, `dimension` what it IS (the same
    vocabulary as `TableFactor.dimension`), `level` its literal level. Lets a
    free-form candidate and a table-derived candidate be compared by the same
    canonical identity: key names never define identity, dimensions and levels
    do."""

    name: str = Field(min_length=1)
    dimension: FactorDimension
    level: str = Field(min_length=1)


class EnumerationCandidate(BaseModel):
    """Multi-record extraction: one distinct real-world instance of an entity type that a paper reports, identified
    BEFORE full field-level extraction runs for it. Deliberately as sealed
    and evidence-anchored as `RawFact` above: a candidate with no real
    anchor is not a candidate, it's an invention.
    """

    candidate_id: str = Field(min_length=1, description="Short, stable, model-chosen slug, unique within one enumeration.")
    description: str = Field(min_length=1, description="One concise sentence identifying and distinguishing this candidate.")
    anchors: list[str] = Field(min_length=1, description="content.md block anchors actually read that support this being a real, distinct instance.")
    linked_candidates: dict[str, str | list[str]] = Field(
        default_factory=dict,
        description="Links to already-known records (e.g. an Observation candidate linking to a Treatment/Variable "
                    "slug). A value is one slug, or -- for a field "
                    "that names SEVERAL records (a Management event's `treatment_ids`) -- a list of slugs.",
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Sealed, orchestrator-authored structured context derived from the deterministic table "
                    "reconstruction (e.g. the factors the table pooled over, with the literal source text stating "
                    "it). Set ONLY by table-enumeration Step C, never by a model; empty for every free-form "
                    "candidate. Passed to Extraction (as candidate context to cite) and to Conversion (as an "
                    "unverified-hint block) -- never an instruction to copy a value verbatim.",
    )
    dimensions: list[CandidateDimension] = Field(
        default_factory=list,
        description="The factor levels that distinguish this candidate, each with its dimension. Set by table "
                    "enumeration Step C from the table's declared factors; DECLARED by the model for a free-form "
                    "Treatment candidate when tables already cover Treatment. Used only to compare identity "
                    "(item 8 dedup) -- never as evidence: grounding still rests on `anchors`.",
    )
    variable_name_hint: Optional[str] = Field(
        default=None,
        description="Unverified hint set ONLY by table enumeration Step C: the name of the measured variable this "
                    "candidate's cell reports -- the paper's own Variable record's name when the table's variable "
                    "resolves to one (so every spelling of one variable carries the same name), else the table's "
                    "declared/verbatim variable name. Travels to Extraction and Conversion as sealed context; never "
                    "source evidence.",
    )
    units_hint: Optional[str] = Field(
        default=None,
        description="Unverified hint set ONLY by Step C: the units the table reconstruction gave for this cell's "
                    "variable, and only when the source text (table, caption or notes) actually contains them. "
                    "Conversion takes units from the source text and uses this only where it agrees.",
    )
    known_value: Optional[str] = Field(
        default=None,
        description="Deterministically known expected reported value for this candidate -- set ONLY by "
                    "table-enumeration Step C (_table_classification_to_candidates), which already knows the "
                    "exact source cell text a candidate was cross-producted from. Never set by free-form LLM "
                    "enumeration (the enumeration prompt never mentions this field). Used solely for the "
                    "orchestrator's own post-Extraction cross-check (see _extraction_matches_known_value) that "
                    "Extraction actually read THIS candidate's own cell rather than a different row/column's "
                    "value -- never surfaced to a model as an instruction to copy verbatim.",
    )

    @model_validator(mode="after")
    def _unique_dimension_names(self) -> "EnumerationCandidate":
        names = [d.name.strip().casefold() for d in self.dimensions]
        if len(names) != len(set(names)):
            raise ValueError(f"candidate {self.candidate_id!r}: dimensions must have unique names, got {[d.name for d in self.dimensions]}")
        return self


class EnumerationResult(BaseModel):
    entity_type: str
    candidates: list[EnumerationCandidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_candidate_ids(self) -> "EnumerationResult":
        seen: set[str] = set()
        for candidate in self.candidates:
            if candidate.candidate_id in seen:
                raise ValueError(
                    f"duplicate candidate_id {candidate.candidate_id!r} -- candidate_ids must be "
                    f"unique within one enumeration"
                )
            seen.add(candidate.candidate_id)
        return self


# Structural description of a table's experimental dimensions (item "factor
# dimension + encoding"). It says WHAT each dimension IS and WHERE it lives in
# the table. It deliberately does NOT say that every experimental factor becomes
# a Treatment entity: that is decided separately, by the protocol's Treatment
# semantics applied on top of these roles (only `treatment`-dimension factors,
# plus the site, can ever form a Treatment candidate's identity; a cultivar is
# `crop`, a date/growth stage/year is `time`, a location is `site` -- protocol
# Section 6.3: "Do not use treatments to represent information that belongs in
# another field such as site, cultivar, replicate, or date").


# The crop / treatment boundary for a cultivar mixture, shared by every prompt that needs it: a designed mixture is a
# treatment level, each individual cultivar stays `crop` (protocol Section 6.3).
MIXTURE_LEVEL_RULE = (
    "'crop' means an INDIVIDUAL cultivar, variety, population or genotype. A designed mixture or composition "
    "level of several cultivars grown together (e.g. a one-, three- and five-cultivar mixture) is a 'treatment' "
    "level when the paper defines it as an experimental treatment or factor -- never infer a treatment merely "
    "because the word 'mixture' appears."
)


class TableFactor(BaseModel):
    name: str = Field(min_length=1, description="The dimension's name as the table/paper calls it, e.g. 'Population', 'Maturity', 'Location', 'Variable'.")
    dimension: FactorDimension = Field(
        description="What the dimension IS: 'treatment' (an experimental management/system condition applied by the "
                    "study, e.g. a cover-crop, tillage, fertilizer or irrigation level), 'crop' (an individual cultivar, "
                    "variety, population or genotype), 'time' (sampling/harvest date, growth stage, year, season, day "
                    "after planting), 'site' (a location), 'variable' (WHICH measured quantity a row/column reports), "
                    "'replicate' (block/plot/replicate), or 'other'. " + MIXTURE_LEVEL_RULE,
    )
    encoding: FactorEncoding = Field(
        description="Where its levels live in the table: 'rows' (a label column: each row group carries a level in "
                    "factor_values), 'columns' (column headers: each value column carries a level in factor_levels), "
                    "or 'context' (one level for the whole table, from its caption/footnote, in context_levels).",
    )


class PooledFactor(BaseModel):
    """A factor whose levels the table POOLED over (it is absent from the
    table's rows/columns because every reported value is a mean across it) --
    protocol Section 7.4 `aggregated_over_factors`. Grounded: the excerpt must
    be literal source text (caption/footnote/body) stating the pooling."""

    name: str = Field(min_length=1)
    dimension: FactorDimension
    evidence_anchor: str = Field(min_length=1, description="content.md block anchor the pooling statement was read from.")
    evidence_excerpt: str = Field(min_length=1, description="The literal source text stating the values are pooled/averaged over this factor.")
    origin: Literal["model", "deterministic"] = Field(
        default="model",
        description="Who established the pooling: the classification model, or the deterministic detector "
                    "(pipeline/pooling_evidence.py). Defaults to 'model' so older cached classifications still load.",
    )
    pattern: Optional[str] = Field(
        default=None, description="For origin='deterministic': the identifier of the linguistic frame that matched.",
    )


class TimeLevel(BaseModel):
    """The DATE a table's time level stands for, as the paper states it elsewhere (usually
    Methods) -- e.g. the harvest date of the 'Vegetative' sward maturity at one site. Sealed
    evidence, never a computed date: `date_text` / `year_text` are the LITERAL source text and
    must be found in the cited `anchors` (checked in Step B). A table cell's candidate carries
    the matching entry as context so Extraction can cite the Methods block and Conversion can
    give `temporal_info` a real, anchored interval instead of UNRESOLVED."""

    factor: str = Field(min_length=1, description="The declared `time`-dimension factor this level belongs to.")
    level: str = Field(min_length=1, description="The level as the table has it, e.g. 'Vegetative'.")
    site: Optional[str] = Field(default=None, min_length=1, description="Set only when the paper dates this level differently per site.")
    date_text: Optional[str] = Field(default=None, min_length=1, description="Literal source text giving the day/month, e.g. '9 June'.")
    year_text: Optional[str] = Field(default=None, min_length=1, description="Literal source text giving the year, e.g. '1993'.")

    _blank_optional = field_validator("site", "date_text", "year_text", mode="before")(_blank_to_none)
    anchors: list[str] = Field(min_length=1, description="content.md blocks the date/year text was read from.")

    @model_validator(mode="after")
    def _has_a_date(self) -> "TimeLevel":
        if not (self.date_text or self.year_text):
            raise ValueError(f"time_level {self.factor}={self.level!r} gives neither date_text nor year_text")
        return self


class UnitHintFlag(BaseModel):
    """A units hint the source text does NOT support (set only by the orchestrator, never by the model).
    Example: Step B wrote 'kg DM' where the table's own rendering reads 'kg DI | M m -2' -- flagged for
    review and withheld from candidates; never silently corrected to either reading."""

    scope: Literal["variable", "column"]
    key: str = Field(description="The variable label (scope 'variable') or value_column_id (scope 'column').")
    units_hint: str
    reason: str


class MethodHintFlag(BaseModel):
    """A method hint whose significant words no single prose block contains (set by the orchestrator, never the
    model): withheld from candidates and flagged."""

    scope: Literal["variable", "column"]
    key: str = Field(description="The variable label (scope 'variable') or value_column_id (scope 'column').")
    method_hint: str
    reason: str


class TableVariable(BaseModel):
    """One measured VARIABLE a table reports -- the stable semantic unit that
    carries its canonical name, units and measurement method, whether the table
    names it in a column header (`TableValueColumn.variable`) or in a row label
    (a `variable`-dimension factor with encoding 'rows'). Repeating a method hint
    on every row instead would disagree with itself (a variable recurs across time
    points), and a bare method hint carries neither units nor a canonical name."""

    label: str = Field(min_length=1, description="The variable as the table names it: the row label or column header, verbatim.")
    variable_name: Optional[str] = Field(default=None, min_length=1, description="Normalized name of the measured quantity, e.g. 'shoot biomass'.")
    units: Optional[str] = Field(default=None, min_length=1, description="Units as the table reports them.")
    method_hint: Optional[str] = Field(
        default=None, min_length=1,
        description="How the paper's Methods says THIS variable was measured -- only when it says so; never invented.",
    )

    _blank_optional = field_validator("variable_name", "units", "method_hint", mode="before")(_blank_to_none)


class TableValueColumn(BaseModel):
    """Table enumeration (Step B, "table classification"): one column of a
    reconstructed table that reports an actual measured value, as opposed
    to a factor/label column (Population, Maturity, ...). The hint fields
    are free text for Step C's templated candidate descriptions and for
    the Extraction stage that later re-reads this candidate's own anchor --
    never trusted as IR-shaped values themselves, same as everything else
    in this module."""

    value_column_id: str = Field(min_length=1, description="Short, stable slug, unique within this table classification.")
    variable_name_hint: Optional[str] = Field(
        default=None, min_length=1,
        description="e.g. 'leaf sheath dry weight' -- combines multi-level headers if the source table has them. "
                    "May be omitted when the variable is named by `variable` (a TableVariable label) or by a row "
                    "label (a `variable`-dimension row factor); the classification must still identify the variable.",
    )
    variable: Optional[str] = Field(
        default=None, min_length=1,
        description="Label of the TableClassification.variables entry this column reports (column -> variable).",
    )
    units_hint: Optional[str] = None
    site_hint: Optional[str] = Field(default=None, description="Set when the column ITSELF encodes a site/location, e.g. a table with separate 'Ames'/'Mead' sub-columns.")
    method_hint: Optional[str] = Field(
        default=None,
        description="The real name/description of the measurement method this column's values were produced by, as "
                    "actually stated in the paper's Methods section (e.g. 'hand-clipping harvest', 'LI-COR LAI-2000 "
                    "leaf area analyzer') -- required for Observation.method_id linking (a required field) to ever "
                    "resolve deterministically; Step C has no other source of method information for a table row.",
    )
    factor_levels: dict[str, str] = Field(
        default_factory=dict,
        description="{factor name: level} for every factor declared in TableClassification.factors with "
                    "encoding='columns' whose level THIS column carries (e.g. {'Maturity': 'Vegetative'}). "
                    "Generalizes the legacy site_hint / treatment_level_hint, which stay valid.",
    )
    treatment_level_hint: Optional[str] = Field(
        default=None,
        description="Set when the column ITSELF encodes a distinct EXPERIMENTAL "
                    "TREATMENT LEVEL, e.g. a table with separate 'Fallow'/'Mustard' sub-columns, rather than the "
                    "treatment being named in a row/factor column. When this is set on a table's value_columns, "
                    "row-level factor_values (e.g. a DAP time point, or which variable is being reported) are "
                    "CONTEXT for the observation, not part of the treatment's own identity -- the treatment "
                    "candidate for that column is defined by treatment_level_hint (plus site_hint, if also set), "
                    "never by the row's own factor_values. Mutually informative with, never a replacement for, "
                    "row-encoded factor_values['Treatment']-style columns in a different table -- a paper can use "
                    "either shape, and this field is simply absent (None) for the existing row-encoded case.",
    )

    _blank_optional = field_validator(
        "variable_name_hint", "variable", "units_hint", "site_hint", "method_hint", "treatment_level_hint", mode="before",
    )(_blank_to_none)


class TableRowGroup(BaseModel):
    """Table enumeration (Step B): one logical data row of the reconstructed table. A packed raw cell
    ('0.19 0.90 1.16') may be split into several logical rows, since Marker's row clustering is not always reliable."""

    row_group_id: str = Field(min_length=1, description="Short, stable slug, unique within this table classification.")
    factor_values: dict[str, str] = Field(default_factory=dict, description="e.g. {'Population': 'Trailblazer', 'Maturity': 'Vegetative'}.")
    source_table_anchor: str = Field(min_length=1, description="Which ONE physical table block this row's cells actually come from -- must be a member of table_anchors.")
    cells: dict[str, Optional[str]] = Field(default_factory=dict, description="{value_column_id: raw cell text, or null/absent if genuinely not reported for this row.}")


# What KIND of table a reconstructed table is (protocol-generic, never
# paper-specific). Only "treatment_response" tables feed cell-level Treatment /
# Observation candidate generation. The others are kept identifiable (see
# TableClassification.table_role) -- in particular an "aggregated_summary" is a
# valid SOURCE of aggregated data (protocol Section 7.4) that the current
# cell-level enumeration path simply cannot represent as a normal
# treatment-combination row.
TableRole = Literal["treatment_response", "aggregated_summary", "weather_context", "non_enumerable"]

# Roles whose table was successfully reconstructed into row_groups and is worth
# keeping for later use (legacy `applicable=True`).
_RECONSTRUCTED_ROLES = ("treatment_response", "aggregated_summary")


class TableClassification(BaseModel):
    """Table enumeration (Step B): reconstructs ONE logical table -- which
    may span more than one raw content.md block, e.g. a page-split
    continuation -- into a flat, long-format structure Step C can
    mechanically cross-product into EnumerationCandidates. Sealed evidence,
    not yet the final IR contract, same philosophy as RawExtraction above:
    this stage's job is "what does this table actually contain, row by
    row", not "shape it into the exact IR payload."."""

    table_role: Optional[TableRole] = Field(
        default=None,
        description="What KIND of table this is: 'treatment_response' (measured response values broken out by "
                    "the study's conditions -- the normal data table), 'aggregated_summary' (pooled/averaged "
                    "main-effect summaries, not per-combination cells), 'weather_context' (meteorological/climate "
                    "data describing the study conditions), or 'non_enumerable' (statistical-model/test tables, "
                    "abbreviation lists, unreconstructable tables). Authoritative; `applicable` and "
                    "`aggregation_scope` below are derived from it and kept only for backward compatibility. "
                    "When absent (legacy payload), it is derived from `applicable`/`aggregation_scope`.",
    )
    applicable: Optional[bool] = Field(
        default=None,
        description="LEGACY (derived from table_role): False when this table does not report per-instance values "
                    "for the target entity_type at all (e.g. a regression-equation table). If given together with "
                    "table_role the two must agree.",
    )
    aggregation_scope: Literal["cell_level", "aggregated_summary"] = Field(
        default="cell_level",
        description="'cell_level' (default) means each row/column reports a real, individually-measured value for "
                    "one experimental unit -- normal candidate generation applies. 'aggregated_summary' means this "
                    "table reports a POOLED/AVERAGED main-effect summary instead -- e.g. a value averaged 'across "
                    "populations', 'across locations and maturities', or otherwise explicitly pooled -- per the "
                    "Calibration/Validation Protocol Section 7.4 ('Do not: Use a main-effect row as if it were a "
                    "normal treatment-combination row... without documenting the aggregation'). Orthogonal to "
                    "`applicable`: a table can be successfully RECONSTRUCTED (applicable=True) while still being "
                    "the wrong kind of table for Treatment/Observation candidate generation -- reconstruction "
                    "success and candidate-generation eligibility are different questions, kept as different "
                    "fields rather than overloading `applicable` to mean both.",
    )
    factors: list[TableFactor] = Field(
        default_factory=list,
        description="The table's experimental dimensions: for each, what it IS (dimension) and where its levels live "
                    "(encoding). Empty = legacy classification (every row factor is treated as before).",
    )
    context_levels: dict[str, str] = Field(
        default_factory=dict,
        description="{factor name: level} for factors declared with encoding='context' (one level for the whole table).",
    )
    pooled_factors: list[PooledFactor] = Field(
        default_factory=list,
        description="Factors the table's values are pooled/averaged OVER (absent from rows/columns), each with the "
                    "literal source text stating it (protocol Section 7.4 aggregated_over_factors).",
    )
    reason: Optional[str] = Field(default=None, description="Required when applicable=False, when applicable=True but row_groups is empty (reconstruction was not confident enough to trust), or when aggregation_scope=aggregated_summary (explain which factor(s) were pooled/averaged).")
    table_anchors: list[str] = Field(min_length=1, description="Every content.md block anchor that is part of THIS one logical table -- more than one only for a page-split continuation.")
    unit_hint_flags: list[UnitHintFlag] = Field(
        default_factory=list,
        description="Units hints (TableVariable.units / TableValueColumn.units_hint) the table, its caption and its "
                    "notes do not contain. Computed deterministically after validation; anything the model supplies is discarded.",
    )
    method_hint_flags: list[MethodHintFlag] = Field(
        default_factory=list,
        description="Method hints (TableVariable.method_hint / TableValueColumn.method_hint) that no prose block of the "
                    "paper supports, withheld from the classification. Computed deterministically after validation; "
                    "anything the model supplies is discarded.",
    )
    time_levels: list[TimeLevel] = Field(
        default_factory=list,
        description="The dates the paper gives for this table's `time`-dimension levels (usually in Methods), each "
                    "with the literal source text and the blocks it was read from. Empty when the table has no "
                    "time factor or the paper states no date; never a computed or guessed date.",
    )
    variables: list[TableVariable] = Field(
        default_factory=list,
        description="The measured variables the table reports, each with its canonical name, units and method hint. "
                    "Columns point to one (`TableValueColumn.variable`); a variable named by a row label resolves "
                    "through that row's `variable`-dimension level. Empty = legacy classification: each column's own "
                    "variable_name_hint / units_hint / method_hint stand for its variable.",
    )
    value_columns: list[TableValueColumn] = Field(default_factory=list)
    row_groups: list[TableRowGroup] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistency(self) -> "TableClassification":
        # table_role is authoritative; applicable/aggregation_scope are its
        # backward-compatible projections. A legacy payload (no table_role) is
        # upgraded from them; a payload that supplies both must not disagree.
        if self.table_role is None:
            if self.applicable is None:
                raise ValueError("table_role is required (or, for a legacy payload, applicable)")
            if not self.applicable:
                self.table_role = "non_enumerable"
            elif self.aggregation_scope == "aggregated_summary":
                self.table_role = "aggregated_summary"
            else:
                self.table_role = "treatment_response"
        else:
            expected_applicable = self.table_role in _RECONSTRUCTED_ROLES
            expected_scope = "aggregated_summary" if self.table_role == "aggregated_summary" else "cell_level"
            if self.applicable is not None and self.applicable != expected_applicable:
                raise ValueError(
                    f"table_role={self.table_role!r} contradicts applicable={self.applicable!r} "
                    f"(applicable must be {expected_applicable} for that role)"
                )
            if "aggregation_scope" in self.model_fields_set and self.aggregation_scope != expected_scope:
                raise ValueError(
                    f"table_role={self.table_role!r} contradicts aggregation_scope={self.aggregation_scope!r} "
                    f"(must be {expected_scope!r} for that role)"
                )
            self.applicable = expected_applicable
            self.aggregation_scope = expected_scope

        if not self.applicable and not (self.reason or "").strip():
            raise ValueError("applicable=False requires a real, non-empty reason")
        if self.applicable and not self.row_groups and not (self.reason or "").strip():
            raise ValueError(
                "applicable=True with an empty row_groups requires a real, non-empty reason "
                "explaining why reconstruction was not confident enough to trust"
            )
        if self.aggregation_scope == "aggregated_summary" and not (self.reason or "").strip():
            raise ValueError(
                "aggregation_scope=aggregated_summary requires a real, non-empty reason explaining which "
                "factor(s) this table's values are pooled/averaged over"
            )

        self._check_declared_factors()
        self._check_variables()
        self._check_time_levels()

        value_ids = [c.value_column_id for c in self.value_columns]
        if len(value_ids) != len(set(value_ids)):
            raise ValueError("value_column_id must be unique within one table classification")

        row_ids = [r.row_group_id for r in self.row_groups]
        if len(row_ids) != len(set(row_ids)):
            raise ValueError("row_group_id must be unique within one table classification")

        known_value_ids = set(value_ids)
        for row in self.row_groups:
            if row.source_table_anchor not in self.table_anchors:
                raise ValueError(
                    f"row_group '{row.row_group_id}' cites source_table_anchor "
                    f"'{row.source_table_anchor}' which is not in table_anchors {self.table_anchors}"
                )
            unknown_cells = set(row.cells) - known_value_ids
            if unknown_cells:
                raise ValueError(
                    f"row_group '{row.row_group_id}' has cells for unknown value_column_id(s) "
                    f"{sorted(unknown_cells)}"
                )
        return self

    def _check_declared_factors(self) -> None:
        problems = _declared_factor_problems(self)
        if problems:
            raise ValueError("; ".join(problems))

    def _check_time_levels(self) -> None:
        if not self.time_levels:
            return
        time_factors = {f.name: f for f in self.factors if f.dimension == "time"}
        seen: set[tuple[str, str, str]] = set()
        for tl in self.time_levels:
            if tl.factor not in time_factors:
                raise ValueError(
                    f"time_level {tl.factor}={tl.level!r}: {tl.factor!r} is not a declared factor with dimension 'time' "
                    f"(declared time factors: {sorted(time_factors)})"
                )
            if _variable_key(tl.level) not in {_variable_key(level) for level in _levels_of(self, tl.factor)}:
                raise ValueError(f"time_level {tl.factor}={tl.level!r}: {tl.level!r} is not a level this table reports for {tl.factor!r}")
            key = (_variable_key(tl.factor), _variable_key(tl.level), _variable_key(tl.site or ""))
            if key in seen:
                raise ValueError(f"time_level {tl.factor}={tl.level!r} (site {tl.site!r}) is given more than once")
            seen.add(key)

    def _check_variables(self) -> None:
        labels = [_variable_key(v.label) for v in self.variables]
        if len(labels) != len(set(labels)):
            raise ValueError("variables must have unique labels (compared ignoring case and punctuation)")
        row_encoded_variable = any(f.dimension == "variable" and f.encoding == "rows" for f in self.factors)
        for column in self.value_columns:
            if column.variable is not None and _variable_key(column.variable) not in labels:
                raise ValueError(
                    f"value_column '{column.value_column_id}' reports variable {column.variable!r}, which is not "
                    f"declared in variables {[v.label for v in self.variables]}"
                )
            if not (column.variable_name_hint or column.variable or row_encoded_variable):
                raise ValueError(
                    f"value_column '{column.value_column_id}' does not identify its variable: give variable_name_hint "
                    f"or `variable`, or declare a `variable`-dimension factor with encoding 'rows' when the variable "
                    f"is named by the row labels"
                )

    def factor_dimension(self, name: str) -> Optional[str]:
        """The declared dimension of factor `name`, or None when undeclared
        (legacy classification, or a key the table never declared)."""
        for f in self.factors:
            if f.name == name:
                return f.dimension
        return None


def _levels_of(tc: "TableClassification", name: str) -> set[str]:
    """Every level factor `name` takes in a table's rows, columns or context."""
    levels = {row.factor_values[name] for row in tc.row_groups if name in (row.factor_values or {})}
    levels |= {col.factor_levels[name] for col in tc.value_columns if name in (col.factor_levels or {})}
    if name in (tc.context_levels or {}):
        levels.add(tc.context_levels[name])
    return {level for level in levels if level and level.strip()}


def _variable_key(text: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in (text or "").lower()).split())


def _declared_factor_problems(tc: "TableClassification") -> list[str]:
    problems: list[str] = []
    names = [f.name for f in tc.factors]
    if len(names) != len(set(names)):
        problems.append("factor names must be unique within factors")
    encoding = {f.name: f.encoding for f in tc.factors}

    if not tc.factors:
        if any(c.factor_levels for c in tc.value_columns) or tc.context_levels:
            problems.append("factor_levels/context_levels are used but factors is empty -- declare every factor in `factors`")
        return problems

    for row in tc.row_groups:
        for key in row.factor_values:
            if key not in encoding:
                problems.append(f"row_group '{row.row_group_id}' uses factor '{key}' which is not declared in factors")
            elif encoding[key] != "rows":
                problems.append(f"factor '{key}' is declared encoding='{encoding[key]}' but appears in row_group '{row.row_group_id}'.factor_values (encoding must be 'rows')")
    for col in tc.value_columns:
        for key in col.factor_levels:
            if key not in encoding:
                problems.append(f"value_column '{col.value_column_id}' uses factor '{key}' which is not declared in factors")
            elif encoding[key] != "columns":
                problems.append(f"factor '{key}' is declared encoding='{encoding[key]}' but appears in value_column '{col.value_column_id}'.factor_levels (encoding must be 'columns')")
    for key in tc.context_levels:
        if key not in encoding:
            problems.append(f"context_levels uses factor '{key}' which is not declared in factors")
        elif encoding[key] != "context":
            problems.append(f"factor '{key}' is declared encoding='{encoding[key]}' but appears in context_levels (encoding must be 'context')")
    for pooled in tc.pooled_factors:
        if pooled.name in encoding:
            problems.append(f"pooled factor '{pooled.name}' is also declared in factors -- a pooled factor is ABSENT from the table's rows/columns")
    return problems


def all_anchors(raw_extraction: dict) -> list[str]:
    """Every anchor cited anywhere in a raw extraction dict, deduplicated and
    sorted -- used by the AI Validator stage to fetch exactly the anchor
    texts a candidate record's evidence is grounded in, nothing more."""
    seen: set[str] = set()
    for fact in raw_extraction.get("facts", []) or []:
        for anchor in fact.get("anchors", []) or []:
            seen.add(anchor)
    return sorted(seen)
