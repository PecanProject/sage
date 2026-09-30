"""
Unresolved-cause taxonomy: WHY a record is not ready, stated as one of a fixed set of causes, so a failure
is never just "unresolved".

    NOT_RETRIEVED          the evidence exists in the paper but never reached the model (or an empty answer came after
                           a stall / provider hiccup -- nothing was actually looked at)
    AMBIGUOUS              the evidence was there but did not tie the record to exactly one referent (method, treatment,
                           site link ambiguous -- refused rather than guessed)
    ABSENT                 the paper does not state it (a whole-document pattern scan found nothing), or the source
                           gives no value (e.g. a directional statement without a number)
    GROUNDING_FAILURE      the model reported text that is not in the block it cites (paraphrase / wrong block)
    SCHEMA_LIMITATION      the fact is extracted but the current IR cannot represent it (L1/L2/L3 limitations)
    PROVIDER_FAILURE       the provider failed (timeouts, empty or malformed responses) -- never scientific absence
    VALIDATION_FAILURE     the model's output never had the required shape / never passed deterministic validation
    AI_CONCERN             deterministically valid, but an unanswered AI-validator concern kept it from ready
    CONFLICT               the source states contradictory values (reserved)
    BLOCKED_PREREQUISITE   not attempted: a record it depends on is not usable
    NOT_READY              valid but a required field is unresolved and no finer cause could be established

Deterministic: derived only from what the record's own artifacts say. The coverage audit, when a bundle was used,
upgrades a readiness failure into NOT_RETRIEVED / ABSENT / "retrieved but not resolved" (AMBIGUOUS).
"""

from __future__ import annotations

import json
from typing import Any, Optional

NOT_RETRIEVED = "NOT_RETRIEVED"
AMBIGUOUS = "AMBIGUOUS"
ABSENT = "ABSENT"
GROUNDING_FAILURE = "GROUNDING_FAILURE"
SCHEMA_LIMITATION = "SCHEMA_LIMITATION"
PROVIDER_FAILURE = "PROVIDER_FAILURE"
VALIDATION_FAILURE = "VALIDATION_FAILURE"
AI_CONCERN = "AI_CONCERN"
CONFLICT = "CONFLICT"
BLOCKED_PREREQUISITE = "BLOCKED_PREREQUISITE"
NOT_READY = "NOT_READY"

CAUSES = (NOT_RETRIEVED, AMBIGUOUS, ABSENT, GROUNDING_FAILURE, SCHEMA_LIMITATION, PROVIDER_FAILURE, VALIDATION_FAILURE,
          AI_CONCERN, CONFLICT, BLOCKED_PREREQUISITE, NOT_READY)

# Readiness field -> the coverage-audit concept that holds its evidence, per entity type.
_FIELD_CONCEPTS: dict[str, dict[str, str]] = {
    "Site": {"latitude": "coordinates", "longitude": "coordinates", "elevation": "elevation", "soil_context": "soil",
             "name": "site_name"},
    "Variable": {"name": "definition", "units": "units"},
    "Method": {"name": "procedure", "description": "procedure"},
}


def _text(node: Any) -> str:
    return json.dumps(node, ensure_ascii=False, default=str) if node is not None else ""


def classify(status: str, detail: Optional[dict], reason: Any = None, coverage: Optional[dict[str, str]] = None,
             entity_type: Optional[str] = None) -> Optional[str]:
    """The cause of a non-ready outcome, or None for a ready one. `coverage` is {concept: status} from the record's
    context bundle, when one was used."""
    if status == "ready":
        return None
    detail = detail or {}
    if detail.get("unresolved_cause") in CAUSES:
        return detail["unresolved_cause"]
    if status == "blocked":
        text = _text(reason)
        return SCHEMA_LIMITATION if "current-IR limitation" in text else BLOCKED_PREREQUISITE
    if status == "error":
        if detail.get("failure_kind") == "provider" or str(detail.get("failure_class") or "").startswith("provider"):
            return PROVIDER_FAILURE
        message = _text(detail.get("message"))
        if "raw evidence grounding failed" in message:
            return GROUNDING_FAILURE
        if "no final answer" in message or detail.get("failure_class") == "no_final_answer":
            return NOT_RETRIEVED
        return VALIDATION_FAILURE
    # unresolved
    errors = detail.get("last_errors") or []
    text = _text(errors)
    if detail.get("unresolved_by") == "ai_validation" or "AI Validator" in text or "AI validator" in text:
        withdrawn = "value withdrawn" in text
        if not withdrawn:
            return AI_CONCERN
    if "is ambiguous among" in text:
        return AMBIGUOUS
    if "conflict" in text.lower() and "contradict" in text.lower():
        return CONFLICT
    if "provenance_" in text or "is not supported by block" in text or "is not found in the cited block" in text:
        return GROUNDING_FAILURE
    readiness = detail.get("readiness_issues") or []
    if readiness:
        if coverage and entity_type in _FIELD_CONCEPTS:
            for issue in readiness:
                code = str(issue.get("code") or "")
                for field, concept in _FIELD_CONCEPTS[entity_type].items():
                    if f"_{field}_" in code:
                        state = coverage.get(concept)
                        if state == "NOT_RETRIEVED":
                            return NOT_RETRIEVED
                        if state in ("ABSENT_BY_PATTERN",):
                            return ABSENT
                        if state == "FOUND":
                            return AMBIGUOUS if "AI validator" not in _text(issue) else AI_CONCERN
        if "without providing a numeric" in _text(readiness) or "does not give" in _text(readiness):
            return ABSENT
        if "AI validator concern" in _text(readiness):
            return AI_CONCERN
        return NOT_READY
    if detail.get("flag_result") is not None and detail.get("payload") is None:
        return VALIDATION_FAILURE
    return NOT_READY
