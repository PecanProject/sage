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
    _load_provenance.cache_clear()
    _page_geometry.cache_clear()


def resolve_anchor(paper_id: str, block_anchor: str) -> Optional[dict]:
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
