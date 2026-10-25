"""The deterministic checker. It tells the agent what is wrong or missing; code settles, never the agent."""

from collections.abc import Collection

from sage.ir.draft import Draft
from sage.parse.document import Document
from sage.pipeline.checker.coverage import check_coverage
from sage.pipeline.checker.dataset import check_dataset
from sage.pipeline.checker.evidence import check_evidence
from sage.pipeline.checker.issues import Issue
from sage.pipeline.checker.record import check_record

__all__ = ["Issue", "check_all", "open_issues"]


def check_all(draft: Draft, doc: Document, uncompiled: Collection[str] = ()) -> list[Issue]:
    issues: list[Issue] = []
    for record in draft.records.values():
        issues += check_evidence(record, doc) + check_record(record)
    return issues + check_dataset(draft, uncompiled) + check_coverage(draft, doc)


def open_issues(issues: list[Issue], draft: Draft) -> list[Issue]:
    """Issues not acknowledged by an unresolved mark that stands on their target or its record."""
    return [i for i in issues if draft.mark(i.target) is None and draft.mark(i.record_id) is None]
