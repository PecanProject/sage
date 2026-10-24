"""Model ids from settings, as pydantic-ai models. SAGE supports Anthropic, OpenAI, Google and Amazon Bedrock."""

from typing import Any

import anthropic as anthropic_sdk
from anthropic.types.beta import BetaCacheMissPreviousMessageNotFound, BetaCacheMissUnavailable, BetaDiagnostics
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import Model, infer_model, known_model_names
from pydantic_ai.models.anthropic import AnthropicModelSettings
from pydantic_ai.providers import Provider
from pydantic_ai.settings import ModelSettings, ThinkingEffort
from pydantic_ai.usage import RunUsage

from sage.events import PROVIDERS, CacheMiss, Effort, ModelId, Usage

_KNOWN = [m for m in known_model_names() if m.partition(":")[0] in PROVIDERS]
_BY_NAME = {m.partition(":")[2]: m for m in _KNOWN}
_AMBIGUOUS = frozenset(name for m in _KNOWN if (name := m.partition(":")[2]) and _BY_NAME[name] != m)
"""Bare names two of our providers both publish. Empty today, so a bare name resolves on its own; a name that lands
here has to be written with its provider instead of being guessed at."""


def resolve(model_id: ModelId) -> str:
    """The canonical 'provider:name' for either form of id. A prefixed id passes through unchecked, so a model
    released after this install's pydantic-ai still runs. A bare name has to be one pydantic-ai knows, since
    guessing the provider from the name's shape picks wrong the day a vendor reuses another's naming style."""
    if ":" in model_id:
        return model_id
    found = None if model_id in _AMBIGUOUS else _BY_NAME.get(model_id)
    if found is None:
        raise LookupError(f"write it as {' or '.join(f'{p}:{model_id}' for p in PROVIDERS)}")
    return found


def provider_of(model_id: ModelId) -> str:
    return resolve(model_id).partition(":")[0]


def _provider(name: str, api_key: str, bedrock_region: str) -> Provider[Any]:
    """One provider, its SDK imported on use: google-genai alone takes longer to import than the whole of SAGE, and
    most commands never call a model. `infer_provider_class` would defer the import too, but `Provider` declares no
    constructor, so the key couldn't be passed through it."""
    if name == "anthropic":
        from pydantic_ai.providers.anthropic import AnthropicProvider

        return AnthropicProvider(api_key=api_key)
    if name == "openai":
        from pydantic_ai.providers.openai import OpenAIProvider

        return OpenAIProvider(api_key=api_key)
    if name == "google":
        from pydantic_ai.providers.google import GoogleProvider

        return GoogleProvider(api_key=api_key)
    if name == "bedrock":
        from pydantic_ai.providers.bedrock import BedrockProvider

        # A Bedrock API key is a bearer token, so boto3 never reads the ambient AWS credential chain here, and the
        # region has to be given: it is part of the endpoint, not of the key.
        return BedrockProvider(api_key=api_key, region_name=bedrock_region)
    raise LookupError(f"no provider named {name!r}")


def build(model_id: ModelId, *, keys: dict[str, str], bedrock_region: str) -> Model:
    """Any of the four providers, keyed from Settings rather than from the environment. `keys` can't be missing the
    one it needs: `core.require_keys` refused the run without it. `bedrock_region` is passed whatever the provider,
    and only Bedrock reads it, so a Bedrock model never falls back to boto3's own region lookup."""
    return infer_model(resolve(model_id), provider_factory=lambda name: _provider(name, keys[name], bedrock_region))


_THINKING: dict[Effort, ThinkingEffort] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",
}
"""Effort on a provider without Anthropic's `max`: the hardest level the unified scale has."""


def settings(model_id: ModelId, effort: Effort) -> ModelSettings:
    """Anthropic takes its own effort scale, which has a `max` the unified one doesn't, and shows thinking
    summarized because the API omits its text by default and the run log shows it. Elsewhere the unified level
    maps onto whatever the provider calls it."""
    if provider_of(model_id) != "anthropic":
        return ModelSettings(thinking=_THINKING[effort])
    return AnthropicModelSettings(
        anthropic_effort=effort,
        anthropic_thinking={"type": "adaptive", "display": "summarized"},
    )


def cached(base: ModelSettings, model_id: ModelId, *, diagnostics: bool) -> ModelSettings:
    """The unified `cache` caches the tool definitions, the instructions and the growing conversation, each provider
    its own way. Diagnostics are Anthropic's alone, and asking another provider for them is a 400."""
    whole = ModelSettings(**base, cache=True)
    if not diagnostics or provider_of(model_id) != "anthropic":
        return whole
    return AnthropicModelSettings(**whole, anthropic_cache_diagnostics=True)


def cached_instructions(base: ModelSettings) -> ModelSettings:
    """The instructions breakpoint also caches the tool definitions before it. Caching the conversation as well would
    land a breakpoint on each call's own image instead, which no other call reads."""
    return ModelSettings(**base, cache={"messages": False})


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
        where = f"the API behind {e.model_name}"
        # Google answers a bad key with 400 and says so in the body, where Anthropic and OpenAI use 401.
        bad_key = e.status_code in (401, 403) or (e.status_code == 400 and "api key not valid" in str(e.body).lower())
        if bad_key:
            return f"{where} rejected the API key (HTTP {e.status_code}); check it in Settings"
        if e.status_code == 429:
            return f"{where} is rate limiting this key (HTTP 429); wait a minute and resume the run"
        if e.status_code >= 500:
            return f"{where} failed (HTTP {e.status_code}); resume the run to try again"
        return f"{where} refused the request (HTTP {e.status_code}): {e.body}"
    return str(e)


def check_key(model_id: ModelId, api_key: str) -> tuple[bool, str]:
    """Whether the key works, and what to tell the scientist, which the caller shows as it is. Anthropic lists
    models for free, so its key is really checked. OpenAI, Google and Bedrock have no call that costs nothing, so a
    saved key is reported as unchecked rather than as good: saying it was accepted would claim a check that never
    happened. A gateway set through ANTHROPIC_BASE_URL may not serve the model list either, which reads the same
    way."""
    if provider_of(model_id) != "anthropic":
        return True, "Saved. Only a run can tell whether this key works."
    try:
        anthropic_sdk.Anthropic(api_key=api_key, max_retries=0, timeout=15).models.list(limit=1)
    except anthropic_sdk.APIStatusError as e:
        if e.status_code in (401, 403):
            return False, f"Anthropic rejected this key (HTTP {e.status_code})"
        if e.status_code == 404:
            # A gateway behind ANTHROPIC_BASE_URL may serve messages without the model list. Not a bad key.
            return True, "Saved. This endpoint has no model list to check against, so only a run can tell."
        return False, f"Anthropic answered HTTP {e.status_code}; try again in a minute"
    except anthropic_sdk.APIConnectionError:
        return False, "couldn't reach the Anthropic API from this machine"
    return True, "Anthropic accepted the key"
