"""
Batch QC runner.

Runs the QC gate over every paper on disk and returns only a compact aggregate summary.

Usage:
    python3 run_qc_batch.py <papers_root_dir>

Writes <papers_root_dir>/<paper_id>/qc_report.json for every subdirectory
found, and prints one JSON object to stdout: the aggregate summary.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from qc_gate import write_qc_report


def summarize(paper_report: dict) -> dict:
    """Compact per-paper summary -- small enough to safely return to an LLM
    context without ever including the underlying content.md/provenance.json
    text."""
    rt = paper_report.get("round_trip", {})
    tl = paper_report.get("tag_leak", {})
    bc = paper_report.get("block_type_coverage", {})
    return {
        "paper_id": paper_report["paper_id"],
        "passed": paper_report["passed"],
        "round_trip_ok": rt.get("ok"),
        "round_trip_issue_counts": {
            "duplicate_anchors": len(rt.get("duplicate_anchors", [])),
            "anchors_missing_provenance_entry": len(rt.get("anchors_missing_provenance_entry", [])),
            "rendered_true_missing_from_content_md": len(rt.get("rendered_true_missing_from_content_md", [])),
            "anchors_whose_provenance_says_not_rendered": len(rt.get("anchors_whose_provenance_says_not_rendered", [])),
        } if rt.get("ok") is not None else None,
        "tag_leak_ok": tl.get("ok"),
        "tag_leak_total_hits": tl.get("total_hits"),
        "tag_leak_patterns_hit": sorted(tl.get("hits_by_pattern", {}).keys()),
        "block_type_coverage_ok": bc.get("ok"),
        "unhandled_block_type_count": len(bc.get("unhandled_block_type_entries", [])),
        "block_types_not_in_any_policy_set": bc.get("block_types_not_in_any_policy_set", []),
        "repaired_entry_count": paper_report.get("repaired_entry_count"),
        "error": rt.get("error"),
    }


def run_batch(papers_root: Path) -> dict:
    paper_dirs = sorted(
        p for p in papers_root.iterdir()
        if p.is_dir()
    )

    per_paper_summaries = []
    for pdir in paper_dirs:
        full_report = write_qc_report(pdir)
        per_paper_summaries.append(summarize(full_report))

    total = len(per_paper_summaries)
    passed = sum(1 for s in per_paper_summaries if s["passed"])
    missing_input = sum(1 for s in per_paper_summaries if s.get("error"))

    aggregate = {
        "papers_root": str(papers_root),
        "papers_found": total,
        "papers_passed": passed,
        "papers_failed": total - passed,
        "papers_missing_input": missing_input,
        "per_paper": per_paper_summaries,
    }
    return aggregate


if __name__ == "__main__":
    root = Path(sys.argv[1])
    result = run_batch(root)
    print(json.dumps(result, indent=2))
