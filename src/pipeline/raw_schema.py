"""`pipeline/raw_schema.py` -- the sealed raw-evidence format the Extraction
AI produces, and the ONLY thing the Conversion AI is ever shown of the
source paper.

Architecture decision (this sprint): Extraction and Conversion are two
separate, narrowly-scoped AI stages. Extraction AI has `content.md` read
tools and no knowledge of the Sage IR contract; Conversion AI has the IR
schema tools (`get_schema`, `lookup_vocab`, `apply_reconstruction`) and NO
`content.md` read tools at all. `RawExtraction` is the sealed handoff
between them -- deliberately looser than `ir_schema.py`'s IR models, since
this stage's job is "what does the source say and where", not "shape it
into the exact IR Pydantic contract."

The one invariant carried over unchanged from the IR layer: every fact must
cite at least one `content.md` block anchor it was actually read from
(`SourceLocator`/`ExtractionSource.locators` in `ir_schema.py` enforce the
same thing at the IR layer; `min_length=1` here is the same rule one stage
earlier). Nothing in this module is EXTRACTED/INFERRED/UNRESOLVED-labeled --
that provenance-label decision belongs to the Conversion AI, which is the
one actually mapping into the IR contract, working only from this sealed
package.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator


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


class EnumerationCandidate(BaseModel):
    """Multi-record extraction (Phase A: Variable only) -- one distinct
    real-world instance of an entity type that a paper reports, identified
    BEFORE full field-level extraction runs for it. Deliberately as sealed
    and evidence-anchored as `RawFact` above: a candidate with no real
    anchor is not a candidate, it's an invention.
    """

    candidate_id: str = Field(min_length=1, description="Short, stable, model-chosen slug, unique within one enumeration.")
    description: str = Field(min_length=1, description="One concise sentence identifying and distinguishing this candidate.")
    anchors: list[str] = Field(min_length=1, description="content.md block anchors actually read that support this being a real, distinct instance.")
    linked_candidates: dict[str, str] = Field(
        default_factory=dict,
        description="Reserved for later phases (e.g. an Observation candidate linking to a Treatment/Variable "
                    "candidate_id) -- always empty for Variable in Phase A.",
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


class TableValueColumn(BaseModel):
    """Table enumeration (Step B, "table classification"): one column of a
    reconstructed table that reports an actual measured value, as opposed
    to a factor/label column (Population, Maturity, ...). The hint fields
    are free text for Step C's templated candidate descriptions and for
    the Extraction stage that later re-reads this candidate's own anchor --
    never trusted as IR-shaped values themselves, same as everything else
    in this module."""

    value_column_id: str = Field(min_length=1, description="Short, stable slug, unique within this table classification.")
    variable_name_hint: str = Field(min_length=1, description="e.g. 'leaf sheath dry weight' -- combines multi-level headers if the source table has them.")
    units_hint: Optional[str] = None
    site_hint: Optional[str] = Field(default=None, description="Set when the column ITSELF encodes a site/location, e.g. a table with separate 'Ames'/'Mead' sub-columns.")
    method_hint: Optional[str] = Field(
        default=None,
        description="The real name/description of the measurement method this column's values were produced by, as "
                    "actually stated in the paper's Methods section (e.g. 'hand-clipping harvest', 'LI-COR LAI-2000 "
                    "leaf area analyzer') -- required for Observation.method_id linking (a required field) to ever "
                    "resolve deterministically; Step C has no other source of method information for a table row.",
    )


class TableRowGroup(BaseModel):
    """Table enumeration (Step B): one logical data row of the
    reconstructed table. May correspond to a SPLIT of a single raw
    geometric table cell -- real confirmed case (Daren-1997-Canopy Table 2,
    content.md anchor b:0119): the raw cell for one population's
    Total-yield-at-Ames column is the single string '0.19 0.90 1.16',
    which is really three logical rows, one per maturity stage. Splitting
    a packed cell like this into the correct number of logical rows is
    exactly the reconstruction judgment this stage asks of the model,
    rather than trusting raw row/col indices uncritically (Marker's own
    geometric row clustering is not reliable on every page -- see this
    same real paper's Table 2 continuation block, b:0178, where a footnote
    row breaks up the visual layout and several unrelated rows get
    clustered into one row_index)."""

    row_group_id: str = Field(min_length=1, description="Short, stable slug, unique within this table classification.")
    factor_values: dict[str, str] = Field(default_factory=dict, description="e.g. {'Population': 'Trailblazer', 'Maturity': 'Vegetative'}.")
    source_table_anchor: str = Field(min_length=1, description="Which ONE physical table block this row's cells actually come from -- must be a member of table_anchors.")
    cells: dict[str, Optional[str]] = Field(default_factory=dict, description="{value_column_id: raw cell text, or null/absent if genuinely not reported for this row.}")


class TableClassification(BaseModel):
    """Table enumeration (Step B): reconstructs ONE logical table -- which
    may span more than one raw content.md block, e.g. a page-split
    continuation -- into a flat, long-format structure Step C can
    mechanically cross-product into EnumerationCandidates. Sealed evidence,
    not yet the final IR contract, same philosophy as RawExtraction above:
    this stage's job is "what does this table actually contain, row by
    row", not "shape it into the exact IR payload."."""

    applicable: bool = Field(description="False when this table does not report per-instance values for the target entity_type at all (e.g. a regression-equation table).")
    reason: Optional[str] = Field(default=None, description="Required when applicable=False, or when applicable=True but row_groups is empty (reconstruction was not confident enough to trust).")
    table_anchors: list[str] = Field(min_length=1, description="Every content.md block anchor that is part of THIS one logical table -- more than one only for a page-split continuation.")
    value_columns: list[TableValueColumn] = Field(default_factory=list)
    row_groups: list[TableRowGroup] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistency(self) -> "TableClassification":
        if not self.applicable and not (self.reason or "").strip():
            raise ValueError("applicable=False requires a real, non-empty reason")
        if self.applicable and not self.row_groups and not (self.reason or "").strip():
            raise ValueError(
                "applicable=True with an empty row_groups requires a real, non-empty reason "
                "explaining why reconstruction was not confident enough to trust"
            )

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


def all_anchors(raw_extraction: dict) -> list[str]:
    """Every anchor cited anywhere in a raw extraction dict, deduplicated and
    sorted -- used by the AI Validator stage to fetch exactly the anchor
    texts a candidate record's evidence is grounded in, nothing more."""
    seen: set[str] = set()
    for fact in raw_extraction.get("facts", []) or []:
        for anchor in fact.get("anchors", []) or []:
            seen.add(anchor)
    return sorted(seen)
