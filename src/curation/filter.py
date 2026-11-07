"""
curation/filter.py
==================
Filtering and ranking utility to curate candidate papers from the
PecanProject/betydata database (citations, treatments, managements, traits).

Implements the candidate selection criteria established in Issue #5:
- Multiple observed treatments (>= 2)
- Linked management records
- Trait or yield observations
- Valid, auto-downloadable DOI
- Modern publication year (excluding out-of-scope historical entries)
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import httpx
from pydantic import BaseModel, Field

from curation.schema import CandidatePaper, PaperManifest


BETYDATA_RAW_BASE = "https://raw.githubusercontent.com/PecanProject/betydata/main/data-raw/csv"


class FilterCriteria(BaseModel):
    """Configurable thresholds for filtering candidate papers."""
    min_treatments: int = Field(default=2, description="Minimum distinct treatments per paper")
    min_managements: int = Field(default=1, description="Minimum linked management records per paper")
    min_traits: int = Field(default=1, description="Minimum trait or yield observations per paper")
    require_doi: bool = Field(default=True, description="Require a non-empty, valid DOI")
    require_control: bool = Field(default=False, description="Require at least one identified control treatment")
    min_year: int = Field(default=1990, description="Minimum publication year")
    max_year: int = Field(default=2026, description="Maximum publication year")


def clean_doi(doi: Optional[str]) -> Optional[str]:
    """Clean and standardize DOI strings."""
    if not doi:
        return None
    cleaned = doi.strip()
    if not cleaned or cleaned in ('""', "''", "\\N", "NA", "null"):
        return None
    # Strip URL prefixes if present
    cleaned = re.sub(r"^https?://(dx\.)?doi\.org/", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()
    # Basic DOI format validation (e.g. 10.xxxx/...)
    if re.match(r"^10\.\d{4,9}/[-._;()/:A-Za-z0-9]+$", cleaned):
        return cleaned
    return cleaned if cleaned.startswith("10.") else None


class CandidateFilter:
    """
    Loads BETYdata citation and relationship tables and applies filtering rules.
    """

    def __init__(self, data_dir: Optional[Path] = None, http_client: Optional[httpx.Client] = None):
        self.data_dir = data_dir
        self.client = http_client or httpx.Client(timeout=30.0)

    def _read_csv(self, filename: str) -> List[Dict[str, str]]:
        """Read a CSV file either from a local directory or fetch from GitHub."""
        if self.data_dir:
            file_path = self.data_dir / filename
            if file_path.exists():
                with open(file_path, mode="r", encoding="utf-8", errors="replace") as f:
                    reader = csv.DictReader(f)
                    return list(reader)

        url = f"{BETYDATA_RAW_BASE}/{filename}"
        resp = self.client.get(url)
        resp.raise_for_status()
        text_stream = io.StringIO(resp.text)
        reader = csv.DictReader(text_stream)
        return list(reader)

    def load_data(
        self,
        citations_rows: Optional[List[Dict[str, str]]] = None,
        traits_rows: Optional[List[Dict[str, str]]] = None,
        treatments_rows: Optional[List[Dict[str, str]]] = None,
        managements_treatments_rows: Optional[List[Dict[str, str]]] = None,
    ) -> Tuple[
        List[Dict[str, str]],
        List[Dict[str, str]],
        Dict[int, Dict[str, Any]],
        Dict[int, Set[int]],
    ]:
        """
        Load required tables and pre-build relational indices.
        """
        citations = citations_rows if citations_rows is not None else self._read_csv("citations.csv")
        traits = traits_rows if traits_rows is not None else self._read_csv("traitsview.csv")
        treatments_raw = treatments_rows if treatments_rows is not None else self._read_csv("treatments.csv")
        mgmt_treat_raw = (
            managements_treatments_rows
            if managements_treatments_rows is not None
            else self._read_csv("managements_treatments.csv")
        )

        treatments_by_id: Dict[int, Dict[str, Any]] = {}
        for row in treatments_raw:
            try:
                tid = int(row.get("id") or 0)
                if tid:
                    treatments_by_id[tid] = {
                        "name": (row.get("name") or "").strip(),
                        "control": str(row.get("control") or "").strip().lower() in ("t", "true", "1"),
                    }
            except (ValueError, TypeError):
                continue

        # Map treatment_id -> set of management_ids
        treatment_to_managements: Dict[int, Set[int]] = {}
        for row in mgmt_treat_raw:
            try:
                tid = int(row.get("treatment_id") or 0)
                mid = int(row.get("management_id") or 0)
                if tid and mid:
                    treatment_to_managements.setdefault(tid, set()).add(mid)
            except (ValueError, TypeError):
                continue

        return citations, traits, treatments_by_id, treatment_to_managements

    def filter_candidates(
        self,
        criteria: Optional[FilterCriteria] = None,
        citations_rows: Optional[List[Dict[str, str]]] = None,
        traits_rows: Optional[List[Dict[str, str]]] = None,
        treatments_rows: Optional[List[Dict[str, str]]] = None,
        managements_treatments_rows: Optional[List[Dict[str, str]]] = None,
    ) -> List[CandidatePaper]:
        """
        Filter and rank candidate papers according to criteria.
        """
        crit = criteria or FilterCriteria()
        citations, traits, treatments_by_id, treatment_to_mgmt = self.load_data(
            citations_rows=citations_rows,
            traits_rows=traits_rows,
            treatments_rows=treatments_rows,
            managements_treatments_rows=managements_treatments_rows,
        )

        # Aggregate traits by citation_id
        traits_by_citation: Dict[int, List[Dict[str, str]]] = {}
        for row in traits:
            try:
                cid = int(row.get("citation_id") or 0)
                if cid:
                    traits_by_citation.setdefault(cid, []).append(row)
            except (ValueError, TypeError):
                continue

        candidate_list: List[CandidatePaper] = []

        for cite in citations:
            try:
                cid = int(cite.get("id") or 0)
            except (ValueError, TypeError):
                continue

            if not cid:
                continue

            raw_doi = cite.get("doi")
            doi = clean_doi(raw_doi)
            if crit.require_doi and not doi:
                continue

            try:
                year_val = int(cite.get("year") or 0)
            except (ValueError, TypeError):
                year_val = 0

            if year_val:
                if year_val < crit.min_year or year_val > crit.max_year:
                    continue
            elif crit.min_year > 0:
                continue

            cite_traits = traits_by_citation.get(cid, [])
            trait_count = len(cite_traits)
            if trait_count < crit.min_traits:
                continue

            # Gather associated treatments from traitsview rows
            distinct_treatment_ids: Set[int] = set()
            distinct_treatment_names: Set[str] = set()
            distinct_sites: Set[str] = set()
            distinct_species: Set[str] = set()
            distinct_traits: Set[str] = set()
            has_control = False
            linked_management_ids: Set[int] = set()

            for t_row in cite_traits:
                t_name = (t_row.get("treatment") or "").strip()
                if t_name:
                    distinct_treatment_names.add(t_name)
                    if "control" in t_name.lower():
                        has_control = True

                try:
                    tid = int(t_row.get("treatment_id") or 0)
                    if tid:
                        distinct_treatment_ids.add(tid)
                        # Check treatment table lookup for control status
                        t_meta = treatments_by_id.get(tid)
                        if t_meta:
                            if t_meta.get("control"):
                                has_control = True
                            if t_meta.get("name"):
                                distinct_treatment_names.add(t_meta["name"])
                        # Check linked managements
                        if tid in treatment_to_mgmt:
                            linked_management_ids.update(treatment_to_mgmt[tid])
                except (ValueError, TypeError):
                    pass

                site_name = (t_row.get("sitename") or "").strip()
                if site_name:
                    distinct_sites.add(site_name)

                spec = (t_row.get("scientificname") or t_row.get("commonname") or "").strip()
                if spec:
                    distinct_species.add(spec)

                trait_name = (t_row.get("trait") or "").strip()
                if trait_name:
                    distinct_traits.add(trait_name)

            treatment_count = max(len(distinct_treatment_ids), len(distinct_treatment_names))
            if treatment_count < crit.min_treatments:
                continue

            mgmt_count = len(linked_management_ids)
            if mgmt_count < crit.min_managements:
                continue

            if crit.require_control and not has_control:
                continue

            # Paper qualifies!
            paper = CandidatePaper(
                citation_id=cid,
                doi=doi,
                author=(cite.get("author") or "").strip(),
                year=year_val if year_val else None,
                title=(cite.get("title") or "").strip(),
                journal=(cite.get("journal") or "").strip(),
                url=(cite.get("url") or "").strip() or None,
                pdf_url=(cite.get("pdf") or "").strip() or None,
                treatment_count=treatment_count,
                treatments=sorted(distinct_treatment_names),
                has_control_treatment=has_control,
                management_count=mgmt_count,
                managements=[f"mgmt_{m}" for m in sorted(linked_management_ids)],
                trait_count=trait_count,
                traits=sorted(distinct_traits),
                site_count=len(distinct_sites),
                species_list=sorted(distinct_species),
                status="candidate",
            )
            candidate_list.append(paper)

        # Sort candidate papers by rich content score:
        # Prioritize: has_control (1/0), treatment_count * 3 + mgmt_count * 2 + trait_count
        def rank_score(p: CandidatePaper) -> float:
            ctrl_boost = 50.0 if p.has_control_treatment else 0.0
            return ctrl_boost + (p.treatment_count * 5.0) + (p.management_count * 3.0) + min(p.trait_count, 100)

        candidate_list.sort(key=rank_score, reverse=True)
        return candidate_list

    def build_manifest(
        self,
        candidates: List[CandidatePaper],
        criteria: Optional[FilterCriteria] = None,
        description: Optional[str] = None,
    ) -> PaperManifest:
        """Construct a PaperManifest from filtered candidate papers."""
        crit_dict = criteria.model_dump() if criteria else FilterCriteria().model_dump()
        return PaperManifest(
            description=description or "Curated gold dataset candidates from BETYdb citation pool",
            selection_criteria=crit_dict,
            papers=candidates,
        )
