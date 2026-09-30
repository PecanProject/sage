"""Ablation study: how the link policy changes what survives an upstream failure.

Question: when an upstream record (the Site, a Method, a Treatment) fails, how much of what depends
on it is lost, and does easing the rules ("extract first, link later") add wrong data?

Design
  inputs   the same paper with ONE upstream failure injected, identical for every policy:
             none            -- nothing injected (the base run)
             site_name       -- the Site name returned by conversion is corrupted on every attempt (ASCII letter change
                                no repair can undo; SAGE_INJECT_FAULT=site_name)
             method_failed   -- one ready Method (the gas-exchange method: Vcmax, Jmax, Rd) is marked unresolved
             treatment_failed-- one ready Treatment (PAR 0-0.1, the only Treatment ready in the base cell) is marked unresolved
  policies strict | extract_first | extract_first_withdrawal   (SAGE_LINK_POLICY)
  method   every cell is a CLONE of a base run, then `run-paper --resume-run <clone> --from <entity>`: only the failed
           step and what depends on it are re-run; everything upstream is byte-identical across cells.
  truth    Philippe-2007-Six Tables 1-3, parsed from content.md (the source text), never from extractor output.

Usage
  python scripts/ablation.py run   --plan plan.json      # runs every cell sequentially (hours; background it)
  python scripts/ablation.py score --plan plan.json --out results.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
RUNS = SRC / "runs"
RESULTS = SRC / "results"
PAPERS = SRC / "paper"
LOGS = Path(__file__).resolve().parent / "overnight_logs" / "ablation"


# --------------------------------------------------------------------------- #
# Run plumbing
# --------------------------------------------------------------------------- #

def clone_run(paper: str, source: str, clone: str) -> None:
    """A full copy of a run (records + results) under a new run_id; the source is never touched."""
    if (RUNS / clone).exists():
        raise SystemExit(f"{clone} already exists")
    shutil.copytree(RUNS / source, RUNS / clone, ignore=shutil.ignore_patterns("superseded"))
    manifest_path = RUNS / clone / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(run_id=clone, cloned_from=source)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    shutil.copytree(RESULTS / paper / source, RESULTS / paper / clone)
    marker = RESULTS / paper / clone / "_run.json"
    if marker.is_file():
        data = json.loads(marker.read_text())
        data["run_id"] = clone
        marker.write_text(json.dumps(data, indent=2))


def mark_failed(paper: str, run: str, entity: str, record_id: str) -> None:
    """Inject an upstream failure: the record becomes `unresolved` (its payload kept, as a record that failed
    readiness or validation keeps it) in both the run's final.json and its results file."""
    final = RUNS / run / "records" / f"{entity}__{record_id}" / "final.json"
    detail = json.loads(final.read_text())
    detail.update(status="unresolved", injected_failure=True,
                  last_errors=[{"field": None, "message": "ablation: injected upstream failure"}])
    final.write_text(json.dumps(detail, indent=2))
    result = RESULTS / paper / run / entity / f"{record_id}.json"
    if result.is_file():
        data = json.loads(result.read_text())
        data.update(status="unresolved", reason=[{"field": None, "message": "ablation: injected upstream failure"}])
        result.write_text(json.dumps(data, indent=2))


def resolve_record(paper: str, run: str, entity: str, pattern: str) -> str:
    """The record id itself, or the one READY record of `entity` whose id matches the regex `pattern`."""
    folder = RESULTS / paper / run / entity
    if (folder / f"{pattern}.json").is_file():
        return pattern
    ready = [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]
    hits = [r["record_id"] for r in ready if r.get("status") == "ready" and re.search(pattern, r["record_id"])]
    if len(hits) != 1:
        raise SystemExit(f"{entity} pattern {pattern!r} matches {hits} ready records in {run}; need exactly one")
    return hits[0]


def resume(paper: str, run: str, entity: str, policy: str, fault: str | None) -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "SAGE_LINK_POLICY": policy}
    env.pop("SAGE_INJECT_FAULT", None)
    if fault:
        env["SAGE_INJECT_FAULT"] = fault
    latest = (RESULTS / paper / "LATEST").read_text() if (RESULTS / paper / "LATEST").is_file() else None
    with open(LOGS / f"{run}.log", "w") as log:
        code = subprocess.call(
            [sys.executable, "-m", "pipeline.orchestrator", "run-paper", "--paper-id", paper,
             "--resume-run", run, "--from", entity], cwd=SRC, env=env, stdout=log, stderr=subprocess.STDOUT)
    if latest is not None:                     # an ablation cell never becomes the paper's latest result
        (RESULTS / paper / "LATEST").write_text(latest)
    return code


def run_plan(plan: dict) -> None:
    paper = plan["paper"]
    state_path = LOGS / f"{plan['name']}_state.json"
    LOGS.mkdir(parents=True, exist_ok=True)
    state = json.loads(state_path.read_text()) if state_path.is_file() else {}
    for cell in plan["cells"]:
        cid = cell["id"]
        if state.get(cid, {}).get("status") == "done":
            continue
        source = state[cell["base_cell"]]["run"] if cell.get("base_cell") else cell["base_run"]
        run = f"abl_{plan['name']}_{cid}"
        if not (RUNS / run).exists():
            clone_run(paper, source, run)
            for entity, pattern in cell.get("fail", []):
                mark_failed(paper, run, entity, resolve_record(paper, run, entity, pattern))
        state[cid] = {"run": run, "status": "running", "started": time.time()}
        state_path.write_text(json.dumps(state, indent=2))
        code = resume(paper, run, cell["from"], cell["policy"], cell.get("fault"))
        state[cid].update(status="done" if code in (0, 1) else f"exit {code}", exit=code, finished=time.time())
        state_path.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------- #
# Truth from the paper's own tables
# --------------------------------------------------------------------------- #

PAR_KEYS = {"0-0.1": "0_0_1", "0.1-0.2": "0_1_0_2", "0.2-0.35": "0_2_0_35"}
TABLES = {
    "b:0053": {"columns": {"BSD 1": ["bsd", "basal", "diameter"], "BSD 6": ["bsd", "basal", "diameter"],
                           "INC": ["inc", "increment"], "INC d": ["incd", "inc_d", "inc d", "covariable"]},
               "pooled_columns": {"INC", "INC d"}, "methods": ["diameter", "calliper", "caliper", "stem"]},
    "b:0115": {"columns": {"Sapling total leaf area": ["total", "sapling", "leaf area", "la"],
                           "Leaf number": ["number"], "Leaf area": ["leaf area", "area"],
                           "Leaf inclination": ["inclination", "angle"], "STAR sky": ["star"]},
               "pooled_columns": "all", "methods": ["digitiz", "leaf_surface", "leaf_area", "star", "vegestar"]},
    "b:0193": {"columns": {"Na": ["na", "nitrogen"], "Nm": ["nm", "nitrogen"], "Ma": ["ma", "dry", "matter"],
                           "Vcmax": ["vcmax", "carboxylation"], "Jmax": ["jmax", "electron"],
                           "Rd": ["rd", "respiration"]},
               "pooled_columns": "all", "methods": ["gas_exchange", "photosynth", "nitrogen", "chemical", "dry"]},
}
_VALUE_RE = re.compile(r"[–\-−]?\d+(?:\.\d+)?")


def _num(text: str) -> float:
    return float(text.replace("–", "-").replace("−", "-"))


def table_truth(paper: str) -> list[dict]:
    """Every PAR-class and Year cell of Tables 1-3: (table, column, row kind, PAR key, value)."""
    content = (PAPERS / paper / "content.md").read_text(encoding="utf-8")
    cells = []
    for anchor, spec in TABLES.items():
        end = content.index(f"⟦{anchor}⟧")
        start = content.rindex("⟧", 0, end) + 1
        rows = [r for r in content[start:end].strip().splitlines() if r.startswith("|")]
        header = [h.strip() for h in rows[0].strip("|").split("|")][1:]
        names = sorted(spec["columns"], key=len, reverse=True)          # "INC d" before "INC"
        columns = [next((c for c in names if h.startswith(c)), h) for h in header]
        body = []
        for row in rows[2:]:
            parts = [p.strip() for p in row.strip("|").split("|")]
            label, values = parts[0], parts[1:]
            pars = re.findall(r"(\d(?:\.\d+)?)\s*[-–]\s*(\d?\.\d+)", label.replace("$", ""))
            if len(pars) > 1:                                  # a packed row: "PAR t 0-0.1 PAR t 0.1-0.2 | 6.8 7.2 | ..."
                for k, (a, b) in enumerate(pars):
                    body.append((f"PAR {a}-{b}", [(_VALUE_RE.findall(v) or [None] * len(pars))[k] if v else None for v in values]))
            else:
                body.append((label, [(_VALUE_RE.findall(v) or [None])[0] if v and not v.startswith("<") else None for v in values]))
        for label, values in body:
            clean = label.replace("$", "").replace("_", " ")
            if re.search(r"p\s*value", clean, re.I):
                continue
            year = re.search(r"(19|20)\d\d", clean)
            par = re.search(r"(\d(?:\.\d+)?)\s*[-–]\s*(\d?\.\d+)", clean)
            for column, value in zip(columns, values):
                if value is None:
                    continue
                if year and not par:
                    cells.append({"table": anchor, "column": column, "kind": "year", "par": None, "value": _num(value)})
                elif par:
                    key = PAR_KEYS.get(f"{par.group(1)}-{par.group(2)}")
                    pooled = spec["pooled_columns"] == "all" or column in spec["pooled_columns"]
                    cells.append({"table": anchor, "column": column, "kind": "par", "par": key, "value": _num(value),
                                  "scope": "aggregated_mean" if pooled else "treatment_mean"})
    return cells


# --------------------------------------------------------------------------- #
# Scoring one cell of the matrix
# --------------------------------------------------------------------------- #

def _v(field):
    return field.get("value") if isinstance(field, dict) else field


def _observation_verdict(payload: dict, truth: list[dict]) -> tuple[str, str]:
    """(verdict, why): correct | wrong | unverifiable -- against the table cells sharing this value."""
    value_field = payload.get("value") or {}
    value = (_v(value_field) or {}).get("reported_numeric_value") if isinstance(_v(value_field), dict) else None
    locators = ((value_field.get("source") or {}).get("locators") or []) if isinstance(value_field, dict) else []
    anchor = next((l.get("block_anchor") for l in locators if isinstance(l, dict) and l.get("block_anchor") in TABLES), None)
    if value is None or anchor is None:
        return "unverifiable", "not a Table 1-3 value"
    text = " ".join(str(x).lower() for x in (payload.get("variable_id"), _v(payload.get("variable_name"))) if x)

    def names_column(column: str) -> bool:
        keys = TABLES[anchor]["columns"].get(column, [])
        # the more specific column wins: an INC_d (covariable) record never names plain INC
        more_specific = [c for c in TABLES[anchor]["columns"] if c != column and c.startswith(column)]
        if any(any(k in text for k in TABLES[anchor]["columns"][c]) for c in more_specific):
            return False
        return any(k in text for k in keys)

    same_value = [c for c in truth if c["table"] == anchor and abs(c["value"] - value) < 1e-6]
    matches = [c for c in same_value if names_column(c["column"])] or same_value
    if not matches:
        return "wrong", f"{value} is not a value of {anchor}"
    treatment = str(payload.get("treatment_id") or "")
    scope = _v(payload.get("reported_effect_scope"))
    method = str(payload.get("method_id") or "").lower()
    problems = []
    for cell in matches:
        issues = []
        if cell["kind"] == "year":
            issues.append("a Year-row value bound to a Treatment")
        elif not treatment.endswith(cell["par"]):
            issues.append(f"treatment {treatment.rsplit('_treatment_', 1)[-1]} for PAR {cell['par']}")
        elif scope and scope != cell["scope"]:
            issues.append(f"scope {scope} (table: {cell['scope']})")
        elif cell["scope"] == "aggregated_mean":
            over = _v(payload.get("aggregated_over_factors")) or []
            if over and [str(f).lower() for f in over] != ["year"]:
                issues.append(f"aggregated over {over} (table: Year)")
        if method and not any(k in method for k in TABLES[anchor]["methods"]):
            issues.append(f"method {method.rsplit('_method_', 1)[-1]}")
        if not issues:
            return "correct", ""
        problems.append("; ".join(issues))
    return "wrong", problems[0]


def score_run(paper: str, run: str, truth: list[dict]) -> dict:
    base = RESULTS / paper / run
    manifest = json.loads((RUNS / run / "manifest.json").read_text())

    def records(entity):
        folder = base / entity
        if folder.is_dir():
            return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]
        single = base / f"{entity}.json"
        return [json.loads(single.read_text())] if single.is_file() else []

    observations = records("Observation")
    obs = {"ready": 0, "extracted": 0, "pending": 0, "blocked": 0, "correct_ready": 0, "wrong_ready": 0,
           "unverifiable_ready": 0, "wrong_examples": [], "cells_captured": set()}
    for record in observations:
        payload = record.get("payload") or {}
        if record.get("status") == "blocked":
            obs["blocked"] += 1
            continue
        has_value = isinstance(payload.get("value"), dict) and _v(payload["value"]) is not None
        if has_value:
            obs["extracted"] += 1
            verdict, _why = _observation_verdict(payload, truth)
            if verdict != "wrong":
                value = (_v(payload["value"]) or {}).get("reported_numeric_value")
                obs["cells_captured"].add(value)
        if record.get("pending_links"):
            obs["pending"] += 1
        if record.get("status") == "ready":
            obs["ready"] += 1
            verdict, why = _observation_verdict(payload, truth)
            obs[f"{verdict}_ready"] += 1
            if verdict == "wrong" and len(obs["wrong_examples"]) < 6:
                obs["wrong_examples"].append(f"{record['record_id'].rsplit('_observation_', 1)[-1]}: {why}")
    obs["cells_captured"] = len(obs["cells_captured"])
    treatments = records("Treatment")
    site = records("Site")
    status = manifest.get("status") or {}
    blocked_types = sorted(t for t, s in status.items() if s == "blocked" or (isinstance(s, list) and s and set(s) == {"blocked"}))
    return {
        "run": run, "observations": obs,
        "treatments": {"ready": sum(1 for t in treatments if t.get("status") == "ready"), "total": len(treatments),
                       "pending": sum(1 for t in treatments if t.get("pending_links"))},
        "site": site[0].get("status") if site else None,
        "blocked_entity_types": blocked_types,
        "model_calls": (manifest.get("agent_calls") or {}).get("total"),
        "minutes": round((manifest.get("finished_at", 0) - manifest.get("started_at", 0)) / 60) if manifest.get("finished_at") else None,
        "run_status": manifest.get("run_status"),
    }


def score_plan(plan: dict) -> dict:
    paper = plan["paper"]
    state = json.loads((LOGS / f"{plan['name']}_state.json").read_text())
    truth = table_truth(paper)
    out = {"paper": paper, "truth_cells": {"par_rows": sum(1 for c in truth if c["kind"] == "par"),
                                           "year_rows": sum(1 for c in truth if c["kind"] == "year")}, "cells": []}
    for cell in plan["cells"]:
        entry = state.get(cell["id"])
        if not entry or entry.get("status") != "done":
            out["cells"].append({**cell, "state": (entry or {}).get("status", "not run")})
            continue
        out["cells"].append({**cell, "state": "done", **score_run(paper, entry["run"], truth)})
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "score", "truth"])
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--paper", default="Philippe-2007-Six")
    args = parser.parse_args()
    if args.command == "truth":
        print(json.dumps(table_truth(args.paper), indent=1))
        return
    plan = json.loads(args.plan.read_text())
    if args.command == "run":
        run_plan(plan)
    else:
        result = score_plan(plan)
        text = json.dumps(result, indent=2, default=str)
        (args.out.write_text(text) if args.out else print(text))


if __name__ == "__main__":
    main()
