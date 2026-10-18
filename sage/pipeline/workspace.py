"""The agent's tools, made real. Every change lands in the Draft and is saved before the tool returns."""

import re
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import JsonValue, ValidationError

from sage.agents.contract import CheckResult, ExpandResult, FinishResult, WriteOutcome, WriteResult
from sage.events import CheckFailed, Event
from sage.ir.draft import Draft, Finish, Mark, Strike, TableDecision, TableRoles, Unresolved
from sage.ir.schema import Evidence, Observation, Record, RecordAdapter
from sage.parse.document import Document
from sage.pipeline.checker import Issue, check_all, check_one, open_issues
from sage.pipeline.checker.dataset import check_dataset, recompute_dataset
from sage.pipeline.checker.record import recompute
from sage.pipeline.expand import expand

RECORD_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


@dataclass(frozen=True)
class Limits:
    same_error_limit: int
    error_list_cap: int


class DraftWorkspace:
    def __init__(
        self,
        *,
        doc: Document,
        draft: Draft,
        limits: Limits,
        save: Callable[[Draft], None],
        emit: Callable[[Event], None],
    ) -> None:
        self._doc = doc
        self._draft = draft
        self._limits = limits
        self._save = save
        self._emit = emit

    @property
    def draft(self) -> Draft:
        return self._draft

    def write(self, records: list[JsonValue]) -> WriteResult:
        """Store every record, let code derive what the whole draft decides (`recompute_dataset`), then check each
        one, so no record is reported for a value code fills in."""
        staged = [self._write_one(raw) for raw in records]
        self._draft.overwrites += recompute_dataset(self._draft)
        last = {rid: n for n, (_, rid) in enumerate(staged) if rid is not None}
        refs = [i for i in check_dataset(self._draft) if i.record_id in last]
        outcomes = [outcome for outcome, _ in staged]
        for n, (outcome, rid) in enumerate(staged):
            if rid is None or last[rid] != n:
                continue
            for issue in [*check_one(self._draft.records[rid], self._doc), *(i for i in refs if i.record_id == rid)]:
                self._report(outcome, issue)
        self._save(self._draft)
        for o in outcomes:
            if o.errors:
                self._emit(CheckFailed(record_id=o.id, errors=o.errors))
        cap = self._limits.error_list_cap
        shown = [o.model_copy(update={"errors": _capped(o.errors, cap)}) for o in outcomes]
        return WriteResult(results=shown, open_blocking=self._blocking_count())

    def _write_one(self, raw: JsonValue) -> tuple[WriteOutcome, str | None]:
        """Validate and store one record, or delete one. The id is returned only for a record that was stored."""
        if not isinstance(raw, dict):
            return WriteOutcome(id="?", ok=False, errors=["each item must be a record object"]), None
        if isinstance(drop := raw.get("delete"), str):
            removed = self._delete(drop)
            message = [] if removed else [f"no record '{drop}' to delete"]
            return WriteOutcome(id=drop, ok=removed, errors=message), None
        rid = raw.get("id")
        label = rid if isinstance(rid, str) else "?"
        try:
            record = RecordAdapter.validate_python(raw)
        except ValidationError as e:
            return WriteOutcome(id=label, ok=False, errors=self._schema_errors(e)), None
        if not RECORD_ID.match(record.id):
            message = f"id '{record.id}' must be letters, digits, '-' or '_' (no dots or spaces)"
            return WriteOutcome(id=label, ok=False, errors=[message]), None
        record = self._store(record)
        return WriteOutcome(id=record.id, ok=True), record.id

    def _delete(self, rid: str) -> bool:
        """Drop the record with the marks and strikes on it and its fields, so a record written later under the same
        id starts clean."""
        record = self._draft.records.pop(rid, None)
        if record is None:
            return False
        targets = {rid, *(f"{rid}.{name}" for name in type(record).model_fields)}
        self._draft.unresolved = {t: u for t, u in self._draft.unresolved.items() if t not in targets}
        self._draft.strikes = {k: v for k, v in self._draft.strikes.items() if k.partition("|")[0] not in targets}
        return True

    def _store(self, record: Record) -> Record:
        fixed, overwrites = recompute(record)
        self._draft.records[record.id] = fixed
        self._draft.overwrites += overwrites
        return fixed

    def _report(self, outcome: WriteOutcome, issue: Issue) -> None:
        if issue.severity == "warning":
            outcome.warnings.append(f"{issue.target}: {issue.message}")
            return
        if self._draft.mark(issue.target) is not None:
            return
        if self._strike(issue):
            outcome.marked_unresolved.append(issue.target)
            return
        outcome.ok = False
        outcome.errors.append(f"{issue.target}: {issue.message}")

    def _strike(self, issue: Issue) -> bool:
        key = f"{issue.target}|{issue.message}"
        strike = self._draft.strikes.setdefault(key, Strike(message=issue.message, count=0))
        strike.count += 1
        if strike.count < self._limits.same_error_limit:
            return False
        self._draft.unresolved[issue.target] = Unresolved(
            target=issue.target, cause="repeated_error", reason=issue.message
        )
        return True

    def expand_table(self, table_id: str, roles: TableRoles | None, irrelevant_reason: str | None) -> ExpandResult:
        table = self._doc.table(table_id)
        if table is None:
            known = ", ".join(f"{t.anchor} ({t.label})" for t in self._doc.tables) or "none"
            return ExpandResult(table_id=table_id, observations=0, errors=[f"no table '{table_id}'; tables: {known}"])
        if irrelevant_reason is not None:
            self._drop_expansion(table_id)
            self._draft.tables[table_id] = TableDecision(
                table_id=table_id, status="irrelevant", reason=irrelevant_reason
            )
            self._save(self._draft)
            return ExpandResult(table_id=table_id, observations=0)
        if roles is None:
            return ExpandResult(
                table_id=table_id, observations=0, errors=["pass roles, or irrelevant_reason if it holds no values"]
            )
        if not table.rows:
            message = (
                f"{table.label} has no transcribed cells (status {table.status}); look at it with "
                f"view({table.page}, ...) and write its Observations with block anchors instead"
            )
            return ExpandResult(table_id=table_id, observations=0, errors=[message])
        self._drop_expansion(table_id)
        notes = [Evidence(anchor=r.anchor, quote=r.text) for a in table.notes_anchors if (r := self._doc.resolve(a))]
        result = expand(table, roles, self._draft, notes)
        errors = list(result.errors)
        ids: list[str] = []
        failed = 0
        for obs in result.observations:
            stored = self._store(obs)
            ids.append(stored.id)
            issues = [i for i in check_one(stored, self._doc) if i.severity == "blocking"]
            if issues:
                failed += 1
                errors += [f"{i.target}: {i.message}" for i in issues]
        if ids:
            self._draft.tables[table_id] = TableDecision(
                table_id=table_id, status="expanded", roles=roles, observation_ids=ids
            )
        else:
            self._draft.tables.pop(table_id, None)
        self._save(self._draft)
        unique = list(dict.fromkeys(errors))
        if unique:
            self._emit(CheckFailed(record_id=table_id, errors=unique[: self._limits.error_list_cap]))
        return ExpandResult(
            table_id=table_id,
            observations=len(ids),
            failed=failed,
            empty_cells=result.empty_cells,
            skipped_rows=result.skipped_rows,
            errors=_capped(unique, self._limits.error_list_cap),
            samples=[_sample(o) for o in result.observations[:3]],
        )

    def _drop_expansion(self, table_id: str) -> None:
        previous = self._draft.tables.get(table_id)
        if previous is not None:
            for oid in previous.observation_ids:
                self._delete(oid)

    def check(self) -> CheckResult:
        issues = self._open()
        blocking = [i.line for i in issues if i.severity == "blocking"]
        warnings = [i.line for i in issues if i.severity == "warning"]
        cap = self._limits.error_list_cap
        return CheckResult(
            blocking=_capped(blocking, cap),
            warnings=_capped(warnings, cap),
            blocking_total=len(blocking),
            warning_total=len(warnings),
        )

    def finish(self, summary: str, unresolved: list[Mark]) -> FinishResult:
        bad = [m.target for m in unresolved if not self._known_target(m.target)]
        if bad:
            return FinishResult(
                accepted=False,
                message=(
                    f"unknown unresolved targets: {', '.join(bad)}; a target is a record id, record_id.field, "
                    "a table id, or the target of an open issue"
                ),
            )
        valued = [m.target for m in unresolved if self._draft.holds_value(m.target)]
        if valued:
            one = len(valued) == 1
            return FinishResult(
                accepted=False,
                message=(
                    f"{', '.join(valued)} {'holds a value' if one else 'hold values'}, so "
                    f"{'it' if one else 'they'} can't be marked unresolved. Fix the evidence, or set the value null "
                    "with a note saying why the paper doesn't give it"
                ),
            )
        misfit = [p for m in unresolved if (p := self._draft.mark_problem(m.target, m.cause)) is not None]
        if misfit:
            return FinishResult(accepted=False, message="; ".join(misfit))
        for m in unresolved:
            self._draft.unresolved[m.target] = Unresolved(target=m.target, cause=m.cause, reason=m.reason)
        blocking = [i.line for i in self._open() if i.severity == "blocking"]
        if blocking:
            self._save(self._draft)
            return FinishResult(
                accepted=False,
                message=(
                    f"{len(blocking)} blocking issue{'s are' if len(blocking) != 1 else ' is'} open. Fix each one, "
                    "set a value the paper doesn't give to null with a note, or list the target in unresolved with "
                    "a cause and reason"
                ),
                blocking=_capped(blocking, self._limits.error_list_cap),
            )
        self._draft.finished = Finish(summary=summary)
        self._save(self._draft)
        return FinishResult(accepted=True, message="finished")

    def _open(self) -> list[Issue]:
        return open_issues(check_all(self._draft, self._doc, cap=self._limits.error_list_cap), self._draft)

    def _blocking_count(self) -> int:
        return sum(1 for i in self._open() if i.severity == "blocking")

    def _known_target(self, target: str) -> bool:
        rid, _, name = target.partition(".")
        record = self._draft.records.get(rid)
        return (
            (record is not None and (not name or name in type(record).model_fields))
            or self._doc.table(target) is not None
            or target in {i.target for i in self._open()}
        )

    def _schema_errors(self, e: ValidationError) -> list[str]:
        lines = [
            f"{loc}: {err['msg']}" if (loc := ".".join(str(p) for p in err["loc"])) else err["msg"]
            for err in e.errors()
        ]
        return _capped(lines, self._limits.error_list_cap)


def _capped(lines: list[str], cap: int) -> list[str]:
    return lines if len(lines) <= cap else [*lines[:cap], f"... and {len(lines) - cap} more"]


def _sample(o: Observation) -> str:
    value = o.value.value
    shown = f"{value.reported_text} {value.reported_units}".strip() if value is not None else "null"
    when = o.temporal_info.value.reported_text if o.temporal_info.value is not None else "no time"
    return f"{o.id}: {o.variable_id} | {o.treatment_id} | {shown} | {when}"
