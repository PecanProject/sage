"""The HTTP surface. Thin: every route calls one function in core."""

import asyncio
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Header, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, JsonValue

from sage import core

WEB_DIST = (Path(__file__).parent / "web_dist").resolve()

app = FastAPI(title="SAGE", version="2.0.0")

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


@app.get("/api/settings", responses=_errors)
def get_settings() -> core.SettingsView:
    return core.settings_view()


@app.put("/api/settings", responses=_errors)
def put_settings(body: SettingsUpdate) -> core.SettingsView:
    return core.update_settings(body.values)


@app.post("/api/settings/test-key", responses=_errors)
def test_api_key() -> core.KeyCheck:
    return core.test_api_key()


@app.get("/api/papers", responses=_errors)
def list_papers() -> list[core.PaperSummary]:
    return core.papers()


@app.post("/api/papers", responses=_errors)
async def upload_papers(files: Annotated[list[UploadFile], File()]) -> list[core.Added | core.NotAdded]:
    return [await run_in_threadpool(core.add_paper, f.filename or "paper.pdf", await f.read()) for f in files]


@app.get("/api/papers/{paper_id}/pdf", response_class=Response, responses=_errors)
def paper_pdf(paper_id: str) -> Response:
    return Response(core.paper_pdf(paper_id), media_type="application/pdf")


@app.get("/api/papers/{paper_id}", responses=_errors)
def get_paper(paper_id: str) -> core.PaperSummary:
    return core.paper_summary(paper_id)


@app.get("/api/papers/{paper_id}/document", responses=_errors)
def paper_document(paper_id: str) -> core.Document:
    return core.document(paper_id)


@app.get("/api/runs/{run_id}/document", responses=_errors)
def get_run_document(run_id: str) -> core.Document:
    return core.run_doc(run_id)


@app.get("/api/papers/{paper_id}/reviews", responses=_errors)
def paper_reviews(paper_id: str) -> list[core.Review]:
    return core.reviews(paper_id)


class StartRun(BaseModel):
    model: str | None = None
    effort: str | None = None


@app.post("/api/papers/{paper_id}/runs", responses=_errors)
def start_run(paper_id: str, body: StartRun) -> core.RunInfo:
    return core.start_run(paper_id, model=body.model, effort=body.effort)


class StartBatch(BaseModel):
    paper_ids: list[str] | None = None
    jobs: int | None = None
    model: str | None = None
    effort: str | None = None


@app.post("/api/batches", responses=_errors)
def start_batch(body: StartBatch | None = None) -> core.Batch:
    b = body or StartBatch()
    return core.start_batch(b.paper_ids, jobs=b.jobs, model=b.model, effort=b.effort)


@app.get("/api/runs", responses=_errors)
def list_runs(paper_id: str | None = None) -> list[core.RunInfo]:
    return core.runs(paper_id)


@app.get("/api/runs/{run_id}", responses=_errors)
def get_run(run_id: str) -> core.RunInfo:
    return core.run(run_id)


class ResumeRun(BaseModel):
    guidance: str | None = None
    review_notes: bool = True


class ContinueRun(BaseModel):
    guidance: str | None = None
    review_notes: bool = True
    model: str | None = None
    effort: str | None = None


@app.post("/api/runs/{run_id}/resume", responses=_errors)
def resume_run(run_id: str, body: ResumeRun | None = None) -> core.RunInfo:
    b = body or ResumeRun()
    return core.resume_run(run_id, guidance=b.guidance, review_notes=b.review_notes)


@app.post("/api/runs/{run_id}/continue", responses=_errors)
def continue_run(run_id: str, body: ContinueRun) -> core.RunInfo:
    return core.continue_run(
        run_id, body.guidance, review_notes=body.review_notes, model=body.model, effort=body.effort
    )


@app.post("/api/runs/{run_id}/stop", responses=_errors)
def stop_run(run_id: str) -> core.RunInfo:
    return core.stop_run(run_id)


@app.get("/api/runs/{run_id}/events", responses=_errors)
def run_events(run_id: str, offset: int = 0) -> core.EventPage:
    return core.events(run_id, offset=offset)


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


@app.get("/api/runs/{run_id}/records", responses=_errors)
def run_records(run_id: str) -> core.RecordsView:
    return core.records(run_id)


@app.get("/api/runs/{run_id}/queue", responses=_errors)
def run_queue(run_id: str) -> list[core.QueueItem]:
    return core.review_queue(run_id)


@app.get("/api/review", responses=_errors)
def review_inbox() -> list[core.InboxEntry]:
    return core.review_inbox()


@app.post("/api/reviews", responses=_errors)
def add_review(review: core.Review) -> core.Review:
    return core.add_review(review)


@app.post("/api/runs/{run_id}/accept-looks-good", responses=_errors)
def accept_looks_good(run_id: str) -> list[core.Review]:
    return core.accept_looks_good(run_id)


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
