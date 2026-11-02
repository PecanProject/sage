"""The one process that executes runs. A run waits in run.json as `queued` until the runner takes it on. The runner
keeps up to `parallel_runs` going at once, each as a task in this process, and stops a run when its folder holds a
stop request. It leaves once nothing has run or waited for `IDLE_SECONDS`."""

import asyncio
import os
import signal
from collections.abc import Callable
from contextlib import suppress

from sage.agents.models import Route
from sage.events import RunError, RunFinished, now
from sage.pipeline import agent_run, run_state
from sage.pipeline.run import execute
from sage.settings import Settings
from sage.store import files, proc
from sage.store.models import RunInfo, RunStatus
from sage.store.paths import Home

POLL_SECONDS = 0.5
"""How often the runner looks for queued runs and stop requests: soon enough that a click feels immediate."""

IDLE_SECONDS = 30.0


class RefusedError(Exception):
    """A run can't start until its provider or agent is set up: a key, a sign-in, an install."""


RouteFor = Callable[[Settings, RunInfo], Route | None]
"""How to reach a run's model. It raises, with a message the run records, when a provider lacks what it needs or the
run's agent isn't configured."""


def queued(home: Home) -> list[RunInfo]:
    """Every queued run, oldest first, which is the order they start in."""
    return [r for r in run_state.listed(home) if r.status == "queued"][::-1]


def wake(home: Home) -> None:
    """Start the runner unless one is live. This process takes the runner's lock and hands it to the child, so there
    is never a moment when a queued run has no runner and no one starting one."""
    fd = files.lock(home.runner_lock)
    if fd is None:
        return
    try:
        pid = proc.spawn_sage(["runner", "--lock-fd", str(fd)], home.runner_log, keep=fd)
        files.write_text(home.runner_pid, str(pid))
    finally:
        files.unlock(fd)


def serve(home: Home, settings: Callable[[], Settings], route: RouteFor, lock_fd: int | None) -> None:
    """The runner process's body: run queued runs until none are left for a while. `lock_fd` is the runner's lock,
    taken by the process that started this one. Without it, this takes the lock itself, and leaves at once when
    another runner holds it."""
    fd = lock_fd if lock_fd is not None else files.lock(home.runner_lock)
    while fd is not None:
        files.write_text(home.runner_pid, str(os.getpid()))
        idle = asyncio.run(_serve(home, settings, route))
        files.unlock(fd)
        # A run queued just as this runner chose to leave found the lock held and started no runner: look once more.
        fd = files.lock(home.runner_lock) if idle and queued(home) else None


async def _serve(home: Home, settings: Callable[[], Settings], route: RouteFor) -> bool:
    """Run queued runs until SIGINT or SIGTERM, which stops every live run, or until idle. Returns whether it left
    because it was idle, when the caller has to look once more for a run queued as it left."""
    tasks: dict[str, tuple[RunInfo, asyncio.Task[RunInfo]]] = {}
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)
    idle = 0.0
    while not stopping.is_set():
        for run_id, (info, task) in list(tasks.items()):
            stop = home.run(info.paper_id, run_id).stop
            if task.done():
                files.remove(stop)
                del tasks[run_id]
            elif files.exists(stop):
                task.cancel()
        waiting = [r for r in queued(home) if r.id not in tasks]
        s = settings()
        for info in waiting:
            if files.exists(home.run(info.paper_id, info.id).stop):
                _end(home, info, "interrupted", None)
            elif len(tasks) < s.parallel_runs:
                try:
                    found = route(s, info)
                except Exception as e:
                    _end(home, info, "failed", str(e), refused=isinstance(e, RefusedError))
                    continue
                job = agent_run.execute(home, s, info) if found is None else execute(home, s, info, route=found)
                tasks[info.id] = (info, asyncio.create_task(job))
        idle = 0.0 if tasks or waiting else idle + POLL_SECONDS
        if idle >= IDLE_SECONDS:
            return True
        with suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), POLL_SECONDS)
    for _, task in tasks.values():
        task.cancel()
    await asyncio.gather(*(task for _, task in tasks.values()), return_exceptions=True)
    return False


def _end(home: Home, info: RunInfo, status: RunStatus, error: str | None, *, refused: bool = False) -> None:
    """End a queued run that never started: stopped before its turn, or missing a key or a sign-in. Its log says
    which, as a run that started would."""
    paths = home.run(info.paper_id, info.id)
    if error is not None:
        files.append_jsonl(paths.events, RunError(message=error))
    files.append_jsonl(paths.events, RunFinished(status=status))
    update = {"status": status, "error": error, "refused": refused, "finished_at": now(), "pid": None}
    files.write_model(paths.info, info.model_copy(update=update))
    files.remove(paths.stop)
