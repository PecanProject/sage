"""The boundary every driver passes through. `evaluate` turns the compiled files into findings that point at the
line to fix; `finish` refuses while anything blocks. Neither reads a file: `pipeline/extraction.py` does the I/O."""

from dataclasses import dataclass, field

from sage.ir.draft import Draft, Finish, Mark, Strike, Unresolved
from sage.parse.document import Document
from sage.pipeline.checker import Issue, check_all, open_issues
from sage.pipeline.checker.issues import Severity
from sage.pipeline.compile import RECORDS, UNRESOLVED, Compiled

SAME_ERROR_LIMIT = 3
"""Strikes after which an error's target is marked `repeated_error`: the agent has tried to fix it this often."""


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    target: str
    severity: Severity
    message: str

    @property
    def text(self) -> str:
        where = f"{self.path}:{self.line}"
        return f"{where}  {self.target}: {self.message}" if self.target else f"{where}  {self.message}"


@dataclass
class Evaluation:
    draft: Draft
    findings: list[Finding]
    strikes: dict[str, Strike]
    code_marks: dict[str, Unresolved]
    newly_marked: list[str] = field(default_factory=list)
    """Targets the same-error rule marked unresolved on this check."""

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocking"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]


def evaluate(
    compiled: Compiled,
    doc: Document,
    *,
    code_marks: dict[str, Unresolved],
    strikes: dict[str, Strike],
    changed: frozenset[str],
) -> Evaluation:
    """Check the compiled draft. `changed` holds the files edited since the last check. An error counts a strike only
    when the file it points at is one of them, an attempt at a fix, and after `SAME_ERROR_LIMIT` strikes its target is
    marked `repeated_error`; checking unchanged files again counts nothing. A code mark stands only while its target
    still has a blocking error, so a target fixed after it was marked counts again."""
    draft = compiled.draft
    marks, refused = _agent_marks(compiled, doc)
    draft.unresolved = {m.target: Unresolved(target=m.target, cause=m.cause, reason=m.reason) for m in marks}
    issues = open_issues(check_all(draft, doc, compiled.uncompiled), draft)
    still = {i.target for i in issues if i.severity == "blocking"}
    code_marks = {t: m for t, m in code_marks.items() if t in still}
    findings = [Finding(i.path, i.line, "", "blocking", i.message) for i in compiled.issues]
    findings += refused
    open_keys = {_key(i) for i in issues if i.severity == "blocking"}
    strikes = {k: s for k, s in strikes.items() if k in open_keys} if changed else dict(strikes)
    newly: list[str] = []
    for issue in issues:
        if issue.target in code_marks:
            continue
        finding = _finding(issue, compiled)
        tried = finding.path in changed or (finding.path == RECORDS and bool(changed))
        if issue.severity == "blocking" and tried and _strike(issue, strikes):
            code_marks[issue.target] = Unresolved(target=issue.target, cause="repeated_error", reason=issue.message)
            newly.append(issue.target)
            continue
        findings.append(finding)
    draft.unresolved |= code_marks
    findings.sort(key=lambda f: (f.severity != "blocking", f.path, f.line))
    return Evaluation(draft=draft, findings=findings, strikes=strikes, code_marks=code_marks, newly_marked=newly)


def finish(evaluation: Evaluation, summary: str) -> bool:
    """Accept the draft when nothing blocks, recording the summary."""
    if evaluation.blocking:
        return False
    evaluation.draft.finished = Finish(summary=summary)
    return True


def _finding(issue: Issue, compiled: Compiled) -> Finding:
    at = compiled.locate(issue.target)
    path, line = (at.path, at.line) if at is not None else (RECORDS, 1)
    return Finding(path, line, issue.target, issue.severity, issue.message)


def _key(issue: Issue) -> str:
    return f"{issue.target}|{issue.message}"


def _strike(issue: Issue, strikes: dict[str, Strike]) -> bool:
    strike = strikes.setdefault(_key(issue), Strike(message=issue.message, count=0))
    strike.count += 1
    return strike.count >= SAME_ERROR_LIMIT


def _agent_marks(compiled: Compiled, doc: Document) -> tuple[list[Mark], list[Finding]]:
    """The marks in unresolved.yaml that can stand, and a finding for each that can't: an unknown target, a target that
    holds a value, or a cause that doesn't fit it (as `Draft.mark_problem` says)."""
    draft = compiled.draft
    kept: list[Mark] = []
    refused: list[Finding] = []
    for m in compiled.marks:
        at = compiled.mark_at.get(m.target)
        line = at.line if at is not None else 1
        problem = _mark_problem(m, draft, doc)
        if problem is None:
            kept.append(m)
        else:
            refused.append(Finding(UNRESOLVED, line, m.target, "blocking", problem))
    return kept, refused


def _mark_problem(m: Mark, draft: Draft, doc: Document) -> str | None:
    rid, _, name = m.target.partition(".")
    record = draft.records.get(rid)
    known = (
        (record is not None and (not name or name in type(record).model_fields))
        or doc.table(m.target) is not None
        or m.target == "results"
    )
    if not known:
        return (
            "no such target; a mark names a record id, record_id.attribute, a table anchor, or results. If the "
            "record failed to compile, fix it first"
        )
    if draft.holds_value(m.target):
        return (
            "it holds a value, so it can't be marked unresolved; fix where it points, or make it "
            "[null, note: what you looked for]"
        )
    return draft.mark_problem(m.target, m.cause)
