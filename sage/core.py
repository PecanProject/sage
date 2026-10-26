"""Every user action. `cli.py` and `api.py` call only this module, so the CLI and the UI stay equal."""

import asyncio
import errno
import os
import socket
from collections.abc import AsyncIterator, Generator, Iterable, Mapping
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, JsonValue, SecretStr, TypeAdapter, ValidationError

from sage.agents import acp_client
from sage.agents.models import Price, Route, entry, keep_prices_fresh, list_models, resolve, runs_at
from sage.events import (
    BUILTIN,
    JSON,  # noqa: F401 - cli.py reads a typed value with it
    PROVIDER_ID,
    STAGE_STEP,  # noqa: F401 - cli.py words the live stage with it
    Connection,
    Driver,
    Effort,
    Event,
    EventAdapter,
    ModelId,
    ProviderKind,
    Stage,
    StageStarted,
    describe,  # noqa: F401 - cli.py prints events with it
    driven_by,  # noqa: F401 - cli.py words a run's driver with it
    now,
    stopped_at,  # noqa: F401 - cli.py words a budget stop with it
)
from sage.ir.settled import Settled
from sage.parse import pdf
from sage.parse.document import Document
from sage.pipeline import agent_run, extraction, paper_files, run_state, runner
from sage.pipeline.export import csv_zip, workbook, xlsx
from sage.pipeline.extraction import CheckReport, DoneReport
from sage.pipeline.quotes import cites, quote_lines
from sage.pipeline.review import QueueItem, labels, queue, standing
from sage.settings import (
    KINDS,
    PROVIDER_ENV,
    AgentConfig,
    Kind,
    KindField,
    Settings,
    StoredSettings,
    YourModel,
    config_path,
    locked_fields,
    missing,
    provider_locked,
    stored_with,
    unmet,
)
from sage.store import files, proc, skills, watch
from sage.store.models import (
    PaperInfo,
    Review,
    ReviewAction,  # noqa: F401 - cli.py types the review action with it
    ReviewAdapter,
    RunInfo,
    UiInfo,
)
from sage.store.paths import ID, ChangeKind, Home

ModelIdAdapter: TypeAdapter[ModelId] = TypeAdapter(ModelId)

ErrorKind = Literal["not_found", "invalid", "conflict", "missing_key", "corrupt"]
"""corrupt: a file under SAGE_HOME or config.toml exists but won't parse."""


class ErrorBody(BaseModel):
    """How an error leaves either surface: the API's error response, and the CLI's under --json."""

    kind: ErrorKind
    message: str


class SageError(Exception):
    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def settings() -> Settings:
    """Read fresh on every call, so a change made in the UI takes effect without a restart."""
    _stored()  # through the store first, so a config.toml that is binary, unreadable or a folder fails as corrupt
    try:
        return Settings()
    except ValidationError as e:
        raise SageError("invalid", f"invalid setting: {_validation_message(e, locked_fields())}") from e


def listen(host: str, port: int) -> socket.socket:
    """A socket bound for the UI, so a port in use or a bad host fails before anything says it is serving."""
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as e:
        sock.close()
        kind: ErrorKind = "conflict" if e.errno == errno.EADDRINUSE else "invalid"
        raise SageError(kind, f"can't serve on {host}:{port}: {e.strerror or e}") from e
    return sock


def claim_ui() -> int:
    """Take the data folder's UI lock, which `sage ui` holds for as long as it serves. A second `sage ui` on the
    same data, on any port, is refused with where the first one is. Returns the descriptor holding the lock."""
    h = home()
    fd = files.lock(h.ui_lock)
    if fd is None:
        found = _read(h.ui_info, UiInfo)
        where = f" at {found.url} (pid {found.pid})" if found is not None else ""
        raise SageError("conflict", f"SAGE is already open for {h.root}{where}; use that one, or stop it first")
    return fd


def announce_ui(url: str) -> None:
    """Say where the UI is, and start a runner for runs left queued when the last one stopped, by a crash or a
    restart, so opening SAGE carries them on."""
    h = home()
    files.write_model(h.ui_info, UiInfo(url=url, pid=os.getpid()))
    if runner.queued(h):
        runner.wake(h)


def release_ui(fd: int) -> None:
    files.remove(home().ui_info)
    files.unlock(fd)


def home() -> Home:
    s = settings()
    h = Home(s.home_dir)
    _refreshed(h.root, s.page_render_dpi)
    return h


@cache
def _refreshed(root: Path, dpi: int) -> None:
    """Render what each paper is missing, once per process."""
    del dpi  # part of the cache key: a new render scale renders again
    paper_files.refresh(Home(root), settings())


def _checked_id(what: Literal["paper", "run"], value: str) -> str:
    if ID.fullmatch(value) is None:
        raise SageError(
            "invalid",
            f"{value[:80]!r} is not a {what} id; ids are at most 128 letters, digits, '.', '_' and '-', and start with "
            "a letter or digit",
        )
    return value


@contextmanager
def _on_disk() -> Generator[None]:
    """A file that exists but won't parse is an error that names it. It never reads as missing."""
    try:
        yield
    except files.UnreadableError as e:
        raise SageError("corrupt", f"{e}. Fix or remove the file.") from e
    except run_state.MissingDocumentError as e:
        raise SageError("not_found", str(e)) from e


def _read[M: BaseModel](path: Path, cls: type[M]) -> M | None:
    with _on_disk():
        return files.read_model_or_none(path, cls)


# Settings ---------------------------------------------------------------------------------------------------------

MASK = "••••"

SettingSource = Literal["env", "file", "default"]


class SettingsView(BaseModel):
    values: dict[str, JsonValue]
    sources: dict[str, SettingSource]
    locked: dict[str, str]
    """Field name -> the environment variable that sets it. Locked fields can't be edited here."""
    json_schema: dict[str, JsonValue]
    config_path: str


def settings_view() -> SettingsView:
    current = settings()
    file_values = _stored()
    locked = locked_fields()
    values = current.model_dump(mode="json")
    sources: dict[str, SettingSource] = {
        name: "env" if name in locked else "file" if name in file_values else "default" for name in values
    }
    return SettingsView(
        values=values,
        sources=sources,
        locked=locked,
        json_schema=Settings.model_json_schema(),
        config_path=str(config_path()),
    )


def update_settings(changes: dict[str, JsonValue]) -> SettingsView:
    """A null or empty value removes the key, so the default applies again. Only the values being set are checked,
    and the rest of the file is written back as it was, so a bad value elsewhere, in the file or the environment, never
    blocks a change."""
    unknown = sorted(set(changes) - set(Settings.model_fields))
    if unknown:
        raise SageError("invalid", f"unknown setting(s): {', '.join(unknown)}")
    managed = sorted(set(changes) & {"providers", "models"})
    if managed:
        where = f"change them on the Manage page, or with `sage {managed[0]}`"
        raise SageError("invalid", f"{', '.join(managed)}: {where}")
    _refuse_locked(changes, locked_fields())
    stored = _stored()
    setting = {k: v for k, v in changes.items() if v is not None and v != ""}
    chosen = setting.get("extract_with")
    if isinstance(chosen, str):
        current = settings()
        if chosen.partition(":")[0] not in current.agents:
            setting["extract_with"] = _usable(current, chosen)
    try:
        validated = StoredSettings.model_validate(setting)
    except ValidationError as e:
        raise SageError("invalid", _validation_message(e)) from e
    files.write_toml(config_path(), stored_with(stored, validated, changes.keys() - setting.keys()))
    try:
        return settings_view()
    except SageError as e:
        raise SageError(e.kind, f"saved, but {e.message}") from e


def _stored() -> dict[str, object]:
    """config.toml as it is on disk."""
    with _on_disk():
        return files.read_toml(config_path())


def _refuse_locked(keys: Iterable[str], locked: Mapping[str, str]) -> None:
    """Refuse to change what an environment variable sets, naming the variable."""
    blocked = sorted(k for k in keys if k in locked)
    if blocked:
        raise SageError("conflict", "set through the environment: " + ", ".join(f"{k} ({locked[k]})" for k in blocked))


def config_get(key: str) -> JsonValue:
    view = settings_view()
    if key not in view.values:
        raise SageError("not_found", f"no setting named {key}; see `sage config list`")
    return view.values[key]


def route(s: Settings, model_id: str) -> Route:
    """How to reach the model, so a run fails in the caller's terminal rather than in the runner when its provider
    lacks a key or a field."""
    found = _route(s, model_id)
    if unmet(found.connection):
        provider = model_id.partition(":")[0]
        raise SageError("missing_key", _needs(provider, found.connection, model_id))
    return found


def _needs(provider: str, c: Connection, needed_by: str) -> str:
    """Anthropic has no API key set, which … needs. Then where to set it: the Manage page, `sage providers set`,
    and for a built-in provider its environment variables."""
    gaps = missing(c)
    first = unmet(c)[0][0]
    envs = PROVIDER_ENV.get((provider, first), ())
    env = f" (or env {' or '.join(envs)})" if envs else ""
    lacks = " and no ".join(" ".join(w if w.isupper() else w.lower() for w in g.split()) for g in gaps)
    return (
        f"{_provider_title(provider, c)} has no {lacks} set, which {needed_by} needs. "
        f"Set it on the Manage page in `sage ui`, or run `sage providers set {provider} {first}`{env}."
    )


def _connection(s: Settings, provider: str) -> Connection:
    c = s.providers.get(provider)
    if c is None:
        raise SageError("not_found", f"no provider {provider}; see `sage providers`")
    return c


def _provider(s: Settings, model_id: str) -> str:
    """The provider a canonical id names. It has to be one of yours."""
    provider = model_id.partition(":")[0]
    if provider not in s.providers:
        raise SageError(
            "invalid", f"{model_id}: no provider {provider!r}; add it on the Manage page, or `sage providers add`"
        )
    return provider


BUILTIN_TITLES = {"anthropic": "Anthropic", "openai": "OpenAI", "google": "Google Gemini", "bedrock": "Amazon Bedrock"}


def _provider_title(provider: str, c: Connection) -> str:
    return c.name or BUILTIN_TITLES.get(provider) or provider


def _mask(secret: SecretStr) -> str | None:
    value = secret.get_secret_value()
    if not value:
        return None
    return MASK + (value[-4:] if len(value) >= 16 else "")


def _validation_message(e: ValidationError, locked: dict[str, str] | None = None) -> str:
    """Each problem by field. A field the environment sets names its variable, and one from the file names the file."""
    where = locked or {}

    def source(field: str) -> str:
        return f" (from {where[field]})" if field in where else f" (in {config_path()})" if locked is not None else ""

    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc'])}{source(str(err['loc'][0]) if err['loc'] else '')}: {err['msg']}"
        for err in e.errors()
    )


# Models -----------------------------------------------------------------------------------------------------------


class KindFieldInfo(BaseModel):
    name: str
    title: str
    help: str
    secret: bool
    placeholder: str
    default: str | None
    required: bool
    """The provider can't run without it, or without one of `instead`."""
    instead: list[str]
    """The titles of the fields that would do in its place."""


class KindInfo(BaseModel):
    """A kind of provider someone can add, and the fields it takes."""

    kind: ProviderKind
    title: str
    help: str
    fields: list[KindFieldInfo]


class ProviderInfo(BaseModel):
    id: str
    kind: ProviderKind
    title: str
    builtin: bool
    values: dict[str, str | None]
    """Each field its kind takes, a key masked. Unset fields are null."""
    locked: dict[str, str]
    """Field -> the environment variable that sets it. Locked fields can't be edited here."""
    missing: list[str]
    """What it still needs before it can run a model, by field title. Empty when it's ready."""


class ModelOption(BaseModel):
    """One of your models, with what it costs and how hard it can think."""

    id: str
    provider: str
    title: str
    """Its name: genai-prices', else the provider's own from when it was added, else its id's."""
    input_usd: float
    """Per million input tokens, before any cache discount."""
    output_usd: float
    """Per million output tokens."""
    cache_read_usd: float
    """Per million tokens read from the prompt cache."""
    cache_write_usd: float
    """Per million tokens written to the prompt cache."""
    your_price: bool
    """The price is the one you gave, not genai-prices'."""
    efforts: list[Effort]
    """The effort levels it tells apart. Empty for a model that doesn't think, where effort changes nothing."""
    effort: Effort | None
    """The level SAGE's own agent has it think at, one of `efforts`; None for a model that doesn't think."""


class Candidate(BaseModel):
    """A model a provider offers, as SAGE sees it before it is added."""

    id: str
    title: str
    input_usd: float | None
    """None when SAGE has no price for it, so it needs yours before a run can use it."""
    output_usd: float | None
    tools: bool
    """False for a model that can't call tools, which SAGE can't extract with."""
    yours: bool


class Available(BaseModel):
    """What a provider said when asked which models it lets these credentials use, which is also their test."""

    provider: str
    ok: bool
    message: str
    models: list[Candidate]


def _field_info(k: Kind, f: KindField) -> KindFieldInfo:
    group = k.group(f.name)
    titles = {x.name: x.title for x in k.fields}
    return KindFieldInfo(
        name=f.name,
        title=f.title,
        help=f.help,
        secret=f.secret,
        placeholder=f.placeholder,
        default=f.default,
        required=group is not None,
        instead=[titles[n] for n in group or () if n != f.name],
    )


def provider_kinds() -> list[KindInfo]:
    return [
        KindInfo(
            kind=kind,
            title=k.title,
            help=k.help,
            fields=[_field_info(k, f) for f in k.fields],
        )
        for kind, k in KINDS.items()
    ]


def providers() -> list[ProviderInfo]:
    """Every provider: the built-in ones first, then the ones added, in the order they were."""
    s = settings()
    locked = provider_locked()
    return [_provider_info(p, c, locked.get(p, {})) for p, c in s.providers.items()]


def _provider_info(provider: str, c: Connection, locked: dict[str, str]) -> ProviderInfo:
    values: dict[str, str | None] = {}
    for f in KINDS[c.kind].fields:
        raw = getattr(c, f.name)
        values[f.name] = _mask(raw) if isinstance(raw, SecretStr) else raw
    return ProviderInfo(
        id=provider,
        kind=c.kind,
        title=_provider_title(provider, c),
        builtin=provider in BUILTIN,
        values=values,
        locked=locked,
        missing=missing(c),
    )


def save_provider(provider: str, values: Mapping[str, str | None], *, kind: str | None = None) -> ProviderInfo:
    """Add a provider, given its kind, or change one, given none. A key sent back masked stays as it is, and an empty
    value clears its field."""
    if PROVIDER_ID.fullmatch(provider) is None:
        raise SageError("invalid", f"{provider!r}: a provider id is up to 32 lowercase letters, digits and '-'")
    s = settings()
    current = s.providers.get(provider)
    chosen = current.kind if current is not None else kind
    if chosen is None or chosen not in KINDS:
        raise SageError("invalid", f"a new provider needs its kind: {', '.join(KINDS)}")
    if kind is not None and current is not None:
        raise SageError("conflict", f"there is already a provider {provider}; pick another id")
    fields = {f.name for f in KINDS[chosen].fields} | {"name"}
    unknown = sorted(set(values) - fields)
    if unknown:
        raise SageError(
            "invalid", f"a {chosen} provider has no {', '.join(unknown)}; it takes {', '.join(sorted(fields))}"
        )
    _refuse_locked(values, provider_locked().get(provider, {}))
    stored = _stored()
    tables = stored.setdefault("providers", {})
    if not isinstance(tables, dict):
        raise SageError("corrupt", f"providers in {config_path()} isn't a table")
    new = provider not in tables
    table = dict(tables.get(provider, {})) | {"kind": chosen}
    for name, value in values.items():
        text = (value or "").strip()
        if text.startswith(MASK):
            continue
        if text:
            table[name] = text
        else:
            table.pop(name, None)
    if new:
        for f in KINDS[chosen].fields:
            if f.default is not None:
                table.setdefault(f.name, f.default)
    try:
        Connection.model_validate(table)
    except ValidationError as e:
        raise SageError("invalid", _validation_message(e)) from e
    tables[provider] = table
    files.write_toml(config_path(), stored)
    return next(p for p in providers() if p.id == provider)


def remove_provider(provider: str) -> list[ProviderInfo]:
    """Remove a provider someone added. A built-in one stays, and one your models use stays until they go."""
    s = settings()
    if provider in BUILTIN:
        raise SageError("invalid", f"{provider} is built in; clear its fields instead")
    _connection(s, provider)
    using = [m.id for m in s.models if m.id.partition(":")[0] == provider]
    if using:
        verb = "uses" if len(using) == 1 else "use"
        raise SageError("conflict", f"{', '.join(using)} {verb} {provider}; remove them first")
    stored = _stored()
    tables = stored.get("providers")
    if isinstance(tables, dict):
        tables.pop(provider, None)
    files.write_toml(config_path(), stored)
    return providers()


def available_models(provider: str) -> Available:
    """Ask the provider which models its credentials can use. It costs nothing, and tests them."""
    s = settings()
    c = _connection(s, provider)
    gaps = missing(c)
    if gaps:
        return Available(provider=provider, ok=False, message=f"needs {' and '.join(gaps)}", models=[])
    found = list_models(c)
    yours = {m.id for m in s.models}
    return Available(
        provider=provider,
        ok=found.ok,
        message=found.message,
        models=[_candidate(s, f"{provider}:{m.name}", m.title, yours) for m in found.models],
    )


def describe_model(value: str) -> Candidate:
    """Any model as a person wrote it, as SAGE sees it: its name, price and whether it can call tools."""
    s = settings()
    return _candidate(s, _canonical(value), None, {m.id for m in s.models})


def _candidate(s: Settings, model_id: str, listed: str | None, yours: set[str]) -> Candidate:
    found = entry(_route(s, model_id))
    return Candidate(
        id=model_id,
        title=_title(found.title, listed, model_id),
        input_usd=found.input_usd,
        output_usd=found.output_usd,
        tools=found.tools,
        yours=model_id in yours,
    )


def _title(priced: str | None, listed: str | None, model_id: str) -> str:
    return priced or listed or model_id.partition(":")[2]


def _route(s: Settings, model_id: str) -> Route:
    yours = s.model(model_id)
    price = yours.price if yours is not None else None
    return Route(model_id, s.providers[_provider(s, model_id)], Price(*price) if price is not None else None)


def models() -> list[ModelOption]:
    """Your models, the ones extraction may use, in their order. One that has lost its price, after an update to
    genai-prices or a change of provider, is left out until it has one again."""
    s = settings()
    return [option for m in s.models if (option := _option(s, m)) is not None]


def _option(s: Settings, m: YourModel) -> ModelOption | None:
    if m.id.partition(":")[0] not in s.providers:
        return None
    found = entry(_route(s, m.id))
    if found.input_usd is None or found.output_usd is None:
        return None
    return ModelOption(
        id=m.id,
        provider=m.id.partition(":")[0],
        title=_title(found.title, m.name, m.id),
        input_usd=found.input_usd,
        output_usd=found.output_usd,
        cache_read_usd=found.cache_read_usd if found.cache_read_usd is not None else found.input_usd,
        cache_write_usd=found.cache_write_usd if found.cache_write_usd is not None else found.input_usd,
        your_price=m.price is not None,
        efforts=list(found.efforts),
        effort=runs_at(found.efforts, m.effort),
    )


def add_model(
    value: str,
    *,
    title: str | None = None,
    price: tuple[float, float] | None = None,
    cache_price: tuple[float, float] | None = None,
) -> list[ModelOption]:
    """Add a model to yours, or change its price: input and output, and optionally cache reads and writes. One SAGE
    has no price for needs yours, since pydantic-ai can't hold a run to its cost limit without one, and one that can't
    call tools is refused, since the extraction agent works only through them. With no price, one already given
    stays."""
    s = settings()
    model_id = _canonical(value)
    before = s.model(model_id)
    full = None if price is None else (*price, *(cache_price or (None, None)))
    if before is not None and price is None:
        full = before.price
    if full is not None and min(v for v in full if v is not None) < 0:
        raise SageError("invalid", "a price can't be negative")
    if price is None and cache_price is not None:
        raise SageError("invalid", "give the input and output price with the cache prices")
    found = entry(Route(model_id, s.providers[_provider(s, model_id)], Price(*full) if full is not None else None))
    if not found.tools:
        raise SageError("invalid", f"{model_id} can't call tools, and SAGE extracts only through them")
    if found.input_usd is None:
        raise SageError(
            "invalid",
            f"SAGE has no price for {model_id}; give yours, on the Manage page or with "
            f"`sage models add {model_id} --price IN OUT`, so a run can stop at its cost limit",
        )
    added = YourModel(
        id=model_id,
        name=title if found.title is None else None,
        input_usd=None if full is None else full[0],
        output_usd=None if full is None else full[1],
        cache_read_usd=None if full is None else full[2],
        cache_write_usd=None if full is None else full[3],
        effort=before.effort if before is not None else YourModel.model_fields["effort"].default,
    )
    listed = [added if m.id == model_id else m for m in s.models]
    _write_models(listed if before is not None else [*s.models, added])
    return models()


def set_model_effort(value: str, effort: str) -> list[ModelOption]:
    """How hard SAGE's own agent has one of your models think, one of the levels it has. A model that doesn't think
    has none to set."""
    s = settings()
    model_id = _canonical(value)
    yours = s.model(model_id)
    if yours is None:
        raise SageError("invalid", f"{model_id} isn't one of your models; add it first with `sage models add`")
    _write_models(
        [m.model_copy(update={"effort": _model_effort(s, model_id, effort)}) if m is yours else m for m in s.models]
    )
    return models()


def _model_effort(s: Settings, model_id: str, value: str) -> Effort:
    """`value` as an effort the model has, or an `invalid` error that lists the ones it has."""
    efforts = entry(_route(s, model_id)).efforts
    if not efforts:
        raise SageError("invalid", f"{model_id} doesn't think, so it has no effort to set")
    found = next((e for e in efforts if e == value), None)
    if found is None:
        raise SageError("invalid", f"{model_id} has no effort '{value}'; it has {', '.join(efforts)}")
    return found


def remove_models(model_ids: list[str]) -> list[ModelOption]:
    """Remove models from yours. When Extract with names one of them, it goes back to empty, so Extract uses the first
    model left."""
    s = settings()
    gone = {_canonical(m) for m in model_ids}
    chosen_gone = False
    if s.extract_with is not None and s.extract_with.partition(":")[0] not in s.agents:
        with suppress(SageError):
            chosen_gone = _canonical(s.extract_with) in gone
    _write_models([m for m in s.models if m.id not in gone], forget_choice=chosen_gone)
    return models()


def _write_models(listed: list[YourModel], *, forget_choice: bool = False) -> None:
    """Write your models; `forget_choice` also clears Extract with."""
    stored = _stored()
    if forget_choice:
        stored.pop("extract_with", None)
    stored["models"] = [m.model_dump(exclude_defaults=True) for m in listed]
    files.write_toml(config_path(), stored)


def _canonical(value: str) -> ModelId:
    """A model as a person wrote it, as its canonical `provider:name`, on one of your providers."""
    try:
        model_id = resolve(ModelIdAdapter.validate_python(value))
    except ValidationError as e:
        raise SageError("invalid", f"model {value!r}: {_validation_message(e).removeprefix(': ')}") from e
    except LookupError as e:
        raise SageError("invalid", f"no model named {value!r}: {e}") from e
    _provider(settings(), model_id)
    return model_id


def _usable(s: Settings, value: str) -> ModelId:
    """A model a run may use: one of yours, with a price."""
    model_id = _canonical(value)
    if s.model(model_id) is None:
        raise SageError(
            "invalid",
            f"{model_id} isn't one of your models; add it on the Manage page, or `sage models add {model_id}`",
        )
    if entry(_route(s, model_id)).input_usd is None:
        raise SageError("invalid", f"SAGE has no price for {model_id}; give it yours on the Manage page")
    return model_id


# Papers -----------------------------------------------------------------------------------------------------------


class PaperSummary(BaseModel):
    paper: PaperInfo
    latest_run: RunInfo | None = None
    """The live run, else the newest."""
    run_count: int = 0
    data_run: RunInfo | None = None
    """The run whose records stand for the paper. See `run_state.data_run`."""
    to_check: int | None = None
    """Unreviewed "Check this" and "Missing" items on `data_run`. None when no run has records."""
    looks_good: int | None = None
    """Unreviewed "Looks good" items on `data_run`, which `accept_looks_good` accepts in one call."""
    live_stage: Stage | None = None
    """The stage the live run is in. None when no run is live, or one is live but hasn't started a stage."""


class Added(BaseModel):
    filename: str
    paper: PaperInfo
    new: bool


class NotAdded(BaseModel):
    """A file in a batch that couldn't be added. The others still were."""

    filename: str
    error: ErrorBody


def add_paper(filename: str, data: bytes) -> Added | NotAdded:
    """Adding the same bytes again returns the existing paper."""
    name = Path(filename).name
    try:
        return _add(name, data)
    except SageError as e:
        return NotAdded(filename=name, error=ErrorBody(kind=e.kind, message=e.message))
    except OSError as e:
        error = ErrorBody(kind="corrupt", message=f"couldn't save {name}: {e.strerror or e}")
        return NotAdded(filename=name, error=error)


def add_paper_file(path: Path) -> Added | NotAdded:
    try:
        data = files.read_bytes(path)
    except OSError as e:
        error = ErrorBody(kind="not_found", message=f"can't read {path}: {e.strerror}")
        return NotAdded(filename=path.name, error=error)
    return add_paper(path.name, data)


def _add(filename: str, data: bytes) -> Added:
    if not data.startswith(b"%PDF"):
        raise SageError("invalid", f"{filename} is not a PDF")
    try:
        with _on_disk():
            info, new = paper_files.add(home(), settings(), filename, data)
    except pdf.PdfError as e:
        raise SageError("invalid", f"{filename}: {e}") from e
    return Added(filename=filename, paper=info, new=new)


def papers() -> list[PaperSummary]:
    return [_summary(info) for info in _library()]


def paper_summary(paper_id: str) -> PaperSummary:
    return _summary(paper(paper_id))


def _summary(info: PaperInfo) -> PaperSummary:
    paper_runs = runs(info.id)
    shown = run_state.data_run(paper_runs)
    latest = run_state.current(paper_runs)
    to_check = looks_good = None
    if shown is not None:
        unreviewed = [i for i in review_queue(shown.id) if i.review is None]
        looks_good = sum(1 for i in unreviewed if i.status == "verified")
        to_check = len(unreviewed) - looks_good
    return PaperSummary(
        paper=info,
        latest_run=latest,
        run_count=len(paper_runs),
        data_run=shown,
        to_check=to_check,
        looks_good=looks_good,
        live_stage=_live_stage(latest) if latest is not None and run_state.live(latest) else None,
    )


def _live_stage(info: RunInfo) -> Stage | None:
    started = [e for e in events(info.id).events if isinstance(e, StageStarted)]
    return started[-1].stage if started else None


def paper(paper_id: str) -> PaperInfo:
    info = _read(home().paper_json(_checked_id("paper", paper_id)), PaperInfo)
    if info is None:
        raise SageError("not_found", f"no paper {paper_id}; see `sage papers`")
    return info


def paper_pdf(paper_id: str) -> bytes:
    paper(paper_id)
    return files.read_bytes(home().pdf(paper_id))


def _library() -> list[PaperInfo]:
    with _on_disk():
        return paper_files.library(home())


# Runs -------------------------------------------------------------------------------------------------------------


def runs(paper_id: str | None = None) -> list[RunInfo]:
    """A run folder with no run.json is skipped; one whose run.json won't parse fails the list and names it."""
    h = home()
    if paper_id is not None:
        paper(paper_id)
    with _on_disk():
        return run_state.listed(h, paper_id)


def run(run_id: str) -> RunInfo:
    """Reads only this run's run.json, so a broken file elsewhere doesn't hide it."""
    h = home()
    _checked_id("run", run_id)
    with _on_disk():
        info = run_state.find(h, run_id)
    if info is None:
        raise SageError("not_found", f"no run {run_id}; see `sage runs`")
    return info


def start_runs(
    paper_ids: list[str] | None = None,
    *,
    using: str | None = None,
    effort: str | None = None,
) -> list[RunInfo]:
    """Queue a run on each paper, or on every paper with no data and nothing live when none are given. The runner
    takes them on, `parallel_runs` at a time, in a process that outlives the terminal or the server that asked.
    Everything a run would refuse is checked first, so a bad model, a missing key or a paper already being extracted
    fails here and queues nothing."""
    s = settings()
    driver = _driver(s, using)
    chosen = _pending() if paper_ids is None else list(dict.fromkeys(paper(p).id for p in paper_ids))
    if not chosen:
        raise SageError("invalid", "nothing to extract: every paper has data or is being extracted")
    for paper_id in chosen:
        _refuse_live(paper_id)
    asked = _run_effort(s, driver, effort, None)
    return [_queue(_new_run(s, driver, paper_id, asked, _last_run_id(paper_id))) for paper_id in chosen]


class _Driver(BaseModel):
    """Who drives a new run, as the RunInfo fields that say so."""

    driver: Driver
    agent: str | None = None
    agent_model: str | None = None
    extraction_model: ModelId | None = None


def _driver(s: Settings, using: str | None) -> _Driver:
    """Who a new run is driven by: an agent (`claude`, or `claude:sonnet` for its model) or a model SAGE's own agent
    runs on. Unset, it is the `extract_with` setting, else the first of your models."""
    chosen = using or s.default_using()
    if chosen is None:
        raise SageError("invalid", "nothing to extract with: add a model or set up an agent on the Manage page")
    agent, _, agent_model = chosen.partition(":")
    if agent in s.agents:
        unready = _unready(agent, s.agents[agent])
        if unready is not None:
            raise SageError("invalid", unready)
        return _Driver(driver="agent", agent=agent, agent_model=agent_model or s.agents[agent].model)
    model = _usable(s, chosen)
    route(s, model)
    return _Driver(driver="sage", extraction_model=model)


class _Effort(BaseModel):
    """What a new run asks for, as the RunInfo fields that say so."""

    effort: str | None
    effort_given: bool


def _run_effort(s: Settings, driver: _Driver, given: str | None, inherited: str | None) -> _Effort:
    """The effort a new run asks for. For SAGE's own agent: the one given, which the model has to have; else the one
    the run it continues had, while the model has it; else the model's own. For an agent: the one given, else the
    earlier run's, else the agent's own, which the agent checks once it starts, since its levels depend on its
    model."""
    if driver.driver == "agent":
        config = s.agents[driver.agent or ""]
        chosen = given or inherited or config.effort
        return _Effort(effort=chosen, effort_given=given is not None)
    model_id = driver.extraction_model or ""
    if given is not None:
        return _Effort(effort=_model_effort(s, model_id, given), effort_given=True)
    efforts = entry(_route(s, model_id)).efforts
    if inherited in efforts:
        return _Effort(effort=inherited, effort_given=False)
    yours = s.model(model_id)
    own = runs_at(efforts, yours.effort) if yours is not None else None
    return _Effort(effort=own, effort_given=False)


def _new_run(
    s: Settings,
    driver: _Driver,
    paper_id: str,
    asked: _Effort,
    parent_id: str | None,
    guidance: str | None = None,
) -> RunInfo:
    return RunInfo(
        id=_new_id(),
        paper_id=paper_id,
        **driver.model_dump(),
        **asked.model_dump(),
        max_turns=s.max_turns,
        max_cost_usd=s.max_cost_usd,
        parent_id=parent_id,
        guidance=guidance,
    )


def _pending() -> list[str]:
    """Papers with no data and nothing live on them: what "Extract all" takes."""
    return [p.paper.id for p in papers() if p.data_run is None and not run_state.live(p.latest_run)]


def _refuse_live(paper_id: str) -> None:
    live = _open_run(paper_id)
    if live is not None:
        raise SageError("conflict", f"run {live.id} is already {live.status} on {paper_id}")


def resume_run(run_id: str, *, guidance: str | None = None) -> RunInfo:
    """Carry on a run SAGE drove that didn't finish, in a new conversation over the same folder. Its budget is the
    current settings' maximums minus what the run already spent, so a run with nothing left is refused: raise the
    limit, or continue it, which gets a budget of its own. New guidance is added to what the run was already given."""
    s = settings()
    info = run(run_id)
    if info.driver != "sage" or info.extraction_model is None:
        raise SageError("conflict", f"run {run_id} wasn't driven by SAGE; continue it instead")
    route(s, _usable(s, info.extraction_model))
    if run_state.live(info):
        raise SageError("conflict", f"run {run_id} is still {info.status}")
    if info.status == "finished":
        raise SageError("conflict", f"run {run_id} already finished; continue it with guidance, or start a new run")
    spent = info.usage.cost_usd
    if info.turns >= s.max_turns or spent >= s.max_cost_usd:
        used = (
            f"{info.turns} of {s.max_turns} turns"
            if info.turns >= s.max_turns
            else f"${spent:.2f} of the ${s.max_cost_usd:.2f} budget"
        )
        raise SageError(
            "conflict",
            f"run {run_id} already used {used}; raise the limit in settings, or continue it with "
            "`sage extract run --continue`, which gets a budget of its own",
        )
    text = _guidance_text(guidance)
    if text is not None:
        info.guidance = text if info.guidance is None else f"{info.guidance}\n\n{text}"
    info.max_turns = s.max_turns
    info.max_cost_usd = s.max_cost_usd
    return _queue(info)


def continue_run(
    run_id: str,
    guidance: str | None,
    *,
    using: str | None = None,
    effort: str | None = None,
) -> RunInfo:
    """Start a new run on the paper's folder as an ended run left it, with the scientist's guidance on what to add or
    fix. The earlier run stays as it was. The new run has its own budget, and its model and effort default to the
    earlier run's. The scientist's reviews reach the agent through check."""
    s = settings()
    parent = run(run_id)
    if using is None and parent.driver == "agent" and parent.agent is not None:
        using = parent.agent + (f":{parent.agent_model}" if parent.agent_model is not None else "")
    elif using is None and parent.driver == "sage":
        using = parent.extraction_model
    driver = _driver(s, using)
    if run_state.live(parent):
        raise SageError("conflict", f"run {run_id} is still {parent.status}")
    same = (parent.driver, parent.agent) == (driver.driver, driver.agent)
    asked = _run_effort(s, driver, effort, parent.effort if same else None)
    return _queue(_new_run(s, driver, parent.paper_id, asked, parent.id, _guidance_text(guidance)))


def stop_runs(run_ids: list[str] | None = None) -> list[RunInfo]:
    """Stop each run, or every live run when none are given. A run the runner drives is asked to stop: it settles what
    its folder holds and reads as interrupted, so it shows what it found and `resume_run` can carry it on, and one
    still queued ends without starting. One driven from a terminal ends at once, its files kept for the next run."""
    chosen = [r for r in runs() if run_state.live(r)] if run_ids is None else [run(i) for i in run_ids]
    for info in chosen:
        if not run_state.live(info):
            raise SageError("conflict", f"run {info.id} is {info.status}, so there is nothing to stop")
    return [_stop(info) for info in chosen]


def _stop(info: RunInfo) -> RunInfo:
    h = home()
    if info.driver != "here":
        files.touch(h.run(info.paper_id, info.id).stop)
        return info
    with _on_disk(), files.exclusive(h.run_lock(info.paper_id)):
        current = run(info.id)
        if not run_state.live(current):
            return current
        return extraction.stopped(h, settings(), current, "stopped from the terminal or the UI")


def _new_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + os.urandom(2).hex()


def _guidance_text(guidance: str | None) -> str | None:
    text = (guidance or "").strip()
    return text or None


def _queue(info: RunInfo) -> RunInfo:
    """Write run.json as queued and make sure a runner is there to take it on. The paper's lock makes the live-run
    check and the queued run.json one step, so two starts at once can't both pass. run.json has one writer at a
    time: this process while the run is queued, the runner from when it takes it on."""
    h = home()
    paths = h.run(info.paper_id, info.id)
    with files.exclusive(h.run_lock(info.paper_id)):
        _refuse_live(info.paper_id)
        files.remove(paths.stop)
        info = info.model_copy(update={"status": "queued", "pid": None, "finished_at": None})
        files.write_model(paths.info, info)
    runner.wake(h)
    return info


def serve_runs(lock_fd: int | None = None) -> None:
    """The runner process's body (`runner.serve`)."""
    fresh_prices()
    runner.serve(home(), settings, _run_route, lock_fd)


def fresh_prices() -> None:
    """For a long-lived process (the runner, the API): keep model prices current, unless `fetch_prices` is off."""
    with suppress(SageError):
        if settings().fetch_prices:
            keep_prices_fresh()


def _run_route(s: Settings, info: RunInfo) -> Route | None:
    """What the runner checks before it starts a run. A missing key or sign-in refuses the run, so the Paper screen
    points at the setup rather than offering a retry."""
    if info.driver == "agent":
        config = s.agents.get(info.agent or "")
        if config is None:
            raise SageError("invalid", f"no agent named {info.agent}; see `sage agents`")
        unready = _unready(info.agent or "", config)
        if unready is not None:
            raise runner.RefusedError(unready)
        return None
    if info.extraction_model is None:
        raise SageError("invalid", f"run {info.id} names no model")
    try:
        return route(s, info.extraction_model)
    except SageError as e:
        if e.kind == "missing_key":
            raise runner.RefusedError(e.message) from e
        raise


class EventPage(BaseModel):
    events: list[Event]
    offset: int
    done: bool


def events(run_id: str, *, offset: int = 0) -> EventPage:
    """Events from byte `offset` on, and the offset to read from next. `done` is read before the events, so a page
    that says done holds every event the run wrote: a follower can stop on it without missing the last ones."""
    info = run(run_id)
    with _on_disk():
        found, end = files.read_jsonl(home().run(info.paper_id, run_id).events, EventAdapter, offset=offset)
    return EventPage(events=found, offset=end, done=not run_state.live(info))


class AnchorBox(BaseModel):
    page: int
    bbox: tuple[float, float, float, float]
    text: str
    quotes: dict[str, list[tuple[float, float, float, float]]] = Field(
        default_factory=dict,
        description="For a text block, each quote cited in it with the boxes of the lines it covers, so the paper can "
        "mark the quoted words. A quote missing here draws as the whole block. Empty while the run is live.",
    )


class RecordsView(BaseModel):
    run: RunInfo
    settled: Settled
    provisional: bool
    """True while the run has no records.json yet: the draft so far, settled on the fly."""
    boxes: dict[str, AnchorBox]
    """Every anchor the evidence cites, resolved to its page and box for drawing on the PDF, in the document the run
    was extracted against."""
    labels: dict[str, str]
    """A short plain name for each record, e.g. "Yield, Agria, 2008"."""


def records(run_id: str) -> RecordsView:
    info = run(run_id)
    h = home()
    with _on_disk():
        saved = run_state.saved(h, info)
        found, doc = (
            run_state.provisional(h, settings(), info) if saved is None else (saved, run_state.document(h, info))
        )
        lines = {} if saved is None else quote_lines(h, info.paper_id, run_id, found, doc)
    provisional = saved is None
    anchors = {a for a, _ in cites(found)}
    boxes: dict[str, AnchorBox] = {}
    for a in sorted(anchors):
        resolved = doc.resolve(a)
        if resolved is not None:
            boxes[a] = AnchorBox(page=resolved.page, bbox=resolved.bbox, text=resolved.text, quotes=lines.get(a, {}))
    return RecordsView(run=info, settled=found, provisional=provisional, boxes=boxes, labels=labels(found))


def _settled(info: RunInfo) -> Settled:
    with _on_disk():
        return run_state.settled(home(), settings(), info)


def document(paper_id: str) -> Document:
    paper(paper_id)
    doc = _read(home().document(paper_id), Document)
    if doc is None:
        raise SageError("not_found", f"paper {paper_id} has no document.json; add it again")
    return doc


def run_doc(run_id: str) -> Document:
    """The document a run's anchors point into, which a later reparse of the paper doesn't change."""
    info = run(run_id)
    with _on_disk():
        return run_state.document(home(), info)


# Extract ----------------------------------------------------------------------------------------------------------
# An agent extracts a paper by editing the files in its folder and calling these with the paper's id. A run is the
# session from `extract_start` to `extract_done` or `extract_stop`; the folder carries on from one run to the next.


class ExtractStatus(BaseModel):
    paper_id: str
    folder: str
    """The paper's folder, where the agent works."""
    run: RunInfo | None = None
    """The open run, else the newest."""


def extract_start(paper_id: str, *, guidance: str | None = None) -> ExtractStatus:
    """Open a run that the caller drives from a terminal. Starting a paper the caller already has open returns that
    run; one SAGE or an agent it started is extracting is refused."""
    info = paper(paper_id)
    h = home()
    with files.exclusive(h.run_lock(info.id)):
        current = _open_run(info.id)
        if current is None:
            current = extraction.opened(
                h,
                RunInfo(
                    id=_new_id(),
                    paper_id=info.id,
                    status="running",
                    driver="here",
                    started_at=now(),
                    parent_id=_last_run_id(info.id),
                    guidance=_guidance_text(guidance),
                ),
            )
        elif current.driver == "sage":
            raise SageError("conflict", f"{info.id} is being extracted by SAGE; stop it first")
    return ExtractStatus(paper_id=info.id, folder=str(h.paper(info.id)), run=current)


def extract_check(paper_id: str, *, every: bool = False) -> CheckReport:
    """Check the paper's folder. While a run is open the check is recorded in it. `every` lists every finding, not the
    first `error_list_cap` of each severity."""
    info = paper(paper_id)
    with _on_disk():
        return extraction.check(home(), settings(), _open_run(info.id), info.id, every=every)


def extract_done(paper_id: str, summary: str) -> DoneReport:
    """Finish the open run: refused while anything blocks, otherwise settled. With no open run, one is opened for the
    caller first, so an agent that skipped `start` loses nothing."""
    info = paper(paper_id)
    current = _open_run(info.id)
    if current is None:
        current = extract_start(info.id).run
    if current is None or current.driver == "sage":
        raise SageError("conflict", f"{info.id} is being extracted by SAGE, which finishes it itself")
    h = home()
    # An agent SAGE started runs this in its own process while the runner writes the same run.json.
    with _on_disk(), files.exclusive(h.run_lock(info.id)):
        return extraction.done(h, settings(), run(current.id), summary)


def extract_stop(paper_ids: list[str] | None = None) -> list[RunInfo]:
    """Stop each paper's open run, or every open run, as `stop_runs` does."""
    if paper_ids is None:
        return stop_runs()
    return [_stop(_open_or_fail(p)) for p in paper_ids]


def extract_status(paper_id: str | None = None) -> list[ExtractStatus]:
    h = home()
    ids = [paper(paper_id).id] if paper_id is not None else [p.id for p in _library()]
    return [ExtractStatus(paper_id=pid, folder=str(h.paper(pid)), run=run_state.current(runs(pid))) for pid in ids]


def _open_run(paper_id: str) -> RunInfo | None:
    return run_state.open_run(runs(paper_id))


def _open_or_fail(paper_id: str) -> RunInfo:
    current = _open_run(paper(paper_id).id)
    if current is None:
        raise SageError("conflict", f"nothing is extracting {paper_id}")
    return current


def _last_run_id(paper_id: str) -> str | None:
    paper_runs = runs(paper_id)
    return paper_runs[0].id if paper_runs else None


# Agents -----------------------------------------------------------------------------------------------------------
# Coding agents SAGE starts in a paper's folder over ACP, each with its own sign-in.


class AgentStatus(BaseModel):
    id: str
    title: str
    state: proc.AgentState
    detail: str | None = None
    """What is missing, or what the sign-in check said."""
    fix: str | None = None
    """What to do about it."""
    model: str | None = None
    """The model it is set to use; None for its own default."""
    effort: str | None = None
    """The thought level it is set to use, in its own words; None for its own default."""
    running: int = 0
    """Runs it is driving now."""


class AgentOffer(BaseModel):
    """What an agent offers once started: the models it can use, its modes, and the thought levels of the model it is
    on."""

    id: str
    models: list[str]
    model: str | None
    modes: list[str]
    mode: str | None
    efforts: list[str]
    """The levels `effort` may take on `model`. Empty when the agent offers no effort choice for it."""
    effort: str | None
    """The level it starts at on `model`."""


def agents() -> list[AgentStatus]:
    """Each configured agent and whether it can run: installed, and signed in where it says how to tell."""
    s = settings()
    live = [r.agent for r in runs() if r.driver == "agent" and run_state.live(r)]
    return [_agent_state(a, c).model_copy(update={"running": live.count(a)}) for a, c in s.agents.items()]


def agent_offer(agent_id: str, model: str | None = None) -> AgentOffer:
    """Start the agent in an empty folder and read its models and modes, and the efforts of `model` (else its
    default model), without prompting it. Takes a few seconds, longer the first time npx fetches its adapter."""
    config = _agent_config(agent_id)
    with files.scratch_dir() as folder:
        launch = agent_run.launch_for(config, model or None, env=proc.agent_env(config.env), cwd=folder)
        try:
            found = asyncio.run(acp_client.offer(launch))
        except acp_client.AgentError as e:
            raise SageError("invalid", f"{config.title}: {e}") from e
    return AgentOffer(id=agent_id, **found.model_dump())


def set_agent(agent_id: str, *, model: str | None = None, effort: str | None = None) -> AgentStatus:
    """Set the model and the effort an agent runs with, in its own words. None leaves one as it is, and an empty
    string clears it, so the agent's own default applies. Neither is checked here, since that means starting the
    agent: a run checks them once it starts, and `agent_offer` lists what the agent has."""
    s = settings()
    config = _agent_config(agent_id)
    update = {k: v or None for k, v in (("model", model), ("effort", effort)) if v is not None}
    if not update:
        raise SageError("invalid", "give a model or an effort to set")
    agents: dict[str, JsonValue] = {a: c.model_dump(mode="json") for a, c in s.agents.items()}
    agents[agent_id] = config.model_copy(update=update).model_dump(mode="json")
    update_settings({"agents": agents})
    return _agent_state(agent_id, settings().agents[agent_id])


def _agent_config(agent_id: str) -> AgentConfig:
    config = settings().agents.get(agent_id)
    if config is None:
        raise SageError("not_found", f"no agent named {agent_id}; see `sage agents`")
    return config


def answer_agent(paper_id: str, *, allow: bool) -> RunInfo:
    """Answer what the agent driving the paper's run is waiting on."""
    current = _open_or_fail(paper_id)
    path = home().run(current.paper_id, current.id).permission
    with _on_disk():
        asked = files.read_model_or_none(path, agent_run.PermissionFile)
        if asked is None or asked.allowed is not None:
            raise SageError("conflict", f"the agent on {paper_id} isn't waiting for an answer")
        files.write_model(path, asked.model_copy(update={"allowed": allow}))
    return current


def _agent_state(agent_id: str, config: AgentConfig) -> AgentStatus:
    path = proc.agent_env(config.env)["PATH"]
    state, detail = proc.agent_state(config.requires or [config.command], config.login_check, path)
    fix = {"not_installed": config.install, "not_signed_in": config.login}.get(state)
    return AgentStatus(
        id=agent_id,
        title=config.title,
        state=state,
        detail=detail,
        fix=fix,
        model=config.model,
        effort=config.effort,
    )


def _unready(agent_id: str, config: AgentConfig) -> str | None:
    state = _agent_state(agent_id, config)
    if state.state == "ready":
        return None
    fix = f"; {state.fix}" if state.fix is not None else ""
    return f"{state.title} isn't ready: {state.detail}{fix}"


# Skill ------------------------------------------------------------------------------------------------------------
# The SAGE skill tells an agent how to extract. It ships in the package; installing it puts it where coding agents
# look, so an agent in any terminal (or one SAGE starts) knows the files, the shapes and check and done.

SkillState = Literal["missing", "outdated", "current"]


class SkillPlace(BaseModel):
    folder: str
    state: SkillState
    linked: bool = False
    """The folder is a link to the shared copy."""


def skill_status() -> list[SkillPlace]:
    shipped = skills.bundled()
    return [_skill_place(folder, shipped) for folder in skills.places()]


def install_skill() -> list[SkillPlace]:
    """Install the skill into the shared folder and link Claude Code's folder to it. A copy someone put in Claude Code's
    folder by hand is brought up to date in place instead."""
    shipped = skills.bundled()
    shared, claude = skills.places()
    with _on_disk():
        skills.install(shared, shipped)
        if not skills.link(shared, claude):
            skills.install(claude, shipped)
    return skill_status()


def _skill_place(folder: Path, shipped: dict[str, str]) -> SkillPlace:
    with _on_disk():
        found = skills.installed(folder)
    state: SkillState = "missing" if found is None else "current" if found == shipped else "outdated"
    return SkillPlace(folder=str(folder), state=state, linked=folder.is_symlink())


# Review -----------------------------------------------------------------------------------------------------------


def reviews(paper_id: str) -> list[Review]:
    paper(paper_id)
    with _on_disk():
        found, _ = files.read_jsonl(home().reviews(paper_id), ReviewAdapter)
    return found


def _standing(info: RunInfo, settled: Settled) -> list[Review]:
    with _on_disk():
        before = run_state.ancestors(home(), info)
    return standing(reviews(info.paper_id), settled, [_settled(a) for a in before])


def review_queue(run_id: str) -> list[QueueItem]:
    """Every record of the run, unresolved first, with its latest standing review."""
    info = run(run_id)
    settled = _settled(info)
    return queue(settled, _standing(info, settled))


def add_review(review: Review) -> Review:
    info = run(review.run_id)
    rec = _settled(info).record(review.record_id)
    if rec is None:
        raise SageError("not_found", f"run {review.run_id} has no record {review.record_id}")
    if review.field is not None and review.field not in type(rec.record).model_fields:
        raise SageError("invalid", f"{rec.record.type} has no field '{review.field}'")
    if review.action == "edit" and (review.field is None or review.value is None):
        raise SageError("invalid", "an edit names the field and gives the corrected value")
    files.append_jsonl(home().reviews(info.paper_id), review)
    return review


def accept_looks_good(run_id: str) -> list[Review]:
    """Running it again accepts nothing new."""
    info = run(run_id)
    path = home().reviews(info.paper_id)
    accepted = [
        Review(run_id=run_id, record_id=i.record_id, action="accept")
        for i in review_queue(run_id)
        if i.status == "verified" and i.review is None
    ]
    for r in accepted:
        files.append_jsonl(path, r)
    return accepted


# Export -----------------------------------------------------------------------------------------------------------

ExportFormat = Literal["xlsx", "csv", "json"]
"""xlsx: the PEcAn curation workbook; csv: the same tabs as one CSV each, zipped; json: every record as settled."""


class ExportFile(BaseModel):
    data: bytes
    media_type: str
    filename: str


def write_export(path: Path, data: bytes) -> None:
    try:
        files.write_bytes(path, data)
    except OSError as e:
        raise SageError("invalid", f"can't write {path}: {e.strerror}") from e


def export(run_id: str, fmt: ExportFormat) -> ExportFile:
    info = run(run_id)
    settled = _settled(info)
    if fmt == "json":
        return ExportFile(
            data=settled.model_dump_json(indent=1).encode(),
            media_type="application/json",
            filename=f"{run_id}.json",
        )
    tabs = workbook(settled, run_doc(run_id), _standing(info, settled), info)
    if fmt == "xlsx":
        return ExportFile(
            data=xlsx(tabs),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=f"{run_id}.xlsx",
        )
    return ExportFile(data=csv_zip(tabs), media_type="application/zip", filename=f"{run_id}-csv.zip")


# Changes ----------------------------------------------------------------------------------------------------------
# Agents, runs and other windows write the library while a screen shows it. The screen follows each change instead of
# asking again on a timer.


class Change(BaseModel):
    paper_id: str
    what: ChangeKind
    run_id: str | None = None
    """For `run` and `log`, the run whose folder changed. For `files` and `paper`, the paper's open run, whose records
    are compiled from them as they are; none when nothing is extracting the paper."""


async def changes() -> AsyncIterator[list[Change]]:
    """Each batch of changes in the library as it happens, until the caller stops iterating."""
    h = await asyncio.to_thread(home)
    async for batch in watch.changed(h.papers):
        found = await asyncio.to_thread(_changes, h, batch)
        if found:
            yield found


def _changes(h: Home, batch: set[Path]) -> list[Change]:
    """The changes in a batch of paths under papers/, each once."""
    found: dict[tuple[str, ChangeKind, str | None], None] = {}
    open_runs: dict[str, str | None] = {}
    for path in sorted(batch):
        kind = h.change(path)
        if kind is None:
            continue
        paper_id, what, run_id = kind
        if what in ("files", "paper"):
            if paper_id not in open_runs:
                open_runs[paper_id] = _open_run_id(paper_id)
            run_id = open_runs[paper_id]
        found[paper_id, what, run_id] = None
    return [Change(paper_id=p, what=w, run_id=r) for p, w, r in found]


def _open_run_id(paper_id: str) -> str | None:
    try:
        found = _open_run(paper_id)
    except SageError:  # the paper is going or broken; the screen hears of that as its own change
        return None
    return found.id if found is not None else None
