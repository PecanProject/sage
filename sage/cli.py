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
models_app = typer.Typer(help="The models extraction may use, with their prices.")
app.add_typer(models_app, name="models")
providers_app = typer.Typer(
    help="Where models come from: Anthropic, OpenAI or compatible, Azure, Gemini, Vertex, Bedrock."
)
app.add_typer(providers_app, name="providers")
extract_app = typer.Typer(
    no_args_is_help=True,
    help="Extract a paper by editing the files in its folder: start, check until clean, done. The SAGE skill says how.",
)
app.add_typer(extract_app, name="extract")
agents_app = typer.Typer(help="Coding agents SAGE can start: Claude Code, Codex, Gemini CLI, OpenCode.")
app.add_typer(agents_app, name="agents")
skill_app = typer.Typer(no_args_is_help=True, help="The SAGE skill, which tells coding agents how to extract.")
app.add_typer(skill_app, name="skill")

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
def config_set(
    key: str,
    value: str,
    as_json: JsonOpt = False,
) -> None:
    """Change a setting in config.toml. An empty value restores the default."""
    view = core.update_settings({key: value})
    emit({key: view.values[key]}) if as_json else print(f"{key} = {json.dumps(view.values[key])}")


@models_app.callback(invoke_without_command=True)
def models_list(ctx: typer.Context, as_json: JsonOpt = False) -> None:
    """Your models, with their price, whether their provider is ready, and which one extracts."""
    if ctx.invoked_subcommand is not None:
        return
    s = core.settings()
    found = core.models()
    if as_json:
        emit(found)
        return
    ready = {p.id: not p.missing for p in core.providers()}
    table = Table(ids("model"), "name", "$ in / out per M", "effort", "provider", "default for")
    for m in found:
        price = f"{m.input_usd:g} / {m.output_usd:g}" + (" (yours)" if m.your_price else "")
        status = "ready" if ready.get(m.provider, False) else "needs setup"
        effort = m.effort or "doesn't think"
        table.add_row(m.id, m.title, price, effort, status, "extract" if s.default_using() == m.id else "")
    out.print(table)


@models_app.command("available")
def models_available(
    provider: Annotated[str | None, typer.Argument(help="A provider id. Default: every ready one.")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Ask each provider which models it lets you use, which tests its credentials too. Costs nothing."""
    chosen = [provider] if provider is not None else [p.id for p in core.providers() if not p.missing]
    if not chosen:
        raise core.SageError("missing_key", "no provider is set up yet; see `sage providers`")
    found = [core.available_models(p) for p in chosen]
    if as_json:
        emit(found)
    for a in [] if as_json else found:
        out.print(Text.assemble((f"{a.provider}: ", "bold"), (a.message, "green" if a.ok else "red")))
        for m in a.models:
            price = "no price" if m.input_usd is None else f"${m.input_usd:g} / ${m.output_usd:g}"
            notes = [n for n, on in (("yours", m.yours), ("can't call tools", not m.tools)) if on]
            out.print(f"  {m.id}  {m.title}  {price}" + (f" ({', '.join(notes)})" if notes else ""))
    if not found or not all(a.ok for a in found):
        raise typer.Exit(1)


@models_app.command("add")
def models_add(
    model_id: Annotated[str, typer.Argument(help="As provider:name, such as work:gpt-5.1.")],
    price: Annotated[
        tuple[float, float] | None,
        typer.Option(help="Your dollars per million input and output tokens. Needed where SAGE has no price."),
    ] = None,
    cache_price: Annotated[
        tuple[float, float] | None,
        typer.Option(
            help="Your dollars per million tokens read from and written to the prompt cache, with --price. Default: "
            "genai-prices' discount for the model, else the input price."
        ),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Add a model to yours, or change its price."""
    found = core.add_model(model_id, price=price, cache_price=cache_price)
    emit(found) if as_json else out.print(", ".join(m.id for m in found))


@models_app.command("set")
def models_set(
    model_id: Annotated[str, typer.Argument(help="One of your models, as `sage models` lists it.")],
    effort: Annotated[
        str, typer.Option(help="How hard SAGE's own agent has it think: one of the levels `sage models --json` lists.")
    ],
    as_json: JsonOpt = False,
) -> None:
    """Change how hard one of your models thinks when SAGE's own agent runs it."""
    found = core.set_model_effort(model_id, effort)
    if as_json:
        emit(found)
        return
    out.print(", ".join(f"{m.id} {m.effort or 'no effort'}" for m in found))


@models_app.command("remove")
def models_remove(model_ids: list[str], as_json: JsonOpt = False) -> None:
    """Remove models from yours. If Extract with named one, it goes back to the first model left."""
    found = core.remove_models(model_ids)
    emit(found) if as_json else out.print(", ".join(m.id for m in found))


@providers_app.callback(invoke_without_command=True)
def providers_list(ctx: typer.Context, as_json: JsonOpt = False) -> None:
    """Your providers, and what each still needs."""
    if ctx.invoked_subcommand is not None:
        return
    found = core.providers()
    if as_json:
        emit(found)
        return
    table = Table(ids("provider"), "kind", "name", "status")
    for p in found:
        table.add_row(p.id, p.kind, p.title, "ready" if not p.missing else f"needs {' and '.join(p.missing)}")
    out.print(table)


@providers_app.command("kinds")
def providers_kinds(as_json: JsonOpt = False) -> None:
    """The kinds of provider you can add, and the fields each takes."""
    found = core.provider_kinds()
    if as_json:
        emit(found)
    for k in [] if as_json else found:
        out.print(Text.assemble((k.kind, "bold"), f"  {k.title}: {k.help}"))
        out.print("  fields: " + ", ".join(f.name for f in k.fields) + ", name")


@providers_app.command("add")
def providers_add(
    provider: Annotated[str, typer.Argument(help="An id of your own, such as work or azure-eu.")],
    kind: Annotated[str, typer.Argument(help="anthropic, openai, azure, google, vertex or bedrock.")],
    name: Annotated[str | None, typer.Option(help="What the Manage page calls it.")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Add a provider. Then give it its fields with `sage providers set`."""
    found = core.save_provider(provider, {} if name is None else {"name": name}, kind=kind)
    emit(found) if as_json else out.print(f"added {found.id}; it needs {' and '.join(found.missing) or 'nothing more'}")


@providers_app.command("set")
def providers_set(
    provider: str,
    field: Annotated[str, typer.Argument(help="api_key, base_url, region, project, api_version, aws_profile, ...")],
    value: Annotated[
        str | None, typer.Argument(help="Leave it out to type it at a hidden prompt, which keeps a key out of history.")
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Set one field of a provider. An empty value clears it."""
    if value is None:
        value = str(typer.prompt(field, hide_input=True, default="", show_default=False))
    found = core.save_provider(provider, {field: value})
    if as_json:
        emit(found)
    else:
        status = "ready" if not found.missing else f"needs {' and '.join(found.missing)}"
        out.print(f"{provider}: {field} {'set' if value.strip() else 'cleared'}; {status}")


@providers_app.command("remove")
def providers_remove(provider: str, as_json: JsonOpt = False) -> None:
    """Remove a provider you added. Its models have to go first."""
    found = core.remove_provider(provider)
    emit(found) if as_json else out.print(", ".join(p.id for p in found))


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


# extract ----------------------------------------------------------------------------------------------------------

PaperArg = Annotated[str, typer.Argument(help="The paper's id, as `sage papers` lists it.")]


@extract_app.command("run")
def extract_run(
    paper_ids: Annotated[
        list[str] | None, typer.Argument(help="Papers to extract. Omit them with --all, --resume or --continue.")
    ] = None,
    every: Annotated[bool, typer.Option("--all", help="Extract every paper with no data and nothing running.")] = False,
    using: Annotated[
        str | None,
        typer.Option(
            "--with",
            help="An agent (claude, codex, or claude:sonnet for its model) or a model SAGE's own agent runs on. "
            "Default: the extract_with setting, else the first of your models.",
        ),
    ] = None,
    effort: Annotated[
        str | None,
        typer.Option(
            help="How hard it thinks: the model's or agent's own level; see `sage models` or `sage agents offer`. "
            "Default: the model's or agent's effort setting."
        ),
    ] = None,
    resume: Annotated[
        str | None, typer.Option(help="Carry on this unfinished run with what its budget has left.")
    ] = None,
    continue_from: Annotated[
        str | None,
        typer.Option("--continue", help="Start a new run after this one, with --guidance on what to add or fix."),
    ] = None,
    guidance: Annotated[
        str | None, typer.Option(help="What the agent should add or fix, with --continue or --resume.")
    ] = None,
    follow: Annotated[
        bool, typer.Option(help="Stream the run's log until it ends. Ctrl-C stops watching only.")
    ] = True,
    as_json: JsonOpt = False,
) -> None:
    """Have SAGE extract papers. Runs go in the background, `parallel_runs` at a time, the rest queued. One is
    followed to its end; several print a line as each ends."""
    if resume is not None:
        info = core.resume_run(resume, guidance=guidance)
    elif continue_from is not None:
        info = core.continue_run(continue_from, guidance, using=using, effort=effort)
    elif (paper_ids is None) == (not every):
        raise core.SageError("invalid", "give paper ids or --all, or --resume or --continue <run id>")
    else:
        started = core.start_runs(None if every else paper_ids, using=using, effort=effort)
        if len(started) > 1 or every:
            _follow_many(started, follow=follow, as_json=as_json)
            return
        info = started[0]
    if not follow:
        emit(info) if as_json else out.print(Text.assemble("started run ", (info.id, "bold"), f" on {info.paper_id}"))
        return
    if not as_json:
        out.print(Text.assemble("run ", (info.id, "bold"), f" on {info.paper_id} ({_driver_line(info)})"))
    _follow(info.id, as_json=as_json)
    final = core.run(info.id)
    if as_json:
        emit(final)
    else:
        _print_run_line(final)


def _driver_line(info: core.RunInfo) -> str:
    if info.driver == "here":
        return "from a terminal"
    effort = f", effort {info.effort}" if info.effort is not None else ""
    if info.driver == "agent":
        return f"{info.agent}" + (f", {info.agent_model}" if info.agent_model is not None else "") + effort
    return f"{info.extraction_model}{effort}"


@extract_app.command("start")
def extract_start(
    paper_id: PaperArg,
    guidance: Annotated[str | None, typer.Option(help="What this run should add or fix.")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Open a run on a paper that you (or the agent in this terminal) drive. Prints the folder to work in."""
    status = core.extract_start(paper_id, guidance=guidance)
    if as_json:
        emit(status)
        return
    run_id = status.run.id if status.run is not None else "?"
    out.print(f"run {run_id} open on {status.paper_id}\nfolder: {status.folder}")


@extract_app.command("check")
def extract_check(
    paper_id: PaperArg,
    every: Annotated[bool, typer.Option("--all", help="List every error and warning, not the first few.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Check the paper's files. Every problem is `file:line  target: what to do`. Exits 1 while anything blocks, 2 when
    a file doesn't parse at all."""
    report = core.extract_check(paper_id, every=every)
    emit(report) if as_json else out.print(report.text())
    raise typer.Exit(2 if report.unreadable else 1 if report.blocking_total else 0)


@extract_app.command("done")
def extract_done(
    paper_id: PaperArg,
    summary: Annotated[str, typer.Option("--summary", "-m", help="What was extracted, and what wasn't and why.")],
    as_json: JsonOpt = False,
) -> None:
    """Finish the run: refused while anything blocks, otherwise its records are settled and it ends."""
    report = core.extract_done(paper_id, summary)
    if as_json:
        emit(report)
    else:
        out.print(report.text())
    raise typer.Exit(0 if report.accepted else 1)


@extract_app.command("stop")
def extract_stop(
    paper_ids: Annotated[
        list[str] | None, typer.Argument(help="Papers whose run to stop. Omit them with --all.")
    ] = None,
    every: Annotated[bool, typer.Option("--all", help="Stop every open run.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Stop runs. The files stay as they are, for the next run."""
    if (paper_ids is None) == (not every):
        raise core.SageError("invalid", "give paper ids, or --all")
    found = core.extract_stop(None if every else paper_ids)
    emit(found) if as_json else out.print(f"stopped {len(found)} run{'' if len(found) == 1 else 's'}")


@extract_app.command("answer")
def extract_answer(
    paper_id: PaperArg,
    allow: Annotated[bool, typer.Option("--allow/--deny", help="Let the agent do what it asks, or refuse.")],
    as_json: JsonOpt = False,
) -> None:
    """Answer what the agent extracting a paper is waiting on, as `sage extract status` shows it."""
    info = core.answer_agent(paper_id, allow=allow)
    emit(info) if as_json else out.print(f"{'allowed' if allow else 'refused'}: {info.waiting}")


@agents_app.callback(invoke_without_command=True)
def agents_default(ctx: typer.Context, as_json: JsonOpt = False) -> None:
    """Coding agents SAGE can start in a paper's folder, and whether each is ready."""
    if ctx.invoked_subcommand is None:
        agents_list(as_json=as_json)


@agents_app.command("list")
def agents_list(as_json: JsonOpt = False) -> None:
    """Coding agents SAGE can start in a paper's folder, and whether each is ready."""
    found = core.agents()
    if as_json:
        emit(found)
        return
    table = Table(ids("agent"), "name", "state", "model", "effort", "running", "fix")
    for a in found:
        state = a.state.replace("_", " ") + (f": {a.detail}" if a.detail is not None and a.state != "ready" else "")
        model, effort = a.model or "its default", a.effort or "its default"
        table.add_row(a.id, a.title, state, model, effort, str(a.running or ""), a.fix or "")
    out.print(table)


AgentArg = Annotated[str, typer.Argument(help="The agent's id, as `sage agents` lists it.")]


@agents_app.command("offer")
def agents_offer(
    agent_id: AgentArg,
    model: Annotated[
        str | None, typer.Option(help="The agent's own name for a model, to list its efforts. Default: its default.")
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Start the agent in an empty folder and list the models and modes it offers, and the efforts of the model.
    Spends no tokens."""
    found = core.agent_offer(agent_id, model)
    if as_json:
        emit(found)
        return
    out.print(f"models: {', '.join(found.models)} (now {found.model})")
    out.print(f"modes: {', '.join(found.modes)} (now {found.mode})")
    efforts = f"{', '.join(found.efforts)} (now {found.effort})" if found.efforts else "none for this model"
    out.print(f"efforts: {efforts}")


@agents_app.command("set")
def agents_set(
    agent_id: AgentArg,
    model: Annotated[
        str | None, typer.Option(help="The agent's own name for the model to use. Empty: its default.")
    ] = None,
    effort: Annotated[
        str | None,
        typer.Option(help="The agent's own level, one `sage agents offer AGENT --model M` lists. Empty: its default."),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Set the model and effort an agent runs with. A run checks them when the agent starts."""
    found = core.set_agent(agent_id, model=model, effort=effort)
    if as_json:
        emit(found)
        return
    out.print(f"{found.id}: model {found.model or 'its default'}, effort {found.effort or 'its default'}")


@skill_app.command("install")
def skill_install(as_json: JsonOpt = False) -> None:
    """Install the skill for Claude Code, Codex, Gemini and other agents (~/.agents/skills/sage, linked from
    ~/.claude/skills/sage)."""
    _print_skill(core.install_skill(), as_json)


@skill_app.command("status")
def skill_status(as_json: JsonOpt = False) -> None:
    """Whether the skill is installed and current."""
    _print_skill(core.skill_status(), as_json)


def _print_skill(places: list[core.SkillPlace], as_json: bool) -> None:
    if as_json:
        emit(places)
        return
    for p in places:
        link = " (link)" if p.linked else ""
        out.print(f"{p.state:<9} {p.folder}{link}")


@extract_app.command("status")
def extract_status(
    paper_id: Annotated[str | None, typer.Argument(help="Only this paper.")] = None, as_json: JsonOpt = False
) -> None:
    """Each paper's folder and its open or latest run."""
    found = core.extract_status(paper_id)
    if as_json:
        emit(found)
        return
    table = Table(ids("paper"), "run", "by", "status", "folder")
    for s in found:
        r = s.run
        by = "" if r is None else (r.agent or "agent") if r.driver == "agent" else r.driver
        table.add_row(s.paper_id, r.id if r else "", by, r.status if r else "not extracted", s.folder)
    out.print(table)


@app.command("runner", hidden=True)
def runner(
    lock_fd: Annotated[int | None, typer.Option(help="The runner's lock, taken by whoever started it.")] = None,
) -> None:
    """The one process that runs every queued extraction. Whoever queues a run starts it when none is alive; it leaves
    when nothing is left to run. Started by hand while another runs, it leaves at once."""
    core.serve_runs(lock_fd)


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
            r.id, r.paper_id, r.status, r.extraction_model, str(r.turns),
            f"${r.usage.cost_usd:.3f}", str(c.records), str(c.verified), str(c.flagged), str(c.unresolved),
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
    if path is None:
        text = exported.data.decode()
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return
    core.write_export(path, exported.data)
    if as_json:
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
        f"{r.id}  {status}  {r.turns} turn{'' if r.turns == 1 else 's'}  ${r.usage.cost_usd:.3f}  "
        f"{c.records} records: {c.verified} verified, {c.flagged} flagged, {c.unresolved} unresolved"
        + (f"\n  error: {r.error}" if r.error is not None else "")
    )


def _follow_many(started: list[core.RunInfo], *, follow: bool, as_json: bool) -> None:
    """Print a line as each run ends, until all have."""
    if not as_json:
        out.print(f"queued {len(started)} run{'' if len(started) == 1 else 's'}")
    if not follow:
        if as_json:
            emit(started)
        return
    left = {r.id for r in started}
    try:
        while left:
            for r in [core.run(i) for i in sorted(left)]:
                if r.status not in ("queued", "running"):
                    left.discard(r.id)
                    if not as_json:
                        out.print(
                            Text.assemble(
                                (f"{r.finished_at:%H:%M:%S} " if r.finished_at else "", "dim"), (r.paper_id, "bold")
                            )
                        )
                        _print_run_line(r)
            if left:
                time.sleep(2)
    except KeyboardInterrupt:
        if not as_json:
            err.print("stopped watching; the runs go on. `sage runs` shows where they are")
    if as_json:
        emit([core.run(r.id) for r in started])


# review -----------------------------------------------------------------------------------------------------------


@app.command()
def queue(
    run_id: Annotated[str, typer.Argument(help="The run whose records to list.")],
    as_json: JsonOpt = False,
) -> None:
    """Every record of a run with its status and your review. `sage papers` has each paper's counts."""
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
    """Serve the web UI and the API, one per data folder. Starts with nothing set up; the Manage page adds providers,
    models and agents."""
    import uvicorn

    lock = core.claim_ui()
    try:
        sock = core.listen(host, port)
        url = f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"
        core.announce_ui(url)
        if as_json:
            print(json.dumps({"url": url, "host": host, "port": port}), flush=True)
        else:
            out.print(f"SAGE UI on {url}")
        # /api/changes never ends on its own, so shutdown would wait on every open window without a limit.
        config = uvicorn.Config("sage.api:app", log_level="warning", timeout_graceful_shutdown=1)
        uvicorn.Server(config).run(sockets=[sock])
    finally:
        core.release_ui(lock)


@app.command()
def openapi(as_json: JsonOpt = True) -> None:
    """Print the API's OpenAPI schema (the web client is generated from it)."""
    from sage.api import app as api

    del as_json
    sys.stdout.write(json.dumps(api.openapi(), indent=2) + "\n")
