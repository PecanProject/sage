"""Coding agents SAGE starts in a paper's folder over the Agent Client Protocol: Claude Code, Codex, Gemini CLI,
OpenCode and any other ACP agent. The agent works on the folder with its own tools, the way it would in a terminal,
and calls `sage extract check` and `sage extract done` itself. This module speaks ACP and turns what the agent says
into run events; the pipeline decides what the agent may do (`Decide`) and when the run is over."""

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import acp
from acp import RequestError
from acp.interfaces import Client
from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AllowedOutcome,
    ClientCapabilities,
    DeniedOutcome,
    Implementation,
    NewSessionResponse,
    PermissionOption,
    RequestPermissionResponse,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    SessionConfigSelectGroup,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UsageUpdate,
)
from pydantic import BaseModel, JsonValue

from sage.events import JSON, Event, LlmRequest, LlmText, LlmThinking, LlmToolCall, LlmToolResult, Note, Usage

START_SECONDS = 180.0
"""How long an agent may take to start and open a session: npx may download its adapter first."""
ConfigOption = SessionConfigOptionSelect | SessionConfigOptionBoolean


@dataclass(frozen=True)
class Launch:
    """How to start one agent in one folder."""

    command: str
    args: tuple[str, ...]
    env: dict[str, str]
    """The whole environment the agent gets: ACP's trimmed one is extended with only these."""
    cwd: Path
    mode: str | None = None
    """The session mode to set, which decides what the agent asks permission for."""
    model: str | None = None
    """The agent's own name for the model to use; None keeps the agent's default."""
    effort: str | None = None
    """The agent's own thought level for that model; None keeps the agent's default."""
    effort_given: bool = False
    """The effort was asked for this run. A model that lacks it is then an error; otherwise the agent runs at its
    default and the log says why."""


class Permission(BaseModel):
    """What the agent asks to do."""

    title: str
    kind: str | None
    paths: list[str]
    """Files it names, absolute."""
    command: str | None
    """The shell command, for an `execute` request."""


Decide = Callable[[Permission], Awaitable[bool]]
"""Allow (True) or refuse what the agent asks."""


class Offer(BaseModel):
    """What an agent offers for a session: its models and modes, and the thought levels of the model it is set to, as
    values `Launch` takes."""

    models: list[str]
    model: str | None
    modes: list[str]
    mode: str | None
    efforts: list[str]
    """Empty when the agent offers no thought level for the model."""
    effort: str | None


class AgentError(Exception):
    def __init__(self, message: str, *, login: bool = False) -> None:
        super().__init__(message)
        self.login = login
        """The agent wants a sign-in first."""


@dataclass
class Turn:
    stop_reason: str
    usage: Usage


@dataclass
class _Client:
    """The two calls SAGE answers. The SDK dispatches by name and answers the ones a client lacks (files, terminals,
    MCP) as not supported, so the agent uses its own tools."""

    emit: Callable[[Event], None]
    decide: Decide
    text: list[str] = field(default_factory=list)
    thought: list[str] = field(default_factory=list)
    tools: dict[str, str] = field(default_factory=dict)
    """Each tool call's title, by id, for its result."""
    cost: float = 0.0
    said: str = ""
    """The agent's last message this turn. A turn that ends in an error often says why there (a usage limit), while
    the error says only "Internal error"."""

    async def session_update(self, session_id: str, update: object, **_: object) -> None:  # noqa: ARG002 - called by name
        if isinstance(update, AgentMessageChunk):
            self._chunk(self.text, update.content)
            return
        if isinstance(update, AgentThoughtChunk):
            self._chunk(self.thought, update.content)
            return
        self.flush()
        if isinstance(update, ToolCallStart):
            self.tools[update.tool_call_id] = update.title
            self.emit(LlmToolCall(call_id=update.tool_call_id, tool=update.title, args=_json(update.raw_input)))
        elif isinstance(update, ToolCallProgress) and update.status in ("completed", "failed"):
            tool = update.title or self.tools.get(update.tool_call_id, update.kind or "tool")
            text = _content_text(update.content) or _text(update.raw_output)
            self.emit(
                LlmToolResult(call_id=update.tool_call_id, tool=tool, ok=update.status == "completed", text=text[:2000])
            )
        elif isinstance(update, AgentPlanUpdate) and update.entries:
            self.emit(Note(text="plan: " + " · ".join(e.content for e in update.entries)))
        elif isinstance(update, UsageUpdate) and update.cost is not None and update.cost.currency == "USD":
            self.cost = update.cost.amount

    async def request_permission(  # called by name
        self,
        session_id: str,  # noqa: ARG002
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **_: object,
    ) -> RequestPermissionResponse:
        self.flush()
        ask = Permission(
            title=tool_call.title or tool_call.kind or "a tool",
            kind=tool_call.kind,
            paths=[loc.path for loc in tool_call.locations or []],
            command=_command(tool_call.raw_input),
        )
        allowed = await self.decide(ask)
        kinds = ("allow_once", "allow_always") if allowed else ("reject_once", "reject_always")
        option = next((o for k in kinds for o in options if o.kind == k), None)
        if option is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id=option.option_id))

    def flush(self) -> None:
        if self.thought:
            self.emit(LlmThinking(text="".join(self.thought)))
            self.thought.clear()
        if self.text:
            text = "".join(self.text)
            if text.strip():
                self.emit(LlmText(text=text))
                self.said = text.strip()
            self.text.clear()

    def last_said(self) -> str:
        self.flush()
        return self.said

    @staticmethod
    def _chunk(into: list[str], content: object) -> None:
        if isinstance(content, TextContentBlock):
            into.append(content.text)


class Session:
    """One open ACP session in the paper's folder."""

    def __init__(self, conn: acp.core.ClientSideConnection, session_id: str, client: _Client) -> None:
        self._conn = conn
        self._id = session_id
        self._client = client
        self._turns = 0

    async def prompt(self, text: str) -> Turn:
        """Send one prompt and wait for the agent to end its turn."""
        self._turns += 1
        self._client.emit(LlmRequest(turn=self._turns))
        self._client.said = ""
        try:
            result = await self._conn.prompt(session_id=self._id, prompt=[TextContentBlock(type="text", text=text)])
        except RequestError as e:
            # The agent's own error, such as a usage limit or a refused model. Its message says what to do, or when
            # it is bare, the agent's last message does.
            said = self._client.last_said()
            extra = f"; it said: {said}" if said and said not in str(e) else ""
            raise AgentError(f"the agent stopped with an error: {e}{extra}") from e
        except (ConnectionError, EOFError) as e:
            raise AgentError(f"the agent exited mid-turn: {e}") from e
        finally:
            self._client.flush()
        u = result.usage
        if u is None:
            return Turn(stop_reason=result.stop_reason, usage=Usage(requests=1))
        usage = Usage(
            requests=1,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=u.cached_read_tokens or 0,
            cache_write_tokens=u.cached_write_tokens or 0,
        )
        return Turn(stop_reason=result.stop_reason, usage=usage)

    @property
    def cost(self) -> float:
        """What the agent says the session has cost so far, in USD, when it says."""
        return self._client.cost


@asynccontextmanager
async def session(launch: Launch, *, emit: Callable[[Event], None], decide: Decide) -> AsyncGenerator[Session]:
    """Start the agent in `launch.cwd`, open a session there with its mode and model set, and close it all on exit."""
    client = _Client(emit=emit, decide=decide)
    async with _spawn(launch, client) as (conn, new):
        await _configure(conn, new, launch, emit)
        yield Session(conn, new.session_id, client)


async def offer(launch: Launch) -> Offer:
    """Start the agent, open a session in `launch.cwd`, set `launch.model` when given, and read what it offers,
    without prompting it. Thought levels depend on the model, so they are the ones of the model it ends up on."""

    async def refuse(_: Permission) -> bool:
        return False

    async with _spawn(launch, _Client(emit=lambda _: None, decide=refuse)) as (conn, new):
        options = new.config_options or []
        if launch.model is not None:
            options = await _set(conn, new.session_id, options, "model", launch.model)
    models = _select(options, "model")
    modes = _select(options, "mode")
    efforts = _select(options, "thought_level")
    mode_values = [m.id for m in new.modes.available_modes] if new.modes is not None else []
    return Offer(
        models=_values(models),
        model=models.current_value if models is not None else None,
        modes=_values(modes) or mode_values,
        mode=modes.current_value if modes is not None else new.modes.current_mode_id if new.modes else None,
        efforts=_values(efforts),
        effort=efforts.current_value if efforts is not None else None,
    )


@asynccontextmanager
async def _spawn(
    launch: Launch, client: _Client
) -> AsyncGenerator[tuple[acp.core.ClientSideConnection, NewSessionResponse]]:
    """Start the agent in `launch.cwd` and open a session there."""
    try:
        async with acp.spawn_agent_process(
            cast("Client", client), launch.command, *launch.args, env=launch.env, cwd=launch.cwd
        ) as (conn, _):
            yield conn, await _open(conn, launch.cwd)
    except FileNotFoundError as e:
        raise AgentError(f"{launch.command} isn't installed or isn't on PATH") from e


async def _open(conn: acp.core.ClientSideConnection, cwd: Path) -> NewSessionResponse:
    try:
        init = await asyncio.wait_for(
            conn.initialize(
                protocol_version=acp.PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(),
                client_info=Implementation(name="sage", title="SAGE", version="2"),
            ),
            START_SECONDS,
        )
    except TimeoutError as e:
        raise AgentError(f"the agent didn't start within {START_SECONDS:.0f} seconds") from e
    except (ConnectionError, EOFError, RuntimeError, RequestError) as e:
        raise AgentError(f"the agent stopped while starting: {e}") from e
    try:
        return await asyncio.wait_for(conn.new_session(cwd=str(cwd), mcp_servers=[]), START_SECONDS)
    except TimeoutError as e:
        raise AgentError(f"the agent didn't open a session within {START_SECONDS:.0f} seconds") from e
    except RequestError as e:
        methods = ", ".join(m.name for m in init.auth_methods or [])
        login = bool(methods) or "auth" in str(e).lower()
        hint = f"; it signs in with {methods}" if methods else ""
        raise AgentError(f"the agent refused to open a session: {e}{hint}", login=login) from e
    except (ConnectionError, EOFError, RuntimeError) as e:
        raise AgentError(f"the agent stopped while opening a session: {e}") from e


async def _configure(
    conn: acp.core.ClientSideConnection, new: NewSessionResponse, launch: Launch, emit: Callable[[Event], None]
) -> None:
    """Set the mode, the model and then the effort, by the options the agent offers (matched on category, not id),
    else the mode through the older session-mode call. A value the agent doesn't offer is an error, so a typo doesn't
    run on the default. The effort's levels depend on the model, so they are read from the options as setting the
    model left them. An effort that came from settings and that the model lacks is only a note, so changing an
    agent's model never stops its runs."""
    options = new.config_options or []
    if launch.mode is not None:
        if _select(options, "mode") is not None:
            options = await _set(conn, new.session_id, options, "mode", launch.mode)
        elif new.modes is not None:
            if launch.mode not in [m.id for m in new.modes.available_modes]:
                raise AgentError(f"the agent offers no mode '{launch.mode}'")
            await conn.set_session_mode(session_id=new.session_id, mode_id=launch.mode)
    if launch.model is not None and _select(options, "model") is not None:
        options = await _set(conn, new.session_id, options, "model", launch.model)
    if launch.effort is None:
        return
    levels = _values(_select(options, "thought_level"))
    if launch.effort in levels:
        await _set(conn, new.session_id, options, "thought_level", launch.effort)
        return
    offered = f"it offers {', '.join(levels)}" if levels else "it has no effort choice for it"
    lacks = f"the agent offers no effort '{launch.effort}' for this model; {offered}"
    if launch.effort_given:
        raise AgentError(lacks)
    emit(Note(level="warning", text=f"{lacks}. It thinks at its default."))


async def _set(
    conn: acp.core.ClientSideConnection,
    session_id: str,
    options: list[ConfigOption],
    category: str,
    value: str,
) -> list[ConfigOption]:
    """Set the option of `category` to `value`, refusing one it doesn't offer, and return the options as the agent
    says they are now: setting the model changes which others it has."""
    option = _select(options, category)
    if option is None:
        raise AgentError(f"the agent offers no {category} setting")
    if value not in _values(option):
        raise AgentError(f"the agent offers no {category} '{value}'; it offers {', '.join(_values(option))}")
    answer = await conn.set_config_option(config_id=option.id, session_id=session_id, value=value)
    return answer.config_options


def _select(options: list[ConfigOption], category: str) -> SessionConfigOptionSelect | None:
    for option in options:
        if isinstance(option, SessionConfigOptionSelect) and option.category == category:
            return option
    return None


def _values(option: SessionConfigOptionSelect | None) -> list[str]:
    if option is None:
        return []
    out: list[str] = []
    for o in option.options:
        if isinstance(o, SessionConfigSelectGroup):
            out += [x.value for x in o.options]
        else:
            out.append(o.value)
    return out


def _command(raw: object) -> str | None:
    if isinstance(raw, dict):
        for key in ("command", "cmd"):
            value = raw.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                return " ".join(str(v) for v in value)
    return None


def _json(raw: object) -> JsonValue:
    try:
        return JSON.validate_python(raw if raw is not None else {})
    except ValueError:
        return str(raw)


def _text(raw: object) -> str:
    if raw is None:
        return ""
    text = raw if isinstance(raw, str) else json.dumps(raw, default=str)
    return "[image]" if '"base64"' in text or '"type": "image"' in text else text


def _content_text(content: list[Any] | None) -> str:
    parts: list[str] = []
    for c in content or []:
        inner = getattr(c, "content", None)
        if isinstance(inner, TextContentBlock):
            parts.append(inner.text)
    return "\n".join(parts)
