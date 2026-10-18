"""The extraction agent: one pydantic-ai agent per paper. Its tools act on the Workspace, the checker answers every
write, and the run ends when `finish` is accepted or the budget runs out. Code settles the result afterwards."""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from pydantic import JsonValue, TypeAdapter, ValidationError
from pydantic_ai import Agent, BinaryContent, ModelRetry, RunContext, Tool, ToolOutput
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

from sage.agents import models, paper_view
from sage.agents.contract import CheckResult, ExpandResult, FinishResult, ReadResult, Workspace, WriteResult
from sage.events import (
    CheckTally,
    Event,
    ExpandTally,
    LlmRequest,
    LlmText,
    LlmThinking,
    LlmToolCall,
    LlmToolResult,
    Tally,
    Usage,
    UsageEvent,
    WriteTally,
)
from sage.ir.draft import Draft, Mark, TableRoles
from sage.ir.schema import RecordAdapter
from sage.ir.settled import Outcome
from sage.parse import pdf
from sage.parse.document import Document

ROLE = "extractor"
JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
START = (
    "Extract this paper into records, following the guide's workflow: read, write the context records, expand "
    "every table, write text-only observations, check, then finish."
)
MAX_READ_IDS = 40


@dataclass
class Deps:
    workspace: Workspace
    doc: Document
    pdf: bytes
    emit: Callable[[Event], None]
    render_scale: float


@dataclass(frozen=True)
class Budget:
    max_turns: int
    max_cost_usd: float


@dataclass(frozen=True)
class ReviewNote:
    record_id: str
    action: str
    field: str | None = None
    value: JsonValue = None
    note: str | None = None


@dataclass(frozen=True)
class Guidance:
    text: str | None = None
    reviews: tuple[ReviewNote, ...] = ()


@dataclass(frozen=True)
class ExtractResult:
    outcome: Outcome
    usage: Usage
    turns: int
    message: str | None = None


def instructions(guide: str, doc: Document, *, title: str | None) -> str:
    return f"{guide}\n\n# The paper\n\n{paper_view.full_view(doc, title=title)}"


def build(*, prompt: str, max_turns: int) -> Agent[Deps, FinishResult]:
    # The draft is one shared object, so the tools that read or change it run one at a time, never side by side.
    tools: list[Tool[Deps]] = [
        _write_tool(),
        Tool(expand_table, sequential=True),
        Tool(check, sequential=True),
        Tool(read, sequential=True),
        Tool(view),
    ]
    return Agent(
        output_type=ToolOutput(
            finish,
            name="finish",
            description=(
                "End the run. Refused while blocking issues are open: fix them, set a value the paper doesn't give to "
                "null with a note, and list each null field, table or issue target you could not resolve in "
                "`unresolved` as {target, cause, reason}. A field that holds a value can't be marked."
            ),
            max_retries=max_turns,
        ),
        instructions=prompt,
        deps_type=Deps,
        name=ROLE,
        tools=tools,
        retries={"tools": 3, "output": max_turns},
    )


async def extract(
    agent: Agent[Deps, FinishResult],
    deps: Deps,
    *,
    model: Model,
    settings: ModelSettings,
    budget: Budget,
    resume: bool,
    guidance: Guidance | None = None,
) -> ExtractResult:
    """The caller settles whatever the outcome. The cost limit trips only after the response that crossed it is billed,
    so the usage reported is the run's own tally in `spent`, not the limit's."""
    limits = UsageLimits(request_limit=budget.max_turns, cost_limit=Decimal(str(budget.max_cost_usd)))
    opening = (_resume_prompt(deps.workspace.draft) if resume else START) + _guidance_prompt(guidance)
    turn = 0
    spent = RunUsage()
    total = Usage()
    outcome: Outcome
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
                        deps.emit(LlmRequest(role=ROLE, turn=turn))
                elif Agent.is_call_tools_node(node):
                    before, total = total, models.usage_of(spent)
                    _emit_response(deps, node.model_response, turn=turn, total=total, before=before)
            result = run.result
    except UsageLimitExceeded as e:
        outcome, message = "budget_exhausted", str(e)
    except AgentRunError as e:
        outcome, message = "failed", models.explain(e)
    else:
        if result is None:
            outcome, message = "failed", "the run ended without a result"
        else:
            outcome, message = "finished", result.output.message
    total = models.usage_of(spent)
    return ExtractResult(outcome=outcome, usage=total, turns=total.requests, message=message)


def _resume_prompt(draft: Draft) -> str:
    by_type: dict[str, list[str]] = {}
    for r in draft.records.values():
        by_type.setdefault(r.type, []).append(r.id)
    held = "; ".join(f"{t}: {', '.join(ids[:30])}{' …' if len(ids) > 30 else ''}" for t, ids in by_type.items())
    tables = ", ".join(f"{d.table_id} {d.status}" for d in draft.tables.values()) or "none yet"
    return (
        "This run is resuming. The workspace already holds these records, so don't write them again unless they "
        f"need fixing. {held or 'No records yet.'} Tables decided: {tables}. Call check() to see what is open, "
        "carry on with the workflow from there, then finish."
    )


def _guidance_prompt(guidance: Guidance | None) -> str:
    if guidance is None or (guidance.text is None and not guidance.reviews):
        return ""
    parts = ["\n\nThe scientist has looked at the extraction. Follow the guide's section on continuing a run."]
    if guidance.text is not None:
        parts.append(f"Their guidance:\n\n{guidance.text}")
    if guidance.reviews:
        lines = "\n".join(_review_line(r) for r in guidance.reviews)
        parts.append(f"Their reviews of single records:\n\n{lines}")
    return "\n\n".join(parts)


def _review_line(r: ReviewNote) -> str:
    what = r.action if r.field is None else f"{r.action} {r.field}"
    value = "" if r.value is None else f" to {JSON.dump_json(r.value).decode()}"
    note = "" if r.note is None else f": {r.note}"
    return f"- {r.record_id}: {what}{value}{note}"


def _write_tool() -> Tool[Deps]:
    schema = RecordAdapter.json_schema()
    defs = schema.pop("$defs", {})
    return Tool.from_schema(
        write,
        name="write",
        description=(
            "Add or replace records (any type, many per call). Each is checked at once; the result lists, per id, "
            'whether it passed and the exact errors. Pass {"delete": "<id>"} to remove one.'
        ),
        json_schema={
            "type": "object",
            "properties": {
                "records": {
                    "type": "array",
                    "items": {"anyOf": [schema, {"type": "object", "properties": {"delete": {"type": "string"}}}]},
                }
            },
            "required": ["records"],
            "$defs": defs,
        },
        takes_ctx=True,
        sequential=True,
    )


def write(ctx: RunContext[Deps], records: list[JsonValue]) -> str:
    # The tool is built from a raw schema, so pydantic-ai doesn't validate the arguments. A JSON string here would be
    # walked character by character, one error each: a 700k-token tool result.
    if not isinstance(records, list):
        raise ModelRetry("records must be a JSON array of record objects, not a string holding one")
    return ctx.deps.workspace.write(records).model_dump_json(exclude_defaults=True)


def expand_table(
    ctx: RunContext[Deps],
    table_id: str,
    roles: TableRoles | None = None,
    irrelevant_reason: str | None = None,
) -> str:
    """Turn every value cell of a table into an Observation, from what each column means, or mark the table as
    holding nothing to extract. Calling it again replaces the table's earlier Observations. Returns the count, the
    errors and three sample Observations.

    Args:
        table_id: The table anchor, e.g. "p6.t1".
        roles: What each column means, and the ids every Observation from this table shares.
        irrelevant_reason: Why the table holds nothing to extract, instead of roles.
    """
    return ctx.deps.workspace.expand_table(table_id, roles, irrelevant_reason).model_dump_json(exclude_defaults=True)


def check(ctx: RunContext[Deps]) -> str:
    """Run the full checker over the workspace. Returns the open blocking issues and warnings."""
    return ctx.deps.workspace.check().model_dump_json()


def read(ctx: RunContext[Deps], ids: list[str]) -> str:
    """Read back records already in the workspace, in full. Ask only for those you are about to change.

    Args:
        ids: Record ids, at most 40 per call.
    """
    if len(ids) > MAX_READ_IDS:
        raise ModelRetry(f"read takes at most {MAX_READ_IDS} ids per call; you asked for {len(ids)}")
    held = ctx.deps.workspace.draft.records
    result = ReadResult(records=[held[i] for i in ids if i in held], unknown=[i for i in ids if i not in held])
    return result.model_dump_json()


def view(ctx: RunContext[Deps], page: int, bbox: list[float] | None = None) -> list[str | BinaryContent] | str:
    """Look at a page, or a region of it, as an image.

    Args:
        page: Page number, from 1.
        bbox: [x0, y0, x1, y1] in PDF points from the top left. Leave it out for the whole page.
    """
    doc = ctx.deps.doc
    if not 1 <= page <= len(doc.pages):
        return f"no page {page}; the paper has {len(doc.pages)} pages"
    box = None
    if bbox is not None:
        if len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            return "bbox must be [x0, y0, x1, y1] with x0 < x1 and y0 < y1"
        size = doc.pages[page - 1]
        if bbox[0] >= size.width or bbox[1] >= size.height or bbox[2] <= 0 or bbox[3] <= 0:
            return f"bbox {bbox} lies off page {page}, which is {size.width:g} x {size.height:g} points"
        box = (bbox[0], bbox[1], bbox[2], bbox[3])
    image = pdf.render(ctx.deps.pdf, page, bbox=box, scale=ctx.deps.render_scale)
    where = f"page {page}" if box is None else f"page {page}, region {list(box)}"
    return [f"image of {where}", BinaryContent(data=image, media_type="image/png")]


def finish(ctx: RunContext[Deps], summary: str, unresolved: list[Mark] | None = None) -> FinishResult:
    result = ctx.deps.workspace.finish(summary, unresolved or [])
    if not result.accepted:
        listed = "\n".join(result.blocking)
        raise ModelRetry(f"finish refused: {result.message}\n{listed}".strip())
    return result


def _tally(tool: str, content: object) -> Tally | None:
    """The figures the run log shows for a tool's result, read from its whole JSON before the text is clipped."""
    if not isinstance(content, str):
        return None
    try:
        match tool:
            case "write":
                written = WriteResult.model_validate_json(content)
                return WriteTally(rejected=sum(not o.ok for o in written.results))
            case "expand_table":
                expanded = ExpandResult.model_validate_json(content)
                return ExpandTally(observations=expanded.observations, failed=expanded.failed)
            case "check":
                checked = CheckResult.model_validate_json(content)
                return CheckTally(blocking=checked.blocking_total, warnings=checked.warning_total)
            case _:
                return None
    except ValidationError:
        return None


def _emit_results(deps: Deps, request: ModelRequest) -> None:
    for part in request.parts:
        if isinstance(part, ToolReturnPart):
            # An accepted finish ends the run, so a finish return that reaches a later request was not used.
            ok = part.tool_name != "finish"
            text = part.model_response_str()[:2000]
            tally = _tally(part.tool_name, part.content)
            deps.emit(LlmToolResult(call_id=part.tool_call_id, tool=part.tool_name, ok=ok, text=text, tally=tally))
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
    deps.emit(UsageEvent(role=ROLE, turn=turn, request=request, total=total, cache_miss=models.cache_miss(response)))


def _args(part: ToolCallPart) -> JsonValue:
    try:
        return JSON.validate_python(part.args_as_dict())
    except ValueError:
        return str(part.args)
