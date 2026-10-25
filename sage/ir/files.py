"""A problem in one of the files an agent writes into a paper's folder, at the line to fix."""

from typing import override

from pydantic import BaseModel, ConfigDict


class FileIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    """Relative to the paper's folder, with forward slashes: `tables/p7.t1.csv`, `records/sites.yaml`."""
    line: int
    """Counts from 1. A problem with the whole file is on line 1."""
    message: str

    @override
    def __str__(self) -> str:
        return f"{self.path}:{self.line}  {self.message}"
