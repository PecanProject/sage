"""All filesystem access. JSON files are rewritten atomically; JSONL files are append-only."""

import fcntl
import os
import tempfile
import tomllib
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

import tomli_w
from pydantic import BaseModel, TypeAdapter, ValidationError

_toml_table = TypeAdapter(dict[str, object])


class UnreadableError(Exception):
    """A file that exists but can't be read or parsed. Never read as absent, so nothing silently vanishes."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path} can't be read: {reason}")
        self.path = path


def write_model(path: Path, model: BaseModel) -> None:
    write_text(path, model.model_dump_json(indent=1))


def write_text(path: Path, text: str, *, mode: int = 0o644) -> None:
    write_bytes(path, text.encode(), mode=mode)


def write_bytes(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        Path(tmp).chmod(mode)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_model[M: BaseModel](path: Path, cls: type[M]) -> M:
    data = path.read_bytes()
    try:
        return cls.model_validate_json(data)
    except ValidationError as e:
        raise UnreadableError(path, _reason(e)) from e


def read_model_or_none[M: BaseModel](path: Path, cls: type[M]) -> M | None:
    """None only when the file is missing. A file that fails to parse raises UnreadableError."""
    try:
        return read_model(path, cls)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise UnreadableError(path, e.strerror or type(e).__name__) from e


def read_disposable[M: BaseModel](path: Path, cls: type[M]) -> M | None:
    """A file SAGE can always write again: one that can't be read counts as missing, and nothing is lost."""
    try:
        return read_model_or_none(path, cls)
    except UnreadableError:
        return None


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def append_jsonl(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(model.model_dump_json(exclude_none=True) + "\n")


def read_jsonl[T](path: Path, adapter: TypeAdapter[T], *, offset: int = 0) -> tuple[list[T], int]:
    """Complete lines from `offset` on, and the offset after the last complete line."""
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except FileNotFoundError:
        return [], offset
    except OSError as e:
        raise UnreadableError(path, e.strerror or type(e).__name__) from e
    end = data.rfind(b"\n") + 1
    items: list[T] = []
    for n, line in enumerate(data[:end].splitlines(), start=1):
        if not line.strip():
            continue
        try:
            items.append(adapter.validate_json(line))
        except ValidationError as e:
            where = f"line {n}" if offset == 0 else f"line {n} after byte {offset}"
            raise UnreadableError(path, f"{where}: {_reason(e)}") from e
    return items, offset + end


def read_text(path: Path) -> str:
    """A UTF-8 text file. One that isn't UTF-8 raises UnreadableError."""
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise UnreadableError(path, f"not UTF-8 text at byte {e.start}") from e


def write_text_if_changed(path: Path, text: str) -> bool:
    """Rewrite the file only when its text would change, so a reader watching it sees no write for nothing. True when
    it wrote."""
    try:
        if path.read_bytes() == text.encode():
            return False
    except FileNotFoundError:
        pass
    write_text(path, text)
    return True


def child_dirs(path: Path) -> Iterator[Path]:
    if path.is_dir():
        yield from sorted(p for p in path.iterdir() if p.is_dir() and not p.name.startswith("."))


def child_files(path: Path, suffix: str | None = None) -> list[Path]:
    """The folder's files by name, without hidden ones (an atomic write's temporary file is hidden), and only those
    ending in `suffix` when given. A missing folder has none."""
    if not path.is_dir():
        return []
    return sorted(
        p
        for p in path.iterdir()
        if p.is_file() and not p.name.startswith(".") and (suffix is None or p.name.endswith(suffix))
    )


def exists(path: Path) -> bool:
    return path.exists()


def make_dirs(*paths: Path) -> None:
    """Create each folder that isn't there yet."""
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def make_new_dir(path: Path) -> bool:
    """Create the folder, or return False when it already exists. Two processes never both get True for one path."""
    try:
        path.mkdir(parents=True)
    except FileExistsError:
        return False
    return True


@contextmanager
def scratch_dir() -> Generator[Path]:
    """An empty folder that is removed afterwards, with all it holds."""
    with tempfile.TemporaryDirectory(prefix="sage-") as d:
        yield Path(d)


@contextmanager
def exclusive(path: Path) -> Generator[None]:
    """Hold an exclusive lock on `path` across processes. The OS drops it when the holder exits, so it never goes
    stale."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def lock(path: Path) -> int | None:
    """Take an exclusive lock on `path` without waiting, and return the open descriptor that holds it, or None when
    someone else holds it. The lock lasts until the descriptor closes, in this process or in a child it passed to."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def unlock(fd: int) -> None:
    os.close(fd)


def touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def remove(path: Path) -> None:
    path.unlink(missing_ok=True)


def read_toml(path: Path) -> dict[str, object]:
    try:
        return _toml_table.validate_python(tomllib.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise UnreadableError(path, getattr(e, "strerror", None) or str(e)) from e


def _reason(e: ValidationError) -> str:
    """The first problems, by location. Input values are left out: a file may hold a secret."""
    errors = e.errors(include_input=False)
    shown = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'file'}: {err['msg']}" for err in errors[:3])
    return shown + (f" (and {len(errors) - 3} more)" if len(errors) > 3 else "")


def write_toml(path: Path, data: dict[str, object]) -> None:
    write_text(path, tomli_w.dumps(data), mode=0o600)
