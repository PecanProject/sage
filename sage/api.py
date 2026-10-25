"""The HTTP surface. Thin: every route calls one function in core."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Header, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, JsonValue, model_validator

from sage import core

WEB_DIST = (Path(__file__).parent / "web_dist").resolve()


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncGenerator[None]:
    core.fresh_prices()
    yield


app = FastAPI(title="SAGE", version="2.0.0", lifespan=_lifespan)

_STATUS = {"not_found": 404, "invalid": 400, "conflict": 409, "missing_key": 412, "corrupt": 500}


@app.exception_handler(RequestValidationError)
async def _bad_request(_: Request, exc: RequestValidationError) -> JSONResponse:
    """A request the routes' types refuse answers in the same shape as every other error."""
    problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
    return JSONResponse(core.ErrorBody(kind="invalid", message=problems).model_dump(), status_code=422)


@app.exception_handler(core.SageError)
async def _sage_error(_: Request, exc: core.SageError) -> JSONResponse:
    return JSONResponse(core.ErrorBody(kind=exc.kind, message=exc.message).model_dump(), status_code=_STATUS[exc.kind])


_errors: dict[int | str, dict[str, object]] = {code: {"model": core.ErrorBody} for code in _STATUS.values()}


class SettingsUpdate(BaseModel):
    values: dict[str, JsonValue]


app.get("/api/settings", responses=_errors)(core.settings_view)


@app.put("/api/settings", responses=_errors)
def put_settings(body: SettingsUpdate) -> core.SettingsView:
    return core.update_settings(body.values)


app.get("/api/models", responses=_errors)(core.models)


class AddModel(BaseModel):
    id: str
    title: str | None = None
    """The provider's name for it, kept for a model genai-prices doesn't name."""
    input_usd: float | None = None
    output_usd: float | None = None
    """Your price per million tokens, in and out. Both, or neither to keep the one it has."""
    cache_read_usd: float | None = None
    cache_write_usd: float | None = None
    """Your price per million tokens read from and written to the prompt cache. Both or neither; neither uses
    genai-prices' discount for the model, else the input price."""

    @model_validator(mode="after")
    def _pairs(self) -> "AddModel":
        if (self.input_usd is None) != (self.output_usd is None):
            raise ValueError("give both the input and the output price, or neither")
        if (self.cache_read_usd is None) != (self.cache_write_usd is None):
            raise ValueError("give both the cache read and the cache write price, or neither")
        return self


@app.post("/api/models", responses=_errors)
def add_model(body: AddModel) -> list[core.ModelOption]:
    price = None if body.input_usd is None or body.output_usd is None else (body.input_usd, body.output_usd)
    cache = (
        None
        if body.cache_read_usd is None or body.cache_write_usd is None
        else (body.cache_read_usd, body.cache_write_usd)
    )
    return core.add_model(body.id, title=body.title, price=price, cache_price=cache)


class RemoveModels(BaseModel):
    ids: list[str]


@app.post("/api/models/remove", responses=_errors)
def remove_models(body: RemoveModels) -> list[core.ModelOption]:
    return core.remove_models(body.ids)


class ModelEffort(BaseModel):
    id: str
    effort: str
    """One of the levels the model has, as `ModelOption.efforts` lists them."""


@app.post("/api/models/effort", responses=_errors)
def set_model_effort(body: ModelEffort) -> list[core.ModelOption]:
    return core.set_model_effort(body.id, body.effort)


@app.get("/api/models/describe", responses=_errors)
def describe_model(id: str) -> core.Candidate:  # noqa: A002 - the query parameter's name
    return core.describe_model(id)


app.get("/api/provider-kinds", responses=_errors)(core.provider_kinds)
app.get("/api/providers", responses=_errors)(core.providers)


class SaveProvider(BaseModel):
    kind: str | None = None
    """Needed for a new provider; a built-in one keeps its own."""
    values: dict[str, str | None] = {}
    """Fields to set. A masked key is left as it is, and an empty value clears its field."""


@app.put("/api/providers/{provider}", responses=_errors)
def save_provider(provider: str, body: SaveProvider) -> core.ProviderInfo:
    return core.save_provider(provider, body.values, kind=body.kind)


app.delete("/api/providers/{provider}", responses=_errors)(core.remove_provider)
app.get("/api/providers/{provider}/models", responses=_errors)(core.available_models)
app.get("/api/papers", responses=_errors)(core.papers)


@app.post("/api/papers", responses=_errors)
async def upload_papers(files: Annotated[list[UploadFile], File()]) -> list[core.Added | core.NotAdded]:
    return [await run_in_threadpool(core.add_paper, f.filename or "paper.pdf", await f.read()) for f in files]


@app.get("/api/papers/{paper_id}/pdf", response_class=Response, responses=_errors)
def paper_pdf(paper_id: str) -> Response:
    return Response(core.paper_pdf(paper_id), media_type="application/pdf")


app.get("/api/papers/{paper_id}", responses=_errors)(core.paper_summary)
app.get("/api/papers/{paper_id}/document", responses=_errors)(core.document)
app.get("/api/runs/{run_id}/document", responses=_errors)(core.run_doc)
app.get("/api/papers/{paper_id}/reviews", responses=_errors)(core.reviews)


class ExtractStart(BaseModel):
    guidance: str | None = None


class ExtractDone(BaseModel):
    summary: str


class ExtractStop(BaseModel):
    paper_ids: list[str] | None = None
    """Omit for every open run."""


@app.post("/api/papers/{paper_id}/extract/start", responses=_errors)
def extract_start(paper_id: str, body: ExtractStart | None = None) -> core.ExtractStatus:
    return core.extract_start(paper_id, guidance=(body or ExtractStart()).guidance)


app.post("/api/papers/{paper_id}/extract/check", responses=_errors)(core.extract_check)


@app.post("/api/papers/{paper_id}/extract/done", responses=_errors)
def extract_done(paper_id: str, body: ExtractDone) -> core.DoneReport:
    return core.extract_done(paper_id, body.summary)


@app.post("/api/extract/stop", responses=_errors)
def extract_stop(body: ExtractStop) -> list[core.RunInfo]:
    return core.extract_stop(body.paper_ids)


app.get("/api/extract/status", responses=_errors)(core.extract_status)
app.get("/api/agents", responses=_errors)(core.agents)
app.get("/api/agents/{agent_id}/offer", responses=_errors)(core.agent_offer)


class AgentChange(BaseModel):
    model: str | None = None
    effort: str | None = None
    """In the agent's own words. Leave one out to keep it, and send an empty string for the agent's default."""


@app.put("/api/agents/{agent_id}", responses=_errors)
def set_agent(agent_id: str, body: AgentChange) -> core.AgentStatus:
    return core.set_agent(agent_id, model=body.model, effort=body.effort)


class Answer(BaseModel):
    allow: bool


@app.post("/api/papers/{paper_id}/extract/answer", responses=_errors)
def answer_agent(paper_id: str, body: Answer) -> core.RunInfo:
    return core.answer_agent(paper_id, allow=body.allow)


app.get("/api/skill", responses=_errors)(core.skill_status)
app.post("/api/skill/install", responses=_errors)(core.install_skill)


class StartRuns(BaseModel):
    paper_ids: list[str] | None = None
    """Omit for every paper with no data and nothing live on it."""
    using: str | None = None
    """An agent's id (`claude`, `claude:sonnet`) or a model. Omit for the `extract_with` setting."""
    effort: str | None = None


@app.post("/api/runs", responses=_errors)
def start_runs(body: StartRuns) -> list[core.RunInfo]:
    return core.start_runs(body.paper_ids, using=body.using, effort=body.effort)


class StopRuns(BaseModel):
    run_ids: list[str] | None = None
    """Omit for every live run."""


@app.post("/api/runs/stop", responses=_errors)
def stop_runs(body: StopRuns) -> list[core.RunInfo]:
    return core.stop_runs(body.run_ids)


app.get("/api/runs", responses=_errors)(core.runs)
app.get("/api/runs/{run_id}", responses=_errors)(core.run)


class ResumeRun(BaseModel):
    guidance: str | None = None


class ContinueRun(BaseModel):
    guidance: str | None = None
    using: str | None = None
    """An agent or a model; omit for the one the earlier run used."""
    effort: str | None = None


@app.post("/api/runs/{run_id}/resume", responses=_errors)
def resume_run(run_id: str, body: ResumeRun | None = None) -> core.RunInfo:
    return core.resume_run(run_id, guidance=(body or ResumeRun()).guidance)


@app.post("/api/runs/{run_id}/continue", responses=_errors)
def continue_run(run_id: str, body: ContinueRun) -> core.RunInfo:
    return core.continue_run(run_id, body.guidance, using=body.using, effort=body.effort)


app.get("/api/runs/{run_id}/events", responses=_errors)(core.events)


def _known_run(run_id: str) -> str:
    """Runs before the stream opens, so an unknown run answers 404 instead of an empty 200 stream."""
    core.run(run_id)
    return run_id


@app.get("/api/runs/{run_id}/stream", response_class=EventSourceResponse, responses=_errors)
async def run_stream(
    run_id: Annotated[str, Depends(_known_run)],
    offset: int = 0,
    last_event_id: Annotated[str | None, Header()] = None,
) -> AsyncIterable[ServerSentEvent]:
    """The run's events as Server-Sent Events. Each batch's last event carries the byte offset as its id, so a
    reconnecting EventSource resumes where it left off. An `end` event follows once the run is over."""
    position = int(last_event_id) if last_event_id is not None and last_event_id.isdigit() else offset
    while True:
        page = await run_in_threadpool(core.events, run_id, offset=position)
        position = page.offset
        for i, e in enumerate(page.events):
            yield ServerSentEvent(data=e, event=e.type, id=str(position) if i == len(page.events) - 1 else None)
        if page.done:
            yield ServerSentEvent(data={"offset": position}, event="end")
            return
        await asyncio.sleep(0.4)


@app.get("/api/changes", response_class=EventSourceResponse)
async def changes() -> AsyncIterable[list[core.Change] | ServerSentEvent]:
    """Each batch of changes in the library as Server-Sent Events, for an open screen to fetch again what shows them.
    A quiet stream carries a keep-alive comment, so a proxy doesn't close it."""
    yield ServerSentEvent(comment="following")  # sends the headers now, so the EventSource opens at once
    async for batch in core.changes():
        yield batch


app.get("/api/runs/{run_id}/records", responses=_errors)(core.records)
app.get("/api/runs/{run_id}/queue", responses=_errors)(core.review_queue)
app.post("/api/reviews", responses=_errors)(core.add_review)
app.post("/api/runs/{run_id}/accept-looks-good", responses=_errors)(core.accept_looks_good)


@app.get("/api/runs/{run_id}/export", response_class=Response, responses=_errors)
def export_run(run_id: str, format: core.ExportFormat = "xlsx") -> Response:  # noqa: A002 - the query parameter's name
    exported = core.export(run_id, format)
    return Response(
        exported.data,
        media_type=exported.media_type,
        headers={"Content-Disposition": f'attachment; filename="{exported.filename}"'},
    )


if WEB_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise core.SageError("not_found", f"no API route /{path}")
        candidate = (WEB_DIST / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(WEB_DIST):
            return FileResponse(candidate)
        return FileResponse(WEB_DIST / "index.html")
