"""Check and done on a paper's folder: what every driver calls, SAGE's own loop, an ACP agent or a terminal. The agent
writes `records/`, `tables/*.csv` and `pages/*.md`; this compiles them against the paper, runs the checker, keeps a
snapshot when they changed, and settles the run at `done`."""

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from sage.agents.contract import Finished
from sage.events import CheckRan, Counts, Event, Note, RecordSettled, RunFinished, RunStarted, now
from sage.ir.draft import Draft, Strike, Unresolved
from sage.ir.settled import Outcome, Settled
from sage.parse.document import Document
from sage.pipeline import gate
from sage.pipeline.compile import Compiled, compile_records
from sage.pipeline.paper_files import effective_document, refresh_paper_md
from sage.pipeline.quotes import quote_lines
from sage.pipeline.review import latest_reviews
from sage.pipeline.settle import settle
from sage.settings import Settings
from sage.store import files, workspace
from sage.store.models import Review, ReviewAdapter, RunInfo
from sage.store.paths import Home, RunPaths


class _Strikes(BaseModel):
    strikes: dict[str, Strike] = Field(default_factory=dict)
    shown: list[str] = Field(default_factory=list)
    """The blocking errors the last recorded check listed, by `Finding.key`: only those can count a strike."""


class _Marks(BaseModel):
    marks: dict[str, Unresolved] = Field(default_factory=dict)


class CheckReport(BaseModel):
    paper_id: str
    run_id: str | None
    records: int
    blocking: list[str]
    warnings: list[str]
    blocking_total: int
    warning_total: int
    unreadable: bool = False
    """A file isn't valid YAML or CSV, so what it holds wasn't read at all."""
    snapshot: int | None = None
    """The snapshot this check took, when the files had changed."""
    marked: list[str] = Field(default_factory=list)
    guidance: str | None = None
    reviews: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    """Once nothing blocks: why values would settle flagged, beyond the warnings, a few of each kind."""
    flag_total: int = 0

    def text(self) -> str:
        """The check as an agent reads it: the scientist's word first, then what blocks, the warnings, the flags, the
        totals."""
        lines = [f"guidance: {self.guidance}"] if self.guidance is not None else []
        lines += [f"review: {line}" for line in self.reviews]
        lines += self.blocking
        lines += [f"warning {line}" for line in self.warnings]
        if self.flags:
            lines.append("These values would settle flagged, for a person to check. Fix the ones you can:")
            lines += [f"flag {line}" for line in self.flags]
        snapshot = f" · snapshot {self.snapshot}" if self.snapshot is not None else ""
        marked = f" · marked unresolved after repeats: {', '.join(self.marked)}" if self.marked else ""
        warnings = f"{self.warning_total} warning{'' if self.warning_total == 1 else 's'}"
        flagged = f" · {self.flag_total} flagged" if self.flag_total else ""
        lines.append(f"{self.blocking_total} blocking · {warnings}{flagged} · {self.records} records{snapshot}{marked}")
        return "\n".join(lines)


class DoneReport(BaseModel):
    paper_id: str
    run_id: str
    accepted: bool
    check: CheckReport
    records: int = 0
    verified: int = 0
    flagged: int = 0
    unresolved: int = 0

    def text(self) -> str:
        if not self.accepted:
            return f"{self.check.text()}\nnot done: fix what blocks, or mark it in records/unresolved.yaml"
        return (
            f"done: {self.records} records, {self.verified} verified, {self.flagged} flagged, {self.unresolved} "
            "unresolved"
        )


@dataclass
class FolderWorkspace:
    """The paper's folder as SAGE's own agent sees it, through `agents.contract.Workspace`."""

    home: Home
    settings: Settings
    info: RunInfo

    @property
    def folder(self) -> Path:
        return self.home.paper(self.info.paper_id)

    def image(self, path: str) -> bytes | str:
        target = (self.folder / path).resolve()
        if not target.is_relative_to(self.folder.resolve()) or target.suffix != ".png":
            return f"{path}: view_image shows a .png in the paper's folder, such as pages/p3.png"
        if not files.exists(target):
            return f"no {path}; list pages/, tables/ or figures/ to see what there is"
        return files.read_bytes(target)

    def check(self, *, every: bool = False) -> str:
        return check(self.home, self.settings, self.info, self.info.paper_id, every=every).text()

    def done(self, summary: str) -> Finished:
        report = done(self.home, self.settings, self.info, summary)
        return Finished(accepted=report.accepted, text=report.text())


@dataclass
class _Checked:
    report: CheckReport
    evaluation: gate.Evaluation
    doc: Document


def agent_texts(home: Home, paper_id: str) -> dict[str, str]:
    return workspace.read_agent_files(home.paper(paper_id))


def check(home: Home, settings: Settings, info: RunInfo | None, paper_id: str, *, every: bool = False) -> CheckReport:
    """Check the folder. With an open run, the check is recorded: a snapshot when the files changed, the same-error
    count, the draft the UI shows while the run is open, and a `check.ran` event. `every` lists every finding rather
    than a few of each kind, for the first `error_list_cap` kinds of each severity."""
    return _check(home, settings, info, paper_id, record=info is not None, every=every).report


def done(home: Home, settings: Settings, info: RunInfo, summary: str) -> DoneReport:
    """Check once more and, when nothing blocks, settle the run: records.json, the document it was checked against,
    status finished. Refused while anything blocks, with the check that says why."""
    checked = _check(home, settings, info, info.paper_id, record=True)
    paths = home.run(info.paper_id, info.id)
    if not gate.finish(checked.evaluation, summary.strip() or "done"):
        return DoneReport(paper_id=info.paper_id, run_id=info.id, accepted=False, check=checked.report)
    files.write_model(paths.draft, checked.evaluation.draft)
    settled = settle(checked.evaluation.draft, checked.doc, run_id=info.id, outcome="finished")
    _save_settled(paths, info, settled)
    info.status = "finished"
    info.finished_at = now()
    info.pid = None
    files.write_model(paths.info, info)
    _place_quotes(home, info, settled, checked.doc, paths)
    _emit(paths, RunFinished(status="finished", summary=settled.summary))
    return DoneReport(
        paper_id=info.paper_id,
        run_id=info.id,
        accepted=True,
        check=checked.report,
        records=info.counts.records,
        verified=info.counts.verified,
        flagged=info.counts.flagged,
        unresolved=info.counts.unresolved,
    )


def opened(home: Home, info: RunInfo) -> RunInfo:
    """Write a new run's run.json and its `run.started` event. The folder carries on from the paper's last run, which
    `info.parent_id` names, so reviews of records it keeps unchanged still stand."""
    paths = home.run(info.paper_id, info.id)
    files.write_model(paths.info, info)
    _emit(paths, started(info))
    return info


def started(info: RunInfo, *, resumed: bool = False) -> RunStarted:
    """The `run.started` event every driver writes first. A resumed run carries on itself, so it continues no other."""
    return RunStarted(
        run_id=info.id,
        paper_id=info.paper_id,
        driver=info.driver,
        agent=info.agent,
        agent_model=info.agent_model,
        extraction_model=info.extraction_model,
        effort=info.effort,
        resumed=resumed,
        continued_from=None if resumed else info.parent_id,
        guidance=info.guidance,
    )


def stopped(home: Home, settings: Settings, info: RunInfo, reason: str) -> RunInfo:
    """End an open run before done. What the folder holds is settled, so the run shows what it found, and the files
    stay for the next run."""
    paths = home.run(info.paper_id, info.id)
    _emit(paths, Note(level="warning", text=reason))
    salvage(home, settings, info, "failed")
    info.status = "interrupted"
    info.finished_at = now()
    info.pid = None
    files.write_model(paths.info, info)
    _emit(paths, RunFinished(status="interrupted"))
    return info


def salvage(home: Home, settings: Settings, info: RunInfo, outcome: Outcome) -> None:
    """Settle what the folder holds for a run that ended without done: out of budget, failed or stopped. When the
    files can't be compiled or settling raises, the run gets an empty result instead, so every run that ends has
    records.json."""
    paths = home.run(info.paper_id, info.id)
    try:
        checked = _check(home, settings, info, info.paper_id, record=False)
        files.write_model(paths.document, checked.doc)
        settled = settle(checked.evaluation.draft, checked.doc, run_id=info.id, outcome=outcome)
    except Exception as e:
        _emit(paths, Note(level="warning", text=f"could not settle this run, so it has no records: {e}"))
        settled = Settled(run_id=info.id, paper_id=info.paper_id, outcome=outcome, records=[])
        checked = None
    _save_settled(paths, info, settled)
    files.write_model(paths.info, info)
    if checked is not None and settled.records:
        _place_quotes(home, info, settled, checked.doc, paths)


def live_draft(home: Home, settings: Settings, info: RunInfo) -> tuple[Draft, Document]:
    """The draft and document of an open run as its folder holds them now, so the UI follows an agent's edits between
    checks. Compiled like a check, but nothing is recorded in the run."""
    checked = _check(home, settings, info, info.paper_id, record=False)
    return checked.evaluation.draft, checked.doc


def _check(
    home: Home, settings: Settings, info: RunInfo | None, paper_id: str, *, record: bool, every: bool = False
) -> _Checked:
    doc, table_issues = effective_document(home, paper_id, settings)
    refresh_paper_md(home, paper_id, settings, doc=doc)
    texts = agent_texts(home, paper_id)
    records = {k: v for k, v in texts.items() if k.startswith("records/")}
    compiled = compile_records(records, doc)
    compiled.issues = [*table_issues, *compiled.issues]
    paths = home.run(paper_id, info.id) if info is not None else None
    strikes = files.read_model_or_none(paths.strikes, _Strikes) if paths is not None else None
    marks = files.read_model_or_none(paths.marks, _Marks) if paths is not None else None
    snaps = home.snapshots(paper_id)
    n, last = workspace.latest_snapshot(snaps)
    changed = workspace.digest(texts) != last
    before = workspace.read_snapshot(snaps, n) if changed and n else {}
    edited = frozenset(p for p in texts.keys() | before.keys() if texts.get(p) != before.get(p))
    evaluation = gate.evaluate(
        compiled,
        doc,
        code_marks=marks.marks if marks is not None else {},
        strikes=strikes.strikes if strikes is not None else {},
        changed=edited if record and changed and not compiled.unreadable else frozenset(),
        shown=frozenset(strikes.shown) if strikes is not None else frozenset(),
    )
    review_lines, review_findings = _reviews(home, paper_id, evaluation.draft)
    evaluation.findings = sorted(
        [*evaluation.findings, *review_findings], key=lambda f: (f.severity != "blocking", f.path, f.line)
    )
    kinds = None if every else settings.error_list_cap
    blocking, shown = _listed(evaluation.blocking, kinds)
    warnings, _ = _listed(evaluation.warnings, kinds)
    flagged = [] if evaluation.blocking else _flags(compiled, evaluation, doc)
    flags, _ = _listed(flagged, kinds)
    snapshot = None
    if record and paths is not None:
        if changed and texts and not compiled.unreadable:
            snapshot = workspace.take_snapshot(snaps, texts)
        files.write_model(paths.strikes, _Strikes(strikes=evaluation.strikes, shown=shown))
        files.write_model(paths.marks, _Marks(marks=evaluation.code_marks))
        files.write_model(paths.draft, evaluation.draft)
        files.write_text_if_changed(paths.document, doc.model_dump_json(indent=1))
    report = CheckReport(
        paper_id=paper_id,
        run_id=info.id if info is not None else None,
        records=len(evaluation.draft.records),
        blocking=blocking,
        warnings=warnings,
        blocking_total=len(evaluation.blocking),
        warning_total=len(evaluation.warnings),
        unreadable=compiled.unreadable,
        snapshot=snapshot,
        marked=evaluation.newly_marked,
        guidance=info.guidance if info is not None else None,
        reviews=review_lines,
        flags=flags,
        flag_total=len(flagged),
    )
    if record and paths is not None:
        _emit(
            paths,
            CheckRan(
                records=report.records,
                blocking=report.blocking_total,
                warnings=report.warning_total,
                changed=snapshot is not None,
                snapshot=snapshot,
                marked=report.marked,
                lines=[f.text for f in evaluation.blocking[:5]],
            ),
        )
    return _Checked(report=report, evaluation=evaluation, doc=doc)


def _flags(compiled: Compiled, evaluation: gate.Evaluation, doc: Document) -> list[gate.Finding]:
    """Why each field would settle flagged, for the fields no listed warning already explains: what code had to infer,
    and evidence the PDF's text layer doesn't hold."""
    warned = {f.target for f in evaluation.warnings}
    return [
        gate.finding_at(compiled, target, "warning", f.reason)
        for r in settle(evaluation.draft, doc, run_id="check", outcome="finished").records
        for f in r.fields
        if f.status == "flagged" and f.reason is not None and (target := f"{r.record.id}.{f.field}") not in warned
    ]


def _reviews(home: Home, paper_id: str, draft: Draft) -> tuple[list[str], list[gate.Finding]]:
    """A scientist's reviews of records the folder still holds as they were reviewed. A rejected record blocks until it
    is changed or deleted; an edit is a warning carrying the scientist's value until the record changes."""
    found, _ = files.read_jsonl(home.reviews(paper_id), ReviewAdapter)
    latest = latest_reviews(found)
    reviewed: dict[str, Settled | None] = {}
    lines: list[str] = []
    findings: list[gate.Finding] = []
    for rid, review in latest.items():
        if review.action == "accept" and review.note is None:
            continue
        record = draft.records.get(rid)
        if record is None:
            continue
        if review.run_id not in reviewed:
            reviewed[review.run_id] = files.read_model_or_none(home.run(paper_id, review.run_id).records, Settled)
        before = reviewed[review.run_id]
        old = before.record(rid) if before is not None else None
        if old is None or old.record != record:
            continue
        line = _review_line(review)
        lines.append(line)
        if review.action == "reject":
            findings.append(gate.Finding("records/", 1, rid, "blocking", f"{line}; fix it or delete it"))
        elif review.action == "edit":
            findings.append(
                gate.Finding(
                    "records/",
                    1,
                    rid,
                    "warning",
                    f"{line}; find it in the paper and cite it, or keep yours and say why in a note",
                )
            )
    return lines, findings


def _review_line(r: Review) -> str:
    what = f"{r.record_id}.{r.field}" if r.field is not None else r.record_id
    said = {"reject": "rejected", "edit": "edited", "accept": "accepted"}[r.action]
    value = f" to {r.value!r}" if r.action == "edit" and r.value is not None else ""
    note = f": {r.note}" if r.note is not None else ""
    return f"a scientist {said} {what}{value}{note}"


def _place_quotes(home: Home, info: RunInfo, settled: Settled, doc: Document, paths: RunPaths) -> None:
    try:
        quote_lines(home, info.paper_id, info.id, settled, doc)
    except Exception as e:  # only the precise quote marks are lost; the paper screen retries
        _emit(paths, Note(level="warning", text=f"could not find the quotes on the page: {e}"))


def _save_settled(paths: RunPaths, info: RunInfo, settled: Settled) -> None:
    """Write records.json, log each record's status, and put the counts and summary on `info` for the caller to
    save."""
    files.write_model(paths.records, settled)
    for r in settled.records:
        _emit(paths, RecordSettled(record_id=r.record.id, record_type=r.record.type, status=r.status))
    info.counts = _counts(settled)
    info.summary = settled.summary


def _counts(settled: Settled) -> Counts:
    statuses = [r.status for r in settled.records]
    return Counts(
        records=len(statuses),
        verified=statuses.count("verified"),
        flagged=statuses.count("flagged"),
        unresolved=statuses.count("unresolved"),
    )


def _emit(paths: RunPaths, event: Event) -> None:
    files.append_jsonl(paths.events, event)


PER_KIND = 3
"""Findings check lists of each kind; the rest of that kind are counted."""


def _listed(findings: list[gate.Finding], kinds: int | None) -> tuple[list[str], list[str]]:
    """The lines check prints, and the keys of the findings they show. Findings are grouped by kind, so a thousand of
    one kind can't hide a second kind: up to `PER_KIND` of each of the first `kinds` kinds, then counts of the rest.
    With `kinds` None, every finding."""
    if kinds is None:
        return [f.text for f in findings], [f.key for f in findings]
    groups: dict[str, list[gate.Finding]] = {}
    for f in findings:
        groups.setdefault(f.kind, []).append(f)
    lines: list[str] = []
    shown: list[str] = []
    listed = list(groups.values())
    for group in listed[:kinds]:
        lines += [f.text for f in group[:PER_KIND]]
        shown += [f.key for f in group[:PER_KIND]]
        if len(group) > PER_KIND:
            lines.append(f"  … and {len(group) - PER_KIND} more like this")
    rest = listed[kinds:]
    if rest:
        lines.append(f"… and {sum(len(g) for g in rest)} more, of {len(rest)} other kinds; check --all lists them")
    return lines, shown
