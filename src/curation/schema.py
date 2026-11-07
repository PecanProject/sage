"""
curation/schema.py
==================
Data models and schemas for curating the paper-text-annotated gold dataset
for extraction benchmarking (Issue #5).

Supports the separation of:
  1) Source-level paper provenance (as it appears in the text/tables/figures,
     compatible with Zotero document annotations and SAGE IR source locators).
  2) Harmonized BETYdb representation (standardized vocabularies, units, and entities).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, model_validator


class AnnotationCategory(str, Enum):
    """Extraction categories mapping onto BETYdb and SAGE pipeline entities."""
    CITATION = "Citation"
    SITE = "Site"
    SPECIES = "Species"
    CROP = "Crop"
    TREATMENT = "Treatment"
    MANAGEMENT = "Management"
    OBSERVATION = "Observation"
    FIGURE_TABLE = "Important_Table_Figure"


class LocatorKind(str, Enum):
    TEXT = "text"
    TABLE = "table"
    FIGURE = "figure"


class ZoteroRect(BaseModel):
    """Bounding box coordinates [x1, y1, x2, y2] within a PDF page."""
    x1: float
    y1: float
    x2: float
    y2: float


class ZoteroAnnotation(BaseModel):
    """
    Direct document annotation conforming to Zotero API schema.
    Used for recording direct PDF highlights, text notes, and area selections.
    """
    annotation_type: Literal["highlight", "note", "image", "underline"] = "highlight"
    page_label: Optional[str] = Field(default=None, description="Human-readable page number or label in PDF")
    page_index: int = Field(ge=0, description="0-indexed page number")
    rects: List[List[float]] = Field(default_factory=list, description="List of [x1, y1, x2, y2] bounding boxes")
    text: Optional[str] = Field(default=None, description="Exact verbatim text highlighted or selected")
    comment: Optional[str] = Field(default=None, description="Curator note or field designation")
    color: Optional[str] = Field(default="#ffd400", description="Hex color of highlight")
    tags: List[str] = Field(default_factory=list, description="Tags such as sage:category:Treatment")


class SourceProvenance(BaseModel):
    """
    Paper-level provenance recording source information exactly as it appears
    in the source PDF paper before any harmonization.
    """
    page_number: int = Field(ge=1, description="1-indexed page number in the source PDF")
    locator_kind: LocatorKind = Field(default=LocatorKind.TEXT)
    block_anchor: Optional[str] = Field(default=None, description="content.md anchor (e.g. 'b:0042') if processed by Marker")
    table_id: Optional[str] = Field(default=None, description="Table identifier (e.g. 'Table 1')")
    table_row: Optional[int] = Field(default=None, description="Row index in source table grid")
    table_col: Optional[int] = Field(default=None, description="Column index in source table grid")
    figure_id: Optional[str] = Field(default=None, description="Figure identifier (e.g. 'Figure 2')")
    raw_text: str = Field(description="Verbatim excerpt or value as printed in the paper")
    context_notes: Optional[str] = Field(default=None, description="Surrounding context or footnotes clarifying the excerpt")


class HarmonizedTarget(BaseModel):
    """
    Harmonized BETYdb representation mapped from the source evidence.
    """
    category: AnnotationCategory
    target_field: str = Field(description="Target field in BETYdb schema (e.g. 'trait', 'mean', 'units', 'control')")
    standardized_value: Any = Field(description="Harmonized value (e.g., standardized float, ISO date, controlled vocabulary term)")
    standardized_units: Optional[str] = Field(default=None, description="Standardized units (e.g. 'Mg ha-1', 'kg/m2')")
    entity_id: Optional[str] = Field(default=None, description="Unique entity ID (e.g. 'treatment_1', 'obs_yield_control')")
    bety_table: Optional[str] = Field(default=None, description="Target BETYdb table: citations, sites, treatments, managements, traits")
    notes: Optional[str] = Field(default=None, description="Harmonization notes or transformation logic applied")


class GoldAnnotation(BaseModel):
    """
    A single gold standard annotation linking verbatim paper provenance
    with optional harmonized BETYdb target representation.
    """
    annotation_id: str = Field(description="Unique identifier for this annotation record")
    paper_id: str = Field(description="Paper ID or citation identifier")
    category: AnnotationCategory
    provenance: SourceProvenance
    harmonized: Optional[HarmonizedTarget] = None
    zotero: Optional[ZoteroAnnotation] = None
    annotator: Optional[str] = Field(default=None, description="Name or identifier of the curator/annotator")
    verified: bool = Field(default=False, description="Whether this annotation has undergone secondary reconciliation")


class CandidatePaper(BaseModel):
    """
    Metadata and filtering metrics for a candidate paper from the BETYdb citation pool.
    """
    citation_id: int
    doi: Optional[str] = None
    author: Optional[str] = None
    year: Optional[int] = None
    title: Optional[str] = None
    journal: Optional[str] = None
    url: Optional[str] = None
    pdf_url: Optional[str] = None
    treatment_count: int = 0
    treatments: List[str] = Field(default_factory=list)
    has_control_treatment: bool = False
    management_count: int = 0
    managements: List[str] = Field(default_factory=list)
    trait_count: int = 0
    traits: List[str] = Field(default_factory=list)
    site_count: int = 0
    species_list: List[str] = Field(default_factory=list)
    local_pdf_path: Optional[str] = None
    sha256: Optional[str] = None
    status: Literal["candidate", "downloaded", "annotated", "excluded"] = "candidate"
    exclusion_reason: Optional[str] = None


class PaperManifest(BaseModel):
    """
    Structured manifest describing a curated dataset of candidate / gold papers.
    """
    version: str = "1.0.0"
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    description: str = "Curated gold dataset manifest for extraction benchmarking (Issue #5)"
    selection_criteria: Dict[str, Any] = Field(default_factory=dict)
    total_papers: int = 0
    papers: List[CandidatePaper] = Field(default_factory=list)

    @model_validator(mode="after")
    def _compute_totals(self) -> "PaperManifest":
        self.total_papers = len(self.papers)
        return self
