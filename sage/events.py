"""The run event stream. One line per event in runs/<id>/events.jsonl; the UI, `sage logs` and agents read it."""

import re
from datetime import UTC, datetime
from typing import Annotated, Literal, get_args

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, SecretStr, TypeAdapter
from pydantic_core import PydanticCustomError

Effort = Literal["low", "medium", "high", "xhigh", "max"]
"""Defined here because settings, store and agents may all import this module."""

EFFORTS: tuple[Effort, ...] = ("low", "medium", "high", "xhigh", "max")
if get_args(Effort) != EFFORTS:
    raise TypeError("EFFORTS must list every Effort, in order")
EffortAdapter: TypeAdapter[Effort] = TypeAdapter(Effort)


ProviderKind = Literal["anthropic", "openai", "azure", "google", "vertex", "bedrock"]
"""The APIs SAGE can call a model through. Here for the same reason as `Effort`."""

BUILTIN: dict[str, ProviderKind] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "google",
    "bedrock": "bedrock",
}
"""The providers every install has, each named after its kind. Someone adds more of any kind under ids of their own."""

PROVIDER_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


class Connection(BaseModel):
    """How to reach one provider: its kind, and the fields that kind uses. `settings.KINDS` says which fields each
    kind reads and which it needs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ProviderKind
    name: str | None = None
    """What the Manage page calls it. A built-in provider goes by its kind's title."""
    api_key: SecretStr | None = None
    base_url: str | None = None
    """An endpoint other than the provider's own: a gateway, a proxy, a local server, an Azure resource."""
    region: str | None = None
    """Bedrock's AWS region, or Vertex AI's location."""
    project: str | None = None
    """The Google Cloud project Vertex AI bills."""
    api_version: str | None = None
    """Azure OpenAI's API version."""
    aws_profile: str | None = None
    """A profile in ~/.aws that signs Bedrock requests, instead of a Bedrock API key."""
    credentials_file: str | None = None
    """A Google Cloud service account key file, instead of Application Default Credentials."""


def _model_id(value: str) -> str:
    if not value.strip():
        raise PydanticCustomError("model_id", "must name a model")
    provider, sep, name = value.partition(":")
    if sep and (PROVIDER_ID.fullmatch(provider) is None or not name):
        raise PydanticCustomError("model_id", "must be '<provider>:<name>', with the id of one of your providers")
    return value


ModelId = Annotated[str, AfterValidator(_model_id)]
"""A model as a person writes it: a bare name like 'claude-haiku-5-5', or 'provider:name', where provider is the id
of a configured provider. This checks the shape alone. `core` resolves it to the canonical 'provider:name'
(`sage.agents.models.resolve`) before it is saved or run, so config.toml, run.json and the UI hold only canonical
ids."""


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


Stage = Literal["parse", "agent", "settle"]

STAGE_STEP: dict[Stage, str] = {
    "parse": "Reading the paper",
    "agent": "Extracting",
    "settle": "Checking",
}
"""The plain word for each stage that the Papers list and the Paper status line show while a run is live."""


class _Event(BaseModel):
    ts: datetime = Field(default_factory=now)


Driver = Literal["sage", "agent", "here"]
"""Who drives a run: SAGE's own loop on an API model, an agent SAGE started over ACP, or whoever ran `sage extract
start` in a terminal."""


class RunStarted(_Event):
    type: Literal["run.started"] = "run.started"
    run_id: str
    paper_id: str
    driver: Driver = "sage"
    agent: str | None = None
    agent_model: str | None = None
    extraction_model: str | None = None
    effort: str | None = None
    """What the run asked for: an `Effort` for SAGE's own loop, the agent's own level for an agent run."""
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


class LlmToolResult(_Event):
    type: Literal["llm.tool_result"] = "llm.tool_result"
    call_id: str
    tool: str
    ok: bool
    text: str
    """What the model was sent, clipped to 2,000 characters."""


class CheckRan(_Event):
    """One `sage extract check`, or the check `done` runs first. It is the timeline every driver shares."""

    type: Literal["check.ran"] = "check.ran"
    records: int
    blocking: int
    warnings: int
    changed: bool
    """The files changed since the last check, so a snapshot was taken."""
    snapshot: int | None = None
    marked: list[str] = Field(default_factory=list)
    """Targets the same-error rule marked unresolved on this check."""
    lines: list[str] = Field(default_factory=list)
    """The first findings, as `file:line  target: message`."""


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
    turn: int
    request: Usage
    total: Usage
    cache_miss: CacheMiss | None = None


class Note(_Event):
    type: Literal["note"] = "note"
    level: Literal["info", "warning"] = "info"
    text: str


Limit = Literal["turns", "cost", "time"]
"""Which budget stopped a run that ran out of it: SAGE's agent has turns and cost, an agent SAGE started has time."""


class AgentAsked(_Event):
    """An agent SAGE started asks to do something outside what SAGE allows on its own, and waits for your answer."""

    type: Literal["agent.asked"] = "agent.asked"
    title: str
    kind: str | None = None
    command: str | None = None
    paths: list[str] = Field(default_factory=list)


class AgentAnswered(_Event):
    type: Literal["agent.answered"] = "agent.answered"
    allowed: bool
    by: Literal["you", "rule"]


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
    | CheckRan
    | RecordSettled
    | UsageEvent
    | Note
    | AgentAsked
    | AgentAnswered
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
            if e.driver == "here":
                by = "from a terminal"
            elif e.driver == "agent":
                by = f"by {e.agent}" + (f", {e.agent_model}" if e.agent_model is not None else "")
                by += f", effort {e.effort}" if e.effort is not None else ""
            else:
                by = f"{e.extraction_model}" + (f", effort {e.effort}" if e.effort is not None else "")
            line = f"run started{again}: {by}"
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
        case CheckRan():
            changed = f", snapshot {e.snapshot}" if e.snapshot is not None else ""
            return f"check: {e.records} records, {e.blocking} blocking, {e.warnings} warnings{changed}"
        case UsageEvent():
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
        case AgentAsked():
            return f"waiting for you: the agent asks to {_clip(e.command or e.title, 200)}"
        case AgentAnswered():
            return f"{'allowed' if e.allowed else 'refused'} by {e.by}"
        case RunFinished():
            names = {"turns": "turn", "cost": "cost", "time": "time"}
            ended = f"stopped at the {names[e.limit]} limit" if e.limit else e.status
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
