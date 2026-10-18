"""The deterministic checker. It tells the agent exactly what is wrong or missing; code settles, never the agent."""

from sage.ir.draft import Draft
from sage.ir.schema import Entity
from sage.parse.document import Document
from sage.pipeline.checker.coverage import check_coverage
from sage.pipeline.checker.dataset import check_dataset
from sage.pipeline.checker.evidence import check_evidence
from sage.pipeline.checker.issues import Issue
from sage.pipeline.checker.record import check_record

__all__ = ["Issue", "check_all", "check_one", "open_issues"]


def check_one(record: Entity, doc: Document) -> list[Issue]:
    return check_evidence(record, doc) + check_record(record)


def check_all(draft: Draft, doc: Document, *, cap: int) -> list[Issue]:
    issues: list[Issue] = []
    for record in draft.records.values():
        issues += check_one(record, doc)
    return issues + check_dataset(draft) + check_coverage(draft, doc, cap=cap)


def open_issues(issues: list[Issue], draft: Draft) -> list[Issue]:
    """Issues not acknowledged by an unresolved mark that stands on their target or its record."""
    return [i for i in issues if draft.mark(i.target) is None and draft.mark(i.record_id) is None]
