"""The boundary every driver passes through. `evaluate` turns the compiled files into findings that point at the
line to fix; `finish` refuses while anything blocks. Neither reads a file: `pipeline/extraction.py` does the I/O."""

import re
from dataclasses import dataclass, field

from sage.ir.draft import Draft, Finish, Mark, Strike, Unresolved
from sage.parse.document import Document
from sage.pipeline.checker import Issue, check_all, open_issues
from sage.pipeline.checker.issues import Severity
from sage.pipeline.compile import RECORDS, UNRESOLVED, Compiled

SAME_ERROR_LIMIT = 3
"""Strikes after which an error's target is marked `repeated_error`: the agent has tried to fix it this often."""
BLOCK_CAUSES = frozenset({"ambiguous", "conflict", "schema_limitation"})
"""The causes that can excuse a Results block's numbers: what else would leave a reported number out is a value to
record."""
_LITERAL = re.compile(r"'[^']*'|\"[^\"]*\"|[\w.-]*\d[\w.-]*")
"""What differs between two findings of one kind: quoted text, and ids, anchors and numbers."""


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

    @property
    def key(self) -> str:
        """The same error on the same target, across checks."""
        return f"{self.target}|{self.message}"

    @property
    def kind(self) -> str:
        """The rule it breaks, whatever the target: the message with its literals taken out."""
        return _LITERAL.sub("…", self.message)


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
    shown: frozenset[str],
) -> Evaluation:
    """Check the compiled draft. `changed` holds the files edited since the last check, and `shown` the errors that
    check listed. An error counts a strike only when the agent was shown it and edited its file since, an attempt at a
    fix, and after `SAME_ERROR_LIMIT` strikes its target is marked `repeated_error`; checking unchanged files again
    counts nothing, and neither does an error the agent never saw. A code mark stands only while its target
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
        finding = finding_at(compiled, issue.target, issue.severity, issue.message)
        edited = finding.path in changed or (finding.path == RECORDS and bool(changed))
        tried = edited and finding.key in shown
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


def finding_at(compiled: Compiled, target: str, severity: Severity, message: str) -> Finding:
    """A finding at the line that writes `target`."""
    at = compiled.locate(target)
    path, line = (at.path, at.line) if at is not None else (RECORDS, 1)
    return Finding(path, line, target, severity, message)


def _key(issue: Issue) -> str:
    return Finding("", 0, issue.target, issue.severity, issue.message).key


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
    if record is None and doc.table(m.target) is not None:
        if m.cause == "schema_limitation":
            return None
        return (
            f"a table is mapped in records/observations/{m.target}.yaml, or decided there with irrelevant: why when it "
            "holds no experiment values; mark it here only as schema_limitation, for site-level data the records "
            "can't hold, such as weather"
        )
    if record is None and ".t" not in m.target and doc.resolve(m.target) is not None:
        if m.cause in BLOCK_CAUSES:
            return None
        return (
            f"a Results block holds numbers the records leave out; mark it {', '.join(sorted(BLOCK_CAUSES))}, and "
            "record the experiment values it reports"
        )
    known = record is not None and (not name or name in type(record).model_fields)
    if not known:
        return (
            "no such target; a mark names a record id, record_id.attribute, a table anchor, or a text block a warning "
            "points at. If the record failed to compile, fix it first"
        )
    if draft.holds_value(m.target):
        return (
            "it holds a value, so it can't be marked unresolved; fix where it points, or make it "
            "[null, note: what you looked for]"
        )
    return draft.mark_problem(m.target, m.cause)
