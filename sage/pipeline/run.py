"""Drive one run of SAGE's own agent: bring the paper's files up to date, run the agent on the folder under its budget,
and settle. `done` settles a run that finishes; one that ends any other way settles what the folder holds, so a crash
leaves a run that `sage extract run --resume` picks up."""

import asyncio
import os
import traceback
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field

from pydantic import ValidationError

from sage.agents import extractor, models
from sage.events import (
    EffortAdapter,
    Event,
    Note,
    RunError,
    RunFinished,
    Stage,
    StageFinished,
    StageStarted,
    Usage,
    UsageEvent,
    now,
)
from sage.ir.settled import Outcome
from sage.parse import pdf
from sage.parse.document import PARSER_VERSION, Document
from sage.pipeline import extraction, paper_files
from sage.settings import Settings
from sage.store import files, skills
from sage.store.models import PaperInfo, RunInfo
from sage.store.paths import Home, RunPaths

REFERENCES = ("entities.md", "causes.md", "internals.md")
"""The skill's reference files, all in the agent's instructions: it has no skill loader to fetch them."""


class AbortError(Exception):
    def __init__(self, message: str, *, refused: bool = False) -> None:
        super().__init__(message)
        self.refused = refused
        """The provider refused the key or the model."""


@dataclass
class Run:
    home: Home
    settings: Settings
    info: RunInfo
    route: models.Route
    """How to reach the run's model, from the settings when it was claimed."""
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
        if isinstance(event, UsageEvent):
            self.info.usage = self.base_usage + event.total
            self.info.turns = self.base_turns + event.turn
            self.save()

    def save(self) -> None:
        files.write_model(self.paths.info, self.info)


async def execute(home: Home, settings: Settings, info: RunInfo, *, route: models.Route) -> RunInfo:
    """Run to the end and return the final run.json. A run that has started before resumes. Any error is recorded,
    never raised, and run.json never keeps `running` or `queued` once this returns: a cancelled task leaves
    `interrupted`, anything else `failed`. A BaseException is caught too, because a Rust panic in pydantic-core
    (`pyo3_runtime.PanicException`) derives from it. Everything before the first await runs at once, so the caller's
    next look at run.json finds it running."""
    resumed = info.started_at is not None
    run = Run(home=home, settings=settings, info=info, route=route, resume=resumed)
    info.status = "running"
    info.pid = os.getpid()
    info.started_at = info.started_at or now()
    info.finished_at = None
    info.error = None
    info.refused = False
    if resumed:
        info.resumes += 1
    run.save()
    try:
        run.emit(extraction.started(info, resumed=resumed))
        await _drive(run)
    except asyncio.CancelledError:
        info.status = "interrupted"
        run.emit(Note(level="warning", text="stopped before it finished; resume it to carry on"))
        _salvage(run, "failed")
    except BaseException as e:
        message = str(e) if isinstance(e, AbortError) else f"{type(e).__name__}: {e}"
        info.status = "failed"
        info.error = message
        info.refused = isinstance(e, AbortError) and e.refused
        run.emit(RunError(message=message))
        if not isinstance(e, AbortError):
            run.emit(Note(level="warning", text=traceback.format_exc()[-4000:]))
        _salvage(run, "failed")
    finally:
        if info.status in ("running", "queued"):
            info.status = "interrupted"
        info.finished_at = now()
        info.pid = None
        run.save()
    return info


async def _drive(run: Run) -> None:
    info = run.info
    with _stage(run, "parse"):
        _document(run)

    with _stage(run, "agent") as finished:
        result = await _agent(run)
        # The response that trips a usage limit is billed but emits no usage event, so the result's tally is the one
        # that counts against the budget.
        info.usage = run.base_usage + result.usage
        info.turns = run.base_turns + result.turns
        run.save()
        if result.outcome == "failed":
            raise AbortError(result.message or "the agent failed", refused=result.refused)
        turns = f"{result.turns} turn{'' if result.turns == 1 else 's'}"
        finished.detail = f"{result.outcome} after {turns}, ${result.usage.cost_usd:.4f}"

    if result.outcome != "finished":
        # done settled a finished run and said so; any other ending settles what the folder holds.
        _salvage(run, result.outcome)
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


def _document(run: Run) -> None:
    """Parse the PDF again when the parser has changed since the paper's document.json was written, and bring the
    folder's images and paper.md up to date. Before the new document replaces the old one, every run without a copy of
    its own gets the old one, so its evidence stays where it was extracted."""
    pid = run.info.paper_id
    path = run.home.document(pid)
    old = files.read_model_or_none(path, Document)
    data = files.read_bytes(run.home.pdf(pid))
    if old is None or old.parser_version != PARSER_VERSION:
        paper = files.read_model(run.home.paper_json(pid), PaperInfo)
        doc = pdf.parse(data, paper_id=paper.id, sha256=paper.sha256)
        if old is not None:
            paper_files.keep_run_documents(run.home, pid, old, skip=run.info.id)
        files.write_model(path, doc)
        run.emit(Note(text=f"parsed the PDF again with parser {PARSER_VERSION}"))
    paper_files.materialize(run.home, pid, data, run.settings)


def run_document(home: Home, paper_id: str, run_id: str) -> Document | None:
    """The document a run was checked against: its own copy, written at each check. A run without a copy, such as
    one that stopped before its first check, reads the paper's document.json."""
    own = files.read_model_or_none(home.run(paper_id, run_id).document, Document)
    return own if own is not None else files.read_model_or_none(home.document(paper_id), Document)


async def _agent(run: Run) -> extractor.ExtractResult:
    info = run.info
    remaining = info.max_cost_usd - run.base_usage.cost_usd
    turns_left = info.max_turns - run.base_turns
    if remaining <= 0 or turns_left <= 0:
        used = (
            f"${run.base_usage.cost_usd:.2f} of the ${info.max_cost_usd:.2f} budget"
            if remaining <= 0
            else f"{run.base_turns} of the {info.max_turns} turns"
        )
        run.emit(Note(level="warning", text=f"earlier attempts of this run used {used}; nothing is left"))
        return extractor.ExtractResult(outcome="budget_exhausted", usage=Usage(), turns=0, message="no budget left")
    workspace = extraction.FolderWorkspace(home=run.home, settings=run.settings, info=info)
    skill = skills.bundled()
    paper_md = files.read_text(run.home.paper_md(info.paper_id))
    agent = extractor.build(
        prompt=extractor.instructions(skill["SKILL.md"], [skill[f"references/{n}"] for n in REFERENCES], paper_md),
        folder=str(workspace.folder),
        max_turns=info.max_turns,
    )
    deps = extractor.Deps(workspace=workspace, emit=run.emit)
    model = models.build(run.route)
    try:
        effort = None if info.effort is None else EffortAdapter.validate_python(info.effort)
    except ValidationError as e:
        raise AbortError(f"{info.effort} isn't an effort SAGE's own agent has") from e
    return await extractor.extract(
        agent,
        deps,
        model=model,
        settings=models.cached(
            models.settings(model, effort),
            model,
            diagnostics=run.settings.cache_diagnostics,
        ),
        budget=extractor.Budget(max_turns=turns_left, max_cost_usd=remaining),
        resume=run.resume or bool(extraction.agent_texts(run.home, info.paper_id)),
        guidance=f"The scientist's guidance for this run:\n\n{info.guidance}" if info.guidance is not None else None,
    )


def _salvage(run: Run, outcome: Outcome) -> None:
    with _stage(run, "settle") as finished:
        extraction.salvage(run.home, run.settings, run.info, outcome)
        c = run.info.counts
        finished.counts = c
        finished.detail = f"{c.records} records: {c.verified} verified, {c.flagged} flagged, {c.unresolved} unresolved"
