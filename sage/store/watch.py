"""Changes to files under a folder, as they happen."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from watchfiles import awatch

STEP_MS = 100
"""A batch closes once nothing has changed for this long."""
DEBOUNCE_MS = 1000
"""…or once it has been open this long, so steady writes such as a run's log arrive about once a second."""
WAKE_MS = 1000
"""How often the watching thread wakes with nothing changed. A reader that goes away waits at most this long for it
to stop."""


async def changed(root: Path) -> AsyncIterator[set[Path]]:
    """Each batch of paths added, changed or removed under `root`, relative to it, until the caller stops iterating."""
    bases = await asyncio.to_thread(_bases, root)
    async for batch in awatch(root, step=STEP_MS, debounce=DEBOUNCE_MS, rust_timeout=WAKE_MS):
        yield {rel for _, p in batch if (rel := _relative(Path(p), bases)) is not None}


def _bases(root: Path) -> tuple[Path, ...]:
    """Make `root`, and return the paths the OS may report it by: the real one first, since under a symlink such as
    macOS's /tmp the two differ."""
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve(), root.absolute()


def _relative(path: Path, bases: tuple[Path, ...]) -> Path | None:
    for base in bases:
        if path.is_relative_to(base):
            return path.relative_to(base)
    return None
