"""The agent's workspace: draft.json. It holds the state; the conversation doesn't."""

from typing import Literal, get_args

from pydantic import BaseModel, Field, JsonValue, model_validator

from sage.ir.schema import EffectScope, Evidence, Model, Observation, Record, Variable, fields_of
from sage.ir.schema import Field as IrField

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


ColumnRole = Literal["treatment", "factor", "variable", "time", "depth", "unit", "n", "value", "ignore"]


class Column(Model):
    col: int
    role: ColumnRole
    factor: str | None = Field(default=None, description="role=factor: the factor this column's cells are levels of.")
    treatment_id: str | None = Field(default=None, description="role=value: the treatment every cell here belongs to.")
    variable_id: str | None = Field(default=None, description="role=value: the variable every cell here measures.")
    time: str | None = Field(
        default=None, description='role=value: when every cell here was measured, e.g. "111 days after transplanting".'
    )
    depth: str | None = Field(default=None, description='role=value: the soil depth of every cell here, "0-15 cm".')
    factor_levels: dict[str, str] = Field(
        default_factory=dict,
        description='role=value: factor levels every cell here shares, e.g. {"location": "Arlington"} under an '
        '"Arlington" header. Added to the row\'s treatment or factor levels to find the Treatment.',
    )

    @model_validator(mode="before")
    @classmethod
    def _table_keys_in_a_column(cls, data: object) -> object:
        """Say where a whole-table key belongs, rather than pydantic's bare "extra inputs are not permitted"."""
        if isinstance(data, dict):
            misplaced = sorted(set(data) & (set(TableRoles.model_fields) - set(Column.model_fields)))
            if misplaced:
                raise ValueError(f"{', '.join(misplaced)} go at the top level of roles, not inside a column")
        return data


class TableRoles(Model):
    """What a table means. Code reads it and turns every value cell into an Observation."""

    columns: list[Column]
    header_rows: int | None = Field(
        default=None, ge=0, description="Header row count, if the transcription's is wrong."
    )
    skip_rows: list[int] = Field(default_factory=list, description="Data rows that hold no values (section labels).")
    citation_id: str
    site_id: str
    method_id: str | None = Field(default=None, description="The method for every variable, unless method_by_variable.")
    method_by_variable: dict[str, str] = Field(default_factory=dict, description="variable_id -> method_id.")
    species_id: str | None = None
    crop_id: str | None = None
    treatment_by_label: dict[str, str] = Field(
        default_factory=dict, description="role=treatment cell text -> treatment_id, when names differ."
    )
    variable_by_label: dict[str, str] = Field(
        default_factory=dict, description="role=variable cell text -> variable_id, when names differ."
    )
    statistic: str | None = Field(
        default=None,
        description='What "±" or brackets in a cell mean, e.g. "SE", only when no header, caption or scope_evidence '
        "says. It is then marked inferred.",
    )
    letters: str | None = Field(
        default=None,
        description='What letters after a value mean, e.g. "Tukey HSD, P<0.05", only when no header, caption or '
        "scope_evidence says. It is then marked inferred.",
    )
    time_meaning: str | None = Field(
        default=None, description='How to read role=time cells, e.g. "days after transplanting".'
    )
    default_time: str | None = Field(default=None, description="When the values were measured, if no column says.")
    default_depth: str | None = Field(default=None, description='The soil depth of every value, e.g. "0-30 cm".')
    n: IrField[int] | None = Field(
        default=None,
        description="The replicates each value summarises, with the evidence that states it (the methods' 'four "
        "replicates'), unless a column has role=n. Null with a note when the paper doesn't say.",
    )
    effect_scope: EffectScope = "treatment_mean"
    aggregated_over_factors: list[str] = Field(default_factory=list)
    scope_evidence: list[Evidence] = Field(
        default_factory=list,
        description="Where the paper says what the values are means of, e.g. the table note. Without it the scope is "
        "null.",
    )
    is_raw_replicate_level: bool = False


class TableDecision(BaseModel):
    table_id: str
    status: Literal["expanded", "irrelevant"]
    reason: str | None = None
    roles: TableRoles | None = None
    observation_ids: list[str] = Field(default_factory=list)


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
    strikes: dict[str, Strike] = Field(default_factory=dict)
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
                    "Write that Variable and mark it"
                )
            measured = sum(isinstance(r, Observation) and r.variable_id == rid for r in self.records.values())
            if measured:
                return (
                    f"{rid} has {measured} Observations, so its values are not only in a figure; leave it unmarked, "
                    "and write a separate Variable for a quantity that only a figure shows"
                )
            return None
        if record is None or name or cause in RECORD_CAUSES:
            return None
        if any(f.value is not None for f in fields_of(record).values()):
            return f"{rid} holds values, so it isn't {cause}; mark the null field (record_id.field) instead"
        return None
