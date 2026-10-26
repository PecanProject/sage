"""What a run's files mean now. run.json says what its writer last wrote, so a running run whose runner is gone reads as
interrupted here. A run that has no records.json yet is settled from its folder. This module also decides which run's
records stand for a paper."""

from sage.ir.draft import Draft
from sage.ir.settled import Outcome, Settled
from sage.parse.document import Document
from sage.pipeline import extraction
from sage.pipeline.run import run_document
from sage.pipeline.settle import settle
from sage.settings import Settings
from sage.store import files, proc
from sage.store.models import RunInfo, RunStatus
from sage.store.paths import Home

# How to settle a run that wrote no records.json. A live run is settled as if it finished now, so its open errors
# show as validation or grounding failures. A run that stopped early keeps the reason it stopped.
_PROVISIONAL: dict[RunStatus, Outcome] = {
    "queued": "finished",
    "running": "finished",
    "finished": "finished",
    "budget_exhausted": "budget_exhausted",
    "failed": "failed",
    "interrupted": "failed",
}


class MissingDocumentError(Exception):
    """Neither the run nor its paper has a document.json, so nothing the run cites can be resolved."""

    def __init__(self, paper_id: str) -> None:
        super().__init__(f"paper {paper_id} has no document.json; add it again")


def live(info: RunInfo | None) -> bool:
    return info is not None and info.status in ("queued", "running")


def listed(home: Home, paper_id: str | None = None) -> list[RunInfo]:
    """The paper's runs, or every paper's, newest first. A run folder with no run.json is skipped. A run.json that
    won't parse raises UnreadableError."""
    ids = [paper_id] if paper_id is not None else [d.name for d in files.child_dirs(home.papers)]
    found = [
        live_status(info)
        for pid in ids
        for d in files.child_dirs(home.runs(pid))
        if (info := files.read_model_or_none(home.run(pid, d.name).info, RunInfo)) is not None
    ]
    return sorted(found, key=lambda r: r.created_at, reverse=True)


def find(home: Home, run_id: str) -> RunInfo | None:
    """Reads only this run's run.json, so a broken file elsewhere doesn't hide it."""
    for d in files.child_dirs(home.papers):
        info = files.read_model_or_none(home.run(d.name, run_id).info, RunInfo)
        if info is not None:
            return live_status(info)
    return None


def live_status(info: RunInfo) -> RunInfo:
    """A running run whose runner is gone reads as interrupted. A queued one stays queued: the next runner, started by
    the next run or by `sage ui`, takes it on."""
    if info.driver == "here":
        return info
    if info.status == "running" and (info.pid is None or not proc.alive(info.pid)):
        return info.model_copy(update={"status": "interrupted"})
    return info


def open_run(paper_runs: list[RunInfo]) -> RunInfo | None:
    return next((r for r in paper_runs if live(r)), None)


def current(paper_runs: list[RunInfo]) -> RunInfo | None:
    """The live run, else the newest."""
    return next((r for r in paper_runs if live(r)), paper_runs[0] if paper_runs else None)


def data_run(paper_runs: list[RunInfo]) -> RunInfo | None:
    """Among a paper's runs, newest first: the newest that ran to the end and found records, else the newest ended
    run that kept any. A failed run's salvage only stands in when no complete run has data. A resumed run keeps
    standing while it runs again, so its earlier records stay in view."""
    with_data = [r for r in paper_runs if (_resuming(r) or not live(r)) and r.counts.records]
    complete = (r for r in with_data if r.status in ("finished", "budget_exhausted"))
    return next(complete, with_data[0] if with_data else None)


def _resuming(r: RunInfo) -> bool:
    """A live run on its second attempt or later. One queued to resume has started before, and counts its resume
    only once the runner starts it."""
    return r.resumes > 0 or (r.status == "queued" and r.started_at is not None)


def ancestors(home: Home, info: RunInfo) -> list[RunInfo]:
    """The runs this one continues, nearest first, up to one whose folder was removed by hand."""
    out: list[RunInfo] = []
    parent = info.parent_id
    while parent is not None and (up := find(home, parent)) is not None:
        out.append(up)
        parent = up.parent_id
    return out


def document(home: Home, info: RunInfo) -> Document:
    """The document the run's anchors point into, which a later reparse of the paper doesn't change."""
    doc = run_document(home, info.paper_id, info.id)
    if doc is None:
        raise MissingDocumentError(info.paper_id)
    return doc


def saved(home: Home, info: RunInfo) -> Settled | None:
    """records.json, except while the run is live: a resumed run's file is from its last attempt, and its draft has
    moved on since."""
    if live(info):
        return None
    return files.read_model_or_none(home.run(info.paper_id, info.id).records, Settled)


def settled(home: Home, settings: Settings, info: RunInfo) -> Settled:
    found = saved(home, info)
    return provisional(home, settings, info)[0] if found is None else found


def provisional(home: Home, settings: Settings, info: RunInfo) -> tuple[Settled, Document]:
    """A run with no records.json, settled now, and the document it cites. A live run is compiled from its folder as
    it is now, so the screen follows an agent's edits between checks. A run that ended, or a live one whose folder
    can't be read at this moment, reads the draft of the last check."""
    found = _live_draft(home, settings, info) if live(info) else None
    if found is None:
        draft = files.read_model_or_none(home.run(info.paper_id, info.id).draft, Draft) or Draft()
        found = draft, document(home, info)
    draft, doc = found
    return settle(draft, doc, run_id=info.id, outcome=_PROVISIONAL[info.status]), doc


def _live_draft(home: Home, settings: Settings, info: RunInfo) -> tuple[Draft, Document] | None:
    """None when an agent's edit is in flight. A file can go between listing and reading it (OSError), or be caught
    half written in the middle of a UTF-8 character (UnreadableError). YAML that doesn't parse never raises: compile
    reports it as findings. Any other exception is a bug, and it surfaces."""
    try:
        return extraction.live_draft(home, settings, info)
    except (OSError, files.UnreadableError):
        return None
