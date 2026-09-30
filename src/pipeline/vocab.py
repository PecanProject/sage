"""`lookup_vocab`: advisory vocabulary suggestions, never part of validation. The seed list is a local stand-in for
PEcAn's `events_schema_v0.1.1.json`."""

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
            "Advisory only -- never gating. Free text is always accepted at the IR layer; "
            "controlled-vocabulary matching happens at materialization."
        )
        if not matches
        else "Advisory only -- never gating.",
        "seed_source": "local stand-in seed list; intended seed is PEcAn events_schema_v0.1.1.json.",
    }
