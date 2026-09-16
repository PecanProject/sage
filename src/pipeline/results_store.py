from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_RESULTS_ROOT = Path(os.environ.get("IR_RESULTS_ROOT", "results"))


def _results_root() -> Path:
    return Path(os.environ.get("IR_RESULTS_ROOT", str(DEFAULT_RESULTS_ROOT)))


def paper_dir(paper_id: str) -> Path:
    return _results_root() / paper_id


def list_paper_ids() -> list[str]:
    root = _results_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def rename_paper_dir(old_paper_id: str, new_paper_id: str) -> bool:
    old_dir = paper_dir(old_paper_id)
    if not old_dir.is_dir():
        return False
    old_dir.rename(paper_dir(new_paper_id))
    return True


def result_path(paper_id: str) -> Path:
    return paper_dir(paper_id) / "result.json"


def save_result(paper_id: str, data: Any) -> Path:
    path = result_path(paper_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str, sort_keys=False), encoding="utf-8")
    return path


def load_result(paper_id: str) -> Any:
    return json.loads(result_path(paper_id).read_text(encoding="utf-8"))


def entity_result_path(paper_id: str, entity_type: str) -> Path:
    return paper_dir(paper_id) / f"{entity_type}.json"


def save_entity_result(paper_id: str, entity_type: str, data: Any) -> Path:
    path = entity_result_path(paper_id, entity_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str, sort_keys=False), encoding="utf-8")
    return path


def load_entity_result(paper_id: str, entity_type: str) -> Any:
    return json.loads(entity_result_path(paper_id, entity_type).read_text(encoding="utf-8"))


MULTI_RECORD_ENTITY_TYPES: set[str] = {
    "Variable", "Treatment", "Observation",  # Phase A/B/C
    "Site", "Species", "Method", "Crop", "Management", "Study", "TreatmentPair", "Coverage",
}


def entity_dir(paper_id: str, entity_type: str) -> Path:
    return paper_dir(paper_id) / entity_type


def multi_entity_result_path(paper_id: str, entity_type: str, record_id: str) -> Path:
    return entity_dir(paper_id, entity_type) / f"{record_id}.json"


def save_multi_entity_results(paper_id: str, entity_type: str, records: list[Any]) -> list[Path]:
    directory = entity_dir(paper_id, entity_type)
    if directory.is_dir():
        for existing in directory.glob("*.json"):
            existing.unlink()
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for record in records:
        path = multi_entity_result_path(paper_id, entity_type, record["record_id"])
        path.write_text(json.dumps(record, indent=2, default=str, sort_keys=False), encoding="utf-8")
        paths.append(path)
    return paths


def load_multi_entity_results(paper_id: str, entity_type: str) -> list[Any]:
    directory = entity_dir(paper_id, entity_type)
    if not directory.is_dir():
        return []
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))]


def load_any_entity_results(paper_id: str, entity_type: str) -> list[Any]:
    if entity_type in MULTI_RECORD_ENTITY_TYPES:
        return load_multi_entity_results(paper_id, entity_type)
    try:
        return [load_entity_result(paper_id, entity_type)]
    except FileNotFoundError:
        return []
