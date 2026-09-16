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

    candidate_id: str = Field(min_length=1, description="Short, stable, model-chosen slug, unique within one enumeration.")
    description: str = Field(min_length=1, description="One concise sentence identifying and distinguishing this candidate.")
    anchors: list[str] = Field(min_length=1, description="content.md block anchors actually read that support this being a real, distinct instance.")
    linked_candidates: dict[str, str] = Field(
        default_factory=dict,
        description="Reserved for later phases (e.g. an Observation candidate linking to a Treatment/Variable "
                    "candidate_id) -- always empty for Variable in Phase A.",
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

    value_column_id: str = Field(min_length=1, description="Short, stable slug, unique within this table classification.")
    variable_name_hint: str = Field(min_length=1, description="e.g. 'leaf sheath dry weight' -- combines multi-level headers if the source table has them.")
    units_hint: Optional[str] = None
    site_hint: Optional[str] = Field(default=None, description="Set when the column ITSELF encodes a site/location, e.g. a table with separate 'Ames'/'Mead' sub-columns.")


class TableRowGroup(BaseModel):

    row_group_id: str = Field(min_length=1, description="Short, stable slug, unique within this table classification.")
    factor_values: dict[str, str] = Field(default_factory=dict, description="e.g. {'Population': 'Trailblazer', 'Maturity': 'Vegetative'}.")
    source_table_anchor: str = Field(min_length=1, description="Which ONE physical table block this row's cells actually come from -- must be a member of table_anchors.")
    cells: dict[str, Optional[str]] = Field(default_factory=dict, description="{value_column_id: raw cell text, or null/absent if genuinely not reported for this row.}")


class TableClassification(BaseModel):

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
    seen: set[str] = set()
    for fact in raw_extraction.get("facts", []) or []:
        for anchor in fact.get("anchors", []) or []:
            seen.add(anchor)
    return sorted(seen)
