"""What SAGE's own agent works on: the paper's folder, through the Workspace the pipeline implements. Check and done
are the same `sage extract check` and `sage extract done` every other driver calls."""

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


class Finished(BaseModel):
    accepted: bool
    text: str
    """What `sage extract done` prints: the counts when accepted, the check when refused."""


class Workspace(Protocol):
    @property
    def folder(self) -> Path:
        """The paper's folder, the agent's working directory."""
        ...

    def image(self, path: str) -> bytes | str:
        """A PNG in the folder, or why it can't be read."""
        ...

    def check(self) -> str:
        """The check as `sage extract check` prints it."""
        ...

    def done(self, summary: str) -> Finished: ...
