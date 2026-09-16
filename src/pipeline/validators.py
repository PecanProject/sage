from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, Optional

from pipeline.ir_schema import (
    IRDataset,
    Management,
    ProvenanceLabel,
    Study,
    Treatment,
)


@dataclass
class ValidationIssue:
    severity: str  # "error" | "warning"
    code: str
    message: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
        }


def _effective_study_scope(treatment: Treatment) -> tuple[str, str]:
    sid = treatment.study_id
    if sid.provenance_label != ProvenanceLabel.UNRESOLVED and sid.value:
        return ("study", sid.value)
    return ("citation_fallback", treatment.citation_id)


def check_global_id_uniqueness(ds: IRDataset) -> list[ValidationIssue]:
    """Table 19 #1: every id unique within its own entity-type list."""
    issues: list[ValidationIssue] = []
    entity_lists = {
        "Citation": ds.citations,
        "Study": ds.studies,
        "Site": ds.sites,
        "Species": ds.species,
        "Crop": ds.crops,  # NEW this sprint
        "Method": ds.methods,
        "Treatment": ds.treatments,
        "TreatmentPair": ds.treatment_pairs,  # NEW this sprint
        "Variable": ds.variables,  # NEW this sprint
        "Management": ds.managements,
        "Observation": ds.observations,
        "Coverage": ds.coverages,  # NEW this sprint
    }
    for entity_type, items in entity_lists.items():
        seen: dict[str, int] = {}
        for item in items:
            seen[item.id] = seen.get(item.id, 0) + 1
        for eid, count in seen.items():
            if count > 1:
                issues.append(
                    ValidationIssue(
                        "error",
                        "duplicate_id",
                        f"{entity_type}.id '{eid}' appears {count} times; ids must be unique within the dataset.",
                        entity_type,
                        eid,
                    )
                )
    return issues


def check_site_name_uniqueness(ds: IRDataset) -> list[ValidationIssue]:
    """Table 19 #2."""
    issues: list[ValidationIssue] = []
    seen: dict[str, int] = {}
    for site in ds.sites:
        if site.name.value:
            seen[site.name.value] = seen.get(site.name.value, 0) + 1
    for name, count in seen.items():
        if count > 1:
            issues.append(
                ValidationIssue("error", "duplicate_site_name", f"Site.name '{name}' used {count} times.", "Site")
            )
    return issues


def check_species_scientific_name_uniqueness(ds: IRDataset) -> list[ValidationIssue]:
    """Table 19 #3."""
    issues: list[ValidationIssue] = []
    seen: dict[str, int] = {}
    for sp in ds.species:
        if sp.scientific_name.value:
            seen[sp.scientific_name.value] = seen.get(sp.scientific_name.value, 0) + 1
    for name, count in seen.items():
        if count > 1:
            issues.append(
                ValidationIssue(
                    "error", "duplicate_species_name", f"Species.scientific_name '{name}' used {count} times.", "Species"
                )
            )
    return issues


def check_treatment_name_uniqueness(ds: IRDataset) -> list[ValidationIssue]:
    """Table 19 #4, Option B: unique within study_id; falls back to
    citation_id when study_id is UNRESOLVED."""
    issues: list[ValidationIssue] = []
    seen: dict[tuple[str, str, str], int] = {}
    for t in ds.treatments:
        scope_kind, scope_key = _effective_study_scope(t)
        key = (scope_kind, scope_key, t.name.value or t.id)
        seen[key] = seen.get(key, 0) + 1
    for (scope_kind, scope_key, name), count in seen.items():
        if count > 1:
            scope_desc = f"study_id={scope_key}" if scope_kind == "study" else f"citation_id={scope_key} (study_id UNRESOLVED, fallback scope)"
            issues.append(
                ValidationIssue(
                    "error",
                    "duplicate_treatment_name",
                    f"Treatment.name '{name}' is not unique within {scope_desc}.",
                    "Treatment",
                )
            )
    return issues


def check_control_status_uniqueness(ds: IRDataset) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    seen: dict[tuple, list[str]] = {}
    for t in ds.treatments:
        if t.control_status is None:
            continue
        if t.control_status.provenance_label != ProvenanceLabel.UNRESOLVED and t.control_status.value is True:
            scope_kind, scope_key = _effective_study_scope(t)
            key = (scope_kind, scope_key, t.site_id)
            seen.setdefault(key, []).append(t.id)
    for (scope_kind, scope_key, site_id), tids in seen.items():
        if len(tids) > 1:
            scope_desc = f"study_id={scope_key}" if scope_kind == "study" else f"citation_id={scope_key} (fallback)"
            issues.append(
                ValidationIssue(
                    "error",
                    "multiple_control_treatments",
                    f"Multiple control_status=True Treatments ({', '.join(tids)}) share {scope_desc}, site_id={site_id}.",
                    "Treatment",
                )
            )
    return issues


def check_management_treatment_refs(ds: IRDataset) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    treatment_ids = {t.id for t in ds.treatments}
    for m in ds.managements:
        tids_field = m.treatment_ids
        if tids_field is None:
            continue
        if tids_field.provenance_label == ProvenanceLabel.UNRESOLVED:
            continue
        values = tids_field.value or []
        if not values:
            issues.append(
                ValidationIssue(
                    "error",
                    "empty_management_treatment_ids",
                    f"Management '{m.id}': treatment_ids is resolved but empty.",
                    "Management",
                    m.id,
                )
            )
        for tid in values:
            if tid not in treatment_ids:
                issues.append(
                    ValidationIssue(
                        "error",
                        "dangling_treatment_ref",
                        f"Management '{m.id}': treatment_ids references unknown Treatment '{tid}'.",
                        "Management",
                        m.id,
                    )
                )
    return issues


def check_denormalized_consistency(ds: IRDataset) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    treatments_by_id = {t.id: t for t in ds.treatments}

    for obs in ds.observations:
        t = treatments_by_id.get(obs.treatment_id)
        if t is None:
            issues.append(
                ValidationIssue(
                    "error",
                    "dangling_treatment_ref",
                    f"Observation '{obs.id}': treatment_id '{obs.treatment_id}' does not resolve to any Treatment.",
                    "Observation",
                    obs.id,
                )
            )
            continue
        if obs.site_id != t.site_id:
            issues.append(
                ValidationIssue(
                    "error",
                    "site_id_mismatch",
                    f"Observation '{obs.id}': site_id '{obs.site_id}' does not match its Treatment's site_id '{t.site_id}'.",
                    "Observation",
                    obs.id,
                )
            )
        # citation_id equality deliberately NOT checked (Option B change).

    for m in ds.managements:
        tids_field = m.treatment_ids
        if tids_field is None or tids_field.provenance_label == ProvenanceLabel.UNRESOLVED:
            continue
        study_scopes = set()
        for tid in tids_field.value or []:
            t = treatments_by_id.get(tid)
            if t is None:
                continue  # already flagged by check_management_treatment_refs
            _, scope_key = _effective_study_scope(t)
            study_scopes.add(scope_key)
        if len(study_scopes) > 1:
            issues.append(
                ValidationIssue(
                    "error",
                    "management_spans_multiple_studies",
                    f"Management '{m.id}': treatment_ids span more than one Study/citation scope ({sorted(study_scopes)}).",
                    "Management",
                    m.id,
                )
            )
    return issues


def check_dataset_containment(ds: IRDataset) -> list[ValidationIssue]:
    """Table 19 #8."""
    issues: list[ValidationIssue] = []
    for obs in ds.observations:
        if obs.dataset_id != ds.dataset_id:
            issues.append(
                ValidationIssue(
                    "error",
                    "dataset_containment_violation",
                    f"Observation '{obs.id}': dataset_id '{obs.dataset_id}' != enclosing IRDataset.dataset_id '{ds.dataset_id}'.",
                    "Observation",
                    obs.id,
                )
            )
    return issues


def check_aggregated_over_factors(ds: IRDataset) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for obs in ds.observations:
        scope = obs.reported_effect_scope.value
        f = obs.aggregated_over_factors
        if scope == "treatment_mean":
            if f is None or f.provenance_label != ProvenanceLabel.EXTRACTED or f.value != []:
                issues.append(
                    ValidationIssue(
                        "error",
                        "aggregated_over_factors_shape",
                        f"Observation '{obs.id}': treatment_mean requires aggregated_over_factors=EXTRACTED,[].",
                        "Observation",
                        obs.id,
                    )
                )
        elif scope == "aggregated_mean":
            if f is None:
                issues.append(
                    ValidationIssue(
                        "error",
                        "aggregated_over_factors_shape",
                        f"Observation '{obs.id}': aggregated_mean requires aggregated_over_factors (never absent).",
                        "Observation",
                        obs.id,
                    )
                )
    return issues


def check_treatment_study_ref(ds: IRDataset) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    study_ids = {s.id for s in ds.studies}
    for t in ds.treatments:
        sid = t.study_id
        if sid.provenance_label == ProvenanceLabel.UNRESOLVED:
            issues.append(
                ValidationIssue(
                    "warning",
                    "treatment_study_unresolved",
                    f"Treatment '{t.id}': study_id is UNRESOLVED — falls back to citation_id-scoping until reconciled.",
                    "Treatment",
                    t.id,
                )
            )
            continue
        if sid.value not in study_ids:
            issues.append(
                ValidationIssue(
                    "error",
                    "dangling_study_ref",
                    f"Treatment '{t.id}': study_id '{sid.value}' does not resolve to any Study in this dataset.",
                    "Treatment",
                    t.id,
                )
            )
    return issues


def check_study_citation_refs(ds: IRDataset) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    citation_ids = {c.id for c in ds.citations}
    for study in ds.studies:
        for cid in study.citation_ids:
            if cid not in citation_ids:
                issues.append(
                    ValidationIssue(
                        "error",
                        "dangling_citation_ref",
                        f"Study '{study.id}': citation_ids references unknown Citation '{cid}'.",
                        "Study",
                        study.id,
                    )
                )
    return issues


def check_source_of_record_referential_integrity(ds: IRDataset) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    citation_ids = {c.id for c in ds.citations}

    def _check(entity_type: str, entity_id: str, cid: str) -> None:
        if cid not in citation_ids:
            issues.append(
                ValidationIssue(
                    "error",
                    "dangling_citation_ref",
                    f"{entity_type} '{entity_id}': citation_id '{cid}' does not resolve to any Citation.",
                    entity_type,
                    entity_id,
                )
            )

    for m in ds.methods:
        _check("Method", m.id, m.citation_id)
    for t in ds.treatments:
        _check("Treatment", t.id, t.citation_id)
    for mg in ds.managements:
        _check("Management", mg.id, mg.citation_id)
    for obs in ds.observations:
        _check("Observation", obs.id, obs.citation_id)
    for tp in ds.treatment_pairs:  # NEW this sprint
        _check("TreatmentPair", tp.id, tp.citation_id)
    for crop in ds.crops:  # NEW this sprint
        _check("Crop", crop.id, crop.citation_id)
    for cov in ds.coverages:  # NEW this sprint
        _check("Coverage", cov.id, cov.citation_id)
    return issues


def check_variable_name_uniqueness(ds: IRDataset) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    seen: dict[str, int] = {}
    for var in ds.variables:
        if var.name.value:
            seen[var.name.value] = seen.get(var.name.value, 0) + 1
    for name, count in seen.items():
        if count > 1:
            issues.append(
                ValidationIssue("error", "duplicate_variable_name", f"Variable.name '{name}' used {count} times.", "Variable")
            )
    return issues


def check_crop_species_ref(ds: IRDataset) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    species_ids = {s.id for s in ds.species}
    for crop in ds.crops:
        if crop.species_id not in species_ids:
            issues.append(
                ValidationIssue(
                    "error", "dangling_species_ref",
                    f"Crop '{crop.id}': species_id '{crop.species_id}' does not resolve to any Species.",
                    "Crop", crop.id,
                )
            )
    return issues


def check_treatment_pair_references_resolve(ds: IRDataset) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    treatments_by_id = {t.id: t for t in ds.treatments}
    for pair in ds.treatment_pairs:
        t1 = treatments_by_id.get(pair.treatment_id_1)
        t2 = treatments_by_id.get(pair.treatment_id_2)
        if t1 is None:
            issues.append(
                ValidationIssue(
                    "error", "dangling_treatment_ref",
                    f"TreatmentPair '{pair.id}': treatment_id_1 '{pair.treatment_id_1}' does not resolve to any Treatment.",
                    "TreatmentPair", pair.id,
                )
            )
        if t2 is None:
            issues.append(
                ValidationIssue(
                    "error", "dangling_treatment_ref",
                    f"TreatmentPair '{pair.id}': treatment_id_2 '{pair.treatment_id_2}' does not resolve to any Treatment.",
                    "TreatmentPair", pair.id,
                )
            )
        for label, t in (("treatment_id_1", t1), ("treatment_id_2", t2)):
            if t is not None and pair.citation_id != t.citation_id:
                issues.append(
                    ValidationIssue(
                        "error", "treatment_pair_citation_scope_mismatch",
                        f"TreatmentPair '{pair.id}': citation_id '{pair.citation_id}' does not match "
                        f"{label}'s Treatment citation_id '{t.citation_id}'.",
                        "TreatmentPair", pair.id,
                    )
                )
    return issues


def check_variable_ref_integrity(ds: IRDataset) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    variable_ids = {v.id for v in ds.variables}
    for obs in ds.observations:
        if obs.variable_id is not None and obs.variable_id not in variable_ids:
            issues.append(
                ValidationIssue(
                    "error", "dangling_variable_ref",
                    f"Observation '{obs.id}': variable_id '{obs.variable_id}' does not resolve to any Variable.",
                    "Observation", obs.id,
                )
            )
    for cov in ds.coverages:
        if cov.variable_id is not None and cov.variable_id not in variable_ids:
            issues.append(
                ValidationIssue(
                    "error", "dangling_variable_ref",
                    f"Coverage '{cov.id}': variable_id '{cov.variable_id}' does not resolve to any Variable.",
                    "Coverage", cov.id,
                )
            )
    return issues


def check_crop_ref_integrity(ds: IRDataset) -> list[ValidationIssue]:
    """NEW this sprint: Observation.crop_id, when present, must resolve to
    an existing Crop."""
    issues: list[ValidationIssue] = []
    crop_ids = {c.id for c in ds.crops}
    for obs in ds.observations:
        if obs.crop_id is not None and obs.crop_id not in crop_ids:
            issues.append(
                ValidationIssue(
                    "error", "dangling_crop_ref",
                    f"Observation '{obs.id}': crop_id '{obs.crop_id}' does not resolve to any Crop.",
                    "Observation", obs.id,
                )
            )
    return issues


def check_coverage_site_ref(ds: IRDataset) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    site_ids = {s.id for s in ds.sites}
    for cov in ds.coverages:
        if cov.site_id not in site_ids:
            issues.append(
                ValidationIssue(
                    "error", "dangling_site_ref",
                    f"Coverage '{cov.id}': site_id '{cov.site_id}' does not resolve to any Site.",
                    "Coverage", cov.id,
                )
            )
    return issues


def check_treatment_control_status_optional(ds: IRDataset) -> list[ValidationIssue]:
    return []


def check_treatment_name_not_quantity(ds: IRDataset) -> list[ValidationIssue]:
    import re

    issues: list[ValidationIssue] = []
    quantity_pattern = re.compile(r"\d+(\.\d+)?\s*(kg|g|mg|t|ha|kgha|mm|cm|m|%|ppm)", re.IGNORECASE)
    for t in ds.treatments:
        name_val = t.name.value or ""
        if quantity_pattern.search(name_val.replace("_", "")):
            issues.append(
                ValidationIssue(
                    "warning",
                    "treatment_name_may_encode_quantity",
                    f"Treatment '{t.id}': name '{name_val}' looks like it may encode a continuous quantity; "
                    "quantitative rate info belongs in Management, not the Treatment identifier alone.",
                    "Treatment",
                    t.id,
                )
            )
    return issues


_ANCHOR_RE = re.compile(r"⟦(b:\d+)⟧")
_DEFAULT_PAPERS_ROOT = Path(__file__).resolve().parents[1] / "paper"
PAPERS_ROOT = _DEFAULT_PAPERS_ROOT


def _papers_root() -> Path:
    """Resolve the papers root from the current environment at validation time."""
    return Path(os.environ.get("IR_PAPERS_ROOT", str(PAPERS_ROOT)))


def _load_rendered_blocks(paper_id: str) -> dict[str, str]:
    content_path = _papers_root() / paper_id / "content.md"
    if not content_path.is_file():
        raise FileNotFoundError(
            f"content.md not found for paper '{paper_id}': {content_path}"
        )

    content = content_path.read_text(encoding="utf-8")
    matches = list(_ANCHOR_RE.finditer(content))
    blocks: dict[str, str] = {}

    prev_end = 0
    for match in matches:
        anchor = match.group(1)
        blocks[anchor] = content[prev_end:match.start()]
        prev_end = match.end()

    return blocks

_CATEGORICAL_JUDGMENT_FIELDS = {"reported_effect_scope", "event_type", "variable_name"}


def _leaf_field_name(field_path: str) -> str:
    last = field_path.rsplit(".", 1)[-1]
    return re.sub(r"\[\d+\]$", "", last)

_TYPOGRAPHIC_EQUIVALENTS: list[tuple[str, str]] = [
    ("‐", "-"),  # hyphen
    ("‑", "-"),  # non-breaking hyphen (the confirmed Daren case)
    ("‒", "-"),  # figure dash
    ("–", "-"),  # en dash
    ("—", "-"),  # em dash
    ("−", "-"),  # minus sign
    ("$\\times$", "×"),  # LaTeX inline-math multiplication sign
    ("\\times", "×"),  # bare LaTeX command, no $ delimiters
]


def _normalize_typography(text: str) -> str:
    for variant, canonical in _TYPOGRAPHIC_EQUIVALENTS:
        text = text.replace(variant, canonical)
    return text


def _value_supported_by_text(value: Any, text: str, field_name: Optional[str] = None) -> bool:
    if value is None:
        return True

    normalized_text = _normalize_typography(" ".join(text.split()).casefold())

    if isinstance(value, bool) or field_name in _CATEGORICAL_JUDGMENT_FIELDS:
        return bool(normalized_text)

    if isinstance(value, int):
        return re.search(rf"(?<!\d){value}(?!\d)", normalized_text) is not None

    if isinstance(value, float):
        candidates = {str(value)}
        if value.is_integer():
            candidates.add(str(int(value)))
        return any(
            re.search(rf"(?<![\d.]){re.escape(candidate)}(?![\d.])", normalized_text)
            for candidate in candidates
        )

    if isinstance(value, str):
        needle = _normalize_typography(" ".join(value.split()).casefold())
        if not needle:
            return False
        if needle in normalized_text:
            return True
        stripped_needle = re.sub(r"\s+", "", needle)
        stripped_text = re.sub(r"\s+", "", normalized_text)
        return bool(stripped_needle) and stripped_needle in stripped_text

    return True


def _iter_extracted_fields(node: Any, path: str = ""):
    """Yield (field_path, extracted_field_dict) recursively."""
    if isinstance(node, dict):
        if {"value", "provenance_label", "source"}.issubset(node):
            yield path or "<root>", node
            return

        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from _iter_extracted_fields(value, child_path)

    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iter_extracted_fields(value, f"{path}[{index}]")


def validate_provenance(paper_id: str, payload: dict[str, Any]) -> list[ValidationIssue]:
    try:
        blocks = _load_rendered_blocks(paper_id)
    except FileNotFoundError as exc:
        return [
            ValidationIssue(
                "error",
                "provenance_source_missing",
                str(exc),
            )
        ]

    issues: list[ValidationIssue] = []

    for field_path, field in _iter_extracted_fields(payload):
        label = field.get("provenance_label")
        if label not in (ProvenanceLabel.EXTRACTED.value, ProvenanceLabel.INFERRED.value):
            continue

        value = field.get("value")
        if value is None:
            continue

        leaf_name = _leaf_field_name(field_path)
        is_judgment_field = isinstance(value, bool) or leaf_name in _CATEGORICAL_JUDGMENT_FIELDS
        if is_judgment_field and label == ProvenanceLabel.INFERRED.value:
            reason = (field.get("unresolved_reason") or "").strip()
            if not reason:
                issues.append(
                    ValidationIssue(
                        "error",
                        "boolean_inference_basis_missing",
                        f"{field_path}: INFERRED judgment value requires a real, non-empty "
                        f"inference-basis note (unresolved_reason) explaining why the cited "
                        f"evidence supports this value -- a literal text match of the value "
                        f"itself is never required or expected for a boolean or a "
                        f"controlled-vocabulary judgment field like reported_effect_scope.",
                    )
                )

        source = field.get("source") or {}
        locators = source.get("locators") or []

        for locator in locators:
            if not isinstance(locator, dict):
                continue

            anchor = locator.get("block_anchor")
            if not anchor:
                continue

            anchor = anchor.strip("[]")
            block_text = blocks.get(anchor)
            if block_text is None:
                issues.append(
                    ValidationIssue(
                        "error",
                        "provenance_anchor_not_found",
                        f"{field_path}: locator block {anchor} does not exist in content.md for paper '{paper_id}'.",
                    )
                )
                continue

            if not _value_supported_by_text(value, block_text, field_name=leaf_name):
                issues.append(
                    ValidationIssue(
                        "error",
                        "provenance_value_mismatch",
                        f"{field_path}: value={value!r} is not supported by block {anchor}. "
                        f"Provide a locator whose block text contains the cited value.",
                    )
                )

    return issues


ALL_WHOLE_GRAPH_CHECKS = [
    check_global_id_uniqueness,
    check_site_name_uniqueness,
    check_species_scientific_name_uniqueness,
    check_treatment_name_uniqueness,
    check_control_status_uniqueness,
    check_management_treatment_refs,
    check_denormalized_consistency,
    check_dataset_containment,
    check_aggregated_over_factors,
    check_treatment_study_ref,
    check_study_citation_refs,
    check_source_of_record_referential_integrity,
    check_treatment_control_status_optional,
    check_treatment_name_not_quantity,
    check_variable_name_uniqueness,  # NEW this sprint (Variable)
    check_crop_species_ref,  # NEW this sprint (Crop)
    check_treatment_pair_references_resolve,  # NEW this sprint (TreatmentPair)
    check_variable_ref_integrity,  # NEW this sprint (Variable)
    check_crop_ref_integrity,  # NEW this sprint (Crop)
    check_coverage_site_ref,  # NEW this sprint (Coverage)
]


def validate_dataset(ds: IRDataset) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for check in ALL_WHOLE_GRAPH_CHECKS:
        issues.extend(check(ds))
    return issues
