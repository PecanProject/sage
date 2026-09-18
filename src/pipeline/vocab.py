"""
`lookup_vocab` — Playbook Section 5: "variable_name / event_type: confirmed
free text at the IR layer... lookup_vocab is advisory only, never gating."

This module NEVER blocks anything; `ir_service.lookup_vocab` returns
suggestions with a similarity note, and `propose_record`/`commit_record`
never call into this module at all -- vocabulary matching is not part of the
validation path. That separation is deliberate and mirrors P3 (keep curation
separate from harmonization): controlled-vocabulary matching is a
materialization-time concern, this is just a curator/agent convenience
during curation.

Seed vocabulary: a small stand-in for the PEcAn `events_schema_v0.1.1.json`
list named in the Playbook. Loading the real file over the network is
explicitly Sprint 3+ fixture-review scope, not Sprint 2 infrastructure --
kept as a local seed list here, clearly labeled as such, so `lookup_vocab`
is exercisable and testable now without an unreviewed external fetch baked
into the service's startup path.
"""

from __future__ import annotations

import difflib

SEED_EVENT_TYPES = [
    "planting",
    "harvest",
    "tillage",
    "fertilization",
    "irrigation",
    "pesticide_application",
    "herbicide_application",
    "cover_crop_planting",
    "cover_crop_termination",
    "grazing",
    "burning",
    "liming",
]

SEED_VARIABLE_NAMES = [
    "yield",
    "aboveground_biomass",
    "belowground_biomass",
    "soil_organic_carbon",
    "soil_organic_matter",
    "total_nitrogen",
    "leaf_area_index",
    "plant_height",
    "root_depth",
    "grain_protein_content",
]

_VOCAB_BY_ENTITY_TYPE = {
    "event_type": SEED_EVENT_TYPES,
    "variable_name": SEED_VARIABLE_NAMES,
}


def lookup_vocab(term: str, entity_type: str, max_results: int = 5) -> dict:
    """Advisory fuzzy match against the seed vocabulary for `entity_type`.
    Returns an empty match list (not an error) for an unrecognized
    entity_type or term -- there is nothing to block on here."""
    candidates = _VOCAB_BY_ENTITY_TYPE.get(entity_type, [])
    matches = difflib.get_close_matches(term.strip().lower(), candidates, n=max_results, cutoff=0.3)
    return {
        "term": term,
        "entity_type": entity_type,
        "advisory_only": True,
        "matches": matches,
        "note": (
            "Advisory only -- never gating. Free text is always accepted at the IR layer "
            "(Playbook Section 5); controlled-vocabulary matching happens at materialization."
        )
        if not matches
        else "Advisory only -- never gating.",
        "seed_source": "local stand-in seed list (Sprint 2); intended seed is PEcAn events_schema_v0.1.1.json, "
        "not yet loaded from source -- see Playbook Section 9.",
    }
