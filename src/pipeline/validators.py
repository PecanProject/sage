"""
Whole-graph validators — IR spec Section 9.2 / Table 19, amended per the
Option B diff table in Playbook Section 5.1 ("Exact diff to Section 9.2
(Table 19), if and when Option B is adopted" — that "if and when" has been
resolved this sprint: Option B is approved and implemented, so this module
implements Table 19 *as amended*, not the original citation-scoped version.

Kept deliberately separate from `ir_schema.py`: these rules need the whole
`IRDataset` graph (cross-entity uniqueness, referential integrity), which a
single Pydantic model's own field/model validators can't see. Construction-
time, single-entity invariants (Table 18) live in `ir_schema.py` instead —
this file assumes each entity passed in already satisfies those.

Every check function returns a list of ValidationIssue; nothing raises.
`validate_dataset` aggregates all of them. This mirrors the QC gate's own
design principle from Sprint 1 (Section 4a): each check is independent, and
the caller decides what to do with the aggregate, not any one check itself.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, Optional

from pipeline import coordinates
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
    """Returns (scope_kind, scope_key). study_id when resolved, else a
    citation_id fallback — Table 19 rows 4/5 as amended: 'falls back to
    citation_id-scoping when study_id is UNRESOLVED'."""
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
    """Table 19 #5, Option B: at most one control_status=True Treatment per
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
    """Table 19 #7, Option B version:
      - Observation.site_id must match the site_id of the referenced
        Treatment (UNCHANGED from the original rule).
      - citation_id equality is NO LONGER required (Observation.citation_id
        is its own source-of-record pointer and may legitimately differ from
        Treatment.citation_id under Option B).
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
    """Table 19 #9. Note: the per-observation shape of this rule is already
    enforced at construction time in `ir_schema.Observation`. This
    whole-graph pass exists to catch it even if an Observation somehow
    reached the store without going through that model (defense in depth,
    same rationale as the QC gate's "never trust a single layer" pattern
    from Sprint 1)."""
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
    """Table 19 #10 (NEW under Option B): every Treatment must reference
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
    """Table 19 #11 (NEW under Option B): Study.citation_ids must be
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
    """Not a numbered Table 19 row, but implied by the 'citation_id is
    always a source-of-record pointer' rule (Playbook Section 5): every
    citation_id anywhere in the graph (Method, Treatment, Management,
    Observation) must resolve to a real Citation, even though it's no
    longer an identity/uniqueness scope under Option B."""
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
    """NEW this sprint (Variable entity): Variable.name unique within the
    dataset, same pattern as check_site_name_uniqueness /
    check_species_scientific_name_uniqueness above."""
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
    """NEW this sprint (Crop entity): Crop.species_id must resolve to an
    existing Species -- Crop is deliberately a reference to Species's
    taxonomy, not a duplicate of it (see ir_schema.Crop's docstring)."""
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
    """NEW this sprint (TreatmentPair entity), adapted from
    src2/betydb_extraction/ir/validation/referential_integrity.py's check of
    the same name: both referenced Treatments must exist, and the pair plus
    both Treatments must share the same citation_id scope (mirroring the
    citation-scoping rule already applied to Treatment/Management)."""
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
    """NEW this sprint: Observation.variable_id and Coverage.variable_id,
    when present (both are optional -- see ir_schema.Observation's
    docstring on variable_id), must resolve to an existing Variable."""
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
    """NEW this sprint: Coverage.site_id must resolve to an existing Site.
    Checked here -- unlike the pre-existing, separately-documented gap
    where Treatment/Method/Observation.site_id existence is NOT checked
    anywhere in this module -- because Coverage.site_id is a brand new
    field introduced this sprint, not a retrofit of a prior entity; leaving
    a known dangling reference in a field we are introducing right now
    would be a new bug, not a preserved pre-existing one."""
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
    """Playbook Section 5 confirmation: control_status stays optional /
    UNRESOLVED-able. Not a rejection rule -- this function exists so the
    absence of control_status is explicitly a non-issue, documented as a
    no-op check rather than silently having no code path for it."""
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


# Leaf field names whose value is a controlled-vocabulary label describing a
# JUDGMENT about the cited evidence (like a boolean's truth value), not a
# phrase the source text is ever expected to contain verbatim. Real
# Oceologia-1998 runs (Phase C investigation) confirmed `reported_effect_scope`
# has the exact same structural problem the boolean redesign above already
# fixed for `is_raw_replicate_level`/`control_status`/`use_for_validation`: no
# paper states the literal string "treatment_mean" or "aggregated_mean", so
# the literal-substring check made this field structurally unable to ever
# pass EXTRACTED/INFERRED validation -- 0 of the 61 Observation candidates in
# run 20260915T135025_d291c220 ever got it past UNRESOLVED, and even
# ir_service's own authored worked example for Observation (get_schema's
# `_FILLED_EXAMPLES["Observation"]["reported_effect_scope"]`) cites a table
# anchor without the literal word "treatment_mean" appearing in it -- the
# canonical "correct" answer this module hands the model is itself rejected
# by the old check. Same fix, same reasoning, generalized by leaf field name
# instead of by Python type since this one is a `Literal[str]`, not `bool`.
#
# `Management.event_type` and `Observation.variable_name` (added: design-
# review session following the Daren-1997-Canopy / Oceologia-1998 / Kathryn-
# 2020-Winter paper audits) are a DIFFERENT shape from `reported_effect_scope`
# -- they're open `str` fields, not a closed `Literal[...]`, so there is no
# type-level backstop bounding what value could pass once the literal-match
# requirement is lifted. They belong here anyway because the protocol itself
# already settles the question, independent of this module: pipeline/vocab.py's
# own docstring states "variable_name / event_type: confirmed free text at
# the IR layer... lookup_vocab is advisory only, never gating" (Playbook
# Section 5), and ir_schema.py's ProvenanceLabel docstring independently
# names the same two fields as "deliberately free text at this layer". Real
# confirmed cases across all three audited papers -- Management.event_type
# values like "fertilizer application" (Daren, block b:0028: "...received
# 122 kg N ha-1 in the form of NH4NO3..."), "tubs moved to an outside garden"
# (Oceologia), and "compost application"/"cover crop planting" (Kathryn,
# multiple blocks) -- are all reasonable, correct classifications of a
# described action; none is a literal quote, and none was found to be
# fabricated in the audited records. The remaining safety net for these two
# fields is the same one the codebase already relies on for boolean/
# reported_effect_scope judgments: the anchor must still be real and
# resolve to real, non-empty text (validate_provenance's own
# provenance_anchor_not_found check, unaffected by this set), an INFERRED
# label still requires a genuine inference-basis note, and AI Validation
# remains a second, already-confirmed-effective check on implausible values
# (see corrections_store / the AI Validator's observe-and-correct pass).
_CATEGORICAL_JUDGMENT_FIELDS = {"reported_effect_scope", "event_type", "variable_name"}


def _leaf_field_name(field_path: str) -> str:
    # field_path is dotted/bracketed, e.g. "reported_effect_scope" or
    # "observations[3].reported_effect_scope" -- only the final component
    # (stripping any trailing "[N]") identifies which schema field this is.
    last = field_path.rsplit(".", 1)[-1]
    return re.sub(r"\[\d+\]$", "", last)


# Deterministic, explicitly-enumerated typographic equivalences -- NOT a
# fuzzy/approximate match, only exact known look-alikes where content.md and
# the model's own text generation render the SAME visible glyph through
# different Unicode code points or markup. Confirmed real cases (Daren-1997-
# Canopy audit): Site.soil_context's "fine-loamy" appears in content.md with
# a plain ASCII hyphen (U+002D) but the model reproduced it with a
# non-breaking hyphen (U+2011), byte-verified; and "Ey × FF" appears in
# content.md as literal Unicode "×" (U+00D7) in three places and as LaTeX
# "$\times$" in a fourth -- within the SAME source paragraph, i.e. even
# Marker's own rendering of the identical phrase is inconsistent, so the
# model (which always reproduces the readable Unicode form) can never match
# whichever encoding a given block happened to get. Each of these was
# rejected by validate_provenance as `provenance_value_mismatch` despite the
# value being genuinely, fully supported by the cited text -- a false
# rejection, not a real grounding failure. Deliberately narrow: only
# unambiguous same-glyph pairs belong here (e.g. NOT "×" -> "x", since that
# conflates a symbol with a letter rather than canonicalizing an encoding).
_TYPOGRAPHIC_EQUIVALENTS: list[tuple[str, str]] = [
    ("‐", "-"),  # hyphen
    ("‑", "-"),  # non-breaking hyphen (the confirmed Daren case)
    ("‒", "-"),  # figure dash
    ("–", "-"),  # en dash
    ("—", "-"),  # em dash
    ("−", "-"),  # minus sign
    ("$\\times$", "×"),  # LaTeX inline-math multiplication sign
    ("\\times", "×"),  # bare LaTeX command, no $ delimiters
    # Degree / prime / quote variants. Real case (Kathryn-2020-Winter b:0035): the source prints "36˚37´N" with a
    # RING ABOVE and an ACUTE ACCENT; the model quoted it as "36°37′N" and the (identical-looking) coordinate was
    # rejected, Site went unresolved and blocked every Treatment and Observation.
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


# Inline LaTeX math and unicode super/subscripts as Marker renders them, canonicalised so the same quantity compares
# equal however it is spelled. Real cases (Felipe-2010-Cultivar, correction pass Fix 4): a block reads
# "$4.6\pm0.4~\mathrm{Mg~ha^{-1}}$" while the model writes "Mg ha⁻¹" (false rejection of the units), and "$CO_2$" while
# the model writes "CO2" (false rejection of a `notes` sentence, which sank the whole mustard-CO2 Observation). Only
# NOTATION is normalised -- \pm -> ±, ^{-1} -> -1, ⁻¹ -> -1, CO_2 -> CO2, \mathrm{..}/$/~/braces dropped -- never a
# number, a unit symbol or a word, so no scientific meaning changes.
_SUPERSCRIPTS = str.maketrans({
    "⁻": "-", "⁺": "+", "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
    # Unicode SUBSCRIPT digits: the model writes "NH₄NO₃" where Marker renders "NH 4 NO 3" (real case Daren-1997-Canopy
    # b:0028); the whitespace-stripped fallback then compares "nh4no3" on both sides.
    "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4", "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9", "₊": "+", "₋": "-",
})
_MATH_TEXT_COMMAND_RE = re.compile(r"\\(?:mathrm|mathit|mathbf|textrm|textit|text|rm|it|bf)\s*\{([^{}]*)\}")
# `{\rm cmax}` / `{\it x}`: the old-style font switch inside a group (real case Philippe-2007-Six b:0046 "$V_{\rm cmax}$").
_MATH_FONT_SWITCH_RE = re.compile(r"\\(?:rm|it|bf|mathrm)\s+")
_MATH_BRACED_SCRIPT_RE = re.compile(r"[\^_]\s*\{([^{}]*)\}")
_MATH_SIMPLE_SCRIPT_RE = re.compile(r"(?<=[A-Za-z0-9\)])[\^_]\s*(-?\d+)")
# A single-letter/short alphanumeric subscript written with an underscore: "N_a", "C_i", "V_cmax" -> "Na", "Ci", "Vcmax".
# Real cases (Philippe-2007-Six b:0044): "$N_a$", "$C_i$", "$V_{\text{cmax}}$" never matched the model's "Na"/"Vcmax".
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
    """Canonicalize the small, deterministic set of typographic variants in
    _TYPOGRAPHIC_EQUIVALENTS -- see that list's own comment for the real
    cases this fixes -- and inline LaTeX math notation (`_normalize_math`).
    Applied to both sides of every comparison in _value_supported_by_text,
    so it can only ever ADD a match an exact check already missed, never
    remove one (both the value and the block text collapse onto the same
    canonical spelling)."""
    for variant, canonical in _TYPOGRAPHIC_EQUIVALENTS:
        text = text.replace(variant, canonical)
    return _normalize_math(text)


def _value_supported_by_text(value: Any, text: str, field_name: Optional[str] = None) -> bool:
    """Conservative textual grounding check for scalar extracted values."""
    if value is None:
        return True

    normalized_text = _normalize_typography(" ".join(text.split()).casefold())

    if isinstance(value, bool) or field_name in _CATEGORICAL_JUDGMENT_FIELDS:
        # A boolean's truth, or a controlled-vocabulary judgment label like
        # `reported_effect_scope`, describes what the cited text MEANS, not a
        # literal token scientific prose is ever expected to contain -- a
        # paper essentially never states the word "true"/"false" or
        # "treatment_mean" to describe itself. Requiring that literal word
        # (the previous behavior) made every such field structurally unable
        # to ever pass EXTRACTED/INFERRED validation -- confirmed against
        # real Oceologia-1998/pecan extraction runs. The only thing checkable
        # at this single-(value, single-block-text) granularity is that the
        # cited anchor resolves to real, substantive text a reviewer could
        # judge the claim against (an anchor that doesn't exist is rejected
        # one level up in validate_provenance; an anchor resolving to empty
        # text is rejected here). The actual evidentiary justification for
        # *why* that text supports this specific judgment is enforced by the
        # INFERRED-requires-a-real-inference-basis-note rule -- required at
        # construction time by ir_schema.ExtractedField._provenance_invariants
        # and re-checked defensively in validate_provenance below, not by
        # pattern-matching the block's raw text here.
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
        # Fallback only, never the first check: source rendering of chemical/
        # scientific notation (subscripts, superscripts) frequently comes
        # through content.md with spurious single spaces INSIDE what is
        # really one token -- e.g. real Oceologia-1998 table text renders
        # "NH4+-N" as "NH 4 + -N" (confirmed in content.md for paper
        # Oceologia-1998, block b:0053). Collapsing whitespace runs (above)
        # doesn't fix this since these are single separating spaces, not
        # repeated ones. Stripping ALL whitespace from both sides before a
        # second substring check catches this without weakening the
        # ordered-character-sequence requirement -- it can only ever
        # ADD a match the exact check already missed, never remove one.
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


# --- Nested value-bearing fields (correction pass, Fix 4) -----------------------------------------------------------
#
# `_value_supported_by_text` returns True for any dict, so a QuantityValue / DateRange nested inside an ExtractedField was
# only ever checked for being a dict: its `reported_text`, `reported_units` and date text could contradict the cited
# block and still be committed as EXTRACTED. Real cases (Felipe-2010-Cultivar, run felipe_smoke_20260920T132343):
#   - `reported_text` "39.8 degrees hue angle" / `reported_units` "degrees" where the block says "39.8 hue";
#   - a Management date `reported_text` "May 18-19 2006" / earliest 2006-05-18 where the block says "May 18 and 19"
#     and never gives the year (the protocol forbids supplying one).
# These fields carry the reported value, its units and its date, so each must be supported by what the field cites.
_DIMENSIONLESS_UNITS = frozenset({"", "unitless", "dimensionless", "none", "n/a", "na", "-", "1", "index", "ratio", "fraction"})
_NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def _canon(text: str) -> str:
    return _normalize_typography(" ".join(str(text).split()).casefold())


def _units_key(text: str) -> str:
    """Comparison key for a units string: canonicalised notation, lowercase, spaces and separators dropped
    ('g N m -2' == 'g N m-2' == 'g N m^{-2}' == 'g N m⁻²' == 'g·N·m-2')."""
    return re.sub(r"[\s.^()\[\]|*,;:_·×{}]+", "", _canon(text))


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
    when the nearest non-space character before it is not a digit, letter, '.', ')' , ']' or '%': '–0.69' (Philippe
    Table 3 dark respiration) is -0.69 and '(-121˚32´W' is -121, while a range ('0-0.1', '15 – 30') and a unit exponent
    ('g m -2') keep unsigned numbers exactly as before. Before this, '–0.69' read as 0.69: the correct value -0.69 was
    rejected and a sign-dropped 0.69 accepted."""
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
            # A date the pipeline assembled from two cited stretches ("9 June" in one block + "1993" in another, the
            # Item 11 temporal-context flow: date_text + year_text) is not one contiguous quote. It is accepted only
            # when it splits into at most TWO stretches, each a literal contiguous quote of some cited block --
            # words merely present somewhere in the citations never assemble a date ("June 18 2006" from "mid-June"
            # and "May 18" and "2006" is three stretches, so it is not).
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
    itself grounded in the document, never supplied from outside knowledge. Real case (Philippe-2007-Six b:0028):
    "25-year-old natural P. sylvestris stand" rejected the model's "25-year-old natural Pinus sylvestris stand"."""
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


# ---------------------------------------------------------------------------
# Readiness (Item 15)
# ---------------------------------------------------------------------------

# A record can be structurally valid -- every field has a provenance label, an UNRESOLVED one carries a real
# reason -- and still not be READY: its core has no content. Real evidence: 81 of the 206 Observations stored as
# `ready` across the stored runs had an UNRESOLVED `value` (36 of 108 in one run) and 8 an UNRESOLVED
# `variable_name`, i.e. a "ready" measurement with no measurement. Which fields must be resolved is per entity type.
# `temporal_info` is deliberately NOT here: protocol Section 10.1 says that when no reliable date window can be
# recovered the date fields are left blank with the reason explained -- an undated Observation is still usable.
READINESS_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "Observation": ("value", "variable_name"),
    # Correction pass, Fix 7 -- CORE IDENTITY. A record that does not say what it is cannot be used, whatever else it
    # carries. Real hollow "ready" records (Daren run 20260919T211137_77879c98 and Felipe run felipe_smoke_20260920T132343):
    # a Variable whose name, description, units and notes were ALL UNRESOLVED (Daren leaf-blade dry weight; the reason it
    # gave, "page number not available", was itself spurious); a Variable with a null name (Felipe plant nitrogen
    # content); a Method with a null name (Felipe disease scoring). Deliberately the same principle as Observation, not
    # "every optional field": identity is required; descriptive metadata (description, units, notes) never is.
    "Variable": ("name",),
    "Method": ("name",),
    # Phase A5: protocol Section 6.3 minimum -- a treatment has a recognisable name AND a definition of the intended
    # contrast. Real hollow "ready" Treatments (Kathryn run 20260925T225646): "4" with no name, "mean" with neither.
    "Treatment": ("name", "definition"),
}

# Identity that can be given in either of two fields. A Crop is the paper-specific cultivar/variety (ir_schema.Crop): its
# taxonomy lives in the referenced Species, so with neither `cultivar` nor `common_name` resolved it names nothing that the
# Species record does not (real case: Daren Crop Trailblazer, `cultivar` UNRESOLVED and no common name).
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
