"""Model ids from settings, as pydantic-ai models, reached through the provider each id names: Anthropic, OpenAI or any
server with its API, Azure OpenAI, Google's Gemini API, Vertex AI or Amazon Bedrock."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import cache
from typing import Any, override
from urllib.parse import urlparse

from anthropic.types.beta import BetaCacheMissPreviousMessageNotFound, BetaCacheMissUnavailable, BetaDiagnostics
from genai_prices import UpdatePrices
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, infer_model, known_model_names
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.profiles import DEFAULT_PROFILE
from pydantic_ai.providers import Provider as Client
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage, RunUsage

from sage.events import BUILTIN, EFFORTS, CacheMiss, Connection, Effort, ModelId, ProviderKind, Usage

_KNOWN = [m for m in known_model_names() if m.partition(":")[0] in BUILTIN]
_BY_NAME = {m.partition(":")[2]: m for m in _KNOWN}
_AMBIGUOUS = frozenset(name for m in _KNOWN if (name := m.partition(":")[2]) and _BY_NAME[name] != m)

AZURE_API_VERSION = "2024-10-21"
DIRECT = {"anthropic": "api.anthropic.com", "openai": "api.openai.com"}
"""The hosts of the providers' own APIs. Anywhere else is a gateway or a compatible server."""


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens. A cache price left out is worked out from genai-prices' discount for the model,
    else charged at the input price, so a run never spends past its limit on a guess."""

    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None


@dataclass(frozen=True)
class Route:
    """A model and how to reach it: the provider its id names, and your price when you gave one."""

    model_id: str
    connection: Connection
    price: Price | None = None
    """Your price. It overrides genai-prices'."""

    @property
    def name(self) -> str:
        return self.model_id.partition(":")[2]


@dataclass(frozen=True)
class Listed:
    name: str
    """The provider's id for it, which goes after the provider's in a SAGE id."""
    title: str | None = None
    """The provider's own display name, where it gives one."""


@dataclass(frozen=True)
class Listing:
    """What a provider said when asked for the models it lets these credentials use. Asking is free, so it doubles as
    the credentials' test."""

    ok: bool
    message: str
    models: tuple[Listed, ...] = ()


def _failed(name: str, status: int, detail: object = None, *, bad_key: bool | None = None) -> Listing:
    """An error answer: a refused key, which is a 401 or 403 unless `bad_key` says, else one to try again."""
    if not (status in (401, 403) if bad_key is None else bad_key):
        return Listing(ok=False, message=f"{name} answered HTTP {status}; try again in a minute")
    return Listing(ok=False, message=f"{name} rejected this key (HTTP {status})" + (f": {detail}" if detail else ""))


def _unreachable(name: str) -> Listing:
    return Listing(ok=False, message=f"couldn't reach {name} from this machine")


def _accepted(name: str, found: list[Listed], note: str = "") -> Listing:
    unique = {m.name: m for m in found}
    return Listing(ok=True, message=f"{name} accepted the key{note}", models=tuple(unique[n] for n in sorted(unique)))


def _key(c: Connection) -> str | None:
    return (c.api_key.get_secret_value() or None) if c.api_key is not None else None


def _host(url: str | None) -> str:
    return urlparse(url or "").hostname or (url or "")


def _vertex_credentials(c: Connection) -> Any:  # noqa: ANN401 - google-auth's Credentials, imported lazily
    if c.credentials_file is None:
        return None
    from google.oauth2 import service_account

    return service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
        c.credentials_file, scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )


def _client(c: Connection, name: str, *, offline: bool) -> tuple[str, Client[Any]]:
    """The pydantic-ai model prefix and the provider client for a model on this connection. Offline, the client never
    signs in or sends anything: it is for reading a model's profile and price. Each SDK is imported only when used."""
    key = None if offline else _key(c)
    match c.kind:
        case "anthropic":
            from pydantic_ai.providers.anthropic import AnthropicProvider

            return "anthropic", AnthropicProvider(api_key=key or "offline", base_url=c.base_url)
        case "openai":
            from pydantic_ai.providers.openai import OpenAIProvider

            # A compatible server speaks Chat Completions; only OpenAI's own has the Responses API for sure.
            prefix = "openai" if c.base_url is None else "openai-chat"
            return prefix, OpenAIProvider(base_url=c.base_url, api_key=key or "none")
        case "azure":
            from pydantic_ai.providers.azure import AzureProvider

            endpoint = c.base_url or "https://offline.openai.azure.com"
            return "openai-chat", AzureProvider(
                azure_endpoint=endpoint, api_version=c.api_version or AZURE_API_VERSION, api_key=key or "offline"
            )
        case "google":
            from pydantic_ai.providers.google import GoogleProvider

            return "google", GoogleProvider(api_key=key or "offline")
        case "vertex" if name.startswith("claude"):
            from pydantic_ai.providers.anthropic import AnthropicProvider

            if offline:
                return "anthropic", AnthropicProvider(api_key="offline")
            from anthropic import AsyncAnthropicVertex

            client = AsyncAnthropicVertex(
                project_id=c.project or "", region=c.region or "global", credentials=_vertex_credentials(c)
            )
            return "anthropic", AnthropicProvider(anthropic_client=client)
        case "vertex":
            from pydantic_ai.providers.google import GoogleProvider

            if offline:
                return "google", GoogleProvider(api_key="offline")
            from pydantic_ai.providers.google_cloud import GoogleCloudProvider

            return "google-cloud", GoogleCloudProvider(
                project=c.project, location=c.region or "global", credentials=_vertex_credentials(c)
            )
        case "bedrock":
            from pydantic_ai.providers.bedrock import BedrockProvider

            region = c.region or "us-east-1"
            if offline:
                return "bedrock", BedrockProvider(api_key="offline", region_name=region)
            # A Bedrock API key is a bearer token, so boto3 reads no AWS credentials. Without one, the named profile
            # signs, and nothing else: an empty one would let boto3 pick whatever credentials the shell has.
            if key is not None:
                return "bedrock", BedrockProvider(api_key=key, region_name=region, base_url=c.base_url)
            return "bedrock", BedrockProvider(region_name=region, profile_name=c.aws_profile, base_url=c.base_url)


def build(route: Route) -> Model:
    """The model for a canonical id, signed in with its provider's credentials from Settings, never the environment.
    `core.route` has already refused a provider that lacks what it needs."""
    prefix, client = _client(route.connection, route.name, offline=False)
    model = infer_model(f"{prefix}:{route.name}", provider_factory=lambda _: client)
    if route.price is None:
        return model
    found = entry(route)
    return _Priced(model, replace(route.price, cache_read=found.cache_read_usd, cache_write=found.cache_write_usd))


class _Priced(WrapperModel):
    """A model at your price. pydantic-ai keeps a cost already set on a response, so the cost limit holds on it."""

    def __init__(self, wrapped: Model, price: Price) -> None:
        super().__init__(wrapped)
        self.price = price

    @override
    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        response = await super().request(messages, model_settings, model_request_parameters)
        response.usage.cost = _cost(response.usage, self.price)
        return response


def _cost(usage: RequestUsage, price: Price) -> Decimal:
    """`input_tokens` counts the cached tokens too, so they are taken out and charged at their own price."""

    def usd(value: float | None) -> Decimal:
        return Decimal(str(price.input if value is None else value)) / Decimal(1_000_000)

    fresh = max(0, usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens)
    return (
        usd(price.input) * fresh
        + usd(price.cache_read) * usage.cache_read_tokens
        + usd(price.cache_write) * usage.cache_write_tokens
        + usd(price.output) * usage.output_tokens
    )


def _anthropic(model: Model) -> bool:
    """Whether Anthropic's own client calls the model, directly, through a gateway or on Vertex AI, so it takes
    Anthropic's effort scale and cache settings."""
    inner = model.wrapped if isinstance(model, WrapperModel) else model
    return isinstance(inner, AnthropicModel)


def list_models(c: Connection) -> Listing:
    """The models the provider lets these credentials use, by its own name. Every provider answers this for free."""
    return _LISTERS[c.kind](c)


def _list_anthropic(c: Connection) -> Listing:
    import anthropic

    name = "Anthropic" if c.base_url is None else _host(c.base_url)
    client = anthropic.Anthropic(api_key=_key(c), base_url=c.base_url, max_retries=0, timeout=15)
    try:
        found = [Listed(m.id, m.display_name) for m in client.models.list(limit=1000)]
    except anthropic.APIStatusError as e:
        return _failed(name, e.status_code)
    except anthropic.APIConnectionError:
        return _unreachable(name)
    return _accepted(name, found)


def _list_openai(c: Connection) -> Listing:
    import openai

    name = "OpenAI" if c.base_url is None else _host(c.base_url)
    client = openai.OpenAI(api_key=_key(c) or "none", base_url=c.base_url, max_retries=0, timeout=15)
    try:
        found = [m.id for m in client.models.list()]
    except openai.APIStatusError as e:
        return _failed(name, e.status_code)
    except openai.APIConnectionError:
        return _unreachable(name)
    # OpenAI lists embedding, image and speech models too, and says nothing of what each does: its chat models are
    # the ones pydantic-ai knows by name. A compatible server lists what it serves, which is all it has.
    if c.base_url is None:
        found = [m for m in found if f"openai:{m}" in _KNOWN]
    return _accepted(name, [Listed(m) for m in found])


def _list_azure(c: Connection) -> Listing:
    import httpx
    import openai

    name = f"Azure OpenAI ({_host(c.base_url)})"
    version = c.api_version or AZURE_API_VERSION
    client = openai.AzureOpenAI(azure_endpoint=c.base_url or "", api_key=_key(c), api_version=version, max_retries=0)
    try:
        client.models.list()
    except openai.APIStatusError as e:
        return _failed(name, e.status_code)
    except openai.APIConnectionError:
        return _unreachable(name)
    # A model on Azure goes by its deployment's name. Only this older API version lists them with a key; a resource
    # that doesn't answer it still works, with each deployment added by name.
    try:
        r = httpx.get(
            f"{(c.base_url or '').rstrip('/')}/openai/deployments",
            params={"api-version": "2022-12-01"},
            headers={"api-key": _key(c) or ""},
            timeout=15,
        )
        r.raise_for_status()
        found = [Listed(d["id"], d.get("model")) for d in r.json().get("data", [])]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return _accepted(name, [], note="; add each deployment by its name")
    return _accepted(name, found)


def _list_google(c: Connection) -> Listing:
    import httpx
    from google import genai
    from google.genai import errors, types

    client = genai.Client(api_key=_key(c), http_options=types.HttpOptions(timeout=15_000))
    try:
        found = [
            Listed((m.name or "").removeprefix("models/"), m.display_name)
            for m in client.models.list(config={"page_size": 1000})
            if "generateContent" in (m.supported_actions or [])
        ]
    except errors.APIError as e:
        # Google answers a bad key with 400 and says so in the message, where the others use 401.
        bad = e.code in (401, 403) or (e.code == 400 and "api key not valid" in str(e.message).lower())
        return _failed("Google", e.code, bad_key=bad)
    except httpx.TransportError:
        return _unreachable("Google")
    return _accepted("Google", found)


def _list_vertex(c: Connection) -> Listing:
    import httpx
    from google import genai
    from google.auth.exceptions import GoogleAuthError
    from google.genai import errors, types

    name = f"Vertex AI ({c.project}, {c.region or 'global'})"
    try:
        client = genai.Client(
            vertexai=True,
            project=c.project,
            location=c.region or "global",
            credentials=_vertex_credentials(c),
            http_options=types.HttpOptions(timeout=15_000),
        )
        found = [
            Listed((m.name or "").rpartition("/")[2], m.display_name)
            for m in client.models.list(config={"page_size": 1000, "query_base": True})
            if (m.name or "").rpartition("/")[2].startswith("gemini")
        ]
    except GoogleAuthError as e:
        return Listing(ok=False, message=f"no Google Cloud credentials: {e}")
    except (OSError, ValueError) as e:
        return Listing(ok=False, message=f"couldn't read the service account key file: {e}")
    except errors.APIError as e:
        return _failed(name, e.code, e.message)
    except httpx.TransportError:
        return _unreachable(name)
    # Vertex AI lists Google's own models. Claude is there too, by its Vertex name, but not in the list.
    return Listing(
        ok=True,
        message=f"{name} accepted the credentials; add Claude by name, such as claude-sonnet-4-5@20250929",
        models=tuple(sorted({m.name: m for m in found}.values(), key=lambda m: m.name)),
    )


def _list_bedrock(c: Connection) -> Listing:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
    from botocore.session import Session
    from botocore.tokens import FrozenAuthToken

    region = c.region or "us-east-1"
    key = _key(c)
    config = Config(connect_timeout=15, read_timeout=15)
    name = f"Bedrock in {region}"
    try:
        if key is not None:

            class Bearer(Session):
                @override
                def get_auth_token(self, **_: object) -> FrozenAuthToken:
                    return FrozenAuthToken(key)

            session = boto3.Session(botocore_session=Bearer(), region_name=region)
            config = config.merge(Config(signature_version="bearer"))
        else:
            session = boto3.Session(profile_name=c.aws_profile, region_name=region)
        client = session.client("bedrock", config=config)
        models = client.list_foundation_models(byOutputModality="TEXT")["modelSummaries"]
        profiles = client.list_inference_profiles(maxResults=1000)["inferenceProfileSummaries"]
    except ClientError as e:
        status = int(e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
        return _failed(name, status, e.response.get("Error", {}).get("Message"))
    except BotoCoreError as e:
        # A missing profile, an expired SSO login or no network: botocore's own words say which.
        return Listing(ok=False, message=f"{name}: {e}")
    # A model runs on demand by its own id, or across regions through an inference profile's.
    on_demand = [
        Listed(m["modelId"], m.get("modelName")) for m in models if "ON_DEMAND" in m.get("inferenceTypesSupported", [])
    ]
    return _accepted(
        name, on_demand + [Listed(p["inferenceProfileId"], p.get("inferenceProfileName")) for p in profiles]
    )


_LISTERS: dict[ProviderKind, Callable[[Connection], Listing]] = {
    "anthropic": _list_anthropic,
    "openai": _list_openai,
    "azure": _list_azure,
    "google": _list_google,
    "vertex": _list_vertex,
    "bedrock": _list_bedrock,
}


def resolve(model_id: ModelId) -> str:
    """The canonical 'provider:name' for either form of id. A prefixed id passes through. A bare name has to be one
    pydantic-ai knows on a built-in provider, since guessing the provider from a name's shape picks wrong the day a
    vendor reuses another's naming style."""
    if ":" in model_id:
        return model_id
    found = None if model_id in _AMBIGUOUS else _BY_NAME.get(model_id)
    if found is None:
        raise LookupError(f"write it with its provider's id, such as {' or '.join(f'{p}:{model_id}' for p in BUILTIN)}")
    return found


@dataclass(frozen=True)
class Entry:
    id: str
    title: str | None
    """genai-prices' name for it, such as Claude Haiku 4.5, where it knows the model. None when that name is only the
    id in lower case, such as "gpt 4.1", which reads worse than the id."""
    input_usd: float | None
    """Per million input tokens, before any cache discount: your price, else genai-prices'. None when neither has
    one."""
    output_usd: float | None
    cache_read_usd: float | None
    """Per million tokens read from the prompt cache: your price, else genai-prices' discount applied to the input
    price. None when the input price is None."""
    cache_write_usd: float | None
    tools: bool
    """Whether it can call tools. The extraction agent works only through tools, so a model without them can't run."""
    efforts: tuple[Effort, ...]
    """The effort levels it tells apart. Empty for a model that doesn't think, where effort changes nothing."""


@cache
def _offline(c: Connection, name: str) -> Model:
    """A model that never sends a request, for reading its profile and price. Building one is the slow part."""
    prefix, client = _client(c, name, offline=True)
    return infer_model(f"{prefix}:{name}", provider_factory=lambda _: client)


def keep_prices_fresh() -> None:
    """Fetch genai-prices' newest table now and then every hour, in the background, for as long as the process runs.
    Until a fetch lands, or with no network, the table bundled with the package answers."""
    UpdatePrices().start()


def entry(route: Route) -> Entry:
    """A model's price, name, tools and effort levels, without a request. The price is yours when you gave one, else
    genai-prices', matched on the provider and its endpoint, so a model on OpenRouter gets OpenRouter's price. A model
    it can't price has none, and pydantic-ai would warn and carry on past a cost limit it can't check."""
    model = _offline(route.connection.model_copy(update={"api_key": None}), route.name)
    response = ModelResponse(
        parts=[],
        # A thousand tokens each way: a million crosses into the long-context tier some models price higher.
        usage=RequestUsage(input_tokens=1_000, output_tokens=1_000),
        model_name=model.model_name,
        provider_name=model.system,
        provider_url=model.base_url,
    )
    found = _priced(response)
    title = found.model.name if found is not None else None
    read_share = write_share = None
    if found is not None and found.input_price:
        base = found.input_price
        read = _priced(replace(response, usage=RequestUsage(input_tokens=1_000, cache_read_tokens=1_000)))
        write = _priced(replace(response, usage=RequestUsage(input_tokens=1_000, cache_write_tokens=1_000)))
        read_share = float(read.input_price / base) if read is not None else None
        write_share = float(write.input_price / base) if write is not None else None
    if route.price is not None:
        p = route.price
        input_usd, output_usd = p.input, p.output
        cache_read = p.cache_read if p.cache_read is not None else p.input * (read_share or 1.0)
        cache_write = p.cache_write if p.cache_write is not None else p.input * (write_share or 1.0)
    elif found is not None:
        input_usd, output_usd = float(found.input_price) * 1_000, float(found.output_price) * 1_000
        cache_read, cache_write = input_usd * (read_share or 1.0), input_usd * (write_share or 1.0)
    else:
        input_usd = output_usd = cache_read = cache_write = None
    return Entry(
        id=route.model_id,
        title=title if title is not None and title != title.lower() else None,
        input_usd=input_usd,
        output_usd=output_usd,
        cache_read_usd=cache_read,
        cache_write_usd=cache_write,
        tools=model.profile.get("supports_tools", DEFAULT_PROFILE["supports_tools"]),
        efforts=_efforts(model),
    )


def _priced(response: ModelResponse) -> Any:  # noqa: ANN401 - genai-prices' PriceCalculation, or None
    try:
        return response.cost()
    except (LookupError, ValueError):
        return None


def _efforts(model: Model) -> tuple[Effort, ...]:
    if not model.profile.get("supports_thinking", DEFAULT_PROFILE["supports_thinking"]):
        return ()
    return EFFORTS if _anthropic(model) else tuple(e for e in EFFORTS if e != "max")


def runs_at(efforts: tuple[Effort, ...], effort: Effort) -> Effort | None:
    """The level a model with these `efforts` thinks at when asked for `effort`: that one, else the hardest it has
    below it, or None for a model that doesn't think. A level set before it was checked against the model, such as a
    `max` an old config gave every model, lands here."""
    if not efforts:
        return None
    if effort in efforts:
        return effort
    below = [e for e in efforts if EFFORTS.index(e) < EFFORTS.index(effort)]
    return below[-1] if below else efforts[0]


def settings(model: Model, effort: Effort | None) -> ModelSettings:
    """Anthropic's client takes its own effort scale, which has a `max` the unified one lacks, and shows its thinking
    summarized, because the API omits the text by default and the run log shows it. Elsewhere the unified level maps
    onto whatever the provider calls it. OpenAI then returns no thinking text: its reasoning summaries need a
    verified organization, and asking for one without it fails the request. None, for a model that doesn't think,
    asks for no thinking."""
    if effort is None:
        return ModelSettings()
    if not _anthropic(model):
        # Anthropic's `max` is beyond the unified scale, whose hardest level is xhigh.
        return ModelSettings(thinking="xhigh" if effort == "max" else effort)
    return AnthropicModelSettings(
        anthropic_effort=effort,
        anthropic_thinking={"type": "adaptive", "display": "summarized"},
    )


def cached(base: ModelSettings, model: Model, *, diagnostics: bool) -> ModelSettings:
    """The unified `cache` caches the tool definitions, the instructions and the growing conversation, each provider
    its own way. On Anthropic it sets exactly the three breakpoints `05-prompt-caching.md` measures. Diagnostics are
    Anthropic's own API's alone, and asking anyone else for them is a 400."""
    whole = ModelSettings(**base, cache=True)
    if not diagnostics or not _anthropic(model) or _host(model.base_url) != DIRECT["anthropic"]:
        return whole
    return AnthropicModelSettings(**whole, anthropic_cache_diagnostics=True)


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


def _detail(body: object) -> str:
    """The provider's own words from an error body: Bedrock's `Error.Message`, or the `error.message` OpenAI,
    Anthropic and Google send. Anything else as it came, cut short."""
    if isinstance(body, dict):
        for key, field in (("Error", "Message"), ("error", "message")):
            inner = body.get(key)
            message = inner.get(field) if isinstance(inner, dict) else None
            if isinstance(message, str):
                return message
            if isinstance(inner, str):
                return inner
    text = " ".join(str(body).split())
    return text if len(text) <= 300 else text[:299] + "…"


def _out_of_quota(e: ModelHTTPError) -> bool:
    """A 429 waiting won't fix: OpenAI's `insufficient_quota`, or Google's spent free tier."""
    detail = _detail(e.body).lower()
    return e.status_code == 429 and any(w in detail for w in ("quota", "billing", "credit"))


def refused(e: Exception) -> bool:
    """Whether every other call with this key and model would fail the same way, so the run should stop at once: a
    rejected key, no access to the model, no such model, or no quota left."""
    if not isinstance(e, ModelHTTPError):
        return False
    bad_key = e.status_code == 400 and "api key not valid" in _detail(e.body).lower()
    return e.status_code in (401, 403, 404) or bad_key or _out_of_quota(e)


_SYSTEMS = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "azure": "Azure OpenAI",
    "google": "Google Gemini",
    "google-gla": "Google Gemini",
    "google-cloud": "Vertex AI",
    "google-vertex": "Vertex AI",
    "bedrock": "Bedrock",
}


def _provider_title(model: Model) -> str:
    """Who answered: the provider by name, or a gateway or local server by its host."""
    host = _host(model.base_url)
    if host.endswith("aiplatform.googleapis.com"):
        return "Vertex AI"
    if model.system in DIRECT and host != DIRECT[model.system]:
        return host
    return _SYSTEMS.get(model.system, model.system)


def explain(e: Exception, model: Model) -> str:
    """A provider error in words a scientist can act on, naming the provider that answered. The first clause says
    what happened, since the Paper screen shows only that much."""
    if not isinstance(e, ModelHTTPError):
        return str(e)
    name = _provider_title(model)
    status = e.status_code
    detail = _detail(e.body)
    if status == 401 or (status == 400 and "api key not valid" in detail.lower()):
        text = f"{name} rejected the API key (HTTP {status}); check it on the Manage page"
    elif status == 403 and model.system == "bedrock":
        # Bedrock answers 403 both for a bad key and for a model not enabled in the region.
        text = (
            f"{name} refused access (HTTP 403); check the key on the Manage page, and that {model.model_name} is "
            f"enabled in the Bedrock region: {detail}"
        )
    elif status == 403:
        text = f"{name} refused access (HTTP 403); check the key on the Manage page: {detail}"
    elif status == 404:
        text = (
            f"{name} has no model {model.model_name} for this key (HTTP 404); pick another on the Manage page: {detail}"
        )
    elif _out_of_quota(e):
        text = f"{name} says this key is out of quota (HTTP 429); add credit with {name}, then resume: {detail}"
    elif status == 429:
        text = f"{name} is rate limiting this key (HTTP 429); wait a minute and resume the run"
    elif status >= 500:
        text = f"{name} failed (HTTP {status}); resume the run to try again"
    else:
        text = f"{name} refused the request (HTTP {status}): {detail}"
    if e.suggested_model_id is not None:
        text += f"; did you mean {model.system}:{e.suggested_model_id}?"
    return text if e.hint is None else f"{text}. {e.hint}"
