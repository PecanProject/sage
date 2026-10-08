"""One active extraction run per paper, so two runs never write into the same ir-store and results.

The lock is a file `runs/.locks/<paper_id>.lock` created with
`O_CREAT|O_EXCL` (atomic on a local filesystem) holding the holder's pid and
run id. A lock whose holder pid is no longer alive is STALE (crashed process,
`kill -9`, power loss) and is replaced; a lock whose holder is alive -- including
another thread of THIS process, which has the same pid -- refuses the new run
with `RunAlreadyActive`. Release only removes the lock if it still names the
releasing run, so a late release can never free someone else's lock.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from pipeline import run_store


class RunAlreadyActive(RuntimeError):
    """Raised when another live run already holds this paper's lock."""

    def __init__(self, paper_id: str, holder: dict):
        self.paper_id = paper_id
        self.holder = holder
        super().__init__(
            f"a run for paper '{paper_id}' is already active "
            f"(run_id={holder.get('run_id')}, pid={holder.get('pid')}, started_at={holder.get('started_at')}); "
            f"wait for it to finish or stop it before starting another run on the same paper."
        )


def _lock_dir() -> Path:
    return run_store._runs_root() / ".locks"


def lock_path(paper_id: str) -> Path:
    safe = paper_id.replace(os.sep, "_")
    return _lock_dir() / f"{safe}.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def read_lock(paper_id: str) -> Optional[dict]:
    path = lock_path(paper_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def active_holder(paper_id: str) -> Optional[dict]:
    """The live lock holder for this paper, or None (no lock, or a stale
    lock whose process is gone). Read-only: never removes anything."""
    holder = read_lock(paper_id)
    if holder and _pid_alive(int(holder.get("pid", 0))):
        return holder
    return None


def acquire(paper_id: str, run_id: str) -> Path:
    directory = _lock_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = lock_path(paper_id)
    payload = json.dumps({"paper_id": paper_id, "run_id": run_id, "pid": os.getpid(), "started_at": time.time()})
    for _ in range(2):  # second pass only after replacing a stale lock
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = read_lock(paper_id)
            if holder and _pid_alive(int(holder.get("pid", 0))):
                raise RunAlreadyActive(paper_id, holder)
            try:  # stale (or unreadable) lock: replace it
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        return path
    holder = read_lock(paper_id) or {}
    raise RunAlreadyActive(paper_id, holder)


def release(paper_id: str, run_id: str) -> None:
    holder = read_lock(paper_id)
    if holder is not None and holder.get("run_id") != run_id:
        return  # not ours -- never free another run's lock
    try:
        lock_path(paper_id).unlink()
    except FileNotFoundError:
        pass


@contextmanager
def paper_run_lock(paper_id: str, run_id: str) -> Iterator[Path]:
    path = acquire(paper_id, run_id)
    try:
        yield path
    finally:
        release(paper_id, run_id)
