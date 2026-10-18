"""Drive one run: parse (cached), the table pass (cached), the agent under its budget, then settle. Each step saves
what it did before the next starts, so a crash leaves a run that `sage run --resume` picks up."""

import asyncio
import os
import traceback
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field

from sage.agents import extractor, models, tables
from sage.events import (
    Counts,
    Event,
    Note,
    RecordSettled,
    RunError,
    RunFinished,
    RunStarted,
    Stage,
    StageFinished,
    StageStarted,
    Usage,
    UsageEvent,
    now,
)
from sage.ir.draft import Draft
from sage.ir.settled import Outcome, Settled
from sage.parse import pdf
from sage.parse.document import PARSER_VERSION, Document, Table, TablePass, carry_tables
from sage.parse.grounding import GROUNDER_VERSION, Grid, attach_notes, ground, mark_table_blocks
from sage.pipeline.quotes import quote_lines
from sage.pipeline.settle import settle
from sage.pipeline.workspace import DraftWorkspace, Limits
from sage.settings import Settings
from sage.store import files
from sage.store.models import PaperInfo, RunInfo
from sage.store.paths import Home, RunPaths

REGION_PAD = 4.0
"""Points added around a table region before cropping, so edge rules and minus signs stay in the image."""


class AbortError(Exception):
    pass


@dataclass
class Run:
    home: Home
    settings: Settings
    info: RunInfo
    api_key: str
    resume: bool
    base_usage: Usage = field(init=False)
    base_turns: int = field(init=False)

    def __post_init__(self) -> None:
        self.base_usage = self.info.usage if self.resume else Usage()
        self.base_turns = self.info.turns if self.resume else 0

    @property
    def paths(self) -> RunPaths:
        return self.home.run(self.info.paper_id, self.info.id)

    def emit(self, event: Event) -> None:
        files.append_jsonl(self.paths.events, event)
        if isinstance(event, UsageEvent) and event.role == extractor.ROLE:
            self.info.usage = self.base_usage + event.total
            self.info.turns = self.base_turns + event.turn
            self.save()

    def save(self) -> None:
        files.write_model(self.paths.info, self.info)


def execute(home: Home, settings: Settings, info: RunInfo, *, api_key: str, resume: bool) -> RunInfo:
    """Run to the end in this process and return the final run.json. Any error is recorded, never raised, and
    run.json never keeps `running` or `queued` once this returns or unwinds: a Ctrl-C or exit leaves `interrupted`,
    anything else `failed`. A BaseException is caught too, because a Rust panic in pydantic-core
    (`pyo3_runtime.PanicException`) derives from it."""
    run = Run(home=home, settings=settings, info=info, api_key=api_key, resume=resume)
    resumed = resume and info.started_at is not None
    info.status = "running"
    info.pid = os.getpid()
    info.started_at = info.started_at or now()
    info.finished_at = None
    info.error = None
    if resumed:
        info.resumes += 1
    run.save()
    try:
        run.emit(
            RunStarted(
                run_id=info.id,
                paper_id=info.paper_id,
                extraction_model=info.extraction_model,
                table_model=info.table_model,
                effort=info.effort,
                resumed=resumed,
                continued_from=None if resumed else info.parent_id,
                guidance=info.guidance,
            )
        )
        asyncio.run(_drive(run))
    except (KeyboardInterrupt, SystemExit):
        info.status = "interrupted"
        run.emit(Note(level="warning", text="stopped before it finished; resume it to carry on"))
        _salvage(run)
        raise
    except BaseException as e:
        message = str(e) if isinstance(e, AbortError) else f"{type(e).__name__}: {e}"
        info.status = "failed"
        info.error = message
        run.emit(RunError(message=message))
        if not isinstance(e, AbortError):
            run.emit(Note(level="warning", text=traceback.format_exc()[-4000:]))
        _salvage(run)
    finally:
        if info.status in ("running", "queued"):
            info.status = "interrupted"
        info.finished_at = now()
        info.pid = None
        run.save()
    return info


async def _drive(run: Run) -> None:
    info = run.info
    data = files.read_bytes(run.home.pdf(info.paper_id))
    paper = files.read_model(run.home.paper_json(info.paper_id), PaperInfo)

    with _stage(run, "parse"):
        doc = _document(run, data)

    with _stage(run, "tables") as finished:
        done, used = await _table_pass(run, doc, data)
        if done is not doc and not run.resume:
            files.write_model(run.home.document(info.paper_id), done)
        doc = done
        files.write_model(run.paths.document, doc)
        info.table_usage += used
        run.save()
        finished.detail = _table_summary(doc)

    with _stage(run, "agent") as finished:
        result = await _agent(run, doc, data, paper)
        # The response that trips a usage limit is billed but emits no usage event, so the result's tally is the one
        # that counts against the budget.
        info.usage = run.base_usage + result.usage
        info.turns = run.base_turns + result.turns
        run.save()
        if result.outcome == "failed":
            raise AbortError(result.message or "the agent failed")
        turns = f"{result.turns} turn{'' if result.turns == 1 else 's'}"
        finished.detail = f"{result.outcome} after {turns}, ${result.usage.cost_usd:.4f}"

    _settle(run, doc, result.outcome)
    info.status = result.outcome
    run.emit(RunFinished(status=result.outcome, summary=info.summary, limit=info.limit))


@contextmanager
def _stage(run: Run, stage: Stage) -> Generator[StageFinished]:
    """StageStarted on entry and StageFinished on a clean exit only, so a stage that raised reads as the one that
    failed. What the stage fills in on the yielded event lands on the finish."""
    run.emit(StageStarted(stage=stage))
    finished = StageFinished(stage=stage)
    yield finished
    run.emit(finished.model_copy(update={"ts": now()}))


def _document(run: Run, data: bytes) -> Document:
    """The document the run extracts against. A resumed run keeps the one it started on (`run_document`). A new run
    reads the paper's document.json, parsed again first when the parser has changed since it was written. Before that
    reparse replaces it, every run without a copy of its own gets the old one, so its evidence stays where it was
    extracted. The reparse keeps each table's transcription, so it makes no table call and cells keep their anchors."""
    pid = run.info.paper_id
    if run.resume and (doc := run_document(run.home, pid, run.info.id)) is not None:
        return doc
    path = run.home.document(pid)
    old = files.read_model_or_none(path, Document)
    if old is not None and old.parser_version == PARSER_VERSION:
        return old
    paper = files.read_model(run.home.paper_json(pid), PaperInfo)
    doc = pdf.parse(data, paper_id=paper.id, sha256=paper.sha256)
    if old is not None:
        for d in files.child_dirs(run.home.runs(pid)):
            own = RunPaths(d).document
            if d.name != run.info.id and not files.exists(own):
                files.write_model(own, old)
        doc = carry_tables(old, doc)
    files.write_model(path, doc)
    run.emit(Note(text=f"parsed the PDF again with parser {PARSER_VERSION}"))
    return doc


def run_document(home: Home, paper_id: str, run_id: str) -> Document | None:
    """The document a run was extracted against: its own copy, written after its table pass. A run without a copy,
    such as one that stopped before its table pass, reads the paper's document.json."""
    own = files.read_model_or_none(home.run(paper_id, run_id).document, Document)
    return own if own is not None else files.read_model_or_none(home.document(paper_id), Document)


async def _table_pass(run: Run, doc: Document, data: bytes) -> tuple[Document, Usage]:
    """Transcribe each pending table and ground it in the text layer. A table whose call failed stays pending,
    so the next run tries it again. For a new run, a cached table that older grounding code matched is grounded again
    from its transcription, at no cost; a resumed run keeps the cells it started on. The document comes back unchanged
    when there was nothing to do."""
    s = run.settings
    pending = [t for t in doc.tables if t.status == "pending"]
    stale = set() if run.resume else {t.anchor for t in doc.tables if t.rows and t.grounder_version != GROUNDER_VERSION}
    if not pending and not stale:
        return doc, Usage()
    pages = {p.number: p for p in doc.pages}
    captions = {b.anchor: b.bbox for b in doc.blocks}

    def grounded(t: Table, grid: Grid) -> Table:
        page = pages[t.page]
        return ground(
            t,
            grid,
            pdf.pages_chars(data, [t.page])[t.page],
            caption=captions.get(t.caption_anchor),
            page_size=(page.width, page.height),
            fuzzy_threshold=s.grounding_fuzzy_threshold,
            min_share=s.table_min_grounded,
        )

    results = await _transcribe(run, doc, data, pending) if pending else {}
    used = sum((r.usage for r in results.values()), Usage())
    out: list[Table] = []
    for t in doc.tables:
        r = results.get(t.anchor)
        if t.anchor in stale:
            out.append(grounded(t, Grid.of(t)))
        elif r is None:
            out.append(t)
        elif r.grid is not None:
            g = grounded(t, r.grid)
            run.emit(Note(text=f"transcribed {t.label}: {_grid_line(g)}"))
            out.append(g)
        else:
            run.emit(Note(level="warning", text=f"{t.label} ({t.anchor}): {r.error}"))
            out.append(t if r.retry else t.model_copy(update={"status": "failed"}))
    if stale:
        run.emit(Note(text=f"grounded {len(stale)} cached tables again with grounder {GROUNDER_VERSION}"))
    previous = doc.table_pass.cost_usd if doc.table_pass is not None else 0.0
    table_pass = TablePass(model=s.table_model, cost_usd=previous + used.cost_usd)
    staged = doc.with_tables(out, table_pass)
    marked = staged.model_copy(update={"blocks": mark_table_blocks(staged)})
    return marked.with_tables(attach_notes(marked), table_pass), used


async def _transcribe(run: Run, doc: Document, data: bytes, pending: list[Table]) -> dict[str, tables.TableResult]:
    s = run.settings
    model = models.anthropic(s.table_model, api_key=run.api_key)
    model_settings = models.cached_instructions(models.settings(s.table_effort))
    runner = tables.agent()
    gate = asyncio.Semaphore(s.max_concurrency)
    pages = {p.number: p for p in doc.pages}

    async def one(t: Table) -> tables.TableResult:
        async with gate:
            x0, y0, x1, y1 = t.region
            page = pages[t.page]
            box = (max(0.0, x0 - REGION_PAD), max(0.0, y0 - REGION_PAD), min(page.width, x1 + REGION_PAD),
                   min(page.height, y1 + REGION_PAD))  # fmt: skip
            image = pdf.render(data, t.page, bbox=box, scale=s.table_render_scale, text_rotation=t.rotation)
            job = tables.TableJob(table=t, image=image, text_hint=_text_hint(doc, t))
            return await tables.transcribe(job, runner=runner, model=model, settings=model_settings)

    return {r.table_id: r for r in await asyncio.gather(*(one(t) for t in pending))}


def _grid_line(t: Table) -> str:
    body = len(t.rows) - t.header_rows
    found = f"{body} rows, {t.grounded_share:.0%} of cells found in the PDF text"
    return found if t.status != "failed" else f"{found}, too few to trust; the agent reads it from the text instead"


def _text_hint(doc: Document, t: Table) -> str:
    x0, y0, x1, y1 = t.region
    inside = [
        b.text
        for b in doc.blocks
        if b.page == t.page
        and b.anchor != t.caption_anchor
        and x0 <= (b.bbox[0] + b.bbox[2]) / 2 <= x1
        and y0 <= (b.bbox[1] + b.bbox[3]) / 2 <= y1
    ]
    return "\n".join(inside)[:6000] or "(none)"


def _table_summary(doc: Document) -> str:
    counts = Counter(t.status for t in doc.tables)
    return ", ".join(f"{n} {status}" for status, n in counts.items()) or "no tables"


async def _agent(run: Run, doc: Document, data: bytes, paper: PaperInfo) -> extractor.ExtractResult:
    s, info = run.settings, run.info
    spent = info.table_usage.cost_usd + run.base_usage.cost_usd
    remaining = info.max_cost_usd - spent
    turns_left = info.max_turns - run.base_turns
    if remaining <= 0 or turns_left <= 0:
        used = (
            f"${spent:.2f} of the ${info.max_cost_usd:.2f} budget"
            if remaining <= 0
            else f"{run.base_turns} of the {info.max_turns} turns"
        )
        run.emit(Note(level="warning", text=f"earlier steps of this run used {used}; nothing is left for the agent"))
        return extractor.ExtractResult(outcome="budget_exhausted", usage=Usage(), turns=0, message="no budget left")
    draft = (files.read_model_or_none(run.paths.draft, Draft) if run.resume else None) or Draft()
    draft.finished = None
    workspace = DraftWorkspace(
        doc=doc,
        draft=draft,
        limits=Limits(same_error_limit=s.same_error_limit, error_list_cap=s.error_list_cap),
        save=lambda d: files.write_model(run.paths.draft, d),
        emit=run.emit,
    )
    guide = files.read_resource("sage.agents", "guide.md")
    agent = extractor.build(
        prompt=extractor.instructions(guide, doc, title=paper.title),
        max_turns=info.max_turns,
    )
    deps = extractor.Deps(workspace=workspace, doc=doc, pdf=data, emit=run.emit, render_scale=s.table_render_scale)
    return await extractor.extract(
        agent,
        deps,
        model=models.anthropic(info.extraction_model, api_key=run.api_key),
        settings=models.cached(models.settings(info.effort), diagnostics=s.cache_diagnostics),
        budget=extractor.Budget(max_turns=turns_left, max_cost_usd=remaining),
        resume=run.resume and bool(draft.records),
        guidance=_guidance(info),
    )


def _guidance(info: RunInfo) -> extractor.Guidance | None:
    if info.guidance is None and not info.review_notes:
        return None
    notes = tuple(
        extractor.ReviewNote(record_id=r.record_id, action=r.action, field=r.field, value=r.value, note=r.note)
        for r in info.review_notes
    )
    return extractor.Guidance(text=info.guidance, reviews=notes)


def _salvage(run: Run) -> None:
    """Settle what a failed or stopped run left. When the document or the draft can't be read, or settling raises,
    write an empty result instead, so a run that ends always has records.json."""
    try:
        _settle(run, run_document(run.home, run.info.paper_id, run.info.id), "failed")
    except Exception as e:
        run.emit(Note(level="warning", text=f"could not settle this run, so it has no records: {e}"))
        _write_settled(run, _empty(run, "failed"))


def _settle(run: Run, doc: Document | None, outcome: Outcome) -> None:
    with _stage(run, "settle") as finished:
        draft = files.read_model_or_none(run.paths.draft, Draft) or Draft()
        settled = settle(draft, doc, run_id=run.info.id, outcome=outcome) if doc is not None else _empty(run, outcome)
        c = _write_settled(run, settled)
        finished.counts = c
        finished.detail = f"{c.records} records: {c.verified} verified, {c.flagged} flagged, {c.unresolved} unresolved"
        if doc is not None and settled.records:
            _place_quotes(run, settled, doc)


def _place_quotes(run: Run, settled: Settled, doc: Document) -> None:
    """Find the quotes' lines on the page now, so the paper's first view doesn't wait on the PDF. A failure costs only
    the precise marks, and the Paper screen tries again when it opens the run."""
    try:
        quote_lines(run.home, run.info.paper_id, run.info.id, settled, doc)
    except Exception as e:
        run.emit(Note(level="warning", text=f"could not find the quotes on the page: {e}"))


def _empty(run: Run, outcome: Outcome) -> Settled:
    return Settled(run_id=run.info.id, paper_id=run.info.paper_id, outcome=outcome, records=[])


def _write_settled(run: Run, settled: Settled) -> Counts:
    files.write_model(run.paths.records, settled)
    for r in settled.records:
        run.emit(RecordSettled(record_id=r.record.id, record_type=r.record.type, status=r.status))
    statuses = [r.status for r in settled.records]
    c = run.info.counts = Counts(
        records=len(statuses),
        verified=statuses.count("verified"),
        flagged=statuses.count("flagged"),
        unresolved=statuses.count("unresolved"),
    )
    run.info.summary = settled.summary
    return c
