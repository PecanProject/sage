"""Focused tests for marker_adapter.walk_document's block-type handling --
specifically the PictureGroup fix and the general "unknown block type must
never falsely claim rendered_in_content_md=True" fix.

Both bugs were confirmed against a real Marker JSON file (Winter cover.json,
page 7): a PictureGroup wrapping an image-rendered Table 2 + its caption was
silently dropped from content.md entirely while provenance.json falsely
claimed it had been rendered, tripping qc_gate.round_trip_check's
rendered_true_missing_from_content_md check. See marker_adapter.py's own
comments at STRUCTURAL_CONTAINER_TYPES and the walk() PictureGroup branch,
and the unknown-block-type fallback, for the full reasoning.
"""

from __future__ import annotations

import marker_adapter
import qc_gate


def _block(block_type, block_id, html="", children=None, images=None):
    return {
        "id": block_id,
        "block_type": block_type,
        "html": html,
        "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]],
        "bbox": [0, 0, 1, 1],
        "children": children or [],
        "section_hierarchy": {},
        "images": images or {},
    }


def _doc(*page_children):
    page = _block("Page", "/page/0", children=list(page_children))
    return _block("Document", "/doc", children=[page])


def test_picture_group_unwraps_into_rendered_picture_and_duplicate_caption():
    picture = _block("Picture", "/page/0/Picture/1", images={"/page/0/Picture/1": "base64data"})
    caption = _block("Caption", "/page/0/Caption/2", html="<p>Table 2. Some caption.</p>")
    group = _block(
        "PictureGroup", "/page/0/PictureGroup/3",
        html="<content-ref src='/page/0/Picture/1'></content-ref><content-ref src='/page/0/Caption/2'></content-ref>",
        children=[picture, caption],
    )
    result = marker_adapter.walk_document(_doc(group))

    assert len(result.provenance) == 2
    by_type = {e["block_type"]: (anchor, e) for anchor, e in result.provenance.items()}

    picture_anchor, picture_entry = by_type["Picture"]
    assert picture_entry["rendered_in_content_md"] is True
    assert picture_entry["has_embedded_image_bytes"] is True
    assert picture_entry["caption_text"] == "Table 2. Some caption."
    # The picture's anchor must actually appear in content.md -- this is the
    # exact round-trip property the real bug violated.
    assert f"⟦{picture_anchor}⟧" in result.content_md
    assert "Table 2. Some caption." in result.content_md

    caption_anchor, caption_entry = by_type["Caption"]
    # Caption is logged (capture-first: still a citable leaf type per the
    # policy) but deliberately NOT separately rendered -- its text is
    # already folded into the picture's own placeholder text above, exactly
    # like FigureGroup's existing behavior.
    assert caption_entry["rendered_in_content_md"] is False
    assert f"⟦{caption_anchor}⟧" not in result.content_md


def test_picture_group_is_a_known_block_type_for_qc():
    assert "PictureGroup" in qc_gate.ALL_KNOWN_TYPES


def test_figure_group_still_works_unchanged():
    # Regression: the PictureGroup addition must not disturb FigureGroup's
    # existing, already-relied-upon handling.
    figure = _block("Figure", "/page/0/Figure/1", images={"/page/0/Figure/1": "data"})
    caption = _block("Caption", "/page/0/Caption/2", html="<p>Figure 1 caption.</p>")
    group = _block(
        "FigureGroup", "/page/0/FigureGroup/3",
        html="<content-ref src='/page/0/Figure/1'></content-ref><content-ref src='/page/0/Caption/2'></content-ref>",
        children=[figure, caption],
    )
    result = marker_adapter.walk_document(_doc(group))

    assert len(result.provenance) == 2
    by_type = {e["block_type"]: (anchor, e) for anchor, e in result.provenance.items()}
    figure_anchor, figure_entry = by_type["Figure"]
    assert figure_entry["rendered_in_content_md"] is True
    assert f"⟦{figure_anchor}⟧" in result.content_md
    assert by_type["Caption"][1]["rendered_in_content_md"] is False


def test_unknown_block_type_is_never_falsely_marked_rendered():
    # The general fix: this fallback path never appends anything to
    # content_lines, so it must never claim rendered_in_content_md=True --
    # regardless of what the unrecognized block_type actually is.
    mystery = _block("SomeFutureBlockType", "/page/0/Mystery/1", html="<p>mystery content</p>")
    result = marker_adapter.walk_document(_doc(mystery))

    assert len(result.provenance) == 1
    anchor, entry = next(iter(result.provenance.items()))
    assert entry["unhandled_block_type"] is True
    assert entry["rendered_in_content_md"] is False
    # Honest round-trip: not claimed rendered, and indeed absent from content.md.
    assert f"⟦{anchor}⟧" not in result.content_md


def test_unknown_block_type_passes_round_trip_check():
    # End-to-end proof against the actual QC check this bug tripped: an
    # unhandled block type must never itself cause a round-trip failure
    # (block_type_coverage_check will still separately flag it as an
    # unknown type -- that part is correct and unchanged).
    mystery = _block("SomeFutureBlockType", "/page/0/Mystery/1", html="<p>mystery content</p>")
    result = marker_adapter.walk_document(_doc(mystery))

    rt = qc_gate.round_trip_check(result.content_md, result.provenance)
    assert rt["rendered_true_missing_from_content_md"] == []
