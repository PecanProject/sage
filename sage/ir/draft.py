"""draft.json: what the agent's files compile to at each check."""

from typing import Literal, get_args

from pydantic import BaseModel, Field, JsonValue

from sage.ir.schema import Field as IrField
from sage.ir.schema import Observation, Record, Variable, fields_of

AgentCause = Literal["absent", "ambiguous", "conflict", "schema_limitation", "blocked_prerequisite", "figure_only"]
"""Why the agent leaves a value unresolved. absent: the paper doesn't give it. ambiguous: the paper doesn't tie it to
one referent. conflict: the paper contradicts itself. schema_limitation: the IR can't hold it. blocked_prerequisite:
a record it needs is unusable. figure_only: shown only in a figure."""
CodeCause = Literal["grounding_failure", "validation_failure", "repeated_error", "budget_exhausted"]
"""Causes only code sets. grounding_failure / validation_failure: the last check error. repeated_error: the same error
came back too often. budget_exhausted: the run stopped before it was fixed."""
Cause = Literal[AgentCause, CodeCause]


RECORD_CAUSES: frozenset[AgentCause] = frozenset({"ambiguous", "conflict", "schema_limitation", "blocked_prerequisite"})
"""Causes that can sink a whole record whatever its values: the paper doesn't tie it down, contradicts itself, the IR
can't hold it, or a record it needs is unusable. figure_only sinks only a Variable, and absent only a record that
holds no value."""


class TableDecision(BaseModel):
    table_id: str
    status: Literal["expanded", "irrelevant"]
    reason: str | None = None
    observation_ids: list[str] = Field(default_factory=list)
    rows: list[int] = Field(default_factory=list)
    """The rows that gave at least one Observation."""
    skipped: list[str] = Field(default_factory=list)
    """Rows (`p7.t1.r4`) and cells the agent left out on purpose, such as an LSD or a totals row."""


class Unresolved(BaseModel):
    target: str = Field(description='A record id, "record_id.field", or a table id.')
    cause: Cause
    reason: str


class Mark(BaseModel):
    """An unresolved mark the agent passes to finish. It names a record, a null field, a table or an open issue's
    target; a field that holds a value is fixed or nulled, never marked."""

    target: str = Field(
        description='A record id, "record_id.field" whose value is null, a table id, or an issue target.'
    )
    cause: AgentCause
    reason: str


class Strike(BaseModel):
    message: str
    count: int = 1


class Overwrite(BaseModel):
    record_id: str
    field: str
    before: JsonValue
    after: JsonValue
    why: str


class Finish(BaseModel):
    summary: str


class Draft(BaseModel):
    records: dict[str, Record] = Field(default_factory=dict)
    tables: dict[str, TableDecision] = Field(default_factory=dict)
    unresolved: dict[str, Unresolved] = Field(default_factory=dict)
    overwrites: list[Overwrite] = Field(default_factory=list)
    finished: Finish | None = None

    def holds_value(self, target: str) -> bool:
        rid, _, name = target.partition(".")
        record = self.records.get(rid)
        if record is None or name not in type(record).model_fields:
            return False
        value = getattr(record, name)
        return (value.value if isinstance(value, IrField) else value) is not None

    def mark(self, target: str) -> Unresolved | None:
        """The unresolved mark that stands on `target`. An agent's mark on an attribute that holds a value does not
        stand: the value is still checked, so a mark can't settle an ungrounded value. An agent's mark on a whole
        record stands only for a cause that fits it (`mark_problem`). Code's marks always stand."""
        u = self.unresolved.get(target)
        if u is None or u.cause in get_args(CodeCause):
            return u
        if self.holds_value(target) or self.mark_problem(target, u.cause) is not None:
            return None
        return u

    def mark_problem(self, target: str, cause: Cause) -> str | None:
        """Why an agent's mark with `cause` can't stand on `target`, or None when it can."""
        rid, _, name = target.partition(".")
        record = self.records.get(rid)
        if cause == "figure_only":
            if not isinstance(record, Variable):
                what = f"a {record.type}" if record is not None else "not a record"
                return (
                    f"{target} is {what}; figure_only belongs on the Variable whose values are only in a figure. "
                    "Write that Variable in records/variables.yaml and mark it in records/unresolved.yaml"
                )
            measured = sum(isinstance(r, Observation) and r.variable_id == rid for r in self.records.values())
            if measured:
                return (
                    f"{rid} has {measured} Observations, so its values are not only in a figure; remove its mark from "
                    "records/unresolved.yaml, and write a separate Variable for a quantity that only a figure shows"
                )
            return None
        if record is None or name or cause in RECORD_CAUSES:
            return None
        if any(f.value is not None for f in fields_of(record).values()):
            return (
                f"{rid} holds values, so it isn't {cause}; in records/unresolved.yaml mark the null field "
                f"({rid}.<field>) instead"
            )
        return None
