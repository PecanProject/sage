"""The models of paper.json, run.json and reviews.jsonl."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, JsonValue, TypeAdapter, computed_field

from sage.events import Counts, Driver, Limit, ModelId, Usage, now


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


ReviewAdapter: TypeAdapter[Review] = TypeAdapter(Review)


RunStatus = Literal["queued", "running", "finished", "budget_exhausted", "failed", "interrupted"]
"""A run goes from queued to running, then to finished, budget_exhausted or failed. A run SAGE drives whose process
is gone, or that was stopped, reads as interrupted. A run driven from a terminal is running (open) from
`sage extract start` until `done` or `stop`. A run that isn't finished can be taken up again while it has budget
left."""


class RunInfo(BaseModel):
    id: str
    paper_id: str
    status: RunStatus = "queued"
    driver: Driver = "sage"
    agent: str | None = None
    """The ACP agent driving the run, when `driver` is agent."""
    agent_model: str | None = None
    """The agent's own name for the model it was asked to use; None for its default."""
    extraction_model: ModelId | None = None
    """The model SAGE's own agent runs on."""
    effort: str | None = None
    """What the run asked for: an `Effort` for SAGE's own loop, None for a model that doesn't think, and the agent's
    own level for an agent run, None for its default."""
    effort_given: bool = False
    """The effort was asked for this run, not taken from settings or the run it continues. An agent that lacks it
    fails the run rather than running at its default."""
    max_turns: int = 0
    max_cost_usd: float = 0.0
    created_at: datetime = Field(default_factory=now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    pid: int | None = None
    usage: Usage = Field(default_factory=Usage)
    turns: int = 0
    counts: Counts = Field(default_factory=Counts)
    summary: str | None = None
    error: str | None = None
    refused: bool = False
    """It failed because the provider refused the key or the model, or the agent's sign-in failed, so it needs a
    change on the Manage page (a key, a model or a sign-in) before a resume can get further."""
    resumes: int = 0
    parent_id: str | None = None
    guidance: str | None = None
    waiting: str | None = None
    """What an agent SAGE started is asking you to allow, while it waits for the answer."""

    @computed_field
    @property
    def limit(self) -> Limit | None:
        """Which budget a budget_exhausted run ran out of: time for an agent SAGE started, and for SAGE's own loop
        turns once it used them all, else cost."""
        if self.status != "budget_exhausted":
            return None
        if self.driver == "agent":
            return "time"
        return "turns" if self.turns >= self.max_turns else "cost"


class UiInfo(BaseModel):
    """ui.json: the `sage ui` serving this data folder."""

    url: str
    pid: int
