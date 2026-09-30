"""Phase 1A focused tests: the deterministic evidence tools added to
`pipeline/content_reader.py` (`list_sections`, `read_section_by_path`,
`read_table_row`, `read_table_cell`, `read_nearby`).

Uses a synthetic (content.md, provenance.json) pair shaped exactly like
`docproc/marker_adapter.py`'s real output (section_path already resolved,
table cells carrying row_index/col_index/cell_text, rendered_in_content_md
flags) -- these tests never call a model and never regenerate any of that
metadata; they only verify the lookup functions read it back correctly and
never mutate the underlying text/anchors.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent.parent / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))
import content_reader as cr  # noqa: E402

PAPER_ID = "cr_test_paper"

CONTENT_MD = (
    "# Materials and methods\n"
    "⟦b:0001⟧\n\n"
    "Site details here.\n"
    "⟦b:0002⟧\n\n"
    "## Site description\n"
    "⟦b:0003⟧\n\n"
    "The site is in California.\n"
    "⟦b:0004⟧\n\n"
    "*Table 1 caption text*\n"
    "⟦b:0005⟧\n\n"
    "| Treatment | Yield |\n"
    "| --- | --- |\n"
    "| control | 3.2 |\n"
    "| n_fert | 4.1 |\n"
    "⟦b:0006⟧\n\n"
    "# Results\n"
    "⟦b:0007⟧\n\n"
    "Yields increased under fertilization.\n"
    "⟦b:0008⟧\n"
)

PROVENANCE = {
    "b:0001": {"block_type": "SectionHeader", "page_id": "page_0",
               "section_path": ["Materials and methods"], "rendered_in_content_md": True},
    "b:0002": {"block_type": "Text", "page_id": "page_0",
               "section_path": ["Materials and methods"], "rendered_in_content_md": True},
    "b:0003": {"block_type": "SectionHeader", "page_id": "page_0",
               "section_path": ["Materials and methods", "Site description"], "rendered_in_content_md": True},
    "b:0004": {"block_type": "Text", "page_id": "page_0",
               "section_path": ["Materials and methods", "Site description"], "rendered_in_content_md": True},
    "b:0005": {"block_type": "Caption", "page_id": "page_0",
               "section_path": ["Materials and methods", "Site description"], "rendered_in_content_md": True},
    "b:0006": {"block_type": "Table", "page_id": "page_0",
               "section_path": ["Materials and methods", "Site description"], "rendered_in_content_md": True},
    "b:0007": {"block_type": "SectionHeader", "page_id": "page_1",
               "section_path": ["Results"], "rendered_in_content_md": True},
    "b:0008": {"block_type": "Text", "page_id": "page_1",
               "section_path": ["Results"], "rendered_in_content_md": True},
    # Table cells: provenance-only, never rendered inline in content.md --
    # exactly marker_adapter.render_table's real convention.
    "b:9001": {"block_type": "TableCell", "page_id": "page_0", "section_path": ["Materials and methods", "Site description"],
               "rendered_in_content_md": False, "parent_table_anchor": "b:0006", "row_index": 0, "col_index": 0, "cell_text": "Treatment"},
    "b:9002": {"block_type": "TableCell", "page_id": "page_0", "section_path": ["Materials and methods", "Site description"],
               "rendered_in_content_md": False, "parent_table_anchor": "b:0006", "row_index": 0, "col_index": 1, "cell_text": "Yield"},
    "b:9003": {"block_type": "TableCell", "page_id": "page_0", "section_path": ["Materials and methods", "Site description"],
               "rendered_in_content_md": False, "parent_table_anchor": "b:0006", "row_index": 1, "col_index": 0, "cell_text": "control"},
    "b:9004": {"block_type": "TableCell", "page_id": "page_0", "section_path": ["Materials and methods", "Site description"],
               "rendered_in_content_md": False, "parent_table_anchor": "b:0006", "row_index": 1, "col_index": 1, "cell_text": "3.2"},
    "b:9005": {"block_type": "TableCell", "page_id": "page_0", "section_path": ["Materials and methods", "Site description"],
               "rendered_in_content_md": False, "parent_table_anchor": "b:0006", "row_index": 2, "col_index": 0, "cell_text": "n_fert"},
    "b:9006": {"block_type": "TableCell", "page_id": "page_0", "section_path": ["Materials and methods", "Site description"],
               "rendered_in_content_md": False, "parent_table_anchor": "b:0006", "row_index": 2, "col_index": 1, "cell_text": "4.1"},
}


def write_paper(tmp_path: Path) -> Path:
    pdir = tmp_path / PAPER_ID
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "content.md").write_text(CONTENT_MD, encoding="utf-8")
    (pdir / "provenance.json").write_text(json.dumps(PROVENANCE), encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------- #
# 1. list_sections
# --------------------------------------------------------------------- #

def test_list_sections_returns_real_outline_in_document_order(tmp_path):
    root = write_paper(tmp_path)
    result = cr.list_sections(PAPER_ID, papers_root=root)
    assert result["found"] is True
    paths = [s["section_path"] for s in result["sections"]]
    assert paths == [
        ["Materials and methods"],
        ["Materials and methods", "Site description"],
        ["Results"],
    ]
    # first_anchor points at the first block actually carrying that path
    assert result["sections"][0]["first_anchor"] == "b:0001"
    assert result["sections"][1]["first_anchor"] == "b:0003"
    assert result["sections"][2]["first_anchor"] == "b:0007"


def test_list_sections_missing_provenance_returns_not_found(tmp_path):
    (tmp_path / "no_provenance_paper").mkdir()
    result = cr.list_sections("no_provenance_paper", papers_root=tmp_path)
    assert result["found"] is False


# --------------------------------------------------------------------- #
# 2. read_section_by_path (section_path-based retrieval)
# --------------------------------------------------------------------- #

def test_read_section_by_path_exact_match(tmp_path):
    root = write_paper(tmp_path)
    result = cr.read_section_by_path(PAPER_ID, ["Results"], papers_root=root)
    assert result["found"] is True
    texts = [b["text"] for b in result["blocks"]]
    assert texts == ["# Results", "Yields increased under fertilization."]


def test_read_section_by_path_prefix_includes_subsections(tmp_path):
    root = write_paper(tmp_path)
    result = cr.read_section_by_path(PAPER_ID, ["Materials and methods"], papers_root=root)
    assert result["found"] is True
    anchors = [b["block_anchor"] for b in result["blocks"]]
    # Includes both the top-level heading's own blocks AND the "Site
    # description" subsection nested under it (b:0003-b:0006).
    assert anchors == ["b:0001", "b:0002", "b:0003", "b:0004", "b:0005", "b:0006"]


def test_read_section_by_path_case_insensitive(tmp_path):
    root = write_paper(tmp_path)
    result = cr.read_section_by_path(PAPER_ID, ["RESULTS"], papers_root=root)
    assert result["found"] is True


def test_read_section_by_path_no_match(tmp_path):
    root = write_paper(tmp_path)
    result = cr.read_section_by_path(PAPER_ID, ["Discussion"], papers_root=root)
    assert result["found"] is False


# --------------------------------------------------------------------- #
# 3. read_table_row / 4. read_table_cell
# --------------------------------------------------------------------- #

def test_read_table_row_returns_correct_cells_in_column_order(tmp_path):
    root = write_paper(tmp_path)
    header = cr.read_table_row(PAPER_ID, "b:0006", 0, papers_root=root)
    assert header["found"] is True
    assert header["cells"] == ["Treatment", "Yield"]

    data_row = cr.read_table_row(PAPER_ID, "b:0006", 1, papers_root=root)
    assert data_row["cells"] == ["control", "3.2"]


def test_read_table_row_unknown_row_not_found(tmp_path):
    root = write_paper(tmp_path)
    result = cr.read_table_row(PAPER_ID, "b:0006", 99, papers_root=root)
    assert result["found"] is False


def test_read_table_cell_returns_single_value(tmp_path):
    root = write_paper(tmp_path)
    cell = cr.read_table_cell(PAPER_ID, "b:0006", 2, 1, papers_root=root)
    assert cell["found"] is True
    assert cell["cell_text"] == "4.1"


def test_read_table_cell_out_of_range_column_not_found(tmp_path):
    root = write_paper(tmp_path)
    cell = cr.read_table_cell(PAPER_ID, "b:0006", 1, 5, papers_root=root)
    assert cell["found"] is False


def test_read_table_row_and_cell_always_cite_the_table_anchor_never_a_cell_anchor(tmp_path):
    # The critical Phase 1 property: no matter which row/cell is asked for,
    # the citable anchor returned is always the TABLE's own anchor -- never
    # one of the individual (provenance-only, never-rendered) cell anchors
    # like b:9001-b:9006 -- since only the table anchor can ever be
    # resolved by validators.validate_provenance.
    root = write_paper(tmp_path)
    for row in range(3):
        row_result = cr.read_table_row(PAPER_ID, "b:0006", row, papers_root=root)
        assert row_result["table_anchor"] == "b:0006"
        cell_result = cr.read_table_cell(PAPER_ID, "b:0006", row, 0, papers_root=root)
        assert cell_result["table_anchor"] == "b:0006"


# --------------------------------------------------------------------- #
# 5. read_nearby (nearby block retrieval)
# --------------------------------------------------------------------- #

def test_read_nearby_returns_surrounding_rendered_blocks_in_order(tmp_path):
    root = write_paper(tmp_path)
    result = cr.read_nearby(PAPER_ID, "b:0004", before=1, after=1, papers_root=root)
    assert result["found"] is True
    anchors = [b["block_anchor"] for b in result["blocks"]]
    assert anchors == ["b:0003", "b:0004", "b:0005"]
    target = [b for b in result["blocks"] if b["is_target"]][0]
    assert target["block_anchor"] == "b:0004"
    assert target["text"] == "The site is in California."


def test_read_nearby_clamps_at_document_boundaries(tmp_path):
    root = write_paper(tmp_path)
    result = cr.read_nearby(PAPER_ID, "b:0001", before=5, after=0, papers_root=root)
    assert result["found"] is True
    assert [b["block_anchor"] for b in result["blocks"]] == ["b:0001"]


def test_read_nearby_unknown_anchor_not_found(tmp_path):
    root = write_paper(tmp_path)
    result = cr.read_nearby(PAPER_ID, "b:9999", before=1, after=1, papers_root=root)
    assert result["found"] is False


# --------------------------------------------------------------------- #
# 6. exact anchor preservation / 7. provenance metadata preservation
# --------------------------------------------------------------------- #

def test_section_by_path_preserves_exact_anchor_page_block_type_and_section_path(tmp_path):
    root = write_paper(tmp_path)
    result = cr.read_section_by_path(PAPER_ID, ["Results"], papers_root=root)
    header_block = result["blocks"][0]
    assert header_block["block_anchor"] == "b:0007"
    assert header_block["page"] == "page_1"
    assert header_block["block_type"] == "SectionHeader"
    assert header_block["section_path"] == ["Results"]
    # Text is returned completely unmodified -- no summarization/paraphrase.
    assert header_block["text"] == "# Results"


def test_table_row_preserves_page_and_section_path_metadata(tmp_path):
    root = write_paper(tmp_path)
    row = cr.read_table_row(PAPER_ID, "b:0006", 1, papers_root=root)
    assert row["page"] == "page_0"
    assert row["section_path"] == ["Materials and methods", "Site description"]


def test_nearby_preserves_metadata_for_every_returned_block(tmp_path):
    root = write_paper(tmp_path)
    result = cr.read_nearby(PAPER_ID, "b:0007", before=1, after=1, papers_root=root)
    for block in result["blocks"]:
        assert block["page"] in ("page_0", "page_1")
        assert block["block_type"]
        assert isinstance(block["section_path"], list)


# --------------------------------------------------------------------- #
# 8. list_tables / raw_table_cells / read_table_full (table-enumeration
#    design review, Step A -- see orchestrator.py's run_table_enumeration)
# --------------------------------------------------------------------- #

def test_list_tables_finds_every_table_block(tmp_path):
    root = write_paper(tmp_path)
    result = cr.list_tables(PAPER_ID, papers_root=root)
    assert result["found"] is True
    assert [t["table_anchor"] for t in result["tables"]] == ["b:0006"]
    assert result["tables"][0]["page"] == "page_0"
    assert result["tables"][0]["section_path"] == ["Materials and methods", "Site description"]


def test_list_tables_missing_provenance_returns_not_found(tmp_path):
    (tmp_path / "no_provenance_paper").mkdir()
    result = cr.list_tables("no_provenance_paper", papers_root=tmp_path)
    assert result["found"] is False


def test_list_tables_no_table_blocks_returns_not_found(tmp_path):
    pdir = tmp_path / "no_tables_paper"
    pdir.mkdir()
    (pdir / "content.md").write_text("Text only.\n⟦b:0001⟧\n", encoding="utf-8")
    (pdir / "provenance.json").write_text(
        json.dumps({"b:0001": {"block_type": "Text", "page_id": "page_0", "section_path": []}}),
        encoding="utf-8",
    )
    result = cr.list_tables("no_tables_paper", papers_root=tmp_path)
    assert result["found"] is False


def test_raw_table_cells_returns_every_nonblank_cell_text(tmp_path):
    root = write_paper(tmp_path)
    cells = cr.raw_table_cells(PAPER_ID, ["b:0006"], papers_root=root)
    assert sorted(cells) == sorted(["Treatment", "Yield", "control", "3.2", "n_fert", "4.1"])


def test_raw_table_cells_filters_by_requested_anchors(tmp_path):
    root = write_paper(tmp_path)
    assert cr.raw_table_cells(PAPER_ID, ["b:9999"], papers_root=root) == []


def test_raw_table_cells_missing_provenance_returns_empty(tmp_path):
    (tmp_path / "no_provenance_paper").mkdir()
    assert cr.raw_table_cells("no_provenance_paper", ["b:0006"], papers_root=tmp_path) == []


def test_read_table_full_combines_markdown_and_structured_rows(tmp_path):
    root = write_paper(tmp_path)
    result = cr.read_table_full(PAPER_ID, "b:0006", papers_root=root)
    assert result["found"] is True
    assert result["table_anchor"] == "b:0006"
    assert "Treatment" in result["markdown_table"]
    assert result["rows"] == [
        ["Treatment", "Yield"],
        ["control", "3.2"],
        ["n_fert", "4.1"],
    ]


def test_read_table_full_missing_anchor_returns_not_found(tmp_path):
    root = write_paper(tmp_path)
    result = cr.read_table_full(PAPER_ID, "b:9999", papers_root=root)
    assert result["found"] is False
