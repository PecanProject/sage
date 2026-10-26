"""Settle: code decides each field's status from the checker, the evidence and the unresolved marks that stand."""

from collections import defaultdict
from dataclasses import dataclass

from sage.ir.draft import Cause, Draft
from sage.ir.schema import Entity, Field, fields_of
from sage.ir.settled import FieldStatus, Outcome, Settled, SettledRecord, Status
from sage.parse.document import ANCHOR, CellMatch, Document
from sage.pipeline.checker import Issue, check_all

RANK: dict[Status, int] = {"verified": 0, "flagged": 1, "unresolved": 2}


def settle(draft: Draft, doc: Document, *, run_id: str, outcome: Outcome) -> Settled:
    """A field with a blocking issue becomes unresolved with its last error, unless a mark stands on it. An agent's
    mark never stands on a value, so a value that fails a check is never verified, whatever the agent marked."""
    by_target: dict[str, list[Issue]] = defaultdict(list)
    for i in check_all(draft, doc):
        by_target[i.target].append(i)
    ctx = _Context(
        draft=draft,
        by_target=by_target,
        cells={c.anchor: c.match for t in doc.tables for row in t.rows for c in row},
        blank={p.number for p in doc.pages if not p.has_text},
        outcome=outcome,
    )
    ids = set(draft.records)
    return Settled(
        run_id=run_id,
        paper_id=doc.paper_id,
        outcome=outcome,
        summary=draft.finished.summary if draft.finished is not None else None,
        records=[ctx.record(r) for r in draft.records.values()],
        tables=list(draft.tables.values()),
        unresolved=[u for u in draft.unresolved.values() if u.target.split(".", 1)[0] not in ids],
        overwrites=draft.overwrites,
    )


@dataclass(frozen=True)
class _Context:
    draft: Draft
    by_target: dict[str, list[Issue]]
    cells: dict[str, CellMatch]
    blank: set[int]
    outcome: Outcome

    def record(self, record: Entity) -> SettledRecord:
        rid = record.id
        fields = [self.field(f"{rid}.{name}", f) for name, f in fields_of(record).items()]
        plain = [n for n in type(record).model_fields if n not in fields_of(record)]
        fields += [s for n in plain if (s := self.attribute(f"{rid}.{n}")) is not None]
        own = self.by_target.get(rid, [])
        status = max((f.status for f in fields if f.cause != "absent"), key=RANK.__getitem__, default="verified")
        if self.draft.mark(rid) is not None or any(i.severity == "blocking" for i in own):
            status = "unresolved"
        elif own and status == "verified":
            status = "flagged"
        return SettledRecord(record=record, status=status, fields=fields, issues=[i.line for i in own])

    def field(self, target: str, f: Field[object]) -> FieldStatus:
        """A note is commentary on the record, not a value, so the agent's own note doesn't flag it."""
        name = target.split(".", 1)[1]
        unsettled = self.attribute(target)
        if unsettled is not None:
            return unsettled
        if f.value is None:
            return FieldStatus(field=name, status="unresolved", cause="absent", reason=f.note)
        reasons = [i.message for i in self.by_target.get(target, [])]
        if f.basis == "inferred" and name != "notes":
            reasons.append(f"inferred: {f.note}")
        for ev in f.evidence:
            cell = self.cells.get(ev.anchor)
            if cell == "fuzzy":
                reasons.append(f"{ev.anchor} matched the PDF text only approximately")
            elif cell == "none":
                reasons.append(f"{ev.anchor} was read from the table image; the PDF text layer doesn't hold it")
            if _page(ev.anchor) in self.blank:
                reasons.append(f"page {_page(ev.anchor)} has no text layer")
        if reasons:
            return FieldStatus(field=name, status="flagged", reason="; ".join(reasons))
        return FieldStatus(field=name, status="verified")

    def attribute(self, target: str) -> FieldStatus | None:
        """Unresolved when a mark stands on a field or an id pointer such as variable_id, or it has a blocking issue
        (the last one is the reason)."""
        name = target.split(".", 1)[1]
        mark = self.draft.mark(target)
        if mark is not None:
            return FieldStatus(field=name, status="unresolved", cause=mark.cause, reason=mark.reason)
        failing = [i for i in self.by_target.get(target, []) if i.severity == "blocking"]
        if not failing:
            return None
        last = failing[-1]
        return FieldStatus(field=name, status="unresolved", cause=self.cause(last), reason=last.message)

    def cause(self, issue: Issue) -> Cause:
        if self.outcome == "budget_exhausted":
            return "budget_exhausted"
        return "grounding_failure" if issue.level == "evidence" else "validation_failure"


def _page(anchor: str) -> int:
    m = ANCHOR.match(anchor)
    return int(m.group(1)) if m else 0
