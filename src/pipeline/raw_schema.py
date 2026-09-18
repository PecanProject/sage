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


def all_anchors(raw_extraction: dict) -> list[str]:
    """Every anchor cited anywhere in a raw extraction dict, deduplicated and
    sorted -- used by the AI Validator stage to fetch exactly the anchor
    texts a candidate record's evidence is grounded in, nothing more."""
    seen: set[str] = set()
    for fact in raw_extraction.get("facts", []) or []:
        for anchor in fact.get("anchors", []) or []:
            seen.add(anchor)
    return sorted(seen)
