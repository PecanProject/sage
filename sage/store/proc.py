"""Detached child processes. A run outlives the server or terminal that started it."""

import os
import signal
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path


@contextmanager
def spawn_sage(args: list[str], log: Path) -> Generator[int]:
    """Start `python -m sage ARGS` detached and yield its pid. The child holds in `wait_for_parent` until this
    block exits, so whatever the parent writes inside the block lands before the child's first write."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as out:
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "sage", *args],
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            start_new_session=True,
        )
    assert proc.stdin is not None
    try:
        yield proc.pid
    finally:
        proc.stdin.close()


def wait_for_parent() -> None:
    """Block until the spawning parent leaves `spawn_sage`. Its exit closes the pipe too, so this never hangs."""
    sys.stdin.buffer.read()


def alive(pid: int) -> bool:
    """Whether `pid` still runs. A worker this process spawned stays a zombie until it is reaped, and a zombie still
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


def interrupt(pid: int) -> None:
    with suppress(ProcessLookupError):
        os.kill(pid, signal.SIGINT)
