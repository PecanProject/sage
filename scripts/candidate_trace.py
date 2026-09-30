#!/usr/bin/env python3
"""Candidate decision-trace report.

Answers "why did the agent do that" for table-enumeration entity types
(Treatment, Observation) WITHOUT any new LLM calls: it replays the real,
already-committed pipeline.orchestrator table-classification artifacts a run
already produced on disk (run_store's cached table_classification / *__
enumeration attempts, and results/<paper>/<Entity>/ ready records), using the
SAME match_values construction and the SAME _match_row_group_to_pool function
the live pipeline used -- nothing here re-derives a decision the pipeline
didn't already make, it only makes the decision visible.

For every candidate (table-sourced AND free-form-sourced) it shows:
  - where it came from (which table anchor / row / value-column, or which
    free-form enumeration call)
  - the exact match_values dict used for linking
  - for each dependency field, the FULL score breakdown against the real
    pool (not just the winning slug or None) -- this is what a bare
    pass/fail linking result can never show you: WHY nothing scored high
    enough, or why two entries tied.
  - the candidate's actual terminal status in ir-store (ready / unresolved /
    error / never attempted), with the real conflict_explanation when
    present.

Usage:
    python3 scripts/candidate_trace.py --run-id RUN_ID --paper-id PAPER_ID \
        --entity-type Treatment
    python3 scripts/candidate_trace.py --run-id RUN_ID --paper-id PAPER_ID \
        --entity-type Observation --limit 20
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_ROOT))

from pipeline import orchestrator as orch  # noqa: E402
from pipeline import results_store  # noqa: E402


def _normalize(text: str) -> str:
    return orch._normalize_for_matching(text or "")


def score_breakdown(match_values: dict[str, str], pool: list[dict]) -> list[dict]:
    """Reimplements _match_row_group_to_pool's internal scoring (exact=2,
    substring>=3chars=1, byte-identical rule) but returns EVERY entry's
    score, not just the winner -- the actual "why" a bare match/no-match
    result can never show."""
    normalized_values = {v for v in (_normalize(v) for v in match_values.values() if v) if v}
    rows = []
    for item in pool:
        texts = {_normalize(item.get("name") or ""), _normalize(item["slug"].replace("_", " "))} - {""}
        total = 0
        per_value = {}
        for value in normalized_values:
            if value in texts:
                total += 2
                per_value[value] = "exact"
            elif len(value) >= 3 and any(value in t for t in texts):
                total += 1
                per_value[value] = "substring"
            else:
                per_value[value] = None
        rows.append({"slug": item["slug"], "name": item.get("name"), "score": total, "per_value": per_value})
    rows.sort(key=lambda r: -r["score"])
    return rows


def load_pool(paper_id: str, prereq_type: str) -> list[dict]:
    """Ready records of prereq_type, as {slug, record_id, name} -- the same
    shape _multi_record_link_pools builds, read from disk instead of a live
    in-memory run dict."""
    # Run isolation: results live under results/<paper>/<run_id>/ and
    # results/<paper>/LATEST names the last completed run (legacy flat
    # layout still resolves) -- always go through results_store rather than
    # building the path here.
    os.environ.setdefault("IR_RESULTS_ROOT", str(SRC_ROOT / "results"))
    results_dir = results_store.entity_dir(paper_id, prereq_type)
    items = []
    if not results_dir.is_dir():
        return items
    for f in sorted(results_dir.glob("*.json")):
        d = json.loads(f.read_text())
        if d.get("status") != "ready":
            continue
        record_id = d["record_id"]
        payload = d.get("payload") or {}
        name_field = payload.get("name")
        name = name_field.get("value") if isinstance(name_field, dict) else None
        items.append({
            "slug": orch._candidate_slug_from_record_id(paper_id, prereq_type, record_id),
            "record_id": record_id,
            "name": name,
        })
    return items


def build_link_pools(paper_id: str, entity_type: str) -> dict[str, list[dict]]:
    pools = {}
    for field, prereq_type, _required in orch.ENTITY_DEPENDENCIES.get(entity_type, []):
        if prereq_type not in results_store.MULTI_RECORD_ENTITY_TYPES:
            continue
        pool = load_pool(paper_id, prereq_type)
        if len(pool) > 1:
            pools[field] = pool
    return pools


def load_ir_store_status(paper_id: str) -> dict[str, dict]:
    """Last write per record_id across ir-store's full history for this
    paper (append-only log across every run ever done)."""
    path = SRC_ROOT / "ir-store" / f"{paper_id}.jsonl"
    by_record: dict[str, dict] = {}
    if not path.is_file():
        return by_record
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        by_record[d.get("record_id")] = d
    return by_record


def load_classifications(run_id: str) -> dict[str, orch.TableClassification]:
    """Same cache _table_classification_to_candidates/run_table_classification_
    pass reads -- every table_classification__* artifact already on disk for
    this run_id, applicable ones only."""
    base = SRC_ROOT / "runs" / run_id / "records"
    out: dict[str, orch.TableClassification] = {}
    if not base.is_dir():
        return out
    for d in sorted(base.glob("table_classification__*")):
        final = d / "final.json"
        if not final.is_file():
            continue
        data = json.loads(final.read_text())
        if data.get("status") != "success":
            continue
        cls = orch.TableClassification.model_validate(data["classification"])
        if cls.applicable:
            out[d.name.replace("table_classification__", "")] = cls
    return out


def load_freeform_candidates(run_id: str, entity_type: str) -> list[dict]:
    """run_enumeration's SUCCESS path never calls run_store.save_final (only
    its failure path does) -- a successful enumeration's only artifact is
    its last attemptN.json under <record_key>/enumeration/. Fall back to
    final.json (failure case, or older convention) if no attempts exist."""
    record_dir = SRC_ROOT / "runs" / run_id / "records" / f"{entity_type}__enumeration"
    attempt_dir = record_dir / "enumeration"
    if attempt_dir.is_dir():
        attempts = sorted(attempt_dir.glob("attempt*.json"), key=lambda p: int(p.stem.replace("attempt", "")))
        if attempts:
            d = json.loads(attempts[-1].read_text())
            if not d.get("validation_errors"):
                pj = d.get("parsed_json") or {}
                return pj.get("candidates") or []
    final = record_dir / "final.json"
    if final.is_file():
        d = json.loads(final.read_text())
        return d.get("candidates") or (d.get("parsed_json") or {}).get("candidates") or []
    return []


def fmt_score_row(row: dict) -> str:
    hits = ", ".join(f"{v!r}->{how}" for v, how in row["per_value"].items() if how)
    return f"      [{row['score']}] {row['slug']!r} (name={row['name']!r})" + (f"  matched on: {hits}" if hits else "")


def print_field_scores(match_values: dict, link_pools: dict, linked_candidates: dict):
    for field, pool in link_pools.items():
        rows = score_breakdown(match_values, pool)
        winner = linked_candidates.get(field)
        print(f"    {field}: real linking result = {winner!r}   (pool size {len(pool)}, top 3 scores:)")
        for r in rows[:3]:
            print(fmt_score_row(r))


def print_terminal_status(record_id: str, ir_store: dict):
    entry = ir_store.get(record_id)
    if not entry:
        print(f"    final status: NEVER ATTEMPTED (no ir-store entry for {record_id!r})")
        return
    line = entry["status"]
    if entry["status"] != "ready":
        payload = entry.get("payload") or {}
        ce = payload.get("conflict_explanation") or payload.get("reason")
        if ce:
            line += f" -- {ce[:220]}"
    print(f"    final status: {line}")


def observation_candidates_with_match_values(classifications, link_pools):
    """Reimplements _table_classification_to_candidates's loop, but yields
    (candidate_id, anchors, description, known_value, match_values,
    linked_candidates) so match_values survives for display -- the real
    orchestrator function discards it after linking."""
    out = []
    for anchor_key, cls in classifications.items():
        value_columns_by_id = {c.value_column_id: c for c in cls.value_columns}
        for row in cls.row_groups:
            for value_column_id, cell_text in (row.cells or {}).items():
                if not cell_text or not cell_text.strip():
                    continue
                vc = value_columns_by_id.get(value_column_id)
                if vc is None:
                    continue
                candidate_id = orch._sanitize_candidate_id(f"{value_column_id}_{row.row_group_id}")
                factor_desc = ", ".join(f"{k}={v}" for k, v in (row.factor_values or {}).items())
                label = vc.variable_name_hint or vc.variable or "value"
                description = f"{label} for {factor_desc}" if factor_desc else label
                match_values = dict(row.factor_values or {})
                if vc.site_hint:
                    match_values.setdefault("Site", vc.site_hint)
                if vc.method_hint:
                    match_values.setdefault("Method", vc.method_hint)
                linked = {}
                for field, pool in link_pools.items():
                    m = orch._match_row_group_to_pool(match_values, pool)
                    if m:
                        linked[field] = m
                out.append({
                    "candidate_id": candidate_id, "anchors": [row.source_table_anchor],
                    "description": description, "known_value": cell_text.strip(),
                    "match_values": match_values, "linked_candidates": linked,
                    "source_table": anchor_key,
                })
    return out


def treatment_candidates_with_match_values(classifications, link_pools):
    """Same reimplementation for _table_classifications_to_treatment_
    candidates, keeping the combo dict for display and deduping the same
    way (by sorted combo items)."""
    seen: set[tuple] = set()
    out = []
    for anchor_key, cls in classifications.items():
        value_columns_by_id = {c.value_column_id: c for c in cls.value_columns}
        for row in cls.row_groups:
            for value_column_id, cell_text in (row.cells or {}).items():
                if not cell_text or not cell_text.strip():
                    continue
                vc = value_columns_by_id.get(value_column_id)
                if vc is None:
                    continue
                combo = dict(row.factor_values or {})
                if vc.site_hint:
                    combo.setdefault("Site", vc.site_hint)
                if not combo:
                    continue
                key = tuple(sorted(combo.items()))
                if key in seen:
                    continue
                seen.add(key)
                candidate_id = orch._sanitize_candidate_id("_".join(str(v) for _, v in sorted(combo.items())))
                description = "Experimental condition: " + ", ".join(f"{k}={v}" for k, v in sorted(combo.items()))
                linked = {}
                for field, pool in link_pools.items():
                    m = orch._match_row_group_to_pool(combo, pool)
                    if m:
                        linked[field] = m
                out.append({
                    "candidate_id": candidate_id, "anchors": [row.source_table_anchor],
                    "description": description, "known_value": None,
                    "match_values": combo, "linked_candidates": linked,
                    "source_table": anchor_key,
                })
    return out


def report(run_id: str, paper_id: str, entity_type: str, limit: int | None) -> None:
    link_pools = build_link_pools(paper_id, entity_type)
    print(f"=== Candidate trace: {entity_type} / {paper_id} / run {run_id} ===")
    print(f"Link pools built: {[(f, len(p)) for f, p in link_pools.items()]}")
    print()

    ir_store = load_ir_store_status(paper_id)
    classifications = load_classifications(run_id)
    print(f"Applicable table classifications loaded: {sorted(classifications.keys())}")
    print()

    if entity_type == "Treatment":
        table_candidates = treatment_candidates_with_match_values(classifications, link_pools)
    else:
        table_candidates = observation_candidates_with_match_values(classifications, link_pools)

    covered_anchors = {c["anchors"][0].strip("[]") for c in table_candidates}

    print(f"--- Table-sourced candidates: {len(table_candidates)} ---")
    for c in table_candidates[: limit or len(table_candidates)]:
        record_id = f"{paper_id}_{entity_type.lower()}_{c['candidate_id']}"
        print(f"* [table:{c['source_table']}] {c['candidate_id']!r}  anchors={c['anchors']}")
        print(f"    description: {c['description']!r}" + (f"  known_value={c['known_value']!r}" if c["known_value"] else ""))
        print(f"    match_values used for linking: {c['match_values']}")
        print_field_scores(c["match_values"], link_pools, c["linked_candidates"])
        print_terminal_status(record_id, ir_store)
    if limit and len(table_candidates) > limit:
        print(f"  ... ({len(table_candidates) - limit} more table candidates omitted, raise --limit to see more)")
    print()

    ff = load_freeform_candidates(run_id, entity_type)
    print(f"--- Free-form-sourced candidates (LLM enumeration pass): {len(ff)} ---")
    for raw in ff[: limit or len(ff)]:
        cid = raw.get("candidate_id")
        anchors = raw.get("anchors", [])
        record_id = f"{paper_id}_{entity_type.lower()}_{cid}"
        overlaps_table = any(a.strip("[]") in covered_anchors for a in anchors)
        print(f"* [free-form] {cid!r}  anchors={anchors}"
              + ("  <-- ANCHORED IN A COVERED TABLE (likely redundant with a table candidate)" if overlaps_table else "  <-- grounded outside any covered table (e.g. prose) -- possible coarse duplicate"))
        print(f"    description: {raw.get('description')!r}")
        print(f"    linked_candidates in enumeration: {raw.get('linked_candidates')}")
        print_terminal_status(record_id, ir_store)
    if limit and len(ff) > limit:
        print(f"  ... ({len(ff) - limit} more free-form candidates omitted)")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--paper-id", required=True)
    parser.add_argument("--entity-type", required=True, choices=["Treatment", "Observation"])
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    report(args.run_id, args.paper_id, args.entity_type, args.limit)


if __name__ == "__main__":
    main()
