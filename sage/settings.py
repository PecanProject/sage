"""The one Settings model. Precedence: environment, then config.toml, then defaults."""

import os
from collections.abc import Sequence
from pathlib import Path
from typing import override

from pydantic import Field, JsonValue, SecretStr
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from sage.events import Effort, ModelId

ENV_PREFIX = "SAGE_"
CONFIG_ENV = "SAGE_CONFIG"
DEFAULT_CONFIG = Path("~/.config/sage/config.toml").expanduser()

MODEL_CHOICES = [
    "claude-haiku-5-5",
    "claude-sonnet-5-5",
    "claude-opus-5-5",
    "gpt-5.1",
    "gemini-3.5-flash",
    "bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0",
]
"""Suggestions, not a closed list: any model name pydantic-ai knows works, and so does any `provider:name`."""


def key_field(provider: str) -> str:
    """The field a provider's key lives in. Every provider in `PROVIDERS` has one, by this name."""
    return f"{provider}_api_key"


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV, DEFAULT_CONFIG)).expanduser()


GROUPS: dict[str, JsonValue] = {
    "Model": "Which models read the paper and its tables, how hard they think, and the keys they need.",
    "Budget": "Limits that stop an extraction before it costs too much.",
    "Agent": "How the extraction agent handles repeated mistakes.",
    "Prompt cache": "How to see where a turn missed the prompt cache.",
    "Parsing": "How strictly table cells must match the PDF text.",
    "Paths": "Where SAGE keeps papers, extractions and reviews.",
}
"""Settings' sections, in the JSON schema under `groups`. Each field names one in `group`."""


def _group(name: str, **extra: JsonValue) -> dict[str, JsonValue]:
    if name not in GROUPS:
        raise KeyError(f"settings group {name!r} has no description in GROUPS")
    return {"group": name, **extra}


def _json_list(items: Sequence[str]) -> list[JsonValue]:
    return [*items]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_ignore_empty=True,
        extra="ignore",
        validate_default=True,
        json_schema_extra={"groups": GROUPS},
    )

    anthropic_api_key: SecretStr | None = Field(
        default=None,
        title="Anthropic API key",
        description="Used for a Claude model. Stored in config.toml with mode 0600.",
        json_schema_extra=_group("Model", secret=True, env=_json_list(["SAGE_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"])),
    )
    openai_api_key: SecretStr | None = Field(
        default=None,
        title="OpenAI API key",
        description="Used for a GPT or o-series model. Stored in config.toml with mode 0600.",
        json_schema_extra=_group("Model", secret=True, env=_json_list(["SAGE_OPENAI_API_KEY", "OPENAI_API_KEY"])),
    )
    google_api_key: SecretStr | None = Field(
        default=None,
        title="Google API key",
        description="Used for a Gemini model. Stored in config.toml with mode 0600.",
        json_schema_extra=_group("Model", secret=True, env=_json_list(["SAGE_GOOGLE_API_KEY", "GOOGLE_API_KEY"])),
    )
    bedrock_api_key: SecretStr | None = Field(
        default=None,
        title="Bedrock API key",
        description=(
            "Used for a `bedrock:` model. An Amazon Bedrock API key (a bearer token from the AWS console), not an"
            " access key pair. Stored in config.toml with mode 0600."
        ),
        json_schema_extra=_group(
            "Model", secret=True, env=_json_list(["SAGE_BEDROCK_API_KEY", "AWS_BEARER_TOKEN_BEDROCK"])
        ),
    )
    bedrock_region: str = Field(
        default="us-east-1",
        min_length=1,
        title="Bedrock region",
        description="The AWS region a `bedrock:` model is called in. The model has to be enabled in it.",
        json_schema_extra=_group("Model", env=_json_list(["SAGE_BEDROCK_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"])),
    )
    extraction_model: ModelId = Field(
        default="claude-haiku-5-5",
        title="Extraction model",
        description="The model that reads the paper and writes records.",
        json_schema_extra=_group("Model", choices=_json_list(MODEL_CHOICES)),
    )
    table_model: ModelId = Field(
        default="claude-haiku-5-5",
        title="Table model",
        description="The model that transcribes table images into cell grids.",
        json_schema_extra=_group("Model", choices=_json_list(MODEL_CHOICES)),
    )
    effort: Effort = Field(
        default="medium",
        title="Extraction effort",
        description="How hard the extraction model thinks per turn. Higher costs more.",
        json_schema_extra=_group("Model"),
    )
    table_effort: Effort = Field(
        default="low",
        title="Table effort",
        description="Effort for the table transcription pass.",
        json_schema_extra=_group("Model"),
    )
    max_turns: int = Field(
        default=40,
        ge=1,
        le=500,
        title="Turn budget",
        description="Model turns per paper before code settles the run.",
        json_schema_extra=_group("Budget"),
    )
    max_cost_usd: float = Field(
        default=1.0,
        gt=0,
        le=100,
        title="Cost budget (USD)",
        description="Spend per paper before code settles the run. The table pass counts toward it.",
        json_schema_extra=_group("Budget"),
    )
    max_concurrency: int = Field(
        default=4,
        ge=1,
        le=32,
        title="Max concurrency",
        description="Parallel model calls in the table pass.",
        json_schema_extra=_group("Budget"),
    )
    batch_jobs: int = Field(
        default=2,
        ge=1,
        le=16,
        title="Batch jobs",
        description="Papers `sage run-all` extracts at once. Each one is a separate worker with its own budget.",
        json_schema_extra=_group("Budget"),
    )
    same_error_limit: int = Field(
        default=3,
        ge=1,
        le=10,
        title="Same-error limit",
        description="After the same error on a field this many times, the field becomes unresolved.",
        json_schema_extra=_group("Agent"),
    )
    error_list_cap: int = Field(
        default=12,
        ge=1,
        le=100,
        title="Errors per tool result",
        description="Tool results list this many errors, then a count of the rest.",
        json_schema_extra=_group("Agent"),
    )
    cache_diagnostics: bool = Field(
        default=False,
        title="Cache diagnostics",
        description=(
            "Ask Anthropic where each turn's prompt first differed from the last, and log it with the turn's usage."
            " Free, but Anthropic rejects it for HIPAA-enabled organizations."
        ),
        json_schema_extra=_group("Prompt cache"),
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
    table_render_scale: float = Field(
        default=2.0,
        ge=1,
        le=4,
        title="Table render scale",
        description=(
            "Render scale of the page crops sent to the table model and of the images the extraction model asks for "
            "(1 = 72 dpi)."
        ),
        json_schema_extra=_group("Parsing"),
    )
    home: Path = Field(
        default=Path("~/sage-data"),
        title="Data folder",
        description="Where papers, runs and reviews live. Env SAGE_HOME.",
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
            _FallbackEnv(settings_cls),
            TomlConfigSettingsSource(settings_cls, toml_file=config_path()),
        )

    @property
    def home_dir(self) -> Path:
        return self.home.expanduser().resolve()

    def key(self, provider: str) -> str | None:
        """A provider's API key, if one is set. One entry per name in `PROVIDERS`, spelled out so a provider added
        there without a field here reads as missing rather than raising."""
        fields = {
            "anthropic": self.anthropic_api_key,
            "openai": self.openai_api_key,
            "google": self.google_api_key,
            "bedrock": self.bedrock_api_key,
        }
        secret = fields.get(provider)
        if secret is None or not secret.get_secret_value().strip():
            return None
        return secret.get_secret_value()


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


def env_names(name: str, info: FieldInfo) -> list[str]:
    extra = info.json_schema_extra
    listed = extra.get("env") if isinstance(extra, dict) else None
    if isinstance(listed, list):
        return [str(n) for n in listed]
    return [f"{ENV_PREFIX}{name.upper()}"]


def locked_fields() -> dict[str, str]:
    """Fields set through the environment, mapped to the variable that sets them."""
    locked: dict[str, str] = {}
    for name, info in Settings.model_fields.items():
        for env in env_names(name, info):
            if os.environ.get(env, ""):
                locked[name] = env
                break
    return locked


class _FallbackEnv(PydanticBaseSettingsSource):
    """Env names a field lists beyond the SAGE_ prefix, such as ANTHROPIC_API_KEY."""

    @override
    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[object, str, bool]:
        for env in env_names(field_name, field)[1:]:
            if value := os.environ.get(env, ""):
                return value, field_name, False
        return None, field_name, False

    @override
    def __call__(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for name, info in self.settings_cls.model_fields.items():
            value, _, _ = self.get_field_value(info, name)
            if value is not None:
                out[name] = value
        return out
