"""
curation/cli.py
===============
Command-line interface for curating candidate papers, downloading PDFs,
and validating annotations for Issue #5.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import List, Optional

from curation.downloader import PaperDownloader
from curation.filter import CandidateFilter, FilterCriteria
from curation.schema import CandidatePaper, GoldAnnotation, PaperManifest


def cmd_filter(args: argparse.Namespace) -> int:
    """Filter candidate papers from BETYdata and write manifest."""
    data_dir = Path(args.data_dir) if args.data_dir else None
    criteria = FilterCriteria(
        min_treatments=args.min_treatments,
        min_managements=args.min_managements,
        min_traits=args.min_traits,
        require_doi=not args.no_doi,
        require_control=args.require_control,
        min_year=args.min_year,
        max_year=args.max_year,
    )

    print("Filtering BETYdb papers with criteria:")
    print(f"  - min_treatments: {criteria.min_treatments}")
    print(f"  - min_managements: {criteria.min_managements}")
    print(f"  - min_traits: {criteria.min_traits}")
    print(f"  - require_doi: {criteria.require_doi}")
    print(f"  - year range: {criteria.min_year} - {criteria.max_year}")
    print(f"  - data source: {'local: ' + str(data_dir) if data_dir else 'remote: PecanProject/betydata'}")

    filterer = CandidateFilter(data_dir=data_dir)
    try:
        candidates = filterer.filter_candidates(criteria=criteria)
    except Exception as e:
        print(f"Error loading or filtering data: {e}", file=sys.stderr)
        return 1

    print(f"\nFound {len(candidates)} qualifying candidate papers.")

    if args.limit and args.limit > 0:
        selected = candidates[:args.limit]
        print(f"Selecting top {len(selected)} ranked candidates for manifest.")
    else:
        selected = candidates

    manifest = filterer.build_manifest(
        candidates=selected,
        criteria=criteria,
        description=args.description or "BETYdb candidate papers for extraction benchmarking",
    )

    # Print top candidate summary
    print("\nTop Candidate Papers:")
    print("-" * 90)
    print(f"{'CID':<6} | {'Year':<5} | {'Author':<16} | {'Trt':<4} | {'Mgmt':<5} | {'Traits':<6} | {'DOI'}")
    print("-" * 90)
    for p in selected[:15]:
        auth = (p.author or "Unknown")[:15]
        yr = str(p.year or "N/A")
        print(f"{p.citation_id:<6} | {yr:<5} | {auth:<16} | {p.treatment_count:<4} | {p.management_count:<5} | {p.trait_count:<6} | {p.doi or 'None'}")
    print("-" * 90)

    # Output JSON manifest
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, mode="w", encoding="utf-8") as f:
        json.dump(manifest.model_dump(), f, indent=2)
    print(f"\nManifest saved to: {out_path.resolve()}")

    # Optional CSV summary
    if args.output_csv:
        csv_path = Path(args.output_csv)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(csv_path, mode="w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "citation_id", "doi", "year", "author", "title", "journal",
                "treatment_count", "has_control", "management_count", "trait_count",
                "site_count", "species", "treatments"
            ])
            for p in selected:
                writer.writerow([
                    p.citation_id, p.doi, p.year, p.author, p.title, p.journal,
                    p.treatment_count, p.has_control_treatment, p.management_count, p.trait_count,
                    p.site_count, "; ".join(p.species_list), "; ".join(p.treatments)
                ])
        print(f"CSV summary saved to: {csv_path.resolve()}")

    return 0


def cmd_download(args: argparse.Namespace) -> int:
    """Download PDFs for candidate papers in a manifest."""
    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"Manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    with open(manifest_path, mode="r", encoding="utf-8") as f:
        data = json.load(f)

    manifest = PaperManifest.model_validate(data)
    out_dir = Path(args.output_dir)
    downloader = PaperDownloader(output_dir=out_dir, email=args.email)

    print(f"Downloading PDFs for up to {args.limit or len(manifest.papers)} papers to {out_dir}...")
    success = 0
    count = 0
    for paper in manifest.papers:
        if args.limit and count >= args.limit:
            break
        count += 1
        print(f"[{count}] Citation {paper.citation_id} (DOI: {paper.doi})...", end=" ")
        try:
            path, sha = downloader.download_paper(paper)
            print(f"SUCCESS -> {path.name} (SHA-256: {sha[:8]}...)")
            success += 1
        except Exception as e:
            print(f"FAILED ({e})")

    # Update manifest file with downloaded paths and statuses
    with open(manifest_path, mode="w", encoding="utf-8") as f:
        json.dump(manifest.model_dump(), f, indent=2)

    print(f"\nDone: {success}/{count} papers successfully downloaded.")
    return 0 if success > 0 else 1


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate a gold dataset annotation file."""
    annot_path = Path(args.annotations)
    if not annot_path.exists():
        print(f"Annotation file not found: {annot_path}", file=sys.stderr)
        return 1

    with open(annot_path, mode="r", encoding="utf-8") as f:
        content = f.read().strip()

    valid_records = []
    errors = []

    if content.startswith("["):
        # JSON list
        items = json.loads(content)
        for idx, item in enumerate(items):
            try:
                record = GoldAnnotation.model_validate(item)
                valid_records.append(record)
            except Exception as e:
                errors.append(f"Record {idx} ({item.get('annotation_id', 'unknown')}): {e}")
    else:
        # JSONL format
        for idx, line in enumerate(content.splitlines()):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                record = GoldAnnotation.model_validate(item)
                valid_records.append(record)
            except Exception as e:
                errors.append(f"Line {idx + 1}: {e}")

    print(f"Validation complete: {len(valid_records)} valid annotations, {len(errors)} errors.")
    if errors:
        print("\nErrors encountered:")
        for err in errors[:10]:
            print(f"  - {err}")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m curation.cli",
        description="SAGE Gold Dataset Curation and Benchmarking Utility (Issue #5)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # filter command
    p_filter = subparsers.add_parser("filter", help="Filter candidate papers from BETYdb")
    p_filter.add_argument("--data-dir", type=str, default=None, help="Path to directory containing BETYdata CSVs")
    p_filter.add_argument("--min-treatments", type=int, default=2, help="Minimum treatments (default: 2)")
    p_filter.add_argument("--min-managements", type=int, default=1, help="Minimum managements (default: 1)")
    p_filter.add_argument("--min-traits", type=int, default=1, help="Minimum trait observations (default: 1)")
    p_filter.add_argument("--no-doi", action="store_true", help="Do not require DOI")
    p_filter.add_argument("--require-control", action="store_true", help="Require control treatment")
    p_filter.add_argument("--min-year", type=int, default=1990, help="Minimum year (default: 1990)")
    p_filter.add_argument("--max-year", type=int, default=2026, help="Maximum year (default: 2026)")
    p_filter.add_argument("--limit", type=int, default=25, help="Limit number of candidate papers in manifest (default: 25)")
    p_filter.add_argument("--description", type=str, default=None, help="Manifest description")
    p_filter.add_argument("--output", "-o", type=str, default="gold_candidates_manifest.json", help="Output JSON manifest path")
    p_filter.add_argument("--output-csv", type=str, default=None, help="Optional output CSV summary path")
    p_filter.set_defaults(func=cmd_filter)

    # download command
    p_dl = subparsers.add_parser("download", help="Download PDFs for candidates in a manifest")
    p_dl.add_argument("--manifest", "-m", type=str, required=True, help="Path to candidate manifest JSON")
    p_dl.add_argument("--output-dir", "-o", type=str, default="src/docproc/paper", help="Destination folder for PDFs")
    p_dl.add_argument("--limit", "-n", type=int, default=5, help="Maximum number of papers to download (default: 5)")
    p_dl.add_argument("--email", type=str, default="sage-curator@pecanproject.org", help="Polite contact email for Open Access APIs")
    p_dl.set_defaults(func=cmd_download)

    # validate command
    p_val = subparsers.add_parser("validate", help="Validate a gold dataset annotation file")
    p_val.add_argument("--annotations", "-a", type=str, required=True, help="Path to annotations JSON or JSONL")
    p_val.set_defaults(func=cmd_validate)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
