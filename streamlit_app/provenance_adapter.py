"""
provenance_adapter.py
================
The thin bridge described in the architecture investigation:

    ExtractedField.source.locators[].block_anchor
          -> src/paper/<paper_id>/provenance.json
          -> page_id + bbox/polygon (real Marker geometry, PDF points)
          -> real PDF page dimensions (pypdfium2, against the actual PDF)
          -> normalized [0,1] top-left-origin polygon + 1-indexed page

This is the ONLY place that resolves a source locator to a screen
position. It never trusts `ExtractedField.source.page_number` -- verified
during the investigation to be LLM-self-reported and not cross-checked
against ground truth anywhere in the pipeline (a real committed pecan
Citation had page_number=1 while provenance.json's page_id for that same
anchor is 0-indexed page 1, i.e. physical page 2). The only reliable path
is block_anchor -> provenance.json, every time.

Output shape matches what components/pdf_viewer.py and mock_data.py's own
`_PROVENANCE_GEOMETRY` already expect (see mock_data.py's comment: "so a
real provenance.json ... can be dropped in later without changing the
viewer component, as long as it provides the same shape") -- this module
exists so that promise is actually kept.

v1 scope: block-level locators only (page + one polygon per locator).
Table-cell-level provenance exists in provenance.json (see investigation)
but is deliberately not surfaced here yet.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import pypdfium2 as pdfium

import sage_paths


@lru_cache(maxsize=16)
def _load_provenance(paper_id: str) -> dict[str, Any]:
    path = sage_paths.provenance_path(paper_id)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


@lru_cache(maxsize=16)
def _page_geometry(paper_id: str) -> Optional[tuple[float, float, int]]:
    """(width, height, page_count) of the REAL source PDF in PDF points,
    read directly from the PDF itself -- not assumed from a Marker Page
    block, so this stays correct even for a page whose own Page-block
    entry might be missing or unrendered."""
    path = sage_paths.pdf_path(paper_id)
    if path is None:
        return None
    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception:
        return None
    if len(pdf) == 0:
        return None
    width, height = pdf[0].get_size()
    return (width, height, len(pdf))


def clear_cache() -> None:
    """Call after (re)processing a paper -- provenance.json / the PDF may
    have changed on disk since these were last read."""
    _load_provenance.cache_clear()
    _page_geometry.cache_clear()


def resolve_anchor(paper_id: str, block_anchor: str) -> Optional[dict]:
    """One block_anchor -> {block_anchor, block_type, page (1-indexed),
    polygon (normalized [0,1], top-left origin), section_path}, or None
    when it can't be resolved (missing provenance entry, missing/unreadable
    PDF, or a page_id outside the PDF's real page range) -- never a guess,
    never a fabricated position."""
    if not block_anchor:
        return None
    block_anchor = block_anchor.strip("[]")

    entry = _load_provenance(paper_id).get(block_anchor)
    if entry is None:
        return None

    geometry = _page_geometry(paper_id)
    if geometry is None:
        return None
    width, height, page_count = geometry

    page_id = entry.get("page_id") or ""
    try:
        page_index = int(str(page_id).rsplit("_", 1)[-1])
    except (ValueError, IndexError):
        return None
    if not (0 <= page_index < page_count):
        return None

    polygon = entry.get("polygon")
    bbox = entry.get("bbox")
    if polygon:
        points = polygon
    elif bbox and len(bbox) == 4:
        x0, y0, x1, y1 = bbox
        points = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    else:
        return None

    if width <= 0 or height <= 0:
        return None
    normalized_polygon = [[x / width, y / height] for x, y in points]

    return {
        "block_anchor": block_anchor,
        "block_type": entry.get("block_type"),
        "page": page_index + 1,
        "polygon": normalized_polygon,
        "section_path": entry.get("section_path", []),
    }


def resolve_locators(paper_id: str, source: Optional[dict]) -> list[dict]:
    """Resolve every locator on an ExtractionSource dict
    (`{..., locators: [{block_anchor, ...}, ...]}`) that carries a
    block_anchor. Unresolvable locators are silently omitted (never
    fabricated) -- an empty result means "no visual source available for
    this field right now", which the UI must render as such, not as an
    error. Supports multiple locators (multi-block evidence) by design:
    each resolved entry gets its own polygon; the UI draws one highlight
    per entry."""
    if not source:
        return []
    resolved = []
    for locator in source.get("locators") or []:
        if not isinstance(locator, dict):
            continue
        anchor = locator.get("block_anchor")
        if not anchor:
            continue
        result = resolve_anchor(paper_id, anchor)
        if result:
            resolved.append(result)
    return resolved
