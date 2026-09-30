"""Whole-graph validators (IR spec Section 9.2 / Table 19, study-scoped): cross-entity uniqueness and referential
integrity. Single-entity invariants live in `ir_schema.py`. Every check returns a list of ValidationIssue and never
raises; `validate_dataset` aggregates them."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, Optional

from pipeline import coordinates
from pipeline.ir_schema import (
    IRDataset,
    ProvenanceLabel,
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
    """(scope_kind, scope_key): study_id when resolved, else citation_id."""
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
        "Crop": ds.crops,
        "Method": ds.methods,
        "Treatment": ds.treatments,
        "TreatmentPair": ds.treatment_pairs,
        "Variable": ds.variables,
        "Management": ds.managements,
        "Observation": ds.observations,
        "Coverage": ds.coverages,
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
    """Table 19 #4: unique within study_id; falls back to
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
    """Table 19 #5: at most one control_status=True Treatment per
    (study_id, site_id); falls back to (citation_id, site_id) when study_id
    is UNRESOLVED."""
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
    """Table 19 #6: when Management.treatment_ids is resolved (not
    UNRESOLVED), every id must resolve to an existing Treatment and the list
    must be non-empty."""
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
    """Table 19 #7:
      - Observation.site_id must match the site_id of the referenced
        Treatment.
      - citation_id equality is not required (Observation.citation_id is
        its own source-of-record pointer).
      - Observation's effective study_id (via its Treatment) must equal that
        Treatment's study_id -- trivially true by construction since it's
        read through the same Treatment, so this collapses to: the
        Treatment referenced must actually exist (checked elsewhere) and,
        if UNRESOLVED, is flagged as a soft note rather than an error here.
      - Management's effective study_id must equal the study_id of every
        Treatment in Management.treatment_ids -- i.e. a Management event
        can't silently span two different Studies.
    """
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
        # citation_id equality deliberately not checked

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
    """Table 19 #9; also enforced at construction in `ir_schema.Observation` (defence in depth)."""
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
    """Table 19 #10: every Treatment must reference
    exactly one Study via study_id (ExtractedField, may be UNRESOLVED)."""
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
    """Table 19 #11: Study.citation_ids must be
    non-empty (already enforced by Pydantic's min_length=1 on the field, so
    this only re-checks referential integrity) and every id must resolve to
    an existing Citation."""
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
    """Every citation_id in the graph (Method, Treatment, Management, Observation) must resolve to a real Citation."""
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
    for tp in ds.treatment_pairs:
        _check("TreatmentPair", tp.id, tp.citation_id)
    for crop in ds.crops:
        _check("Crop", crop.id, crop.citation_id)
    for cov in ds.coverages:
        _check("Coverage", cov.id, cov.citation_id)
    return issues


def check_variable_name_uniqueness(ds: IRDataset) -> list[ValidationIssue]:
    """Variable.name unique within the dataset."""
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
    """Crop.species_id must resolve to an existing Species."""
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
    """Both referenced Treatments must exist and share the pair's citation_id."""
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
    """Observation.variable_id and Coverage.variable_id, when present, must resolve to an existing Variable."""
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
    """Observation.crop_id, when present, must resolve to an existing Crop."""
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
    """Coverage.site_id must resolve to an existing Site."""
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
    """control_status may be absent or UNRESOLVED: an explicit no-op check."""
    return []


def check_treatment_name_not_quantity(ds: IRDataset) -> list[ValidationIssue]:
    """Table 2 / Section 4.1: 'a treatment key must not be the only place
    factor structure is documented' — soft, NON-BLOCKING warning only, per
    the spec's own words ('Documented authoring guidance; not mechanically
    enforced'). Implemented here as a warning so it's visible to reviewers
    without ever failing propose_record/commit_record on its own."""
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
    """Load content.md and map each rendered block anchor to its block text.

    Production convention (docproc/marker_adapter.py's render_simple_leaf /
    render_table / render_figure, and mirrored by content_reader.read_table's
    own "immediately precedes that anchor" comment): a block's rendered text
    is written FIRST, and its anchor marker (⟦b:NNNN⟧) immediately follows --
    the anchor marks the END of the block it belongs to, not the start of the
    next one. So anchor_i's own text is everything since the previous anchor
    (or the start of the file, for the first anchor) up to anchor_i's own
    marker -- not the text between anchor_i and anchor_{i+1}.
    """
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


# Fields whose value is a judgment or free-text classification of the cited text, not a verbatim quote: only the
# anchor (real, non-empty text) and, for INFERRED, the inference-basis note are checked.
_CATEGORICAL_JUDGMENT_FIELDS = {"reported_effect_scope", "event_type", "variable_name"}


def _leaf_field_name(field_path: str) -> str:
    # field_path is dotted/bracketed, e.g. "reported_effect_scope" or
    # "observations[3].reported_effect_scope" -- only the final component
    # (stripping any trailing "[N]") identifies which schema field this is.
    last = field_path.rsplit(".", 1)[-1]
    return re.sub(r"\[\d+\]$", "", last)


# Exact same-glyph look-alikes (different code points or markup for one visible character), never fuzzy matches.
_TYPOGRAPHIC_EQUIVALENTS: list[tuple[str, str]] = [
    ("‐", "-"),  # hyphen
    ("‑", "-"),  # non-breaking hyphen
    ("‒", "-"),  # figure dash
    ("–", "-"),  # en dash
    ("—", "-"),  # em dash
    ("−", "-"),  # minus sign
    ("$\\times$", "×"),  # LaTeX inline-math multiplication sign
    ("\\times", "×"),  # bare LaTeX command, no $ delimiters
    # Degree / prime / quote variants ("36˚37´N" == "36°37′N").
    ("˚", "°"),  # ring above
    ("º", "°"),  # masculine ordinal, OCR'd as a degree sign
    ("′", "'"),  # prime
    ("´", "'"),  # acute accent used as a prime
    ("ʹ", "'"),  # modifier prime
    ("’", "'"),  # right single quotation mark
    ("‘", "'"),  # left single quotation mark
    ("″", '"'),  # double prime
    ("“", '"'),
    ("”", '"'),
    ("µ", "μ"),  # MICRO SIGN vs GREEK SMALL LETTER MU: the same unit prefix, two code points
]


# Inline LaTeX and unicode super/subscripts canonicalised (\pm -> ±, ^{-1} -> -1, CO_2 -> CO2); notation only, never a
# number, unit symbol or word.
_SUPERSCRIPTS = str.maketrans({
    "⁻": "-", "⁺": "+", "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
    # Unicode subscript digits ("NH₄NO₃" vs Marker's "NH 4 NO 3"; the whitespace-stripped fallback then matches).
    "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4", "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9", "₊": "+", "₋": "-",
})
_MATH_TEXT_COMMAND_RE = re.compile(r"\\(?:mathrm|mathit|mathbf|textrm|textit|text|rm|it|bf)\s*\{([^{}]*)\}")
# `{\rm cmax}` / `{\it x}`: the old-style font switch inside a group.
_MATH_FONT_SWITCH_RE = re.compile(r"\\(?:rm|it|bf|mathrm)\s+")
_MATH_BRACED_SCRIPT_RE = re.compile(r"[\^_]\s*\{([^{}]*)\}")
_MATH_SIMPLE_SCRIPT_RE = re.compile(r"(?<=[A-Za-z0-9\)])[\^_]\s*(-?\d+)")
# A short subscript written with an underscore: "N_a", "C_i", "V_cmax" -> "Na", "Ci", "Vcmax".
_MATH_LETTER_SUBSCRIPT_RE = re.compile(r"(?<=[A-Za-z0-9\)])_(?=[A-Za-z0-9])")


def _normalize_math(text: str) -> str:
    if "\\" not in text and "$" not in text and "~" not in text and "^" not in text and "_" not in text:
        return text.translate(_SUPERSCRIPTS)
    text = text.replace("\\pm", "±").replace("\\cdot", "·")
    # Nested groups unwrap from the inside out, whichever kind is innermost: "\mathrm{Mg~ha^{-1}}" needs its script
    # flattened before the command matches, "_{\text{cmax}}" needs its command removed before the script matches.
    # Repeat both until nothing changes (bounded: each pass removes at least one brace pair).
    for _ in range(8):
        before = text
        text = _MATH_FONT_SWITCH_RE.sub("", text)
        text = _MATH_BRACED_SCRIPT_RE.sub(lambda m: m.group(1).replace(" ", ""), text)
        text = _MATH_TEXT_COMMAND_RE.sub(r"\1", text)
        if text == before:
            break
    text = _MATH_SIMPLE_SCRIPT_RE.sub(r"\1", text)
    text = _MATH_LETTER_SUBSCRIPT_RE.sub("", text)
    text = text.replace("$", "").replace("~", " ").replace("{", "").replace("}", "")
    return " ".join(text.translate(_SUPERSCRIPTS).split())


def _normalize_typography(text: str) -> str:
    """Canonicalise _TYPOGRAPHIC_EQUIVALENTS and inline math; applied to both sides, so it can only add matches."""
    for variant, canonical in _TYPOGRAPHIC_EQUIVALENTS:
        text = text.replace(variant, canonical)
    return _normalize_math(text)


def _value_supported_by_text(value: Any, text: str, field_name: Optional[str] = None) -> bool:
    """Conservative textual grounding check for scalar extracted values."""
    if value is None:
        return True

    normalized_text = _normalize_typography(" ".join(text.split()).casefold())

    if isinstance(value, bool) or field_name in _CATEGORICAL_JUDGMENT_FIELDS:
        # A judgment (a boolean, an effect-scope label) is never a literal word in the text: only require real text.
        return bool(normalized_text)

    if isinstance(value, int):
        return re.search(rf"(?<!\d){value}(?!\d)", normalized_text) is not None

    if isinstance(value, float):
        # Accept the normal decimal spelling and equivalent integer spelling
        # when the value is integral.
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
        # Fallback: Marker puts spaces inside notation ("NH 4 + -N"), so compare with all whitespace removed.
        stripped_needle = re.sub(r"\s+", "", needle)
        stripped_text = re.sub(r"\s+", "", normalized_text)
        return bool(stripped_needle) and stripped_needle in stripped_text

    # Lists/dicts represent structured values; their scalar subfields are
    # checked recursively when they are themselves ExtractedField objects.
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


# --- Nested value-bearing fields -------------------------------------------------------------------------------------
# A QuantityValue / DateRange inside an ExtractedField: its reported text, units and date must each be supported by
# what the field cites.
_DIMENSIONLESS_UNITS = frozenset({"", "unitless", "dimensionless", "none", "n/a", "na", "-", "1", "index", "ratio", "fraction"})
_NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def _canon(text: str) -> str:
    return _normalize_typography(" ".join(str(text).split()).casefold())


def _units_key(text: Optional[str]) -> str:
    """Units comparison key: canonical notation, lowercase, separators dropped, micro sign as 'u'
    ('g N m -2' == 'g N m^{-2}' == 'g N m⁻²' == 'g·N·m-2'; 'µmol' == 'umol')."""
    return re.sub(r"[\s.^()\[\]|*,;:_·×{}]+", "", _canon(text or "")).replace("μ", "u")


def _short_unit_pattern(canon_units: str) -> str:
    """Regex for a SHORT unit as a whole token, tolerating the single spaces Marker leaves inside rendered
    superscripts ('m 2' for m², 'cm 2' for cm²): whitespace is allowed only between a letter run and a digit/sign run,
    never inside a letter run, and the letter-boundary guards still stop a stray letter of a word from counting.
    Pure-symbol units ('°', '%', '‰') need no word boundaries: '45°42' writes the degree sign between two digits."""
    compact = re.sub(r"\s+", "", canon_units)
    if compact and not re.search(r"[a-z0-9]", compact):
        return re.escape(compact)
    parts = re.findall(r"[a-zμ°%]+|[-+]?\d+|.", compact)
    body = r"\s*".join(re.escape(p) for p in parts)
    left = "" if compact[:1] in "°%" else r"(?<![a-z0-9])"
    return rf"{left}{body}(?![a-z0-9])"


def _unit_supported(units: str, texts: list[str]) -> bool:
    key = _units_key(units)
    if key in _DIMENSIONLESS_UNITS:
        return True
    canon_units = _canon(units)
    pattern = _short_unit_pattern(canon_units)
    for text in texts:
        if key not in _units_key(text):
            continue
        # a very short unit ('g', 'm', 'mm', '%') must stand as a whole token, never a stray letter of a word
        if len(key) >= 3 or re.search(pattern, _canon(text)):
            return True
    return False


def _numbers_in(text: str) -> list[float]:
    """Every number written in `text`, SIGNED when the source writes a minus sign. A hyphen/dash/minus is a sign only
    when the nearest non-space character before it is not a digit, letter, '.', ')' , ']' or '%': '–0.69' is -0.69,
    while a range ('0-0.1') and a unit exponent ('g m -2') keep unsigned numbers."""
    canon = _canon(text)
    numbers: list[float] = []
    for match in _NUMBER_RE.finditer(canon):
        value = float(match.group().replace(",", ""))
        start = match.start()
        if start > 0 and canon[start - 1] == "-":
            j = start - 2
            while j >= 0 and canon[j] == " ":
                j -= 1
            if j < 0 or not (canon[j].isalnum() or canon[j] in ".)]%"):
                value = -value
        numbers.append(value)
    return numbers


def _year_of(value: Any) -> Optional[int]:
    match = re.match(r"\s*(\d{4})", str(value or ""))
    return int(match.group(1)) if match else None


def _max_two_literal_stretches(text: str, texts: list[str]) -> bool:
    """Can `text` (as words/numbers, in order) be covered by at most two contiguous stretches, each of which occurs
    contiguously, word for word, in one of the cited blocks?"""
    words = re.findall(r"[a-z0-9]+", _canon(text))
    if not words:
        return False
    padded = [" " + " ".join(re.findall(r"[a-z0-9]+", _canon(t))) + " " for t in texts]
    n = len(words)
    best = [0] + [n + 1] * n                     # best[i]: fewest stretches covering words[:i]
    for i in range(1, n + 1):
        for j in range(i):
            if best[j] + 1 < best[i] and any(" " + " ".join(words[j:i]) + " " in block for block in padded):
                best[i] = best[j] + 1
    return best[n] <= 2


def _nested_value_issues(field_path: str, value: dict[str, Any], cited: list[tuple[str, str]]) -> list[ValidationIssue]:
    """Grounding of the value-bearing parts of a QuantityValue or DateRange against the blocks its field cites (any one
    cited block may carry each part). Works on the raw payload dict; ignores dicts that are neither."""
    if not cited or not isinstance(value, dict):
        return []
    is_quantity = "reported_units" in value and "reported_text" in value
    is_date = "reported_text" in value and not is_quantity and any(k in value for k in ("earliest", "latest", "relative_timing"))
    if not (is_quantity or is_date):
        return []
    texts = [text for _, text in cited]
    anchors = ", ".join(anchor for anchor, _ in cited)
    issues: list[ValidationIssue] = []

    def issue(code: str, message: str) -> None:
        issues.append(ValidationIssue("error", code, f"{field_path}: {message}"))

    reported_text = value.get("reported_text")
    if isinstance(reported_text, str) and reported_text.strip():
        supported = any(_value_supported_by_text(reported_text, text) for text in texts)
        if not supported and is_quantity and field_path.rsplit(".", 1)[-1] in coordinates.COORDINATE_FIELDS:
            supported = coordinates.stated_equivalently(reported_text, texts)
        if not supported and is_date:
            # A date assembled from date_text + year_text is accepted only as at most two literal quoted stretches.
            supported = _max_two_literal_stretches(reported_text, texts)
        if not supported:
            what = "date text" if is_date else "reported_text"
            issue(
                "provenance_date_text_mismatch" if is_date else "provenance_reported_text_mismatch",
                f"{what} {reported_text!r} is not found in the cited block(s) {anchors} -- quote the source's own words, "
                f"never a reformatted, completed or paraphrased version.",
            )
    if is_quantity:
        for code, message in coordinates.coordinate_issues(field_path, value):
            issues.append(ValidationIssue("error", code, message))
        numeric = value.get("reported_numeric_value")
        if numeric is not None and isinstance(reported_text, str) and not any(abs(n - float(numeric)) < 1e-9 for n in _numbers_in(reported_text)):
            issue("provenance_numeric_mismatch", f"reported_numeric_value={numeric!r} does not appear in reported_text {reported_text!r}.")
        units = value.get("reported_units")
        if isinstance(units, str) and not _unit_supported(units, texts):
            issue(
                "provenance_units_mismatch",
                f"reported_units {units!r} is not found in the cited block(s) {anchors} -- report the units as the source "
                f"writes them; if the source states none, do not supply any.",
            )
    if is_date:
        block_text = " ".join(_canon(t) for t in texts)
        for bound in ("earliest", "latest"):
            year = _year_of(value.get(bound))
            if year is not None and not re.search(rf"(?<!\d){year}(?!\d)", block_text):
                issue(
                    "provenance_date_year_unsupported",
                    f"{bound}={value.get(bound)!r} supplies the year {year}, which the cited block(s) {anchors} do not state "
                    f"-- never supply a year the source does not give (leave the dates null and keep the reported text).",
                )
                break
    return issues


_BINOMIAL_RE = re.compile(r"\b([A-Z][a-z]{2,})\s+([a-z]{3,})\b")


def _genus_abbreviation_variants(value: Any, blocks: dict[str, str]) -> list[str]:
    """`value` with each full binomial ("Pinus sylvestris") abbreviated the way papers write it after first mention
    ("P. sylvestris") -- but only for a binomial the SAME paper spells out in full somewhere, so the expansion is
    itself grounded in the document, never supplied from outside knowledge."""
    if not isinstance(value, str) or not _BINOMIAL_RE.search(value):
        return []
    document = " ".join(blocks.values())
    variants: list[str] = []
    for genus, epithet in {m.groups() for m in _BINOMIAL_RE.finditer(value)}:
        if re.search(rf"\b{re.escape(genus)}\s+{re.escape(epithet)}\b", document):
            variants.append(re.sub(rf"\b{re.escape(genus)}\s+{re.escape(epithet)}\b", f"{genus[0]}. {epithet}", value))
    return variants


def validate_provenance(paper_id: str, payload: dict[str, Any]) -> list[ValidationIssue]:
    """Deterministically verify extracted/inferred values against cited blocks.

    For every EXTRACTED/INFERRED scalar field, each cited block anchor is looked
    up in the rendered content.md for the paper.  A value is accepted only when
    the block text contains that value.  The LLM's assertion that an anchor is
    correct is never trusted.
    """
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
            # Defense in depth (same philosophy as check_aggregated_over_factors's
            # own docstring: "catch it even if it somehow reached the store
            # without going through" the Pydantic model): ir_schema.py's own
            # construction-time invariant already requires a non-empty
            # unresolved_reason for every INFERRED field, boolean or not.
            # Re-checked here because validate_provenance operates on a plain
            # dict and is not guaranteed to only ever be called on payloads
            # that already passed that construction step.
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

            if not _value_supported_by_text(value, block_text, field_name=leaf_name) and not any(
                _value_supported_by_text(variant, block_text, field_name=leaf_name)
                for variant in _genus_abbreviation_variants(value, blocks)
            ):
                issues.append(
                    ValidationIssue(
                        "error",
                        "provenance_value_mismatch",
                        f"{field_path}: value={value!r} is not supported by block {anchor}. "
                        f"Provide a locator whose block text contains the cited value.",
                    )
                )

        if isinstance(value, dict):
            cited = [
                (a, blocks[a]) for a in (
                    (loc.get("block_anchor") or "").strip("[]") for loc in locators if isinstance(loc, dict)
                ) if a in blocks
            ]
            issues.extend(_nested_value_issues(field_path, value, cited))

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
    check_variable_name_uniqueness,
    check_crop_species_ref,
    check_treatment_pair_references_resolve,
    check_variable_ref_integrity,
    check_crop_ref_integrity,
    check_coverage_site_ref,
]


def validate_dataset(ds: IRDataset) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for check in ALL_WHOLE_GRAPH_CHECKS:
        issues.extend(check(ds))
    return issues


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------

# A valid record is READY only when these core fields are resolved (an undated Observation is still usable, so
# temporal_info is not required; protocol Section 10.1).
READINESS_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "Observation": ("value", "variable_name"),
    # identity is required; descriptive metadata never is
    "Variable": ("name",),
    "Method": ("name",),
    # protocol Section 6.3: a treatment has a name and a definition of the intended contrast
    "Treatment": ("name", "definition"),
}

# Identity that can be given in either of two fields (a Crop needs a cultivar or a common name).
READINESS_ANY_OF_FIELDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "Crop": (("cultivar", "common_name"),),
}


def _unresolved_entry(entry: Any) -> bool:
    return (
        not isinstance(entry, dict)
        or entry.get("provenance_label") == "UNRESOLVED"
        or entry.get("value") is None
    )


def readiness_issues(entity_type: str, payload: dict[str, Any], record_id: Optional[str] = None) -> list[ValidationIssue]:
    """Why a (structurally valid) payload is NOT ready to be committed as `ready`, or [] when it is. Works on the raw
    payload dict, so it can run before or without model construction. A required field that is absent, null, or
    labelled UNRESOLVED (or carries no value) makes the record not ready; the reason the extraction gave is quoted.
    An any-of identity group (Crop: cultivar or common_name) needs at least one member resolved."""
    issues: list[ValidationIssue] = []
    for field in READINESS_REQUIRED_FIELDS.get(entity_type, ()):
        entry = (payload or {}).get(field)
        if not _unresolved_entry(entry):
            continue
        reason = entry.get("unresolved_reason") if isinstance(entry, dict) else None
        issues.append(ValidationIssue(
            severity="error",
            code=f"{entity_type.lower()}_{field}_unresolved",
            message=f"{entity_type} is not ready: `{field}` is UNRESOLVED"
                    + (f" ({reason})" if reason else "") + " -- a record with no " + field.replace("_", " ")
                    + " is kept as unresolved, never committed as ready.",
            entity_type=entity_type, entity_id=record_id,
        ))
    for group in READINESS_ANY_OF_FIELDS.get(entity_type, ()):
        entries = [(payload or {}).get(field) for field in group]
        if all(_unresolved_entry(entry) for entry in entries):
            reasons = "; ".join(
                f"{field}: {entry.get('unresolved_reason')}" for field, entry in zip(group, entries)
                if isinstance(entry, dict) and entry.get("unresolved_reason")
            )
            issues.append(ValidationIssue(
                severity="error",
                code=f"{entity_type.lower()}_identity_unresolved",
                message=f"{entity_type} is not ready: none of {' / '.join(f'`{f}`' for f in group)} is resolved"
                        + (f" ({reasons})" if reasons else "") + " -- a record that does not say what it is "
                        "is kept as unresolved, never committed as ready.",
                entity_type=entity_type, entity_id=record_id,
            ))
    return issues
