"""records.json: the run's result. Code attaches every status; the model never sets one."""

from typing import Literal

from pydantic import BaseModel, Field

from sage.ir.draft import Cause, Overwrite, TableDecision, Unresolved
from sage.ir.schema import Record

Status = Literal["verified", "flagged", "unresolved"]
"""verified: every check passed on stated or derived evidence. flagged: it passed, but a person should look (inferred,
an approximate cell match, a checker warning). unresolved: not settled, with a cause."""
Outcome = Literal["finished", "budget_exhausted", "failed"]
"""How the run ended: the agent's finish was accepted, the turn or cost budget ran out, or the run failed."""


class FieldStatus(BaseModel):
    field: str
    status: Status
    cause: Cause | None = None
    reason: str | None = None


class SettledRecord(BaseModel):
    record: Record
    status: Status
    fields: list[FieldStatus] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    """Record-level checker lines that are still open."""


class Settled(BaseModel):
    run_id: str
    paper_id: str
    outcome: Outcome
    summary: str | None = None
    records: list[SettledRecord]
    tables: list[TableDecision] = Field(default_factory=list)
    unresolved: list[Unresolved] = Field(default_factory=list)
    """Marks whose target is not a record or one of its fields: a table, or `results`."""
    overwrites: list[Overwrite] = Field(default_factory=list)

    def record(self, record_id: str) -> SettledRecord | None:
        return next((r for r in self.records if r.record.id == record_id), None)
