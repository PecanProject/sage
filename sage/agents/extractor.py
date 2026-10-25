"""SAGE's own extraction agent: one pydantic-ai agent per paper, working on the paper's folder with file tools, as a
coding agent would. Check answers every batch of edits, and the run ends when `done` is accepted or the budget runs
out. Code settles the result."""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from pydantic import JsonValue
from pydantic_ai import Agent, BinaryContent, ModelRetry, RunContext, Tool, ToolOutput
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.exceptions import AgentRunError, UsageLimitExceeded
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai_harness.filesystem import DEFAULT_TOOL_NAMES, FileSystem

from sage.agents import models
from sage.agents.contract import Finished, Workspace
from sage.events import (
    JSON,
    Event,
    LlmRequest,
    LlmText,
    LlmThinking,
    LlmToolCall,
    LlmToolResult,
    Usage,
    UsageEvent,
)
from sage.ir.settled import Outcome

ROLE = "extractor"
START = "Extract this paper, following the skill's timeline. The run is open; finish with done."
RESUME = (
    "This run is resuming: the folder holds what you wrote before. Call check to see what is open, carry on from "
    "there, then call done."
)
TOOLS_NOTE = """# In this run

You work through tools, not a terminal. The paper's folder is your working directory, and the run is already open.
- read_file, write_file, edit_file, list_directory, find_files and search_files act on the folder. Paths are relative
  to it: `paper.md`, `records/sites.yaml`, `tables/p7.t1.csv`.
- view_image shows a PNG: `pages/p4.png`, `tables/p7.t1.png`, `figures/p5.f1.png`.
- check is `sage extract check`, and done is `sage extract done`, with the summary.
- `paper.md` is below as it was when the run started. Read it again after check grounds a table you transcribed.
- Write a file whole with write_file the first time, and change it with edit_file after that."""
READ_ONLY = ("paper.md", "*.png", "figures")
"""SAGE writes these for the agent to read."""
DENIED = (".sage", "paper.pdf")
"""SAGE's own state, and the PDF the agent reads as paper.md and images instead."""


@dataclass
class Deps:
    workspace: Workspace
    emit: Callable[[Event], None]


@dataclass(frozen=True)
class Budget:
    max_turns: int
    max_cost_usd: float


@dataclass(frozen=True)
class ExtractResult:
    outcome: Outcome
    usage: Usage
    turns: int
    message: str | None = None
    refused: bool = False
    """The provider refused the key or the model, so only a change on the Manage page lets the run carry on."""


def instructions(skill: str, references: list[str], paper_md: str) -> str:
    """The skill, its references and the paper, in that order: the same text for every run of a paper, so the
    prompt cache holds it (`docs/design/05-prompt-caching.md`)."""
    return "\n\n".join([_body(skill), TOOLS_NOTE, *references, f"# The paper (paper.md)\n\n{paper_md}"])


def build(*, prompt: str, folder: str, max_turns: int) -> Agent[Deps, Finished]:
    files = FileSystem(
        root_dir=folder,
        denied_patterns=list(DENIED),
        read_only_patterns=list(READ_ONLY),
        content_hashes=False,
        tools=list(DEFAULT_TOOL_NAMES),
    )
    # Check and done read the folder the file tools write, so they run alone, never beside an edit.
    tools: list[Tool[Deps]] = [Tool(view_image), Tool(check, sequential=True)]
    return Agent(
        output_type=ToolOutput(
            done,
            name="done",
            description=(
                "End the run with a one-paragraph summary: what you extracted, and what you didn't and why. Refused "
                "while check reports anything blocking: fix it, or mark it in records/unresolved.yaml."
            ),
            max_retries=max_turns,
        ),
        instructions=prompt,
        deps_type=Deps,
        name=ROLE,
        tools=tools,
        capabilities=[LocalWorkspace(folder), files],
        retries={"tools": 3, "output": max_turns},
    )


async def extract(
    agent: Agent[Deps, Finished],
    deps: Deps,
    *,
    model: Model,
    settings: ModelSettings,
    budget: Budget,
    resume: bool,
    guidance: str | None = None,
) -> ExtractResult:
    """The caller settles whatever the outcome. The cost limit trips only after the response that crossed it is billed,
    so the usage reported is the run's own tally in `spent`, not the limit's."""
    limits = UsageLimits(request_limit=budget.max_turns, cost_limit=Decimal(str(budget.max_cost_usd)))
    opening = (RESUME if resume else START) + (f"\n\n{guidance}" if guidance is not None else "")
    turn = 0
    spent = RunUsage()
    total = Usage()
    try:
        async with agent.iter(
            opening, deps=deps, model=model, model_settings=settings, usage_limits=limits, usage=spent
        ) as run:
            async for node in run:
                if Agent.is_model_request_node(node):
                    _emit_results(deps, node.request)
                    # pydantic-ai refuses a request past the turn limit only once this node runs: that one isn't a turn.
                    if spent.requests < budget.max_turns:
                        turn += 1
                        deps.emit(LlmRequest(turn=turn))
                elif Agent.is_call_tools_node(node):
                    before, total = total, models.usage_of(spent)
                    _emit_response(deps, node.model_response, turn=turn, total=total, before=before)
            result = run.result
    except UsageLimitExceeded as e:
        total = models.usage_of(spent)
        return ExtractResult(outcome="budget_exhausted", usage=total, turns=total.requests, message=str(e))
    except AgentRunError as e:
        return ExtractResult(
            outcome="failed",
            usage=models.usage_of(spent),
            turns=spent.requests,
            message=models.explain(e, model),
            refused=models.refused(e),
        )
    total = models.usage_of(spent)
    if result is None:
        return ExtractResult(outcome="failed", usage=total, turns=total.requests, message="the run ended without done")
    return ExtractResult(outcome="finished", usage=total, turns=total.requests, message=result.output.text)


def view_image(ctx: RunContext[Deps], path: str) -> list[str | BinaryContent] | str:
    """Look at a PNG in the paper's folder: a page, a table or a figure.

    Args:
        path: Relative to the folder, e.g. "pages/p4.png" or "tables/p7.t1.png".
    """
    image = ctx.deps.workspace.image(path)
    if isinstance(image, str):
        return image
    return [path, BinaryContent(data=image, media_type="image/png")]


def check(ctx: RunContext[Deps]) -> str:
    """Check the folder, as `sage extract check` does: every problem as `file:line  target: what to do`, then the
    totals."""
    return ctx.deps.workspace.check()


def done(ctx: RunContext[Deps], summary: str) -> Finished:
    finished = ctx.deps.workspace.done(summary)
    if not finished.accepted:
        raise ModelRetry(finished.text)
    return finished


def _body(skill: str) -> str:
    """The skill without its front matter, which only tells a harness when to load it."""
    if skill.startswith("---"):
        _, _, rest = skill[3:].partition("\n---")
        return rest.lstrip("\n")
    return skill


def _emit_results(deps: Deps, request: ModelRequest) -> None:
    for part in request.parts:
        if isinstance(part, ToolReturnPart):
            # An accepted done ends the run, so a done return that reaches a later request was not used.
            ok = part.tool_name != "done"
            text = part.model_response_str()[:2000]
            deps.emit(LlmToolResult(call_id=part.tool_call_id, tool=part.tool_name, ok=ok, text=text))
        elif isinstance(part, RetryPromptPart) and part.tool_name is not None:
            text = part.model_response()[:2000]
            deps.emit(LlmToolResult(call_id=part.tool_call_id, tool=part.tool_name, ok=False, text=text))


def _emit_response(deps: Deps, response: ModelResponse, *, turn: int, total: Usage, before: Usage) -> None:
    for part in response.parts:
        if isinstance(part, ThinkingPart) and part.content:
            deps.emit(LlmThinking(text=part.content))
        elif isinstance(part, TextPart) and part.content.strip():
            deps.emit(LlmText(text=part.content))
        elif isinstance(part, ToolCallPart):
            deps.emit(LlmToolCall(call_id=part.tool_call_id, tool=part.tool_name, args=_args(part)))
    request = Usage(
        requests=1,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cache_read_tokens=response.usage.cache_read_tokens,
        cache_write_tokens=response.usage.cache_write_tokens,
        cost_usd=total.cost_usd - before.cost_usd,
    )
    deps.emit(UsageEvent(turn=turn, request=request, total=total, cache_miss=models.cache_miss(response)))


def _args(part: ToolCallPart) -> JsonValue:
    try:
        return JSON.validate_python(part.args_as_dict())
    except ValueError:
        return str(part.args)
