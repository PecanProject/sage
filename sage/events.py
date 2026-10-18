"""The run event stream. One line per event in runs/<id>/events.jsonl; the UI, `sage logs` and agents read it."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, JsonValue, TypeAdapter
from pydantic_core import PydanticCustomError

Effort = Literal["low", "medium", "high", "xhigh", "max"]
"""Defined here because settings, store and agents may all import this module."""


def _anthropic_model(value: str) -> str:
    provider, _, name = value.partition(":")
    if provider != "anthropic" or not name:
        raise PydanticCustomError("model_id", "must be an Anthropic model id like 'anthropic:claude-haiku-5-5'")
    return value


ModelId = Annotated[str, AfterValidator(_anthropic_model)]
"""The only check that a model id names an Anthropic model: settings, a run's `--model` and run.json all parse it,
so the agent layer takes it as given. Here for the same reason as `Effort`."""


def now() -> datetime:
    return datetime.now(UTC)


class Usage(BaseModel):
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            requests=self.requests + other.requests,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


Stage = Literal["parse", "tables", "agent", "settle"]

STAGE_STEP: dict[Stage, str] = {
    "parse": "Reading the paper",
    "tables": "Reading tables",
    "agent": "Extracting",
    "settle": "Checking",
}
"""The plain word for each stage that the Papers list and the Paper status line show while a run is live."""


class _Event(BaseModel):
    ts: datetime = Field(default_factory=now)


class RunStarted(_Event):
    type: Literal["run.started"] = "run.started"
    run_id: str
    paper_id: str
    extraction_model: str
    table_model: str
    effort: Effort
    resumed: bool = False
    continued_from: str | None = None
    guidance: str | None = None


class StageStarted(_Event):
    type: Literal["stage.started"] = "stage.started"
    stage: Stage
    detail: str | None = None


class Counts(BaseModel):
    records: int = 0
    verified: int = 0
    flagged: int = 0
    unresolved: int = 0


class StageFinished(_Event):
    type: Literal["stage.finished"] = "stage.finished"
    stage: Stage
    detail: str | None = None
    counts: Counts | None = None
    """The settle stage's records by status, the figures its detail spells out."""


class LlmRequest(_Event):
    type: Literal["llm.request"] = "llm.request"
    role: str
    turn: int


class LlmThinking(_Event):
    type: Literal["llm.thinking"] = "llm.thinking"
    text: str


class LlmText(_Event):
    type: Literal["llm.text"] = "llm.text"
    text: str


class LlmToolCall(_Event):
    type: Literal["llm.tool_call"] = "llm.tool_call"
    call_id: str
    tool: str
    args: JsonValue = None


class WriteTally(BaseModel):
    tool: Literal["write"] = "write"
    rejected: int


class ExpandTally(BaseModel):
    tool: Literal["expand_table"] = "expand_table"
    observations: int
    failed: int


class CheckTally(BaseModel):
    tool: Literal["check"] = "check"
    blocking: int
    warnings: int


Tally = Annotated[WriteTally | ExpandTally | CheckTally, Field(discriminator="tool")]
"""The figures the run log shows for a tool's result, read from all of it, so no reader parses the clipped text."""


class LlmToolResult(_Event):
    type: Literal["llm.tool_result"] = "llm.tool_result"
    call_id: str
    tool: str
    ok: bool
    text: str
    """What the model was sent, clipped to 2,000 characters."""
    tally: Tally | None = None
    """For write, expand_table and check, when they returned a result."""


class CheckFailed(_Event):
    type: Literal["check.failed"] = "check.failed"
    record_id: str | None = None
    errors: list[str]


class RecordSettled(_Event):
    type: Literal["record.settled"] = "record.settled"
    record_id: str
    record_type: str
    status: str


CacheMissReason = Literal[
    "model_changed",
    "system_changed",
    "tools_changed",
    "messages_changed",
    "previous_message_not_found",
    "unavailable",
    "pending",
]


class CacheMiss(BaseModel):
    reason: CacheMissReason
    missed_tokens: int | None = None


class UsageEvent(_Event):
    type: Literal["usage"] = "usage"
    role: str
    turn: int
    request: Usage
    total: Usage
    cache_miss: CacheMiss | None = None


class Note(_Event):
    type: Literal["note"] = "note"
    level: Literal["info", "warning"] = "info"
    text: str


Limit = Literal["turns", "cost"]
"""Which budget stopped a run that ran out of it."""


class RunFinished(_Event):
    type: Literal["run.finished"] = "run.finished"
    status: str
    summary: str | None = None
    limit: Limit | None = None


class RunError(_Event):
    type: Literal["run.error"] = "run.error"
    message: str


Event = Annotated[
    RunStarted
    | StageStarted
    | StageFinished
    | LlmRequest
    | LlmThinking
    | LlmText
    | LlmToolCall
    | LlmToolResult
    | CheckFailed
    | RecordSettled
    | UsageEvent
    | Note
    | RunFinished
    | RunError,
    Field(discriminator="type"),
]
EventAdapter: TypeAdapter[Event] = TypeAdapter(Event)


def describe(e: Event) -> str | None:
    """One plain line for `sage logs`, or None for an event only --json shows."""
    match e:
        case RunStarted():
            again = " (resumed)" if e.resumed else ""
            if e.continued_from is not None:
                again = f" (continues {e.continued_from})"
            line = f"run started{again}: {e.extraction_model}, effort {e.effort}; tables by {e.table_model}"
            return line + (f"\nguidance: {e.guidance}" if e.guidance is not None else "")
        case StageStarted():
            return f"{e.stage} …"
        case StageFinished():
            return f"{e.stage} done" + (f": {e.detail}" if e.detail is not None else "")
        case LlmRequest():
            return f"turn {e.turn}"
        case LlmText():
            return f"model: {_clip(e.text)}"
        case LlmToolCall():
            return f"→ {e.tool}({_clip(_args(e.args), 140)})"
        case LlmToolResult():
            return f"{'←' if e.ok else '✗'} {e.tool}: {_clip(e.text)}"
        case CheckFailed():
            return f"checker on {e.record_id}: {_clip('; '.join(e.errors))}"
        case UsageEvent():
            if e.role != "extractor":
                return None
            line = f"  ${e.total.cost_usd:.4f} so far"
            if (miss := e.cache_miss) is not None and miss.reason == "pending":
                line += "; cache diagnosis still pending"
            elif miss is not None:
                line += f"; cache missed: {miss.reason}"
                if miss.missed_tokens is not None:
                    line += f", {miss.missed_tokens:,} tokens"
            return line
        case Note():
            return ("warning: " if e.level == "warning" else "") + _clip(e.text, 300)
        case RunFinished():
            ended = f"stopped at the {'turn' if e.limit == 'turns' else 'cost'} limit" if e.limit else e.status
            return f"run {ended}" + (f": {e.summary}" if e.summary is not None else "")
        case RunError():
            return f"run failed: {e.message}"
        case LlmThinking() | RecordSettled():
            return None


def _args(args: JsonValue) -> str:
    if isinstance(args, dict):
        return ", ".join(f"{k}={_short(v)}" for k, v in args.items())
    return _short(args)


def _short(v: JsonValue) -> str:
    if isinstance(v, list):
        return f"[{len(v)} items]"
    if isinstance(v, dict):
        return "{…}"
    return repr(v)


def _clip(text: str, n: int = 200) -> str:
    one = " ".join(text.split())
    return one if len(one) <= n else one[: n - 1] + "…"
