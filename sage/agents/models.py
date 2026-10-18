"""Model ids from settings, as pydantic-ai models. Only Anthropic models are supported."""

import anthropic as anthropic_sdk
from anthropic.types.beta import BetaCacheMissPreviousMessageNotFound, BetaCacheMissUnavailable, BetaDiagnostics
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.usage import RunUsage

from sage.events import CacheMiss, Effort, ModelId, Usage


def anthropic(model_id: ModelId, *, api_key: str) -> Model:
    return AnthropicModel(model_id.removeprefix("anthropic:"), provider=AnthropicProvider(api_key=api_key))


def settings(effort: Effort) -> AnthropicModelSettings:
    """Thinking is summarized because the API omits its text by default, and the run log shows it."""
    return AnthropicModelSettings(
        anthropic_effort=effort,
        anthropic_thinking={"type": "adaptive", "display": "summarized"},
    )


def cached(base: AnthropicModelSettings, *, diagnostics: bool) -> AnthropicModelSettings:
    return AnthropicModelSettings(
        **base,
        anthropic_cache_tool_definitions=True,
        anthropic_cache_instructions=True,
        anthropic_cache=True,
        anthropic_cache_diagnostics=diagnostics,
    )


def cached_instructions(base: AnthropicModelSettings) -> AnthropicModelSettings:
    """The instructions breakpoint also caches the tool definitions before it. An automatic breakpoint would land on
    each call's own image instead, which no other call reads."""
    return AnthropicModelSettings(**base, anthropic_cache_instructions=True)


def cache_miss(response: ModelResponse) -> CacheMiss | None:
    found = (response.provider_details or {}).get("cache_diagnostics")
    if found is None:
        return None
    reason = BetaDiagnostics.model_validate(found).cache_miss_reason
    if reason is None:
        return CacheMiss(reason="pending")
    if isinstance(reason, BetaCacheMissPreviousMessageNotFound | BetaCacheMissUnavailable):
        return CacheMiss(reason=reason.type)
    return CacheMiss(reason=reason.type, missed_tokens=reason.cache_missed_input_tokens)


def usage_of(run: RunUsage) -> Usage:
    return Usage(
        requests=run.requests,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cache_read_tokens=run.cache_read_tokens,
        cache_write_tokens=run.cache_write_tokens,
        cost_usd=float(run.cost) if run.cost is not None else 0.0,
    )


def explain(e: Exception) -> str:
    """A provider error in words a scientist can act on."""
    if isinstance(e, ModelHTTPError):
        if e.status_code in (401, 403):
            return f"the Anthropic API rejected the API key (HTTP {e.status_code}); check it in Settings"
        if e.status_code == 429:
            return "the Anthropic API is rate limiting this key (HTTP 429); wait a minute and resume the run"
        if e.status_code >= 500:
            return f"the Anthropic API failed (HTTP {e.status_code}); resume the run to try again"
        return f"the Anthropic API refused the request (HTTP {e.status_code}): {e.body}"
    return str(e)


def check_key(api_key: str) -> str | None:
    """None when Anthropic accepts the key, else why not. Listing models is free."""
    try:
        anthropic_sdk.Anthropic(api_key=api_key, max_retries=0, timeout=15).models.list(limit=1)
    except anthropic_sdk.APIStatusError as e:
        if e.status_code in (401, 403):
            return f"Anthropic rejected this key (HTTP {e.status_code})"
        return f"Anthropic answered HTTP {e.status_code}; try again in a minute"
    except anthropic_sdk.APIConnectionError:
        return "couldn't reach the Anthropic API from this machine"
    return None
