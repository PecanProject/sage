from __future__ import annotations

from streamlit.testing.v1 import AppTest

_SCRIPT = """
import components.pdf_viewer as pdf_viewer
pdf_viewer.render_pdf_page(
    pdf_bytes=b"%PDF-fake-bytes-for-structural-test",
    page_number={page},
    polygon={polygon},
    block_id={block_id!r},
    height=700,
    key="test_viewer",
)
"""


def _render(page_number=1, polygon=None, block_id=None):
    at = AppTest.from_string(_SCRIPT.format(page=page_number, polygon=polygon, block_id=block_id))
    at.run()
    assert not list(at.exception)
    return at.get("iframe")[0].proto.srcdoc


def test_renders_a_loop_over_every_pdf_page_not_just_one():
    srcdoc = _render()
    # renderAllPages() is called more than once (initial load, and again on
    # every zoom change via setZoom) -- it must read the page count off the
    # persistent `pdfDoc` module-level variable, not the transient `pdf`
    # callback parameter that only exists inside the one-time initial
    # getDocument().then(...) callback (see pdf_viewer.py:253-257).
    assert "pdfDoc.numPages" in srcdoc
    assert "for (let pageIndex = 1; pageIndex <= pdfDoc.numPages; pageIndex++)" in srcdoc


def test_holder_has_its_own_independent_vertical_scroll():
    srcdoc = _render()
    assert "overflow-y:auto" in srcdoc


def test_target_page_and_polygon_are_embedded_for_auto_navigation():
    srcdoc = _render(page_number=7, polygon=[[0.1, 0.2], [0.3, 0.2], [0.3, 0.4], [0.1, 0.4]])
    assert "const targetPage = 7;" in srcdoc
    assert "0.1, 0.2" in srcdoc.replace(" ", "") or "[0.1,0.2]" in srcdoc.replace(" ", "")


def test_no_polygon_still_renders_all_pages_without_error():
    srcdoc = _render(page_number=1, polygon=None)
    assert "const targetPolygon = [];" in srcdoc


def test_missing_pdf_bytes_shows_placeholder_not_an_iframe():
    at = AppTest.from_string("""
import components.pdf_viewer as pdf_viewer
pdf_viewer.render_pdf_page(pdf_bytes=None, page_number=1, polygon=None, key="empty")
""")
    at.run()
    assert not list(at.exception)
    assert at.get("iframe") == []
    assert len(at.warning) == 1
