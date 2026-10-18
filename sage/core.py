"""Every user action. `cli.py` and `api.py` call only this module, so the CLI and the UI stay equal."""

import errno
import hashlib
import os
import re
import socket
import unicodedata
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, Field, JsonValue, SecretStr, TypeAdapter, ValidationError

from sage.agents.models import check_key
from sage.events import (
    STAGE_STEP,  # noqa: F401 - cli.py words the live stage with it
    Effort,
    Event,
    EventAdapter,
    ModelId,
    Stage,
    StageStarted,
    describe,  # noqa: F401 - cli.py prints events with it
)
from sage.ir.draft import Draft
from sage.ir.settled import Outcome, Settled
from sage.parse import pdf
from sage.parse.document import Document
from sage.pipeline.export import csv_zip, workbook, xlsx
from sage.pipeline.quotes import cites, quote_lines
from sage.pipeline.review import QueueItem, labels, latest_reviews, queue, standing
from sage.pipeline.run import execute, run_document
from sage.pipeline.settle import settle
from sage.settings import Settings, StoredSettings, config_path, env_names, locked_fields
from sage.store import files, proc
from sage.store.models import (
    PaperInfo,
    Review,
    ReviewAction,  # noqa: F401 - cli.py types the review action with it
    RunInfo,
    RunStatus,
)
from sage.store.paths import Home

ReviewAdapter: TypeAdapter[Review] = TypeAdapter(Review)
EffortAdapter: TypeAdapter[Effort] = TypeAdapter(Effort)
ModelIdAdapter: TypeAdapter[ModelId] = TypeAdapter(ModelId)

ErrorKind = Literal["not_found", "invalid", "conflict", "missing_key", "corrupt"]
"""corrupt: a file under SAGE_HOME or config.toml exists but won't parse."""


class ErrorBody(BaseModel):
    """How an error leaves either surface: the API's error response, and the CLI's under --json."""

    kind: ErrorKind
    message: str


class SageError(Exception):
    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def settings() -> Settings:
    """Read fresh on every call, so a change made in the UI takes effect without a restart."""
    # Read through the store first, so a config.toml that is binary, unreadable or a folder fails as corrupt.
    with _on_disk():
        files.read_toml(config_path())
    try:
        return Settings()
    except ValidationError as e:
        raise SageError("invalid", f"invalid setting: {_validation_message(e, locked_fields())}") from e


def listen(host: str, port: int) -> socket.socket:
    """A socket bound for the UI, so a port in use or a bad host fails before anything says it is serving."""
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as e:
        sock.close()
        kind: ErrorKind = "conflict" if e.errno == errno.EADDRINUSE else "invalid"
        raise SageError(kind, f"can't serve on {host}:{port}: {e.strerror or e}") from e
    return sock


def home() -> Home:
    return Home(settings().home_dir)


ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
"""Paper and run ids. They name folders, so one from outside is checked against this before it reaches a path."""


def _checked_id(what: Literal["paper", "run"], value: str) -> str:
    if ID.fullmatch(value) is None:
        raise SageError(
            "invalid",
            f"{value[:80]!r} is not a {what} id; ids are at most 128 letters, digits, '.', '_' and '-', and start with "
            "a letter or digit",
        )
    return value


@contextmanager
def _on_disk() -> Generator[None]:
    """A file that exists but won't parse is an error that names it. It never reads as missing."""
    try:
        yield
    except files.UnreadableError as e:
        raise SageError("corrupt", f"{e}. Fix or remove the file.") from e


def _read[M: BaseModel](path: Path, cls: type[M]) -> M | None:
    with _on_disk():
        return files.read_model_or_none(path, cls)


# Settings ---------------------------------------------------------------------------------------------------------

MASK = "••••"

SettingSource = Literal["env", "file", "default"]


class SettingsView(BaseModel):
    values: dict[str, JsonValue]
    sources: dict[str, SettingSource]
    locked: dict[str, str]
    """Field name -> the environment variable that sets it. Locked fields can't be edited here."""
    json_schema: dict[str, JsonValue]
    config_path: str


def settings_view() -> SettingsView:
    current = settings()
    with _on_disk():
        file_values = files.read_toml(config_path())
    locked = locked_fields()
    values: dict[str, JsonValue] = {}
    for name, value in current.model_dump(mode="json").items():
        raw = getattr(current, name)
        values[name] = _mask(raw) if isinstance(raw, SecretStr) else value
    sources: dict[str, SettingSource] = {
        name: "env" if name in locked else "file" if name in file_values else "default" for name in values
    }
    return SettingsView(
        values=values,
        sources=sources,
        locked=locked,
        json_schema=Settings.model_json_schema(),
        config_path=str(config_path()),
    )


def update_settings(changes: dict[str, JsonValue]) -> SettingsView:
    """A secret sent back masked is left as it is; a null or empty value removes the key, so the default applies
    again. Only the values being set are checked, and the rest of the file is written back as it was, so a bad value
    elsewhere, in the file or the environment, never blocks a change."""
    unknown = sorted(set(changes) - set(Settings.model_fields))
    if unknown:
        raise SageError("invalid", f"unknown setting(s): {', '.join(unknown)}")
    locked = locked_fields()
    blocked = sorted(k for k in changes if k in locked)
    if blocked:
        raise SageError("conflict", "set through the environment: " + ", ".join(f"{k} ({locked[k]})" for k in blocked))
    with _on_disk():
        stored = files.read_toml(config_path())
    given = {k: v for k, v in changes.items() if not (isinstance(v, str) and v.startswith(MASK))}
    setting = {k: v for k, v in given.items() if v is not None and v != ""}
    try:
        validated = StoredSettings.model_validate(setting)
    except ValidationError as e:
        raise SageError("invalid", _validation_message(e)) from e
    dumped = validated.model_dump(mode="json")
    for key in given.keys() - setting.keys():
        stored.pop(key, None)
    for key in setting:
        raw = getattr(validated, key)
        stored[key] = raw.get_secret_value() if isinstance(raw, SecretStr) else dumped[key]
    files.write_toml(config_path(), stored)
    try:
        return settings_view()
    except SageError as e:
        raise SageError(e.kind, f"saved, but {e.message}") from e


def config_get(key: str) -> JsonValue:
    view = settings_view()
    if key not in view.values:
        raise SageError("not_found", f"no setting named {key}; see `sage config list`")
    return view.values[key]


class KeyCheck(BaseModel):
    ok: bool
    message: str


def test_api_key() -> KeyCheck:
    """Ask Anthropic whether the saved key works, without spending anything."""
    key = require_api_key(settings())
    problem = check_key(key)
    if problem is not None:
        return KeyCheck(ok=False, message=problem)
    return KeyCheck(ok=True, message="Anthropic accepted the key")


def require_api_key(s: Settings) -> str:
    if s.anthropic_api_key is None or not s.anthropic_api_key.get_secret_value().strip():
        envs = " or ".join(env_names("anthropic_api_key", Settings.model_fields["anthropic_api_key"]))
        raise SageError(
            "missing_key",
            "No Anthropic API key is set. Add it in Settings in `sage ui`, or run "
            f"`sage config set anthropic_api_key <key>` (config key anthropic_api_key in {config_path()}, "
            f"or env {envs}).",
        )
    return s.anthropic_api_key.get_secret_value()


def _mask(secret: SecretStr) -> str | None:
    if not secret.get_secret_value():
        return None
    return MASK + secret.get_secret_value()[-4:]


def _validation_message(e: ValidationError, locked: dict[str, str] | None = None) -> str:
    """Each problem by field. A field the environment sets names its variable, and one from the file names the file."""
    where = locked or {}

    def source(field: str) -> str:
        return f" (from {where[field]})" if field in where else f" (in {config_path()})" if locked is not None else ""

    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc'])}{source(str(err['loc'][0]) if err['loc'] else '')}: {err['msg']}"
        for err in e.errors()
    )


# Papers -----------------------------------------------------------------------------------------------------------


class PaperSummary(BaseModel):
    paper: PaperInfo
    latest_run: RunInfo | None = None
    """The live run, else the newest."""
    run_count: int = 0
    data_run: RunInfo | None = None
    """The run whose records stand for the paper. See `_data_run`."""
    to_check: int | None = None
    """Unreviewed "Check this" and "Missing" items on `data_run`. None when no run has records."""
    looks_good: int | None = None
    """Unreviewed "Looks good" items on `data_run`, which `accept_looks_good` accepts in one call."""
    live_stage: Stage | None = None
    """The stage the live run is in. None when no run is live, or one is live but hasn't started a stage."""


class Added(BaseModel):
    filename: str
    paper: PaperInfo
    new: bool


class NotAdded(BaseModel):
    """A file in a batch that couldn't be added. The others still were."""

    filename: str
    error: ErrorBody


def add_paper(filename: str, data: bytes) -> Added | NotAdded:
    """Adding the same bytes again returns the existing paper."""
    name = Path(filename).name
    try:
        return _add(name, data)
    except SageError as e:
        return NotAdded(filename=name, error=ErrorBody(kind=e.kind, message=e.message))
    except OSError as e:
        error = ErrorBody(kind="corrupt", message=f"couldn't save {name}: {e.strerror or e}")
        return NotAdded(filename=name, error=error)


def add_paper_file(path: Path) -> Added | NotAdded:
    try:
        data = files.read_bytes(path)
    except OSError as e:
        error = ErrorBody(kind="not_found", message=f"can't read {path}: {e.strerror}")
        return NotAdded(filename=path.name, error=error)
    return add_paper(path.name, data)


def _add(filename: str, data: bytes) -> Added:
    if not data.startswith(b"%PDF"):
        raise SageError("invalid", f"{filename} is not a PDF")
    sha = hashlib.sha256(data).hexdigest()
    h = home()
    if (known := _paper_with(h, sha)) is not None:
        return Added(filename=filename, paper=known, new=False)
    base = _slug(re.sub(r"(\.pdf)+$", "", filename, flags=re.IGNORECASE))
    try:
        pages = pdf.page_count(data)
        document = pdf.parse(data, paper_id=base, sha256=sha)
        title = pdf.title(data, document)
    except pdf.PdfError as e:
        raise SageError("invalid", f"{filename}: {e}") from e
    # Parsing runs unlocked. The second look and the writes are one step, so the same file uploaded twice at once is
    # added once.
    with files.exclusive(h.add_lock):
        if (known := _paper_with(h, sha)) is not None:
            return Added(filename=filename, paper=known, new=False)
        paper_id = _claim_id(h, base)
        files.write_bytes(h.pdf(paper_id), data)
        files.write_model(h.document(paper_id), document.model_copy(update={"paper_id": paper_id}))
        info = PaperInfo(id=paper_id, filename=filename, sha256=sha, pages=pages, title=title)
        files.write_model(h.paper_json(paper_id), info)
    return Added(filename=filename, paper=info, new=True)


def _paper_with(h: Home, sha: str) -> PaperInfo | None:
    return next((p for p in _paper_infos(h) if p.sha256 == sha), None)


def papers() -> list[PaperSummary]:
    return [_summary(info) for info in _paper_infos(home())]


def paper_summary(paper_id: str) -> PaperSummary:
    return _summary(paper(paper_id))


def _summary(info: PaperInfo) -> PaperSummary:
    paper_runs = runs(info.id)
    shown = _data_run(paper_runs)
    latest = next((r for r in paper_runs if r.status in ("queued", "running")), paper_runs[0] if paper_runs else None)
    left = None if shown is None else _left(review_queue(shown.id))
    return PaperSummary(
        paper=info,
        latest_run=latest,
        run_count=len(paper_runs),
        data_run=shown,
        to_check=None if left is None else left.to_check,
        looks_good=None if left is None else left.looks_good,
        live_stage=_live_stage(latest) if latest is not None and latest.status in ("queued", "running") else None,
    )


def _live_stage(info: RunInfo) -> Stage | None:
    started = [e for e in events(info.id).events if isinstance(e, StageStarted)]
    return started[-1].stage if started else None


def _data_run(paper_runs: list[RunInfo]) -> RunInfo | None:
    """Among a paper's runs, newest first: the newest that ran to the end and found records, else the newest ended
    run that kept any. A failed run's salvage only stands in when no complete run has data. A resumed run keeps
    standing while it runs again, so its earlier records stay in view."""
    with_data = [r for r in paper_runs if (_resuming(r) or r.status not in ("queued", "running")) and r.counts.records]
    complete = (r for r in with_data if r.status in ("finished", "budget_exhausted"))
    return next(complete, with_data[0] if with_data else None)


def _resuming(r: RunInfo) -> bool:
    """A live run on its second attempt or later. One queued to resume has started before, and counts its resume
    only once its worker starts."""
    return r.resumes > 0 or (r.status == "queued" and r.started_at is not None)


def paper(paper_id: str) -> PaperInfo:
    info = _read(home().paper_json(_checked_id("paper", paper_id)), PaperInfo)
    if info is None:
        raise SageError("not_found", f"no paper {paper_id}; see `sage papers`")
    return info


def paper_pdf(paper_id: str) -> bytes:
    paper(paper_id)
    return files.read_bytes(home().pdf(paper_id))


def _paper_infos(h: Home) -> list[PaperInfo]:
    infos = [_read(h.paper_json(d.name), PaperInfo) for d in files.child_dirs(h.papers)]
    return sorted((i for i in infos if i is not None), key=lambda i: i.added_at, reverse=True)


_LETTERS = str.maketrans({"ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ø": "o", "Ø": "O", "ł": "l",
                          "Ł": "L", "đ": "d", "Đ": "D", "þ": "th", "Þ": "TH", "\u0131": "i"})  # fmt: skip
"""Latin letters NFKD doesn't split into a base letter and an accent."""


def _slug(stem: str) -> str:
    """Accents dropped rather than the letters ("évil" is "evil", "Straße" is "Strasse"); anything else that isn't an
    id character becomes "-". Short enough that `_claim_id`'s "-N" keeps it a valid id."""
    split = unicodedata.normalize("NFKD", stem.translate(_LETTERS))
    bare = "".join(c for c in split if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9._-]+", "-", bare)[:100].strip("-._") or "paper"


def _claim_id(h: Home, base: str) -> str:
    """Creates the paper's folder, so an upload running alongside with the same file name gets the next id."""
    candidate, n = base, 1
    while not files.make_new_dir(h.paper(candidate)):
        n += 1
        candidate = f"{base}-{n}"
    return candidate


# Runs -------------------------------------------------------------------------------------------------------------


def runs(paper_id: str | None = None) -> list[RunInfo]:
    """A run folder with no run.json is skipped; one whose run.json won't parse fails the list and names it."""
    h = home()
    if paper_id is not None:
        paper(paper_id)
    ids = [paper_id] if paper_id is not None else [d.name for d in files.child_dirs(h.papers)]
    out: list[RunInfo] = []
    for pid in ids:
        for d in files.child_dirs(h.runs(pid)):
            info = _read(h.run(pid, d.name).info, RunInfo)
            if info is not None:
                out.append(_live_status(info))
    return sorted(out, key=lambda r: r.created_at, reverse=True)


def run(run_id: str) -> RunInfo:
    """Reads only this run's run.json, so a broken file elsewhere doesn't hide it."""
    h = home()
    _checked_id("run", run_id)
    for d in files.child_dirs(h.papers):
        info = _read(h.run(d.name, run_id).info, RunInfo)
        if info is not None:
            return _live_status(info)
    raise SageError("not_found", f"no run {run_id}; see `sage runs`")


def _live_status(info: RunInfo) -> RunInfo:
    """A running or queued run whose worker is gone reads as interrupted: its pid is dead, or it has no pid but a
    finished_at, which workers from earlier builds left behind."""
    if info.status not in ("running", "queued"):
        return info
    gone = not proc.alive(info.pid) if info.pid is not None else info.finished_at is not None
    return info.model_copy(update={"status": "interrupted"}) if gone else info


def start_run(paper_id: str, *, model: str | None = None, effort: str | None = None) -> RunInfo:
    """Queue a run and start it in a detached worker, so it outlives the terminal or the server that asked."""
    s = settings()
    require_api_key(s)
    paper(paper_id)
    info = RunInfo(
        id=_new_run_id(),
        paper_id=paper_id,
        extraction_model=s.extraction_model if model is None else _model(model),
        table_model=s.table_model,
        effort=s.effort if effort is None else _effort(effort),
        max_turns=s.max_turns,
        max_cost_usd=s.max_cost_usd,
    )
    return _spawn(info, resume=False)


def resume_run(run_id: str, *, guidance: str | None = None, review_notes: bool = True) -> RunInfo:
    """Continue a run that didn't finish, from its saved draft. Its budget is the current settings' maximums minus
    what the run already spent, so a run with nothing left is refused. Raise the limit, or continue it, which gets a
    budget of its own. New guidance is added to what the run was already given. With `review_notes`, the scientist's
    edits, rejections and notes on the run's records go to the agent too."""
    s = settings()
    require_api_key(s)
    info = run(run_id)
    if info.status in ("running", "queued"):
        raise SageError("conflict", f"run {run_id} is still {info.status}")
    if info.status == "finished":
        raise SageError("conflict", f"run {run_id} already finished; continue it with guidance, or start a new run")
    spent = info.total_usage.cost_usd
    if info.turns >= s.max_turns or spent >= s.max_cost_usd:
        used = (
            f"{info.turns} of {s.max_turns} turns"
            if info.turns >= s.max_turns
            else f"${spent:.2f} of the ${s.max_cost_usd:.2f} budget"
        )
        raise SageError(
            "conflict",
            f"run {run_id} already used {used}; raise the limit in settings, or continue it with "
            "`sage run --continue`, which gets a budget of its own",
        )
    text = _guidance_text(guidance)
    if text is not None:
        info.guidance = text if info.guidance is None else f"{info.guidance}\n\n{text}"
    info.review_notes = _review_notes(info) if review_notes else []
    info.max_turns = s.max_turns
    info.max_cost_usd = s.max_cost_usd
    return _spawn(info, resume=True)


def continue_run(
    run_id: str,
    guidance: str | None,
    *,
    review_notes: bool = True,
    model: str | None = None,
    effort: str | None = None,
) -> RunInfo:
    """Start a new run from an ended run's draft and document, with the scientist's guidance on what to add or fix.
    The earlier run stays as it was. The new run has its own budget, and its model and effort default to the earlier
    run's. Reviews of records it leaves unchanged carry over to it (`standing`)."""
    s = settings()
    require_api_key(s)
    parent = run(run_id)
    if parent.status in ("running", "queued"):
        raise SageError("conflict", f"run {run_id} is still {parent.status}")
    text = _guidance_text(guidance)
    notes = _review_notes(parent) if review_notes else []
    if text is None and not notes and parent.status != "budget_exhausted":
        raise SageError("invalid", "say what the run should add or fix")
    draft = _read(home().run(parent.paper_id, run_id).draft, Draft)
    if draft is None:
        raise SageError("not_found", f"run {run_id} kept no draft to continue from; start a new run instead")
    doc = run_doc(run_id)
    info = RunInfo(
        id=_new_run_id(),
        paper_id=parent.paper_id,
        extraction_model=parent.extraction_model if model is None else _model(model),
        table_model=parent.table_model,
        effort=parent.effort if effort is None else _effort(effort),
        max_turns=s.max_turns,
        max_cost_usd=s.max_cost_usd,
        parent_id=parent.id,
        guidance=text,
        review_notes=notes,
    )
    return _spawn(info, resume=True, seed=(draft, doc))


def stop_run(run_id: str) -> RunInfo:
    """Interrupt a live run. Its worker settles what the draft holds and records the run as interrupted, so it shows
    what it found and `resume_run` can carry it on."""
    info = run(run_id)
    if info.status not in ("running", "queued") or info.pid is None:
        raise SageError("conflict", f"run {run_id} is {info.status}, not running")
    proc.interrupt(info.pid)
    return info


def _new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + os.urandom(2).hex()


def _guidance_text(guidance: str | None) -> str | None:
    text = (guidance or "").strip()
    return text or None


def _review_notes(info: RunInfo) -> list[Review]:
    found = latest_reviews(_standing(info, _settled(info)))
    return [r for r in found.values() if r.action != "accept" or r.note is not None]


def _effort(value: str) -> Effort:
    try:
        return EffortAdapter.validate_python(value)
    except ValidationError as e:
        raise SageError("invalid", f"effort must be one of {', '.join(get_args(Effort))}, not '{value}'") from e


def _model(value: str) -> ModelId:
    """Settings already checks extraction_model and table_model when it loads; this checks a run's --model."""
    try:
        return ModelIdAdapter.validate_python(value)
    except ValidationError as e:
        raise SageError("invalid", f"model {value!r}: {_validation_message(e).removeprefix(': ')}") from e


def _spawn(info: RunInfo, *, resume: bool, seed: tuple[Draft, Document] | None = None) -> RunInfo:
    """Refuse a second live run on the paper, then start the worker. The paper's lock makes the check and the queued
    run.json one step, so two starts at once can't both pass. run.json has one writer at a time: this process writes
    it queued, with the worker's pid so a worker that dies early reads as interrupted, and only then lets the worker
    start writing. `seed` is the draft and document a continued run starts from."""
    h = home()
    paths = h.run(info.paper_id, info.id)
    args = ["worker", info.id, *(["--resume"] if resume else [])]
    with files.exclusive(h.run_lock(info.paper_id)):
        live = [r for r in runs(info.paper_id) if r.status in ("running", "queued")]
        if live:
            raise SageError("conflict", f"run {live[0].id} is already {live[0].status} on {info.paper_id}")
        if seed is not None:
            files.write_model(paths.draft, seed[0])
            files.write_model(paths.document, seed[1])
        with proc.spawn_sage(args, paths.dir / "worker.log") as pid:
            info.status = "queued"
            info.pid = pid
            files.write_model(paths.info, info)
    return info


def work(run_id: str, *, resume: bool) -> RunInfo:
    """The detached worker's body: drive the run in this process, once the parent has written run.json."""
    proc.wait_for_parent()
    s = settings()
    info = run(run_id)
    return execute(home(), s, info, api_key=require_api_key(s), resume=resume)


class EventPage(BaseModel):
    events: list[Event]
    offset: int
    done: bool


def events(run_id: str, *, offset: int = 0) -> EventPage:
    """Events from byte `offset` on, and the offset to read from next. `done` is read before the events, so a page
    that says done holds every event the run wrote: a follower can stop on it without missing the last ones."""
    info = run(run_id)
    with _on_disk():
        found, end = files.read_jsonl(home().run(info.paper_id, run_id).events, EventAdapter, offset=offset)
    return EventPage(events=found, offset=end, done=info.status not in ("running", "queued"))


class AnchorBox(BaseModel):
    page: int
    bbox: tuple[float, float, float, float]
    text: str
    quotes: dict[str, list[tuple[float, float, float, float]]] = Field(
        default_factory=dict,
        description="For a text block, each quote cited in it with the boxes of the lines it covers, so the paper can "
        "mark the quoted words. A quote missing here draws as the whole block. Empty while the run is live.",
    )


class RecordsView(BaseModel):
    run: RunInfo
    settled: Settled
    provisional: bool
    """True while the run has no records.json yet: the draft so far, settled on the fly."""
    boxes: dict[str, AnchorBox]
    """Every anchor the evidence cites, resolved to its page and box for drawing on the PDF, in the document the run
    was extracted against."""
    labels: dict[str, str]
    """A short plain name for each record, e.g. "Yield, Agria, 2008"."""


# How to settle a run that wrote no records.json: a live run is read as if it finished now, so open errors show as
# validation or grounding failures; a run that stopped early keeps the reason it stopped.
_PROVISIONAL: dict[RunStatus, Outcome] = {
    "queued": "finished",
    "running": "finished",
    "finished": "finished",
    "budget_exhausted": "budget_exhausted",
    "failed": "failed",
    "interrupted": "failed",
}


def records(run_id: str) -> RecordsView:
    info = run(run_id)
    doc = run_doc(run_id)
    saved = _saved(info)
    provisional = saved is None
    found = _from_draft(info, doc) if saved is None else saved
    anchors = {a for a, _ in cites(found)}
    with _on_disk():
        lines = {} if provisional else quote_lines(home(), info.paper_id, run_id, found, doc)
    boxes: dict[str, AnchorBox] = {}
    for a in sorted(anchors):
        resolved = doc.resolve(a)
        if resolved is not None:
            boxes[a] = AnchorBox(page=resolved.page, bbox=resolved.bbox, text=resolved.text, quotes=lines.get(a, {}))
    return RecordsView(run=info, settled=found, provisional=provisional, boxes=boxes, labels=labels(found))


def _saved(info: RunInfo) -> Settled | None:
    """records.json, except while the run is live: a resumed run's file is from its last attempt, and its draft has
    moved on since."""
    if info.status in ("queued", "running"):
        return None
    return _read(home().run(info.paper_id, info.id).records, Settled)


def _from_draft(info: RunInfo, doc: Document) -> Settled:
    draft = _read(home().run(info.paper_id, info.id).draft, Draft) or Draft()
    return settle(draft, doc, run_id=info.id, outcome=_PROVISIONAL[info.status])


def _settled(info: RunInfo) -> Settled:
    saved = _saved(info)
    return _from_draft(info, run_doc(info.id)) if saved is None else saved


def document(paper_id: str) -> Document:
    paper(paper_id)
    doc = _read(home().document(paper_id), Document)
    if doc is None:
        raise SageError("not_found", f"paper {paper_id} has no document.json; add it again")
    return doc


def run_doc(run_id: str) -> Document:
    """The document a run's anchors point into, which a later reparse of the paper doesn't change."""
    info = run(run_id)
    with _on_disk():
        doc = run_document(home(), info.paper_id, run_id)
    if doc is None:
        raise SageError("not_found", f"paper {info.paper_id} has no document.json; add it again")
    return doc


# Review -----------------------------------------------------------------------------------------------------------


def reviews(paper_id: str) -> list[Review]:
    paper(paper_id)
    with _on_disk():
        found, _ = files.read_jsonl(home().reviews(paper_id), ReviewAdapter)
    return found


def _standing(info: RunInfo, settled: Settled) -> list[Review]:
    return standing(reviews(info.paper_id), settled, [_settled(a) for a in _ancestors(info)])


def _ancestors(info: RunInfo) -> list[RunInfo]:
    """The runs this one continues, nearest first, up to one whose folder was removed by hand."""
    out: list[RunInfo] = []
    parent = info.parent_id
    while parent is not None:
        try:
            up = run(parent)
        except SageError as e:
            if e.kind != "not_found":
                raise
            break
        out.append(up)
        parent = up.parent_id
    return out


def review_queue(run_id: str) -> list[QueueItem]:
    """Every record of the run, unresolved first, with its latest standing review."""
    info = run(run_id)
    settled = _settled(info)
    return queue(settled, _standing(info, settled))


class Left(BaseModel):
    to_check: int
    """Unreviewed items that are flagged or unresolved."""
    looks_good: int
    """Unreviewed items that are verified."""


def _left(items: list[QueueItem]) -> Left:
    open_items = [i for i in items if i.review is None]
    good = sum(1 for i in open_items if i.status == "verified")
    return Left(to_check=len(open_items) - good, looks_good=good)


class InboxEntry(BaseModel):
    paper: PaperInfo
    run: RunInfo
    to_check: int
    """Unreviewed "Check this" and "Missing" items."""
    looks_good: int
    """Unreviewed "Looks good" items."""
    total: int


def review_inbox() -> list[InboxEntry]:
    out: list[InboxEntry] = []
    for info in _paper_infos(home()):
        shown = _data_run(runs(info.id))
        if shown is not None:
            items = review_queue(shown.id)
            left = _left(items)
            out.append(
                InboxEntry(paper=info, run=shown, to_check=left.to_check, looks_good=left.looks_good, total=len(items))
            )
    return out


def add_review(review: Review) -> Review:
    info = run(review.run_id)
    rec = _settled(info).record(review.record_id)
    if rec is None:
        raise SageError("not_found", f"run {review.run_id} has no record {review.record_id}")
    if review.field is not None and review.field not in type(rec.record).model_fields:
        raise SageError("invalid", f"{rec.record.type} has no field '{review.field}'")
    if review.action == "edit" and (review.field is None or review.value is None):
        raise SageError("invalid", "an edit names the field and gives the corrected value")
    files.append_jsonl(home().reviews(info.paper_id), review)
    return review


def accept_looks_good(run_id: str) -> list[Review]:
    """Running it again accepts nothing new."""
    info = run(run_id)
    path = home().reviews(info.paper_id)
    accepted = [
        Review(run_id=run_id, record_id=i.record_id, action="accept")
        for i in review_queue(run_id)
        if i.status == "verified" and i.review is None
    ]
    for r in accepted:
        files.append_jsonl(path, r)
    return accepted


# Export -----------------------------------------------------------------------------------------------------------

ExportFormat = Literal["xlsx", "csv", "json"]
"""xlsx: the PEcAn curation workbook; csv: the same tabs as one CSV each, zipped; json: every record as settled."""


class ExportFile(BaseModel):
    data: bytes
    media_type: str
    filename: str


def write_export(path: Path, data: bytes) -> None:
    try:
        files.write_bytes(path, data)
    except OSError as e:
        raise SageError("invalid", f"can't write {path}: {e.strerror}") from e


def export(run_id: str, fmt: ExportFormat) -> ExportFile:
    info = run(run_id)
    settled = _settled(info)
    if fmt == "json":
        return ExportFile(
            data=settled.model_dump_json(indent=1).encode(),
            media_type="application/json",
            filename=f"{run_id}.json",
        )
    tabs = workbook(settled, run_doc(run_id), _standing(info, settled), info)
    if fmt == "xlsx":
        return ExportFile(
            data=xlsx(tabs),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=f"{run_id}.xlsx",
        )
    return ExportFile(data=csv_zip(tabs), media_type="application/zip", filename=f"{run_id}-csv.zip")
