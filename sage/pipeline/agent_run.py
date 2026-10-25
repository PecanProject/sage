"""Drive one run by a coding agent SAGE starts over ACP. The agent works in the paper's folder with the skill, calls
`sage extract check` itself and ends the run with `sage extract done`, which settles it. This keeps the run's log and
usage, answers the agent's permission requests, nudges an agent that stops before done, and settles what the folder
holds when the run ends any other way."""

import asyncio
import os
import re
import shlex
import traceback
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path

from sage.agents import acp_client
from sage.events import AgentAnswered, AgentAsked, Event, Note, RunError, RunFinished, RunStarted, Usage, now
from sage.ir.settled import Outcome
from sage.pipeline import extraction, paper_files
from sage.settings import AgentConfig, Settings
from sage.store import files, proc, skills
from sage.store.models import RunInfo, RunStatus
from sage.store.paths import Home, RunPaths

NUDGES = 2
"""How often an agent that ends its turn before done is asked to carry on."""
ANSWER_POLL_SECONDS = 0.5
SAGE_SAFE = re.compile(r"sage\s+(?:extract\s+(?:check|done|status)|papers|show|logs|runs|queue)\b")
"""The `sage` commands an agent may run on its own. Settings, providers and starting or stopping runs are the
scientist's to do."""
READS = frozenset({"read", "search"})
"""Request kinds that only look, allowed when they name paths the agent may read. A fetch reaches the network, so it
asks."""
LOOKING = frozenset({
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "sed", "pwd", "file", "stat", "echo", "sort", "uniq",
    "cut", "column",
})  # fmt: skip
"""Shell programs that only look, unless an option makes them write or run something (`_reads_only`)."""
WRITES = re.compile(r">|\$\(|`|\b(?:rm|mv|cp|tee|chmod|curl|wget|python|python3|node|bash|sh|eval|xargs)\b")
FIND_ACTIONS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls", "-fprint", "-fprint0", "-fprintf"})
"""The `find` actions that delete, write a file or run a command."""
SED_PRINT = re.compile(r"(?:\d+(?:,\d+)?)?p(?:;(?:\d+(?:,\d+)?)?p)*")
"""A `sed -n` script that only prints lines by number, such as `10,20p`. Any other command can write (`w`), run
(`e`) or edit."""
SEPARATORS = frozenset({";", "&&", "||", "|", "&"})
EDITS = frozenset({"edit", "delete", "move"})
SAGE_ONLY = (".sage", "paper.pdf", "paper.md")
"""What SAGE writes in the folder: an agent never edits these."""


class PermissionFile(acp_client.Permission):
    allowed: bool | None = None
    """Your answer, once you give it."""


@dataclass
class AgentRun:
    home: Home
    settings: Settings
    info: RunInfo
    agent: AgentConfig
    usage: Usage = field(default_factory=Usage)
    asking: asyncio.Lock = field(default_factory=asyncio.Lock)
    """One question at a time: an agent may ask twice at once, and there is one answer file."""

    @property
    def readable(self) -> list[Path]:
        """Where the agent may read without asking: the paper's folder and the skill, shipped or installed."""
        return [self.folder.resolve(), skills.shipped().resolve(), *(p.resolve() for p in skills.places())]

    @property
    def paths(self) -> RunPaths:
        return self.home.run(self.info.paper_id, self.info.id)

    @property
    def folder(self) -> Path:
        return self.home.paper(self.info.paper_id)

    def emit(self, event: Event) -> None:
        files.append_jsonl(self.paths.events, event)

    def update(self, **changes: object) -> RunInfo:
        """Change run.json's fields under the paper's lock. `sage extract done`, which the agent runs in its own
        process, writes run.json too: once it has finished the run, its status stands."""
        with files.exclusive(self.home.run_lock(self.info.paper_id)):
            disk = files.read_model_or_none(self.paths.info, RunInfo) or self.info
            if disk.status == "finished":
                changes.pop("status", None)
            self.info = disk.model_copy(update=changes)
            files.write_model(self.paths.info, self.info)
        return self.info


async def execute(home: Home, settings: Settings, info: RunInfo) -> RunInfo:
    """Run the agent to the end and return the final run.json. Like `run.execute`, nothing is raised and run.json never
    keeps `running` once this returns: a stop leaves `interrupted`, an error `failed`."""
    agent = settings.agents[info.agent or ""]
    run = AgentRun(home=home, settings=settings, info=info, agent=agent)
    run.update(status="running", pid=os.getpid(), started_at=info.started_at or now(), finished_at=None, error=None)
    outcome: Outcome | None = None
    status: RunStatus = "interrupted"
    try:
        run.emit(
            RunStarted(
                run_id=info.id,
                paper_id=info.paper_id,
                driver="agent",
                agent=info.agent,
                agent_model=info.agent_model,
                effort=info.effort,
                continued_from=info.parent_id,
                guidance=info.guidance,
            )
        )
        paper_files.materialize(home, info.paper_id, files.read_bytes(home.pdf(info.paper_id)), settings)
        limit = agent.time_limit_minutes * 60
        try:
            finished = await asyncio.wait_for(_drive(run), limit)
        except TimeoutError:
            run.emit(Note(level="warning", text=f"stopped at the {agent.time_limit_minutes}-minute limit"))
            outcome, status = "budget_exhausted", "budget_exhausted"
        else:
            if finished:
                status = "finished"
            else:
                outcome, status = "failed", "failed"
                run.update(error="the agent stopped before `sage extract done`; continue the run to carry on")
    except asyncio.CancelledError:
        run.emit(Note(level="warning", text="stopped before it finished; the folder keeps its work"))
        outcome, status = "failed", "interrupted"
    except BaseException as e:
        message = str(e) if isinstance(e, acp_client.AgentError) else f"{type(e).__name__}: {e}"
        run.update(error=message, refused=isinstance(e, acp_client.AgentError) and e.login)
        run.emit(RunError(message=message))
        if not isinstance(e, acp_client.AgentError):
            run.emit(Note(level="warning", text=traceback.format_exc()[-4000:]))
        outcome, status = "failed", "failed"
    finally:
        files.remove(run.paths.permission)
        if outcome is not None and run.update().status != "finished":
            extraction.salvage(home, settings, run.info, outcome)
            run.emit(
                RunFinished(
                    status=status, summary=run.info.summary, limit="time" if status == "budget_exhausted" else None
                )
            )
        run.update(status=status, finished_at=now(), pid=None, waiting=None)
    return run.info


async def _drive(run: AgentRun) -> bool:
    """Prompt the agent, and again while it ends its turn before done. Whether done accepted the run."""
    launch = replace(
        launch_for(run.agent, run.info.agent_model, env=_env(run), cwd=run.folder),
        effort=run.info.effort,
        effort_given=run.info.effort_given,
    )

    async with acp_client.session(launch, emit=run.emit, decide=partial(_decide, run)) as session:
        prompt = _prompt(run)
        for attempt in range(NUDGES + 1):
            turn = await session.prompt(prompt)
            run.usage += turn.usage
            usage = run.usage.model_copy(update={"cost_usd": session.cost})
            if run.update(usage=usage, turns=attempt + 1).status == "finished":
                return True
            if turn.stop_reason in ("cancelled", "refusal"):
                run.emit(Note(level="warning", text=f"the agent ended its turn: {turn.stop_reason}"))
                return False
            prompt = (
                f"The run isn't done yet. Run `sage extract check {run.info.paper_id}`, fix what blocks or mark it in "
                f'records/unresolved.yaml, then `sage extract done {run.info.paper_id} -m "summary"`. If you can\'t '
                "go on, say why."
            )
            run.emit(Note(text="the agent ended its turn before done; asking it to carry on"))
    return False


def _prompt(run: AgentRun) -> str:
    paper = run.info.paper_id
    skill = skills.places()[0]
    where = skill if skills.installed(skill) == skills.bundled() else skills.shipped()
    text = (
        f"Extract the paper {paper} with SAGE. The run {run.info.id} is already open, and your working directory is "
        f"the paper's folder. Follow the SAGE skill: read {where}/SKILL.md first, and its references beside it when "
        f"it says to. Check with `sage extract check {paper}` after each batch of edits, and finish with "
        f'`sage extract done {paper} -m "summary"`.\n\n'
        f"`sage extract check {paper} --all` lists every finding. Never edit `.sage/`, `paper.pdf` or `paper.md`: they "
        "are SAGE's."
    )
    if run.info.guidance is not None:
        text += f"\n\nThe scientist's guidance for this run:\n\n{run.info.guidance}"
    return text


def launch_for(agent: AgentConfig, model: str | None, *, env: dict[str, str], cwd: Path) -> acp_client.Launch:
    """How to start the agent on `model`: as a launch flag for an agent that takes it there, else in the session."""
    flag = agent.model_flag is not None and model is not None
    return acp_client.Launch(
        command=agent.command,
        args=(*agent.args, agent.model_flag, model) if flag else tuple(agent.args),
        env=env,
        cwd=cwd,
        mode=agent.mode,
        model=None if flag else model,
    )


def _env(run: AgentRun) -> dict[str, str]:
    """PATH with this SAGE's `sage` first, so the agent's `sage extract` calls reach this library, and the agent's own
    CLI, so its adapter uses that sign-in. Keys are never passed."""
    env = proc.agent_env({"SAGE_HOME": str(run.settings.home_dir)})
    claude = proc.which("claude", env["PATH"])
    if claude is not None:
        env["CLAUDE_CODE_EXECUTABLE"] = claude
    return env | run.agent.env


async def _decide(run: AgentRun, ask: acp_client.Permission) -> bool:
    """Everything is allowed unless `agents_ask` is on. Then reading anything in the folder, editing the agent's own
    files there and running `sage extract check`/`done` are allowed; editing SAGE's files is refused; anything else
    waits for your answer in the UI or `sage extract answer`."""
    if not run.settings.agents_ask:
        return True
    rule = _rule(run.folder.resolve(), run.readable, ask)
    if rule is not None:
        if not rule:
            run.emit(AgentAnswered(allowed=False, by="rule"))
            run.emit(Note(level="warning", text=f"refused: {ask.title} (SAGE's own files)"))
        return rule
    async with run.asking:
        return await _ask(run, ask)


async def _ask(run: AgentRun, ask: acp_client.Permission) -> bool:
    pending = PermissionFile(**ask.model_dump())
    files.write_model(run.paths.permission, pending)
    run.emit(AgentAsked(title=ask.title, kind=ask.kind, command=ask.command, paths=ask.paths))
    run.update(waiting=ask.command or ask.title)
    while True:
        await asyncio.sleep(ANSWER_POLL_SECONDS)
        answer = files.read_model_or_none(run.paths.permission, PermissionFile)
        if answer is not None and answer.allowed is not None:
            break
    files.remove(run.paths.permission)
    run.update(waiting=None)
    run.emit(AgentAnswered(allowed=answer.allowed, by="you"))
    return answer.allowed


def _rule(folder: Path, readable: list[Path], ask: acp_client.Permission) -> bool | None:
    """True or False when SAGE decides on its own, None when it asks you."""
    if ask.kind == "think":  # it reads and runs nothing
        return True
    if ask.kind in READS and ask.paths and all(_within(readable, p, folder) for p in ask.paths):
        return True
    if ask.kind in EDITS and ask.paths and all(_inside(folder, p) is not None for p in ask.paths):
        return all(_editable(folder, p) for p in ask.paths)
    command = ask.command
    if command is not None and (_looks(command, readable, folder) or _makes(command, folder)):
        return True
    return None


def _commands(command: str) -> list[list[str]] | None:
    """The simple commands in `command`, split at `;`, `&&`, `||`, `|`, `&` and line breaks, each as its words with
    the shell's quoting removed. None when it holds anything else the shell reads (a redirection, a subshell, a
    process substitution) or a quote that isn't closed."""
    if WRITES.search(command.replace("2>&1", "")):
        return None
    lexer = shlex.shlex(command.replace("2>&1", "").replace("\n", ";"), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    commands: list[list[str]] = [[]]
    try:
        for token in lexer:
            if token in SEPARATORS:
                commands.append([])
            elif token and set(token) <= set(lexer.punctuation_chars):
                return None
            else:
                commands[-1].append(token)
    except ValueError:
        return None
    return [words for words in commands if words]


def _makes(command: str, folder: Path) -> bool:
    """`mkdir` of directories the agent may edit, such as `records/observations`."""
    commands = _commands(command)
    if commands is None or len(commands) != 1 or commands[0][0] != "mkdir":
        return False
    dirs = [w for w in commands[0][1:] if not w.startswith("-")]
    return bool(dirs) and all(_editable(folder, d) for d in dirs)


def _editable(folder: Path, path: str) -> bool:
    """Inside the folder and not one of SAGE's own files."""
    rel = _inside(folder, path)
    return rel is not None and rel.parts[:1] not in [(x,) for x in SAGE_ONLY]


def _looks(command: str, readable: list[Path], folder: Path) -> bool:
    """A command that only reads or runs `sage`: every part one of LOOKING or `sage …`, or a `for` loop over them, and
    every path it names (absolute, `~`, or relative to the folder) one the agent may read. A `$` is allowed only as a
    loop's own variable, so nothing reaches a path through the environment."""
    commands = _commands(command)
    if commands is None or not commands:
        return False
    loops: set[str] = set()
    for part in commands:
        words = part[1:] if part[0] == "do" else part
        if not words or words == ["done"]:
            continue
        if words[0] == "for" and len(words) > 2 and words[2] == "in":
            loops.add(words[1])
            words = words[2:]
        elif words[0] == "cd":
            # Relative paths are judged from the folder, so the command may only stay in it.
            if len(words) != 2 or _inside(folder, words[1], base=folder) != Path():
                return False
            continue
        elif SAGE_SAFE.match(" ".join(words)):
            continue  # its arguments name no file to read
        elif not _reads_only(words):
            return False
        if not all(_word_ok(w, loops, readable, folder) for w in words[1:]):
            return False
    return True


def _reads_only(words: list[str]) -> bool:
    """One of LOOKING, with no option that makes it write or run something."""
    name, args = words[0], words[1:]
    match name:
        case "find":
            return FIND_ACTIONS.isdisjoint(args)
        case "sed":
            return (
                len(args) > 1
                and args[0] == "-n"
                and SED_PRINT.fullmatch(args[1]) is not None
                and not any(a.startswith("-") for a in args[2:])
            )
        case "sort":
            return not any(_option(a, "o", "output", "compress-program") for a in args)
        case "file":
            return not any(_option(a, "C", "compile") for a in args)
        case "rg":
            return not any(a == "--pre" or a.startswith("--pre=") for a in args)
        case "uniq":
            return sum(not a.startswith("-") for a in args) < 2  # a second file is where it writes
    return name in LOOKING


def _option(arg: str, short: str, *long: str) -> bool:
    """Whether `arg` gives the option `-<short>`, alone or among other short options, or one of the `long` names,
    which getopt also takes abbreviated."""
    if arg.startswith("--"):
        name = arg[2:].partition("=")[0]
        return bool(name) and any(n.startswith(name) for n in long)
    return arg.startswith("-") and short in arg[1:]


def _word_ok(word: str, loops: set[str], readable: list[Path], folder: Path) -> bool:
    if "$" in word:
        return re.fullmatch(r"\$\{?(\w+)\}?", word) is not None and word.strip("${}") in loops
    if word.startswith("-") and "/" in word:
        # An option's value can name a file (`--file=/etc/x`); one stuck to a short option (`-f/etc/x`) asks.
        if "=" not in word:
            return False
        word = word.partition("=")[2]
    if word.startswith(("/", "~", "..")) or "/" in word:
        return _within(readable, word, folder)
    return True


def _within(roots: list[Path], path: str, folder: Path) -> bool:
    return any(_inside(root, path, base=folder) is not None for root in roots)


def _inside(root: Path, path: str, *, base: Path | None = None) -> Path | None:
    """`path` relative to `root` when it lies inside, else None. A relative path is taken from `base`, else `root`."""
    p = Path(path.strip("'\"")).expanduser()
    resolved = (p if p.is_absolute() else (base or root) / p).resolve()
    return resolved.relative_to(root) if resolved.is_relative_to(root) else None
