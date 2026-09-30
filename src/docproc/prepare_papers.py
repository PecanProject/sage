"""
prepare_papers.py -- bridges Marker's own output directory to the existing,
validated Sprint 1 pipeline. Orchestration only: every content-producing step
below is a direct, unmodified call into marker_adapter.py / run_qc_batch.py
(qc_gate.py transitively). Nothing about the adapter's tree-walk, cleanup
heuristics, or the QC gate's three checks is touched or reimplemented here.

--------------------------------------------------------------------------
Where this fits: you run Marker yourself, first, exactly as already decided
(Playbook Section 4, point 1 -- JSON output mode, --use_llm):

    marker data/marker_file --output_dir data/marker_output \\
        --output_format json --workers 1

Then this script:

    python3 docproc/prepare_papers.py data/marker_output --output_dir papers

Marker never runs inside this script. This script only ever reads a Marker
output directory that already exists on disk -- consistent with the rest of
this sprint's principle of never re-deriving something two separate ways.
--------------------------------------------------------------------------

Marker's own on-disk convention (verified against marker-pdf==2.0.0's actual
source -- marker/output.py:save_output, marker/config/parser.py:
get_output_folder/get_base_filename -- not assumed from memory or docs):
for an input PDF named `<paper_id>.pdf`, Marker creates exactly one
subdirectory per PDF, named after the PDF's stem, and writes:

    <marker_output_dir>/<paper_id>/<paper_id>.json        <- the block tree
    <marker_output_dir>/<paper_id>/<paper_id>_meta.json    <- Marker's own
                                                               run metadata,
                                                               NOT the block
                                                               tree -- must
                                                               not be
                                                               confused with
                                                               the main file
    <marker_output_dir>/<paper_id>/<extracted image files>

So `<paper_id>` -- the Marker subdirectory name -- becomes this project's
paper_id directly; no separate id-assignment scheme is needed here either,
same spirit as the adapter's own block_id decision in Section 4.

--------------------------------------------------------------------------
Determinism:
  - Papers are discovered and processed in sorted (paper_id) order, always.
  - No wall-clock timestamps, random ids, or non-stable iteration order are
    introduced by this script. content.md/provenance.json/_walk_stats.json
    ordering is exactly whatever marker_adapter.process_paper() already
    produces (a single deterministic tree-walk over the same input JSON);
    qc_report.json ordering is exactly whatever run_qc_batch.run_batch()
    already produces. Given the same Marker output directory, re-running
    this script produces byte-identical output every time.
  - A paper whose JSON can't be unambiguously resolved is never silently
    skipped (P1: never silently drop information). Its output directory is
    still created (empty), so run_qc_batch's existing "content.md or
    provenance.json missing" handling in qc_gate.py surfaces it in the
    aggregate report as a real, visible failure rather than an absence
    nobody notices.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

from marker_adapter import process_paper
from run_qc_batch import run_batch


def _resolve_marker_json(paper_marker_dir: Path) -> tuple[Optional[Path], Optional[str]]:
    """Find the one real block-tree JSON file inside a single Marker output
    subdirectory. Returns (path, None) on a clean resolution, or (None, error)
    if it can't be resolved unambiguously.

    Primary rule (verified against marker-pdf==2.0.0 source, see module
    docstring): the file is named `<paper_id>.json`, matching the
    subdirectory's own name exactly.

    Fallback (defensive, not the expected path): if that exact name isn't
    present -- e.g. a different Marker version changes the convention -- look
    for exactly one other `*.json` file that isn't a `*_meta.json` sidecar.
    Zero or multiple such candidates is reported as an error rather than
    guessed at, consistent with this pipeline's "never silently drop, never
    silently guess" stance (P1).
    """
    paper_id = paper_marker_dir.name
    expected = paper_marker_dir / f"{paper_id}.json"
    if expected.exists():
        return expected, None

    candidates = [
        p for p in paper_marker_dir.glob("*.json")
        if not p.name.endswith("_meta.json")
    ]
    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) == 0:
        return None, f"no block-tree JSON found in {paper_marker_dir} (expected {expected.name})"
    return None, (
        f"ambiguous: {len(candidates)} candidate JSON files in {paper_marker_dir} "
        f"and none matches the expected name {expected.name} -- "
        f"{sorted(p.name for p in candidates)}"
    )


def prepare_papers(marker_output_dir: Path, output_dir: Path) -> dict[str, Any]:
    """For every <paper_id>/ subdirectory under marker_output_dir, resolve its
    Marker JSON and run it through the existing, unmodified
    marker_adapter.process_paper(). Then hand the whole output_dir to the
    existing, unmodified run_qc_batch.run_batch() for the QC pass -- this
    script never reimplements the QC checks themselves.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    paper_marker_dirs = sorted(
        p for p in marker_output_dir.iterdir() if p.is_dir()
    )

    adapter_results: list[dict[str, Any]] = []

    for paper_marker_dir in paper_marker_dirs:
        paper_id = paper_marker_dir.name
        paper_out_dir = output_dir / paper_id
        # Always create the output directory, even on resolution failure --
        # see module docstring: a missing/ambiguous JSON must surface as a
        # visible QC-gate failure downstream, not vanish silently.
        paper_out_dir.mkdir(parents=True, exist_ok=True)

        json_path, error = _resolve_marker_json(paper_marker_dir)
        if error:
            adapter_results.append({
                "paper_id": paper_id,
                "marker_json_resolved": False,
                "error": error,
            })
            continue

        try:
            result = process_paper(str(json_path), str(paper_out_dir))
        except Exception as exc:  # noqa: BLE001 -- deliberately broad: one
            # paper's malformed/unexpected JSON must not abort the batch for
            # every other paper; report it and move on (P1 applies to our own
            # pipeline's failures too, not just extracted field values).
            adapter_results.append({
                "paper_id": paper_id,
                "marker_json_resolved": True,
                "marker_json_path": str(json_path),
                "adapter_error": f"{type(exc).__name__}: {exc}",
            })
            continue

        adapter_results.append({
            "paper_id": paper_id,
            "marker_json_resolved": True,
            "marker_json_path": str(json_path),
            "adapter_stats": result.stats,
        })

    # Delegate entirely to the existing, unmodified QC batch runner -- this
    # is the same call that would already be made by hand per the previous
    # session's handoff; nothing about it changes here.
    qc_aggregate = run_batch(output_dir)

    adapter_failures = [
        r for r in adapter_results
        if not r.get("marker_json_resolved") or "adapter_error" in r
    ]

    return {
        "marker_output_dir": str(marker_output_dir),
        "output_dir": str(output_dir),
        "papers_found_in_marker_output": len(paper_marker_dirs),
        "adapter_failures": adapter_failures,
        "adapter_results": adapter_results,
        "qc": qc_aggregate,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Bridge Marker's own output directory to the existing Sprint 1 "
            "pipeline (marker_adapter.py + qc_gate.py via run_qc_batch.py). "
            "Run Marker itself first, separately -- this script does not "
            "invoke Marker."
        )
    )
    parser.add_argument(
        "marker_output_dir",
        type=Path,
        help=(
            "Directory Marker itself wrote to, e.g. the --output_dir given "
            "to `marker <input_dir> --output_dir <this> --output_format "
            "json --workers 1`. Must contain one subdirectory per paper, "
            "each holding that paper's <paper_id>.json block tree."
        ),
    )
    parser.add_argument(
        "--output_dir",
        type=Path,
        default=Path("papers"),
        help=(
            "Where to write this pipeline's per-paper output "
            "(content.md, provenance.json, _walk_stats.json, "
            "qc_report.json), one subdirectory per paper_id. Default: "
            "./papers"
        ),
    )
    args = parser.parse_args()

    summary = prepare_papers(args.marker_output_dir, args.output_dir)
    print(json.dumps(summary, indent=2))

    if summary["adapter_failures"] or summary["qc"]["papers_failed"] > 0:
        sys.exit(1)