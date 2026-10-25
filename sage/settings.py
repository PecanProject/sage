"""The one Settings model. Precedence: environment, then config.toml, then defaults."""

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Self, override

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from sage.events import BUILTIN, PROVIDER_ID, Connection, Effort, ModelId, ProviderKind

ENV_PREFIX = "SAGE_"
CONFIG_ENV = "SAGE_CONFIG"
DEFAULT_CONFIG = Path("~/.config/sage/config.toml").expanduser()


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV, DEFAULT_CONFIG)).expanduser()


GROUPS: dict[str, JsonValue] = {
    "Model": "What extracts and how hard it thinks. Shown on the Manage page.",
    "Budget": (
        "Limits on SAGE's own agent, and how many extractions run at once. A coding agent stops at its own time limit"
        " instead."
    ),
    "Agent": "How much each check tells the extraction agent, and what a coding agent may do on its own.",
    "Parsing": "How strictly table cells must match the PDF text.",
    "Paths": "Where SAGE keeps papers, extractions and reviews.",
}
"""Settings' sections, in the JSON schema under `groups`. Each field names one in `group`."""


def _group(name: str, **extra: JsonValue) -> dict[str, JsonValue]:
    if name not in GROUPS:
        raise KeyError(f"settings group {name!r} has no description in GROUPS")
    return {"group": name, **extra}


class YourModel(BaseModel):
    """One of the models extraction may use. A price here overrides genai-prices', and is how a model it can't price,
    such as one on a company gateway or a local server, gets a cost limit that holds."""

    model_config = ConfigDict(extra="forbid")

    id: ModelId
    name: str | None = None
    """The provider's own name for it, kept when it was added, for a model genai-prices doesn't name."""
    input_usd: float | None = Field(default=None, ge=0)
    """Your price per million input tokens."""
    output_usd: float | None = Field(default=None, ge=0)
    """Your price per million output tokens."""
    cache_read_usd: float | None = Field(default=None, ge=0)
    """Your price per million tokens read from the prompt cache. Left out: genai-prices' discount for the model, else
    the input price."""
    cache_write_usd: float | None = Field(default=None, ge=0)
    """Your price per million tokens written to the prompt cache, the same way."""
    effort: Effort = "medium"
    """How hard SAGE's own agent has it think per turn, one of the levels the model has (`ModelOption.efforts`). A
    model that doesn't think ignores it."""

    @model_validator(mode="before")
    @classmethod
    def _bare_id(cls, data: object) -> object:
        return {"id": data} if isinstance(data, str) else data

    @model_validator(mode="after")
    def _both_prices(self) -> Self:
        if (self.input_usd is None) != (self.output_usd is None):
            raise ValueError("give both prices, input and output, or neither")
        return self

    @property
    def price(self) -> tuple[float, float, float | None, float | None] | None:
        """Input, output, cache read and cache write, per million tokens; None without your input and output price."""
        if self.input_usd is None or self.output_usd is None:
            return None
        return self.input_usd, self.output_usd, self.cache_read_usd, self.cache_write_usd


class AgentConfig(BaseModel):
    """A coding agent SAGE can start in a paper's folder over the Agent Client Protocol. Its own sign-in is used:
    SAGE passes it no keys."""

    model_config = ConfigDict(extra="forbid")

    title: str
    command: str
    """The program that speaks ACP on stdio, e.g. `npx` or `gemini`."""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    """Added to the small environment the agent gets (PATH, HOME, SAGE_HOME)."""
    requires: list[str] = Field(default_factory=list)
    """Programs that must be on PATH, such as the agent's own CLI, whose sign-in it uses."""
    install: str | None = None
    """How to install it, shown when it is missing."""
    login_check: list[str] | None = None
    """A command that exits 0 when the agent is signed in."""
    login: str | None = None
    """How to sign in, shown when it isn't."""
    model: str | None = None
    """The agent's own name for the model to use, e.g. `sonnet` or `gpt-5.6-terra`. None keeps its default."""
    model_flag: str | None = None
    """For an agent that takes the model at launch rather than in the session, the flag that names it, e.g. `-m`."""
    mode: str | None = None
    """The session mode, which decides what the agent asks permission for. SAGE allows edits in the paper's folder
    and `sage` commands itself, and asks you about anything else."""
    effort: str | None = None
    """The agent's own name for how hard its model thinks, e.g. `high`, one of the levels `sage agents offer` lists
    for that model. None keeps its default. A model that lacks it runs at the agent's default, with a note in the
    run's log."""
    time_limit_minutes: int = Field(default=60, ge=1, le=600)


BUILTIN_AGENTS: dict[str, AgentConfig] = {
    "claude": AgentConfig(
        title="Claude Code",
        command="npx",
        args=["-y", "@agentclientprotocol/claude-agent-acp@0.95.0"],
        requires=["npx", "claude"],
        install="npm install -g @anthropic-ai/claude-code (needs Node 20+)",
        login_check=["claude", "auth", "status"],
        login="run `claude` once and sign in",
        mode="default",
    ),
    "codex": AgentConfig(
        title="Codex",
        command="npx",
        args=["-y", "@agentclientprotocol/codex-acp@2.2.2"],
        requires=["npx"],
        install="install Node 20+, then npm install -g @openai/codex",
        login_check=["npx", "-y", "@openai/codex@latest", "login", "status"],
        login="run `codex login`",
        mode="agent",
    ),
    "gemini": AgentConfig(
        title="Gemini CLI",
        command="gemini",
        args=["--acp"],
        requires=["gemini"],
        install="npm install -g @google/gemini-cli",
        login="run `gemini` once and sign in",
        model_flag="-m",
    ),
    "opencode": AgentConfig(
        title="OpenCode",
        command="opencode",
        args=["acp"],
        requires=["opencode"],
        install="see opencode.ai to install it",
        login="run `opencode auth login`",
        mode="build",
    ),
}
"""The agents SAGE knows how to start. config.toml's `[agents.<id>]` changes any field, and adds agents of your own."""
AGENT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_ignore_empty=True,
        extra="ignore",
        validate_default=True,
        json_schema_extra={"groups": GROUPS},
    )

    providers: dict[str, Connection] = Field(
        default_factory=dict,
        title="Providers",
        description=(
            "Where models come from, by id: the built-in anthropic, openai, google and bedrock, and any you add, such"
            " as Azure OpenAI, Vertex AI or a company gateway. Keys are stored in config.toml with mode 0600."
        ),
        json_schema_extra=_group("Model", page="manage"),
    )
    models: list[YourModel] = Field(
        default_factory=list,
        title="Models",
        description=(
            "The models extraction may use, in the order the pickers list them, each with your price when SAGE has"
            " none. The first is what Extract uses when Extract with is empty."
        ),
        json_schema_extra=_group("Model", page="manage"),
    )
    agents: dict[str, AgentConfig] = Field(
        default_factory=lambda: dict(BUILTIN_AGENTS),
        title="Agents",
        description=(
            "Coding agents SAGE can start in a paper's folder, with their own sign-in: Claude Code, Codex, Gemini CLI,"
            " OpenCode, and any other that speaks the Agent Client Protocol."
        ),
        json_schema_extra=_group("Model", page="manage"),
    )
    extract_with: str | None = Field(
        default=None,
        title="Extract with",
        description=(
            "What Extract uses unless a run picks: an agent's id, such as claude, or a model, which SAGE's own agent "
            "runs on. Empty means the first of your models."
        ),
        json_schema_extra=_group("Model", page="manage"),
    )
    max_turns: int = Field(
        default=150,
        ge=1,
        le=500,
        title="Turn budget",
        description=(
            "Turns SAGE's own agent may take per paper before code settles the run. Each file read, edit or check is "
            "a turn."
        ),
        json_schema_extra=_group("Budget"),
    )
    max_cost_usd: float = Field(
        default=1.0,
        gt=0,
        le=100,
        title="Cost budget (USD)",
        description="What SAGE's own agent may spend per paper before code settles the run.",
        json_schema_extra=_group("Budget"),
    )
    fetch_prices: bool = Field(
        default=True,
        title="Latest prices",
        description=(
            "Fetch genai-prices' newest price table every hour, so model prices and cost limits stay current.\n"
            "Off: use the table that shipped with SAGE."
        ),
        json_schema_extra=_group("Budget"),
    )
    parallel_runs: int = Field(
        default=3,
        ge=1,
        le=16,
        title="Extractions at once",
        description="How many extractions run at the same time. The rest wait as queued, and start in the order asked.",
        json_schema_extra=_group("Budget"),
    )
    error_list_cap: int = Field(
        default=12,
        ge=1,
        le=100,
        title="Errors per check",
        description="A check lists this many blocking errors and this many warnings, then a count of the rest.",
        json_schema_extra=_group("Agent"),
    )
    agents_ask: bool = Field(
        default=False,
        title="Agents ask first",
        description=(
            "Off: agents SAGE starts can do anything without asking.\n"
            "On: they can still read the paper, edit their "
            "own files and run sage extract check and done, but anything else waits for your OK."
        ),
        json_schema_extra=_group("Agent"),
    )
    cache_diagnostics: bool = Field(
        default=False,
        title="Cache diagnostics",
        description=(
            "Ask Anthropic where each turn's prompt first differed from the last, and log it with the turn's usage."
            " Free, but Anthropic rejects it for HIPAA-enabled organizations. Only Anthropic's own API has it, so the"
            " Manage page shows it under the Anthropic provider."
        ),
        json_schema_extra=_group("Model", page="manage"),
    )
    grounding_fuzzy_threshold: float = Field(
        default=0.85,
        ge=0.5,
        le=1.0,
        title="Fuzzy grounding threshold",
        description="Similarity a table cell needs to match a text span when no exact match exists.",
        json_schema_extra=_group("Parsing"),
    )
    table_min_grounded: float = Field(
        default=0.9,
        ge=0,
        le=1,
        title="Table grounding floor",
        description="Share of non-empty cells that must ground for a table to count as grounded.",
        json_schema_extra=_group("Parsing"),
    )
    page_render_dpi: int = Field(
        default=200,
        ge=72,
        le=400,
        title="Page image resolution",
        description=(
            "Dots per inch of the page, table and figure images written into each paper's folder (pages/, tables/, "
            "figures/) for agents and people to look at."
        ),
        json_schema_extra=_group("Parsing"),
    )
    home: Path = Field(
        default=Path("~/sage-data"),
        title="Data folder",
        description="Where papers, runs and reviews live. The SAGE_HOME environment variable overrides it.",
        json_schema_extra=_group("Paths"),
    )

    @classmethod
    @override
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            _ProviderEnv(settings_cls),
            TomlConfigSettingsSource(settings_cls, toml_file=config_path()),
        )

    @field_validator("agents", mode="before")
    @classmethod
    def _agents_over_builtins(cls, found: object) -> object:
        """A built-in agent in config.toml changes only the fields it names."""
        if not isinstance(found, dict):
            return found
        bad = [a for a in found if not isinstance(a, str) or AGENT_ID.fullmatch(a) is None]
        if bad:
            raise ValueError(f"agent ids are lowercase letters, digits and '-': {', '.join(map(str, bad))}")
        merged: dict[str, object] = {a: c.model_dump() for a, c in BUILTIN_AGENTS.items()}
        for agent, value in found.items():
            base = merged.get(agent)
            given = value.model_dump() if isinstance(value, AgentConfig) else value
            merged[agent] = base | given if isinstance(base, dict) and isinstance(given, dict) else given
        return merged

    @field_validator("providers", mode="after")
    @classmethod
    def _with_builtins(cls, found: dict[str, Connection]) -> dict[str, Connection]:
        """Every install has the built-in providers, first and as their own kind."""
        for provider, kind in BUILTIN.items():
            if provider in found and found[provider].kind != kind:
                raise ValueError(f"{provider} is a built-in provider of kind {kind}")
        bad = [p for p in found if PROVIDER_ID.fullmatch(p) is None]
        if bad:
            raise ValueError(f"provider ids are lowercase letters, digits and '-': {', '.join(bad)}")
        builtins = {
            p: found.get(p) or Connection(kind=k, region=KINDS[k].default("region")) for p, k in BUILTIN.items()
        }
        return builtins | {p: c for p, c in found.items() if p not in BUILTIN}

    @property
    def home_dir(self) -> Path:
        return self.home.expanduser().resolve()

    def model(self, model_id: str) -> YourModel | None:
        return next((m for m in self.models if m.id == model_id), None)

    def default_using(self) -> str | None:
        """What Extract uses unless a run picks: `extract_with`, else the first of your models."""
        return self.extract_with or (self.models[0].id if self.models else None)


class StoredSettings(Settings):
    """Settings from the values given alone, as config.toml would hold them. Settings reads the environment and the
    current file too, so a bad value there would block the write that fixes it."""

    @classmethod
    @override
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def locked_fields() -> dict[str, str]:
    """Fields set through the environment, mapped to the variable that sets them."""
    envs = {name: f"{ENV_PREFIX}{name.upper()}" for name in Settings.model_fields}
    return {name: env for name, env in envs.items() if os.environ.get(env, "")}


@dataclass(frozen=True)
class KindField:
    """One field of a provider kind, as the Manage page and `sage providers` show it."""

    name: str
    title: str
    help: str = ""
    secret: bool = False
    placeholder: str = ""
    default: str | None = None


@dataclass(frozen=True)
class Kind:
    title: str
    help: str
    fields: tuple[KindField, ...]
    needs: tuple[tuple[str, ...], ...]
    """What it can't run without: each group is fields of which at least one must be set. A field in no group is
    optional."""

    def default(self, field: str) -> str | None:
        return next((f.default for f in self.fields if f.name == field), None)

    def group(self, field: str) -> tuple[str, ...] | None:
        """The fields that would do instead of `field`, with it, or None for an optional field."""
        return next((g for g in self.needs if field in g), None)


_KEY = KindField("api_key", "API key", secret=True)

KINDS: dict[ProviderKind, Kind] = {
    "anthropic": Kind(
        "Anthropic",
        "Claude models from Anthropic, or from a gateway that speaks the Anthropic API.",
        (
            _KEY,
            KindField(
                "base_url",
                "Endpoint",
                "Leave empty for Anthropic's own.",
                placeholder="https://api.anthropic.com",
            ),
        ),
        (("api_key",),),
    ),
    "openai": Kind(
        "OpenAI or compatible",
        "OpenAI, or any server with an OpenAI-compatible API: a company gateway, LiteLLM, OpenRouter, vLLM, Ollama.",
        (
            KindField("api_key", "API key", "A local server may not need one.", secret=True),
            KindField(
                "base_url",
                "Endpoint",
                "Leave empty for OpenAI's own.",
                placeholder="https://api.openai.com/v1",
            ),
        ),
        (("api_key", "base_url"),),
    ),
    "azure": Kind(
        "Azure OpenAI",
        "OpenAI models deployed in your Azure resource, billed to your Azure subscription. Models go by"
        " deployment name.",
        (
            KindField("base_url", "Endpoint", placeholder="https://NAME.openai.azure.com"),
            _KEY,
            KindField("api_version", "API version", default="2024-10-21"),
        ),
        (("base_url",), ("api_key",)),
    ),
    "google": Kind(
        "Google Gemini",
        "Gemini models through the Gemini API, with a key from Google AI Studio.",
        (_KEY,),
        (("api_key",),),
    ),
    "vertex": Kind(
        "Google Cloud Vertex AI",
        "Gemini and Claude on Vertex AI, billed to your Google Cloud project. It signs in with Application Default"
        " Credentials (`gcloud auth application-default login`), or with a service account key file.",
        (
            KindField("project", "Project ID", placeholder="my-project"),
            KindField(
                "region", "Location", "Where the models run. Claude needs a region that offers it.", default="global"
            ),
            KindField(
                "credentials_file",
                "Service account key file",
                "Leave empty for Application Default Credentials.",
                placeholder="/path/to/key.json",
            ),
        ),
        (("project",),),
    ),
    "bedrock": Kind(
        "Amazon Bedrock",
        "Models on Amazon Bedrock, billed to your AWS account. It signs in with a Bedrock API key, or with a profile"
        " from ~/.aws such as an SSO login.",
        (
            KindField("region", "Region", "The model has to be enabled in it.", default="us-east-1"),
            KindField("api_key", "API key", "A Bedrock API key.", secret=True),
            KindField(
                "aws_profile",
                "AWS profile",
                "A profile in ~/.aws, such as an SSO login.",
                placeholder="default",
            ),
            KindField(
                "base_url",
                "Endpoint",
                "A VPC endpoint for Bedrock Runtime. Leave empty for AWS's own.",
                placeholder="https://bedrock-runtime.REGION.amazonaws.com",
            ),
        ),
        (("region",), ("api_key", "aws_profile")),
    ),
}


def unmet(c: Connection) -> list[tuple[str, ...]]:
    """The groups of fields in `needs` with none set, by field name. Empty when the provider is ready."""
    has = {name: bool(value.get_secret_value() if hasattr(value, "get_secret_value") else value) for name, value in c}
    return [group for group in KINDS[c.kind].needs if not any(has.get(n) for n in group)]


def missing(c: Connection) -> list[str]:
    """What the provider still needs before it can run a model, by field title. Empty when it's ready."""
    titles = {f.name: f.title for f in KINDS[c.kind].fields}
    return [" or ".join(titles[n] for n in group) for group in unmet(c)]


PROVIDER_ENV: dict[tuple[str, str], tuple[str, ...]] = {
    ("anthropic", "api_key"): ("SAGE_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    ("openai", "api_key"): ("SAGE_OPENAI_API_KEY", "OPENAI_API_KEY"),
    ("google", "api_key"): ("SAGE_GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
    ("bedrock", "api_key"): ("SAGE_BEDROCK_API_KEY", "AWS_BEARER_TOKEN_BEDROCK"),
    ("bedrock", "region"): ("SAGE_BEDROCK_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"),
}
"""The environment variables that set a built-in provider's field, first match wins. They lock the field."""


def stored_with(stored: dict[str, object], changes: StoredSettings, cleared: Iterable[str]) -> dict[str, object]:
    """config.toml's values after a change: the `cleared` keys gone, so their defaults apply again, and each field
    `changes` was given as config.toml holds it. The rest stays as it was."""
    gone = set(cleared)
    out = {k: v for k, v in stored.items() if k not in gone}
    dumped = changes.model_dump(mode="json")
    for key in changes.model_fields_set:
        out[key] = dumped[key]
    if "agents" in changes.model_fields_set:
        out["agents"] = _agent_overrides(changes.agents)
        if not out["agents"]:
            out.pop("agents")
    return out


def _agent_overrides(agents: dict[str, AgentConfig]) -> dict[str, object]:
    """What config.toml keeps of the agents: a built-in agent's changed fields alone, so it picks up SAGE's own updates
    to the rest, such as a newer adapter, and every set field of an agent you added. TOML has no null, so an unset
    field is left out."""
    kept: dict[str, object] = {}
    for agent, config in agents.items():
        mine = config.model_dump(mode="json", exclude_none=True)
        builtin = BUILTIN_AGENTS.get(agent)
        if builtin is not None:
            base = builtin.model_dump(mode="json")
            mine = {k: v for k, v in mine.items() if base.get(k) != v}
        if mine or builtin is None:
            kept[agent] = mine
    return kept


def provider_locked() -> dict[str, dict[str, str]]:
    """Built-in providers' fields set through the environment, mapped to the variable that sets each."""
    locked: dict[str, dict[str, str]] = {}
    for (provider, field), names in PROVIDER_ENV.items():
        env = next((n for n in names if os.environ.get(n, "")), None)
        if env is not None:
            locked.setdefault(provider, {})[field] = env
    return locked


class _ProviderEnv(PydanticBaseSettingsSource):
    """The environment variables in `PROVIDER_ENV`, as fields of the built-in providers' tables."""

    @override
    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[object, str, bool]:
        return None, field_name, False

    @override
    def __call__(self) -> dict[str, object]:
        found: dict[str, dict[str, str]] = {}
        for provider, fields in provider_locked().items():
            for field, env in fields.items():
                found.setdefault(provider, {"kind": BUILTIN[provider]})[field] = os.environ[env]
        return {"providers": found} if found else {}
