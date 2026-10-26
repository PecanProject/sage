"""The files an agent writes in a paper's folder, read as text, and the snapshots `check` keeps of them."""

import hashlib
from pathlib import Path

from sage.store import files

DIGEST = "digest"
"""The file in a snapshot that names what it holds, so an unchanged folder takes no new snapshot."""


AGENT_FOLDERS: dict[str, tuple[str, ...]] = {"records": (".yaml", ".yml"), "tables": (".csv",), "pages": (".md",)}
"""What the agent writes, by folder. Everything else in the paper's folder is SAGE's."""


def read_agent_files(base: Path) -> dict[str, str]:
    """The agent's files under `base`, a paper's folder or a snapshot of it."""
    out: dict[str, str] = {}
    for folder, suffixes in AGENT_FOLDERS.items():
        out |= _read_tree(base, folder, suffixes)
    return out


def _read_tree(base: Path, folder: str, suffixes: tuple[str, ...]) -> dict[str, str]:
    """Every file under `base/folder` ending in one of `suffixes`, by its path relative to `base` with forward
    slashes ("records/observations/p7.t1.yaml"). Hidden files and folders are left out."""
    root = base / folder
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(base)
        if not path.is_file() or any(part.startswith(".") for part in rel.parts) or not path.name.endswith(suffixes):
            continue
        out[rel.as_posix()] = files.read_text(path)
    return out


def digest(texts: dict[str, str]) -> str:
    h = hashlib.sha256()
    for name in sorted(texts):
        h.update(name.encode() + b"\0" + texts[name].encode() + b"\0")
    return h.hexdigest()


def latest_snapshot(snapshots: Path) -> tuple[int, str | None]:
    """The newest snapshot's number and digest; (0, None) when there is none."""
    numbers = [int(d.name) for d in files.child_dirs(snapshots) if d.name.isdigit()]
    if not numbers:
        return 0, None
    n = max(numbers)
    stamp = snapshots / str(n) / DIGEST
    return n, files.read_text(stamp).strip() if stamp.exists() else None


def take_snapshot(snapshots: Path, texts: dict[str, str]) -> int:
    """Copy `texts` into the next numbered snapshot and return its number."""
    n, _ = latest_snapshot(snapshots)
    target = snapshots / str(n + 1)
    for name, text in texts.items():
        files.write_text(target / name, text)
    files.write_text(target / DIGEST, digest(texts))
    return n + 1


def read_snapshot(snapshots: Path, n: int) -> dict[str, str]:
    return read_agent_files(snapshots / str(n))
