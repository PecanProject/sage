"""
QC gate for docproc's Marker adapter output.

Scope, per this session's redefinition of Sprint 1 (superseding the earlier
paper-by-paper manual forensic pass, which does not converge -- structural
variance across papers is inherent and no adapter will catch all of it by
hand-inspection):

    The adapter's job stops at producing a structurally sound, self-consistent
    (content.md, provenance.json) pair and flagging what it could not
    guarantee. Per-paper structural QUIRKS beyond that (Marker's own layout
    detection producing different heading-level assignments on a different
    paper, a different --use_llm corruption variant, an unexpected new
    block_type) are NOT chased down by hand here -- they are caught
    automatically by this gate and handed to the agent's self-correction loop
    / scientist review downstream, by design (see Playbook Section 4's
    reframing). This module is what actually catches them, mechanically,
    instead of a person reading each paper's output.

This module operates ONLY on already-produced (content.md, provenance.json)
pairs read from disk -- never on raw Marker JSON, and it deliberately does
NOT need it. That's a load-bearing property, not an incidental one: the batch
runner (run_qc_batch.py) is designed so only qc_report.json summaries ever
need to come back into an LLM's context window, never the underlying paper
content.

Three checks, each independent and each contributing to one `qc_report.json`:

  1. round_trip_check       -- every content.md anchor has a matching
                                provenance.json entry and vice versa
                                (rendered_in_content_md=true <-> anchor
                                presence, checked both directions).
  2. tag_leak_sweep         -- regex sweep of the FINAL rendered content.md
                                for any leaked real or HTML-escaped tag
                                fragments (the --use_llm corruption class
                                found in Sprint 1, and a generic net wide
                                enough to catch a different corruption
                                variant on a different paper without needing
                                a new signature written by hand first).
  3. block_type_coverage    -- every block_type actually present in
                                provenance.json is accounted for by one of
                                the adapter's four policy sets (imported
                                directly from marker_adapter.py, not
                                duplicated here, so the two can never drift
                                out of sync); flags any
                                unhandled_block_type: true entries and any
                                block_type absent from all four sets.

A paper's qc_report.json records a top-level "passed": bool plus the
per-check detail; nothing here decides pass/fail thresholds beyond
"zero issues found" -- there's no partial-credit scoring, this is a gate,
not a score.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from marker_adapter import (
    CITABLE_LEAF_TYPES,
    PROVENANCE_ONLY_TYPES,
    PROVENANCE_ONLY_SILENT_TYPES,
    STRUCTURAL_CONTAINER_TYPES,
)

ANCHOR_RE = re.compile(r"⟦(b:\d+)⟧")

# Deliberately wide, not just the exact severity-1/severity-2 signatures the
# adapter already knows how to repair -- the point of this sweep is to catch
# a DIFFERENT corruption variant on a different paper that the adapter has no
# named repair for yet, not just to confirm the known repairs worked.
_LEAK_PATTERNS = {
    "html_escaped_tag_fragment": re.compile(r"&lt;/?[a-zA-Z][a-zA-Z0-9]*(?:\s[^&]*?)?&gt;"),
    "raw_unrendered_tag": re.compile(r"<(?!math\b)[a-zA-Z/][a-zA-Z0-9]*(?:\s[^<>]*)?>"),
    "leftover_content_ref": re.compile(r"<content-ref\b"),
    "dangling_amp_entity": re.compile(r"&amp;(lt|gt|amp);"),  # the exact double-escape residue shape
}

ALL_KNOWN_TYPES = (
    CITABLE_LEAF_TYPES
    | PROVENANCE_ONLY_TYPES
    | PROVENANCE_ONLY_SILENT_TYPES
    | STRUCTURAL_CONTAINER_TYPES
)


@dataclass
class QCReport:
    paper_id: str
    passed: bool
    round_trip: dict[str, Any] = field(default_factory=dict)
    tag_leak: dict[str, Any] = field(default_factory=dict)
    block_type_coverage: dict[str, Any] = field(default_factory=dict)
    repaired_entry_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "passed": self.passed,
            "round_trip": self.round_trip,
            "tag_leak": self.tag_leak,
            "block_type_coverage": self.block_type_coverage,
            "repaired_entry_count": self.repaired_entry_count,
        }


def round_trip_check(content_md: str, provenance: dict[str, Any]) -> dict[str, Any]:
    """Anchors in content.md <-> provenance.json entries marked
    rendered_in_content_md=true must match exactly in both directions."""
    anchors_in_md = re.findall(ANCHOR_RE, content_md)
    anchor_counts: dict[str, int] = {}
    for a in anchors_in_md:
        anchor_counts[a] = anchor_counts.get(a, 0) + 1

    duplicate_anchors = [a for a, n in anchor_counts.items() if n > 1]
    anchors_in_md_set = set(anchor_counts.keys())

    rendered_keys = {
        k for k, v in provenance.items() if v.get("rendered_in_content_md")
    }

    anchors_missing_provenance = sorted(anchors_in_md_set - set(provenance.keys()))
    rendered_missing_anchor = sorted(rendered_keys - anchors_in_md_set)
    # An anchor present in content.md but whose provenance entry exists and is
    # explicitly marked rendered_in_content_md=false is also a real
    # inconsistency (the entry itself disagrees with the artifact).
    anchors_marked_not_rendered = sorted(
        a for a in anchors_in_md_set
        if a in provenance and not provenance[a].get("rendered_in_content_md")
    )

    ok = not (
        duplicate_anchors
        or anchors_missing_provenance
        or rendered_missing_anchor
        or anchors_marked_not_rendered
    )
    return {
        "ok": ok,
        "anchors_in_content_md": len(anchors_in_md_set),
        "provenance_entries_total": len(provenance),
        "provenance_entries_rendered_true": len(rendered_keys),
        "duplicate_anchors": duplicate_anchors,
        "anchors_missing_provenance_entry": anchors_missing_provenance,
        "rendered_true_missing_from_content_md": rendered_missing_anchor,
        "anchors_whose_provenance_says_not_rendered": anchors_marked_not_rendered,
    }


def tag_leak_sweep(content_md: str) -> dict[str, Any]:
    """Sweep the FINAL rendered content.md for leaked tag fragments -- both
    the specific corruption signatures already known and a wider net for
    anything shaped like a leaked tag, so a new paper's different corruption
    variant still gets caught instead of silently passing because it doesn't
    match a signature written for paper 1."""
    hits: dict[str, list[str]] = {}
    total = 0
    for label, pattern in _LEAK_PATTERNS.items():
        found = pattern.findall(content_md)
        if found:
            # cap stored examples; the count is what matters for pass/fail
            hits[label] = found[:10]
            total += len(found)
    return {
        "ok": total == 0,
        "total_hits": total,
        "hits_by_pattern": hits,
    }


def block_type_coverage_check(provenance: dict[str, Any]) -> dict[str, Any]:
    """Every block_type actually seen in provenance.json must be covered by
    exactly one of the adapter's four policy sets (imported, not duplicated),
    and no entry may carry unhandled_block_type=true."""
    seen_types: dict[str, int] = {}
    unhandled_entries = []
    uncovered_types: set[str] = set()

    for anchor, entry in provenance.items():
        bt = entry.get("block_type")
        seen_types[bt] = seen_types.get(bt, 0) + 1
        if entry.get("unhandled_block_type"):
            unhandled_entries.append(anchor)
        if bt not in ALL_KNOWN_TYPES:
            uncovered_types.add(bt)

    ok = not unhandled_entries and not uncovered_types
    return {
        "ok": ok,
        "block_type_counts": seen_types,
        "unhandled_block_type_entries": unhandled_entries,
        "block_types_not_in_any_policy_set": sorted(uncovered_types),
        "known_policy_sets": {
            "CITABLE_LEAF_TYPES": sorted(CITABLE_LEAF_TYPES),
            "PROVENANCE_ONLY_TYPES": sorted(PROVENANCE_ONLY_TYPES),
            "PROVENANCE_ONLY_SILENT_TYPES": sorted(PROVENANCE_ONLY_SILENT_TYPES),
            "STRUCTURAL_CONTAINER_TYPES": sorted(STRUCTURAL_CONTAINER_TYPES),
        },
    }


def run_qc(paper_dir: Path) -> QCReport:
    """Run all three checks for one paper directory containing content.md and
    provenance.json (already-produced adapter output, never raw Marker JSON).
    """
    paper_id = paper_dir.name
    content_md_path = paper_dir / "content.md"
    provenance_path = paper_dir / "provenance.json"

    if not content_md_path.exists() or not provenance_path.exists():
        return QCReport(
            paper_id=paper_id,
            passed=False,
            round_trip={"ok": False, "error": "content.md or provenance.json missing"},
            tag_leak={"ok": False, "error": "skipped -- missing input"},
            block_type_coverage={"ok": False, "error": "skipped -- missing input"},
        )

    content_md = content_md_path.read_text()
    provenance = json.loads(provenance_path.read_text())

    rt = round_trip_check(content_md, provenance)
    tl = tag_leak_sweep(content_md)
    bc = block_type_coverage_check(provenance)

    repaired_count = sum(1 for v in provenance.values() if v.get("repaired"))

    passed = rt["ok"] and tl["ok"] and bc["ok"]

    return QCReport(
        paper_id=paper_id,
        passed=passed,
        round_trip=rt,
        tag_leak=tl,
        block_type_coverage=bc,
        repaired_entry_count=repaired_count,
    )


def write_qc_report(paper_dir: Path) -> dict[str, Any]:
    report = run_qc(paper_dir)
    out = report.to_dict()
    (paper_dir / "qc_report.json").write_text(json.dumps(out, indent=2))
    return out
