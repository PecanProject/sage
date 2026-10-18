from typing import Literal

from pydantic import BaseModel

Severity = Literal["blocking", "warning"]
Level = Literal["evidence", "record", "dataset", "coverage"]


class Issue(BaseModel):
    level: Level
    severity: Severity
    target: str
    """A record id, "record_id.field", a table anchor, or "results" for the Results-coverage warning."""
    message: str
    """What is wrong and what to do next."""

    @property
    def record_id(self) -> str:
        return self.target.split(".", 1)[0]

    @property
    def line(self) -> str:
        return f"[{self.severity}] {self.target}: {self.message}"


def blocking(level: Level, target: str, message: str) -> Issue:
    return Issue(level=level, severity="blocking", target=target, message=message)


def warning(level: Level, target: str, message: str) -> Issue:
    return Issue(level=level, severity="warning", target=target, message=message)
