"""Child processes: the detached runner, which outlives the server or terminal that queued its runs, and the coding
agents SAGE starts."""

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Literal

AgentState = Literal["not_installed", "not_signed_in", "ready"]


def spawn_sage(args: list[str], log: Path, *, keep: int) -> int:
    """Start `python -m sage ARGS` detached, in its own session, and return its pid. The child inherits the open
    descriptor `keep`, and with it any lock the descriptor holds."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as out:
        return subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "sage", *args],
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=(keep,),
        ).pid


def alive(pid: int) -> bool:
    """Whether `pid` still runs. A process this one spawned stays a zombie until it is reaped, and a zombie still
    answers signal 0, so reap our own child first; anyone else's process gets the plain probe."""
    with suppress(ChildProcessError):
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def agent_env(extra: Mapping[str, str]) -> dict[str, str]:
    """The small environment an agent SAGE starts gets: PATH with this SAGE's `sage` first, and HOME, then `extra` over
    them. No key reaches it."""
    path = os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")])
    return {"PATH": path, "HOME": str(Path.home())} | dict(extra)


def agent_state(programs: list[str], login_check: list[str] | None, path: str) -> tuple[AgentState, str | None]:
    """Whether an agent can start: every one of `programs` is on `path`, and `login_check`, when there is one, exits
    0. Otherwise also what is missing, or the last line the check printed."""
    absent = [p for p in programs if which(p, path) is None]
    if absent:
        return "not_installed", f"{', '.join(absent)} isn't on PATH"
    if login_check is not None:
        code, said = _run_quiet(login_check, timeout=30)
        if code != 0:
            return "not_signed_in", said or None
    return "ready", None


def _run_quiet(command: list[str], *, timeout: float) -> tuple[int, str]:
    """Run a short command and return its exit code and the last line it printed, stdout or stderr. A command that
    can't start or runs past `timeout` reads as exit code 127 or 124."""
    try:
        done = subprocess.run(  # noqa: S603
            command, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, check=False
        )
    except FileNotFoundError:
        return 127, f"{command[0]} isn't installed"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(command)} took longer than {timeout:.0f} seconds"
    lines = [line for line in (done.stderr + "\n" + done.stdout).splitlines() if line.strip()]
    return done.returncode, lines[-1].strip() if lines else ""


def which(program: str, path: str) -> str | None:
    """Where `program` is on `path`, or None."""
    return shutil.which(program, path=path)
