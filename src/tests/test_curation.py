"""
tests/test_curation.py
======================
Unit tests for the gold dataset curation, filtering, schema, and downloader utilities (Issue #5).
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import httpx

from curation.downloader import DownloadError, PaperDownloader
from curation.filter import CandidateFilter, FilterCriteria, clean_doi
from curation.schema import (
    AnnotationCategory,
    CandidatePaper,
    GoldAnnotation,
    HarmonizedTarget,
    LocatorKind,
    PaperManifest,
    SourceProvenance,
    ZoteroAnnotation,
)
from curation.cli import build_parser, cmd_filter, cmd_validate


# --------------------------------------------------------------------------- #
# Schema & Model Tests
# --------------------------------------------------------------------------- #


def test_clean_doi():
    assert clean_doi("10.2134/agronj2005.0549") == "10.2134/agronj2005.0549"
    assert clean_doi("https://doi.org/10.1016/j.agee.2010.02.001") == "10.1016/j.agee.2010.02.001"
    assert clean_doi("http://dx.doi.org/10.1111/gcbb.12420") == "10.1111/gcbb.12420"
    assert clean_doi("") is None
    assert clean_doi("NA") is None
    assert clean_doi('\\N') is None
    assert clean_doi("not-a-doi") is None


def test_gold_annotation_model_validation():
    provenance = SourceProvenance(
        page_number=3,
        locator_kind=LocatorKind.TABLE,
        table_id="Table 2",
        table_row=4,
        table_col=2,
        raw_text="3.42 ± 0.25 Mg ha-1",
    )
    harmonized = HarmonizedTarget(
        category=AnnotationCategory.OBSERVATION,
        target_field="mean",
        standardized_value=3.42,
        standardized_units="Mg ha-1",
        entity_id="obs_001",
        bety_table="traits",
    )
    zotero = ZoteroAnnotation(
        page_index=2,
        rects=[[100.0, 200.0, 150.0, 215.0]],
        text="3.42 ± 0.25 Mg ha-1",
        comment="Yield mean",
        tags=["sage:category:Observation"],
    )

    annotation = GoldAnnotation(
        annotation_id="annot_test_1",
        paper_id="paper_101",
        category=AnnotationCategory.OBSERVATION,
        provenance=provenance,
        harmonized=harmonized,
        zotero=zotero,
        annotator="curator_1",
        verified=True,
    )

    data = annotation.model_dump()
    assert data["category"] == "Observation"
    assert data["provenance"]["table_id"] == "Table 2"
    assert data["harmonized"]["standardized_value"] == 3.42
    assert data["zotero"]["page_index"] == 2

    # Roundtrip check
    restored = GoldAnnotation.model_validate(data)
    assert restored.annotation_id == "annot_test_1"
    assert restored.verified is True


def test_paper_manifest_serialization():
    paper = CandidatePaper(
        citation_id=42,
        doi="10.1234/agronomy.42",
        author="Smith et al.",
        year=2018,
        title="Agronomic trials in switchgrass",
        treatment_count=3,
        treatments=["Control", "Low N", "High N"],
        has_control_treatment=True,
        management_count=2,
        managements=["mgmt_1", "mgmt_2"],
        trait_count=12,
        traits=["yield", "height"],
        site_count=1,
        species_list=["Panicum virgatum"],
        status="candidate",
    )

    manifest = PaperManifest(
        description="Test manifest",
        selection_criteria={"min_treatments": 2},
        papers=[paper],
    )
    assert manifest.total_papers == 1

    dumped = manifest.model_dump()
    restored = PaperManifest.model_validate(dumped)
    assert restored.total_papers == 1
    assert restored.papers[0].citation_id == 42


# --------------------------------------------------------------------------- #
# Filtering Logic Tests
# --------------------------------------------------------------------------- #


@pytest.fixture
def sample_bety_data():
    citations = [
        {
            "id": "1",
            "author": "Alice",
            "year": "2015",
            "title": "Study of N Fertilizer",
            "journal": "Field Crops Research",
            "doi": "10.1016/j.fcr.2015.01.001",
            "url": "https://example.com/paper1",
            "pdf": "https://example.com/paper1.pdf",
        },
        {
            "id": "2",
            "author": "Bob",
            "year": "1885",  # Out of range year
            "title": "Ancient trials",
            "journal": "Old Journal",
            "doi": "10.1016/j.old.1885",
            "url": "",
            "pdf": "",
        },
        {
            "id": "3",
            "author": "Charlie",
            "year": "2018",
            "title": "Study without DOI",
            "journal": "Agronomy",
            "doi": "",  # Missing DOI
            "url": "",
            "pdf": "",
        },
        {
            "id": "4",
            "author": "David",
            "year": "2020",
            "title": "Single treatment only",
            "journal": "Crop Science",
            "doi": "10.2135/cropsci2020.01",
            "url": "",
            "pdf": "",
        },
    ]

    traits = [
        # Paper 1: 2 treatments (control, fert), 1 site, traits
        {"id": "101", "citation_id": "1", "treatment_id": "10", "treatment": "Control", "trait": "yield", "sitename": "Site A", "scientificname": "Zea mays"},
        {"id": "102", "citation_id": "1", "treatment_id": "11", "treatment": "Fertilizer_100", "trait": "yield", "sitename": "Site A", "scientificname": "Zea mays"},
        # Paper 4: Only 1 treatment
        {"id": "103", "citation_id": "4", "treatment_id": "12", "treatment": "Mono", "trait": "biomass", "sitename": "Site B", "scientificname": "Glycine max"},
    ]

    treatments = [
        {"id": "10", "name": "Control", "control": "t"},
        {"id": "11", "name": "Fertilizer_100", "control": "f"},
        {"id": "12", "name": "Mono", "control": "f"},
    ]

    managements_treatments = [
        {"management_id": "1001", "treatment_id": "11"},
    ]

    return citations, traits, treatments, managements_treatments


def test_candidate_filter_qualifying(sample_bety_data):
    citations, traits, treatments, mgmt_treat = sample_bety_data
    filterer = CandidateFilter()

    criteria = FilterCriteria(
        min_treatments=2,
        min_managements=1,
        min_traits=1,
        require_doi=True,
        min_year=1990,
    )

    candidates = filterer.filter_candidates(
        criteria=criteria,
        citations_rows=citations,
        traits_rows=traits,
        treatments_rows=treatments,
        managements_treatments_rows=mgmt_treat,
    )

    assert len(candidates) == 1
    selected = candidates[0]
    assert selected.citation_id == 1
    assert selected.doi == "10.1016/j.fcr.2015.01.001"
    assert selected.treatment_count >= 2
    assert selected.has_control_treatment is True
    assert selected.management_count >= 1
    assert "Zea mays" in selected.species_list


def test_candidate_filter_excludes_out_of_range_year(sample_bety_data):
    citations, traits, treatments, mgmt_treat = sample_bety_data
    # Give paper 2 qualifying treatments and traits
    traits.append({"id": "201", "citation_id": "2", "treatment_id": "10", "treatment": "Control", "trait": "yield", "sitename": "Site C", "scientificname": "Wheat"})
    traits.append({"id": "202", "citation_id": "2", "treatment_id": "11", "treatment": "Fert", "trait": "yield", "sitename": "Site C", "scientificname": "Wheat"})

    filterer = CandidateFilter()
    criteria = FilterCriteria(min_year=1990)
    candidates = filterer.filter_candidates(
        criteria=criteria,
        citations_rows=citations,
        traits_rows=traits,
        treatments_rows=treatments,
        managements_treatments_rows=mgmt_treat,
    )

    assert not any(c.citation_id == 2 for c in candidates)


def test_candidate_filter_excludes_missing_doi(sample_bety_data):
    citations, traits, treatments, mgmt_treat = sample_bety_data
    traits.append({"id": "301", "citation_id": "3", "treatment_id": "10", "treatment": "Control", "trait": "yield", "sitename": "Site D", "scientificname": "Barley"})
    traits.append({"id": "302", "citation_id": "3", "treatment_id": "11", "treatment": "Fert", "trait": "yield", "sitename": "Site D", "scientificname": "Barley"})

    filterer = CandidateFilter()
    criteria = FilterCriteria(require_doi=True)
    candidates = filterer.filter_candidates(
        criteria=criteria,
        citations_rows=citations,
        traits_rows=traits,
        treatments_rows=treatments,
        managements_treatments_rows=mgmt_treat,
    )

    assert not any(c.citation_id == 3 for c in candidates)


# --------------------------------------------------------------------------- #
# Downloader Tests
# --------------------------------------------------------------------------- #


def test_downloader_pdf_magic_validation(tmp_path):
    downloader = PaperDownloader(output_dir=tmp_path)

    # Valid PDF content
    valid_pdf = b"%PDF-1.4\n%trailer\n%%EOF"
    mock_resp = MagicMock()
    mock_resp.content = valid_pdf
    mock_resp.raise_for_status.return_value = None

    with patch.object(downloader.client, "get", return_value=mock_resp):
        content = downloader.download_pdf("https://example.com/valid.pdf")
        assert content.startswith(b"%PDF-")

    # Invalid HTML error page
    invalid_html = b"<html><head><title>Paywall</title></head><body>Login required</body></html>"
    mock_resp.content = invalid_html

    with patch.object(downloader.client, "get", return_value=mock_resp):
        with pytest.raises(DownloadError, match="not a valid PDF"):
            downloader.download_pdf("https://example.com/paywall.html")


def test_downloader_paper_success(tmp_path):
    downloader = PaperDownloader(output_dir=tmp_path)

    paper = CandidatePaper(
        citation_id=99,
        doi="10.1000/182",
        pdf_url="https://example.com/test_paper.pdf",
    )

    valid_pdf = b"%PDF-1.5 test document content"
    mock_resp = MagicMock()
    mock_resp.content = valid_pdf
    mock_resp.raise_for_status.return_value = None

    with patch.object(downloader.client, "get", return_value=mock_resp):
        saved_path, sha = downloader.download_paper(paper)
        assert saved_path.exists()
        assert paper.status == "downloaded"
        assert paper.sha256 == sha
        assert saved_path.read_bytes().startswith(b"%PDF-")


# --------------------------------------------------------------------------- #
# CLI Tests
# --------------------------------------------------------------------------- #


def test_cli_parser():
    parser = build_parser()
    args = parser.parse_args(["filter", "--min-treatments", "3", "--min-year", "2000"])
    assert args.command == "filter"
    assert args.min_treatments == 3
    assert args.min_year == 2000

    dl_args = parser.parse_args(["download", "--manifest", "test.json", "--limit", "2"])
    assert dl_args.command == "download"
    assert dl_args.manifest == "test.json"
    assert dl_args.limit == 2


def test_cli_validate_command(tmp_path):
    annot_file = tmp_path / "annotations.json"
    valid_record = {
        "annotation_id": "ann_1",
        "paper_id": "p1",
        "category": "Treatment",
        "provenance": {
            "page_number": 1,
            "locator_kind": "text",
            "raw_text": "Control treatment without irrigation",
        },
    }
    annot_file.write_text(json.dumps([valid_record]), encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(["validate", "--annotations", str(annot_file)])
    ret = cmd_validate(args)
    assert ret == 0
