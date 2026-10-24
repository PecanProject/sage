"""The command line. Thin: every command calls core, and every command takes --json."""

import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError
from rich.console import Console
from rich.table import Column, Table
from rich.text import Text

from sage import core

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Extract experiment records from agronomy papers.")
config_app = typer.Typer(no_args_is_help=True, help="Read and change settings, the same ones the UI's Settings edits.")
app.add_typer(config_app, name="config")

JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
# Markup off: record labels, checker reasons and summaries come from papers and models, and their brackets
# ("[kg ha-1]") would be read as Rich tags, dropped or crashing the print. Styles go through Text instead.
out = Console(markup=False)
err = Console(stderr=True, markup=False)

JsonOpt = Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")]


def ids(header: str) -> Column:
    """A column of ids the next command takes: Rich shortens other columns first, never this one."""
    return Column(header, no_wrap=True)


def emit(data: BaseModel | Sequence[BaseModel] | JsonValue) -> None:
    if isinstance(data, BaseModel):
        print(data.model_dump_json(indent=2))
    elif isinstance(data, Sequence) and not isinstance(data, str) and all(isinstance(d, BaseModel) for d in data):
        print(json.dumps([d.model_dump(mode="json") for d in data if isinstance(d, BaseModel)], indent=2))
    else:
        print(json.dumps(data, indent=2, default=str))


def main() -> None:
    """The entry point and the one error boundary: a SageError from any command prints as an error and exits 1. Under
    --json it prints as JSON, and so does a usage error, which Typer would otherwise print as a box."""
    as_json = "--json" in sys.argv[1:]
    try:
        code = app(standalone_mode=not as_json)
    except core.SageError as e:
        fail(core.ErrorBody(kind=e.kind, message=e.message), 1, as_json=as_json)
    except typer.TyperException as e:
        # A usage error. Typer's own Click gives it a message and an exit code; its public base names neither.
        message = getattr(e, "format_message", e.__str__)()
        fail(core.ErrorBody(kind="invalid", message=message), getattr(e, "exit_code", 2), as_json=True)
    except typer.Abort:
        sys.exit(1)
    sys.exit(code if isinstance(code, int) else 0)


def fail(error: core.ErrorBody, code: int, *, as_json: bool) -> NoReturn:
    if as_json:
        print(json.dumps({"error": error.model_dump()}))
    else:
        err.print(Text.assemble(("error:", "red"), f" {error.message}"))
    sys.exit(code)


# config -----------------------------------------------------------------------------------------------------------


@config_app.command("list")
def config_list(as_json: JsonOpt = False) -> None:
    """Every setting with its value and where it comes from (env, file or default)."""
    view = core.settings_view()
    if as_json:
        emit(view)
        return
    table = Table("key", "value", "source", title=f"settings ({view.config_path})")
    for key, value in view.values.items():
        source = f"env {view.locked[key]}" if key in view.locked else view.sources[key]
        table.add_row(key, json.dumps(value), source)
    out.print(table)


@config_app.command("get")
def config_get(key: str, as_json: JsonOpt = False) -> None:
    """One setting's value."""
    value = core.config_get(key)
    emit({key: value}) if as_json else print(json.dumps(value))


@config_app.command("set")
def config_set(key: str, value: str, as_json: JsonOpt = False) -> None:
    """Change a setting in config.toml. An empty value restores the default."""
    view = core.update_settings({key: value})
    emit({key: view.values[key]}) if as_json else print(f"{key} = {json.dumps(view.values[key])}")


@config_app.command("test-key")
def config_test_key(as_json: JsonOpt = False) -> None:
    """Ask the extraction model's provider whether the saved API key works. Costs nothing."""
    check = core.test_api_key()
    if as_json:
        emit(check)
    else:
        print(check.message)
    if not check.ok:
        raise typer.Exit(1)


# papers -----------------------------------------------------------------------------------------------------------


@app.command()
def add(pdfs: Annotated[list[Path], typer.Argument(help="PDF files to add.")], as_json: JsonOpt = False) -> None:
    """Add PDFs to the library. The same file twice is added once. A file that can't be added is reported, the rest
    are still added, and the command exits 1."""
    results = [core.add_paper_file(p) for p in pdfs]
    if as_json:
        emit(results)
    for a in [] if as_json else results:
        if isinstance(a, core.NotAdded):
            err.print(Text.assemble(("error:", "red"), f" {a.error.message}"))
            continue
        verb = ("added", "green") if a.new else ("already in the library", "dim")
        out.print(Text.assemble(verb, f" {a.paper.id}  ({a.paper.pages} pages, sha256 {a.paper.sha256[:12]})"))
    if any(isinstance(a, core.NotAdded) for a in results):
        raise typer.Exit(1)


@app.command()
def papers(as_json: JsonOpt = False) -> None:
    """The library, like the Home screen: each paper, its latest run and how much is left to check."""
    found = core.papers()
    if as_json:
        emit(found)
        return
    table = Table(ids("paper"), "title", "pages", ids("latest run"), "status", "to check", "looks good")
    for p in found:
        r = p.latest_run
        live = r is not None and r.status in ("queued", "running")
        step = (core.STAGE_STEP[p.live_stage] if p.live_stage else "Starting") if live else None
        status = (r.status + (f": {step}" if step is not None else "")) if r else ""
        table.add_row(
            p.paper.id, (p.paper.title or "")[:60], str(p.paper.pages), r.id if r else "", status,
            "" if p.to_check is None else str(p.to_check), "" if p.looks_good is None else str(p.looks_good),
        )  # fmt: skip
    out.print(table)


# runs -------------------------------------------------------------------------------------------------------------


@app.command("run")
def run_cmd(
    paper_id: Annotated[
        str | None, typer.Argument(help="The paper to extract. Omit it with --resume or --continue.")
    ] = None,
    model: Annotated[
        str | None, typer.Option(help="Extraction model, e.g. claude-sonnet-5-5 or openai:gpt-5.1.")
    ] = None,
    effort: Annotated[str | None, typer.Option(help="low, medium, high, xhigh or max.")] = None,
    resume: Annotated[str | None, typer.Option(help="Continue this unfinished run from its saved draft.")] = None,
    continue_from: Annotated[
        str | None,
        typer.Option("--continue", help="Start a new run from this run's draft, with --guidance on what to add or fix"),
    ] = None,
    guidance: Annotated[
        str | None, typer.Option(help="What the agent should add or fix, with --continue or --resume.")
    ] = None,
    review_notes: Annotated[
        bool, typer.Option(help="Pass your edits, rejections and review notes to the agent too.")
    ] = True,
    follow: Annotated[
        bool, typer.Option(help="Stream the run's log until it ends. Ctrl-C stops watching only.")
    ] = True,
    as_json: JsonOpt = False,
) -> None:
    """Start an extraction. It runs in the background; by default this follows its log until it ends."""
    if resume is not None:
        info = core.resume_run(resume, guidance=guidance, review_notes=review_notes)
    elif continue_from is not None:
        info = core.continue_run(continue_from, guidance, review_notes=review_notes, model=model, effort=effort)
    elif paper_id is None:
        raise core.SageError("invalid", "give a paper id, or --resume or --continue <run id>")
    else:
        info = core.start_run(paper_id, model=model, effort=effort)
    if not follow:
        emit(info) if as_json else out.print(Text.assemble("started run ", (info.id, "bold"), f" on {info.paper_id}"))
        return
    if not as_json:
        out.print(
            Text.assemble(
                "run ", (info.id, "bold"), f" on {info.paper_id} ({info.extraction_model}, effort {info.effort})"
            )
        )
    _follow(info.id, as_json=as_json)
    final = core.run(info.id)
    if as_json:
        emit(final)
    else:
        _print_run_line(final)


@app.command()
def stop(run_id: str, as_json: JsonOpt = False) -> None:
    """Stop a running extraction. It keeps what it found; `sage run --resume` carries it on."""
    info = core.stop_run(run_id)
    emit(info) if as_json else out.print(Text.assemble("stopping run ", (info.id, "bold")))


@app.command("worker", hidden=True)
def worker(run_id: str, resume: bool = False) -> None:
    """The detached process behind a run. It reads stdin to the end first: the parent closes it once run.json is
    written."""
    core.work(run_id, resume=resume)


@app.command("run-all")
def run_all(
    paper_ids: Annotated[
        list[str] | None, typer.Argument(help="Papers to extract. Omit for every paper with no records yet.")
    ] = None,
    jobs: Annotated[
        int | None, typer.Option(help="Papers to extract at once. Default: the batch_jobs setting.")
    ] = None,
    model: Annotated[
        str | None, typer.Option(help="Extraction model, e.g. claude-sonnet-5-5 or openai:gpt-5.1.")
    ] = None,
    effort: Annotated[str | None, typer.Option(help="low, medium, high, xhigh or max.")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Extract many papers in one batch. It runs in the background, a few papers at a time; watch it with `sage
    runs`, and stop a paper with `sage stop`."""
    batch = core.start_batch(paper_ids or None, jobs=jobs, model=model, effort=effort)
    if as_json:
        emit(batch)
        return
    out.print(Text.assemble("batch started on ", (str(len(batch.papers)), "bold"), " papers:"))
    for paper_id in batch.papers:
        out.print(f"  {paper_id}")
    out.print(Text("watch it with `sage runs`", style="dim"))


@app.command("batch", hidden=True)
def batch(
    paper_ids: list[str],
    jobs: int = 2,
    model: str | None = None,
    effort: str | None = None,
) -> None:
    """The detached batch supervisor's body, whose stdout is the batch log."""
    results = core.work_batch(paper_ids, jobs=jobs, model=model, effort=effort, say=out.print)
    skipped = [r for r in results if r.error is not None]
    out.print(f"batch done: {len(results) - len(skipped)} run, {len(skipped)} skipped")


@app.command()
def runs(
    paper_id: Annotated[str | None, typer.Argument(help="Only this paper's runs.")] = None, as_json: JsonOpt = False
) -> None:
    """Runs, newest first."""
    found = core.runs(paper_id)
    if as_json:
        emit(found)
        return
    table = Table(
        ids("run"), ids("paper"), "status", "model", "turns", "cost", "records", "verified", "flagged", "unresolved"
    )
    for r in found:
        c = r.counts
        table.add_row(
            r.id, r.paper_id, r.status, r.extraction_model.removeprefix("anthropic:"), str(r.turns),
            f"${r.total_usage.cost_usd:.3f}", str(c.records), str(c.verified), str(c.flagged), str(c.unresolved),
        )  # fmt: skip
    out.print(table)


@app.command()
def show(run_id: str, as_json: JsonOpt = False) -> None:
    """A run's settled records: status, what each one says, and why anything isn't verified."""
    view = core.records(run_id)
    if as_json:
        emit(view)
        return
    _print_run_line(view.run)
    if view.provisional:
        live = view.run.status in ("queued", "running")
        why = "the run has not settled yet" if live else f"the run ended {view.run.status} before settling"
        out.print(Text.assemble(("provisional:", "yellow"), f" {why}; this is its draft so far"))
    if view.settled.summary is not None:
        out.print(f"summary: {view.settled.summary}")
    table = Table(ids("id"), "type", "status", "what", "why", show_lines=False)
    for r in view.settled.records:
        reason = next(
            (f"{f.field}: {f.reason or f.cause}" for f in r.fields if f.status != "verified" and f.cause != "absent"),
            "",
        )
        table.add_row(r.record.id, r.record.type, r.status, view.labels[r.record.id], reason[:120])
    out.print(table)
    for u in view.settled.unresolved:
        out.print(f"unresolved {u.target}: {u.cause} ({u.reason})")


@app.command()
def logs(
    run_id: str,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Keep streaming until the run ends.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """A run's event log. --json prints one JSON event per line."""
    if follow:
        _follow(run_id, as_json=as_json)
    else:
        for event in core.events(run_id).events:
            _print_event(event, as_json=as_json)


@app.command()
def export(
    run_id: str,
    fmt: Annotated[
        core.ExportFormat,
        typer.Option(
            "--format",
            help="xlsx: the PEcAn curation workbook; csv: its tabs as CSVs in a zip; json: every record as settled.",
        ),
    ] = "xlsx",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Where to write. By default json goes to stdout, the rest to ./<run>.*"),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Export a run's settled records."""
    exported = core.export(run_id, fmt)
    path = output if output is not None or fmt == "json" else Path(exported.filename)
    if path is not None:
        core.write_export(path, exported.data)
    if path is None:
        text = exported.data.decode()
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    elif as_json:
        emit({"path": str(path), "format": fmt, "bytes": len(exported.data)})
    else:
        out.print(f"wrote {path}")


def _follow(run_id: str, *, as_json: bool) -> None:
    offset = 0
    try:
        while True:
            page = core.events(run_id, offset=offset)
            for e in page.events:
                _print_event(e, as_json=as_json)
            if page.done:
                return
            offset = page.offset
            time.sleep(0.5)
    except KeyboardInterrupt:
        if not as_json:
            err.print(f"stopped watching; the run goes on. `sage logs {run_id} -f` picks it up again")


def _print_event(e: core.Event, *, as_json: bool) -> None:
    if as_json:
        print(e.model_dump_json(), flush=True)
        return
    line = core.describe(e)
    if line is not None:
        out.print(Text.assemble((f"{e.ts:%H:%M:%S} ", "dim"), line))


def _print_run_line(r: core.RunInfo) -> None:
    c = r.counts
    status = f"stopped at the {'turn' if r.limit == 'turns' else 'cost'} limit" if r.limit else r.status
    out.print(
        f"{r.id}  {status}  {r.turns} turn{'' if r.turns == 1 else 's'}  ${r.total_usage.cost_usd:.3f}  "
        f"{c.records} records: {c.verified} verified, {c.flagged} flagged, {c.unresolved} unresolved"
        + (f"\n  error: {r.error}" if r.error is not None else "")
    )


# review -----------------------------------------------------------------------------------------------------------


@app.command()
def queue(
    run_id: Annotated[
        str | None, typer.Argument(help="This run's review queue. Omit it for every paper's counts.")
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """What is left to check. With no RUN, each paper's data run and its counts; with a RUN, its every record."""
    if run_id is None:
        inbox = core.review_inbox()
        if as_json:
            emit(inbox)
            return
        table = Table(ids("paper"), ids("run"), "to check", "looks good", "total")
        for entry in inbox:
            table.add_row(entry.paper.id, entry.run.id, str(entry.to_check), str(entry.looks_good), str(entry.total))
        out.print(table)
        return
    items = core.review_queue(run_id)
    if as_json:
        emit(items)
        return
    table = Table(ids("record"), "type", "status", "what", "review", "why")
    for i in items:
        state = i.review.action if i.review is not None else ""
        table.add_row(i.record_id, i.record_type, i.status, i.label, state, (i.reason or "")[:80])
    out.print(table)


@app.command()
def review(
    run_id: str,
    record_id: Annotated[str | None, typer.Argument(help="The record. Omit it with --accept-looks-good.")] = None,
    action: Annotated[core.ReviewAction | None, typer.Argument(help="accept, edit or reject.")] = None,
    field: Annotated[str | None, typer.Option(help="The field an edit corrects.")] = None,
    value: Annotated[str | None, typer.Option(help="The corrected value, as JSON or plain text.")] = None,
    note: Annotated[str | None, typer.Option(help="Why.")] = None,
    accept_looks_good: Annotated[
        bool, typer.Option("--accept-looks-good", help="Accept every unreviewed record that looks good.")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Record a review of one record, or accept every record that looks good, like Check mode on the Paper screen."""
    if accept_looks_good:
        if record_id is not None or action is not None or field is not None or value is not None or note is not None:
            raise core.SageError("invalid", "--accept-looks-good takes only RUN; drop RECORD, ACTION and options")
        accepted = core.accept_looks_good(run_id)
        emit(accepted) if as_json else out.print(f"accepted {len(accepted)} that look good")
        return
    if record_id is None or action is None:
        raise core.SageError("invalid", "give RECORD and ACTION (accept, edit or reject), or --accept-looks-good")
    saved = core.add_review(
        core.Review(
            run_id=run_id, record_id=record_id, action=action, field=field, value=_json_or_text(value), note=note
        )
    )
    emit(saved) if as_json else out.print(f"{action} {record_id}")


def _json_or_text(value: str | None) -> JsonValue:
    if value is None:
        return None
    try:
        return JSON.validate_json(value)
    except ValidationError:
        return value


# ui ---------------------------------------------------------------------------------------------------------------


@app.command()
def ui(
    host: Annotated[str, typer.Option(help="Interface to bind. Use 0.0.0.0 to reach it from other machines.")] = (
        "127.0.0.1"
    ),
    port: Annotated[int, typer.Option(help="Port to serve on.", min=1, max=65535)] = 8765,
    as_json: JsonOpt = False,
) -> None:
    """Serve the web UI and the API. Starts without an API key; Settings takes one."""
    import uvicorn

    sock = core.listen(host, port)
    url = f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"
    if as_json:
        print(json.dumps({"url": url, "host": host, "port": port}), flush=True)
    else:
        out.print(f"SAGE UI on {url}")
    uvicorn.Server(uvicorn.Config("sage.api:app", log_level="warning")).run(sockets=[sock])


@app.command()
def openapi(as_json: JsonOpt = True) -> None:
    """Print the API's OpenAPI schema (the web client is generated from it)."""
    from sage.api import app as api

    del as_json
    sys.stdout.write(json.dumps(api.openapi(), indent=2) + "\n")
