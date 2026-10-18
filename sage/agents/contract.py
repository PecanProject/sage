"""What the agent's tools return, and the workspace they act on. The pipeline implements Workspace."""

from typing import Protocol

from pydantic import BaseModel, JsonValue

from sage.ir.draft import Draft, Mark, TableRoles
from sage.ir.schema import Record


class WriteOutcome(BaseModel):
    id: str
    ok: bool
    errors: list[str] = []
    warnings: list[str] = []
    marked_unresolved: list[str] = []


class WriteResult(BaseModel):
    results: list[WriteOutcome]
    open_blocking: int


class ExpandResult(BaseModel):
    table_id: str
    observations: int
    failed: int = 0
    empty_cells: int = 0
    skipped_rows: list[int] = []
    errors: list[str] = []
    samples: list[str] = []


class CheckResult(BaseModel):
    blocking: list[str]
    warnings: list[str]
    blocking_total: int
    warning_total: int


class ReadResult(BaseModel):
    records: list[Record]
    unknown: list[str] = []


class FinishResult(BaseModel):
    accepted: bool
    message: str
    blocking: list[str] = []


class Workspace(Protocol):
    @property
    def draft(self) -> Draft: ...

    def write(self, records: list[JsonValue]) -> WriteResult: ...

    def expand_table(self, table_id: str, roles: TableRoles | None, irrelevant_reason: str | None) -> ExpandResult: ...

    def check(self) -> CheckResult: ...

    def finish(self, summary: str, unresolved: list[Mark]) -> FinishResult: ...
