"""
curation
========
SAGE curation package for building the paper-text-annotated gold dataset
for extraction benchmarking (Issue #5).
"""

from curation.downloader import PaperDownloader
from curation.filter import CandidateFilter, FilterCriteria
from curation.schema import (
    AnnotationCategory,
    CandidatePaper,
    GoldAnnotation,
    HarmonizedTarget,
    PaperManifest,
    SourceProvenance,
    ZoteroAnnotation,
)

__all__ = [
    "CandidateFilter",
    "FilterCriteria",
    "CandidatePaper",
    "PaperManifest",
    "PaperDownloader",
    "GoldAnnotation",
    "SourceProvenance",
    "HarmonizedTarget",
    "ZoteroAnnotation",
    "AnnotationCategory",
]
