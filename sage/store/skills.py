"""The SAGE skill: shipped in the package, installed where coding agents look for skills. Codex and Gemini read
`~/.agents/skills`, Claude Code reads `~/.claude/skills`, so the skill is copied to the first and linked from the
second."""

from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from sage.store import files

PACKAGE = "sage.agents"
FOLDER = "skill"
NAME = "sage"


def places() -> tuple[Path, Path]:
    """Where agents look: the shared folder (Codex, Gemini and others) and Claude Code's."""
    home = Path.home()
    return home / ".agents" / "skills" / NAME, home / ".claude" / "skills" / NAME


def shipped() -> Path:
    """The skill's folder inside the installed package, which an agent can read in place."""
    return Path(str(resources.files(PACKAGE).joinpath(FOLDER)))


def bundled() -> dict[str, str]:
    """The skill's files as the package ships them, by path relative to the skill's folder."""
    out: dict[str, str] = {}

    def walk(node: Traversable, prefix: str) -> None:
        for child in sorted(node.iterdir(), key=lambda c: c.name):
            rel = f"{prefix}{child.name}"
            if child.is_dir():
                walk(child, rel + "/")
            elif child.name.endswith(".md"):
                out[rel] = child.read_text(encoding="utf-8")

    walk(resources.files(PACKAGE).joinpath(FOLDER), "")
    return out


def installed(folder: Path) -> dict[str, str] | None:
    """The skill as installed in `folder`, or None when it isn't there."""
    if not (folder / "SKILL.md").exists():
        return None
    return {p.relative_to(folder).as_posix(): files.read_text(p) for p in sorted(folder.rglob("*.md")) if p.is_file()}


def install(folder: Path, texts: dict[str, str]) -> None:
    """Write the skill into `folder`, removing files an older version had and this one doesn't."""
    for path in sorted(folder.rglob("*.md")) if folder.is_dir() else []:
        if path.relative_to(folder).as_posix() not in texts:
            files.remove(path)
    for name, text in texts.items():
        files.write_text(folder / name, text)


def link(target: Path, link_path: Path) -> bool:
    """Point `link_path` at `target`. A real folder already there is left alone (someone installed it by hand); False
    then."""
    if link_path.is_symlink():
        if link_path.resolve() == target.resolve():
            return True
        link_path.unlink()
    elif link_path.exists():
        return False
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(target, target_is_directory=True)
    return True
