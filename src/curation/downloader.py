"""
curation/downloader.py
======================
Automated paper acquisition helper for downloading scientific paper PDFs
via DOI and Open Access endpoints (OpenAlex, Unpaywall, CrossRef).

Features:
- Automated DOI resolution to direct open-access PDF URLs
- Fallback to publisher direct links from citation records
- PDF magic-byte validation (b'%PDF-') to prevent saving paywalls or error HTML
- SHA-256 checksum calculation for reproducibility and dataset verification
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional, Tuple
import httpx

from curation.schema import CandidatePaper, PaperManifest

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    """Raised when a paper PDF cannot be acquired or validated."""
    pass


class PaperDownloader:
    """Acquires paper PDFs from open access sources using DOI."""

    def __init__(
        self,
        output_dir: Path,
        email: str = "sage-curator@pecanproject.org",
        http_client: Optional[httpx.Client] = None,
    ):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.email = email
        self.client = http_client or httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": f"SAGE-Curator/1.0 (mailto:{self.email})"},
        )

    def resolve_doi_to_pdf_url(self, doi: str) -> Optional[str]:
        """
        Query OpenAlex and Unpaywall to find an open access PDF link for a DOI.
        """
        clean_d = doi.strip()

        # 1. OpenAlex API
        try:
            oa_url = f"https://api.openalex.org/works/https://doi.org/{clean_d}"
            resp = self.client.get(oa_url)
            if resp.status_code == 200:
                data = resp.json()
                best_oa = data.get("best_oa_location") or {}
                pdf_link = best_oa.get("pdf_url")
                if pdf_link:
                    return pdf_link
                # Secondary locations
                for loc in data.get("locations", []):
                    if loc.get("pdf_url"):
                        return loc["pdf_url"]
        except Exception as e:
            logger.debug(f"OpenAlex lookup failed for DOI {clean_d}: {e}")

        # 2. Unpaywall API
        try:
            unpaywall_url = f"https://api.unpaywall.org/v2/{clean_d}?email={self.email}"
            resp = self.client.get(unpaywall_url)
            if resp.status_code == 200:
                data = resp.json()
                best_oa = data.get("best_oa_location") or {}
                pdf_link = best_oa.get("url_for_pdf")
                if pdf_link:
                    return pdf_link
        except Exception as e:
            logger.debug(f"Unpaywall lookup failed for DOI {clean_d}: {e}")

        # 3. Direct DOI URL
        return f"https://doi.org/{clean_d}"

    def download_pdf(self, url: str) -> bytes:
        """Fetch bytes and verify that the payload is actually a PDF file."""
        resp = self.client.get(url, headers={"Accept": "application/pdf, text/html;q=0.1"})
        resp.raise_for_status()

        content = resp.content
        if not content.startswith(b"%PDF-"):
            # Check if first 1024 bytes contain %PDF- (some publishers prepend comments/BOM)
            if b"%PDF-" not in content[:1024]:
                raise DownloadError(f"Response from {url} is not a valid PDF (got {len(content)} bytes).")
            # Trim prefix before %PDF-
            idx = content.find(b"%PDF-")
            content = content[idx:]

        return content

    def download_paper(
        self,
        paper: CandidatePaper,
        filename_prefix: Optional[str] = None,
    ) -> Tuple[Path, str]:
        """
        Download paper PDF for a candidate paper, verify, compute SHA-256,
        and save to output directory.
        """
        prefix = filename_prefix or f"paper_{paper.citation_id}"
        dest_path = self.output_dir / f"{prefix}.pdf"

        candidates_urls = []
        # Check direct PDF url from citation metadata first
        if paper.pdf_url and paper.pdf_url.lower().endswith(".pdf"):
            candidates_urls.append(paper.pdf_url)

        # Resolve via DOI if available
        if paper.doi:
            resolved = self.resolve_doi_to_pdf_url(paper.doi)
            if resolved and resolved not in candidates_urls:
                candidates_urls.append(resolved)

        if paper.url and paper.url not in candidates_urls:
            candidates_urls.append(paper.url)

        last_error = None
        for url in candidates_urls:
            try:
                pdf_bytes = self.download_pdf(url)
                sha = hashlib.sha256(pdf_bytes).hexdigest()
                dest_path.write_bytes(pdf_bytes)
                paper.local_pdf_path = str(dest_path)
                paper.sha256 = sha
                paper.status = "downloaded"
                return dest_path, sha
            except Exception as e:
                last_error = e
                logger.debug(f"Failed download from {url}: {e}")

        paper.status = "excluded"
        paper.exclusion_reason = f"Could not download PDF: {last_error}"
        raise DownloadError(f"Failed to acquire PDF for paper {paper.citation_id}: {last_error}")

    def download_manifest_papers(
        self,
        manifest: PaperManifest,
        max_papers: Optional[int] = None,
    ) -> int:
        """
        Download candidate papers in a manifest up to max_papers limit.
        Returns number of successfully downloaded papers.
        """
        success_count = 0
        papers_to_process = manifest.papers[:max_papers] if max_papers else manifest.papers

        for paper in papers_to_process:
            if paper.status == "downloaded" and paper.local_pdf_path and Path(paper.local_pdf_path).exists():
                success_count += 1
                continue
            try:
                self.download_paper(paper)
                success_count += 1
            except DownloadError:
                continue

        return success_count
