"""The models of paper.json, run.json and reviews.jsonl."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, JsonValue, computed_field

from sage.events import Counts, Effort, Limit, ModelId, Usage, now


class PaperInfo(BaseModel):
    id: str
    filename: str
    sha256: str
    pages: int
    title: str | None = None
    added_at: datetime = Field(default_factory=now)


ReviewAction = Literal["accept", "edit", "reject"]


class Review(BaseModel):
    ts: datetime = Field(default_factory=now)
    run_id: str
    record_id: str
    field: str | None = None
    action: ReviewAction
    value: JsonValue = None
    note: str | None = None


RunStatus = Literal["queued", "running", "finished", "budget_exhausted", "failed", "interrupted"]
"""queued -> running -> finished | budget_exhausted | failed. A running run whose process is gone, or that `sage stop`
ended, reads as interrupted. `sage run --resume` takes any run that isn't finished back to running, while it has
budget left."""


class RunInfo(BaseModel):
    id: str
    paper_id: str
    status: RunStatus = "queued"
    extraction_model: ModelId
    table_model: ModelId
    effort: Effort
    max_turns: int
    max_cost_usd: float
    created_at: datetime = Field(default_factory=now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    pid: int | None = None
    usage: Usage = Field(default_factory=Usage)
    table_usage: Usage = Field(default_factory=Usage)
    turns: int = 0
    counts: Counts = Field(default_factory=Counts)
    summary: str | None = None
    error: str | None = None
    resumes: int = 0
    parent_id: str | None = None
    guidance: str | None = None
    review_notes: list[Review] = Field(default_factory=list)

    @property
    def total_usage(self) -> Usage:
        return self.usage + self.table_usage

    @computed_field
    @property
    def limit(self) -> Limit | None:
        """Which budget a budget_exhausted run ran out of: turns once it used them all, else cost."""
        if self.status != "budget_exhausted":
            return None
        return "turns" if self.turns >= self.max_turns else "cost"
