"""`pipeline/run_config.py` -- the ONE explicit model/provider configuration a
run uses, and the reproducibility metadata every run manifest records.

Why: before this, three places silently disagreed about the model
(`orchestrator.DEFAULT_MODEL` = llama-4-scout, the Streamlit launcher's own
default = gpt-oss-120b, and `opencode.json` pinning every agent to scout --
a pin the orchestrator overrides with `--model` on every call), and the run
manifest fingerprinted only `opencode.json`, not the agent prompt files or
the orchestrator's in-code prompts. A run could therefore not be reproduced
or attributed reliably.

Design (infrastructure only -- nothing here changes what any model is asked):
  * The model is chosen by a checked-in config file (`src/eval_config.json`),
    never by a default buried in code. A missing/invalid config is an ERROR,
    not a silent fallback.
  * The provider's base URL is resolved from `opencode.json` (single source of
    truth, so it can't drift from what `opencode` actually calls) and the
    config must name a provider and model that block actually declares.
  * One model per run: `run_paper` takes a single `model` and this module's
    manifest records exactly that one (see orchestrator's call guard).
  * The manifest records model, provider, base URL, timeouts/attempt
    constants, config + pipeline + schema fingerprints, git commit and dirty
    state, the `opencode` CLI version, and a `/models` endpoint snapshot. API
    keys are NEVER read into the manifest.
  * Honest limit, recorded in the manifest itself: the `opencode` event stream
    carries no served-model field, so which model actually served a call
    cannot be verified per call; only the REQUESTED model and the endpoint's
    model list at run start can be recorded.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

SRC_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SRC_ROOT.parent
DEFAULT_RUN_CONFIG_PATH = SRC_ROOT / "eval_config.json"
DEFAULT_OPENCODE_CONFIG_PATH = SRC_ROOT / "opencode.json"

# Files whose content determines what the pipeline asks a model and how it
# validates the answer -- the "pipeline fingerprint". (schema_fingerprint()
# separately covers ir_schema.py/validators.py.)
_PIPELINE_CODE_FILES = ["pipeline/orchestrator.py", "pipeline/raw_schema.py", "pipeline/content_reader.py", "pipeline/pooling_evidence.py", "pipeline/reconstruction.py"]
_AGENT_PROMPT_GLOB = "opencode-config/agents/*.md"


class RunConfigError(ValueError):
    """The run configuration is missing, malformed, or names a provider/model
    that does not exist in opencode.json."""


class ModelUnavailable(RuntimeError):
    """The endpoint answered its model list successfully and the configured
    model is not in it."""


@dataclass(frozen=True)
class RunConfig:
    provider_key: str
    model: str
    base_url: str
    agent_call_timeout_seconds: int
    ir_service_timeout_seconds: float
    config_path: str
    opencode_config_path: str
    note: str = ""

    @property
    def model_ref(self) -> str:
        """The `provider/model` string handed to `opencode run --model`."""
        return f"{self.provider_key}/{self.model}"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_run_config(
    path: Optional[Path | str] = None, opencode_config_path: Optional[Path | str] = None,
) -> RunConfig:
    config_path = Path(path) if path else DEFAULT_RUN_CONFIG_PATH
    oc_path = Path(opencode_config_path) if opencode_config_path else DEFAULT_OPENCODE_CONFIG_PATH
    if not config_path.is_file():
        raise RunConfigError(
            f"run config not found: {config_path}. A run's model is never chosen by a default in code -- "
            f"create the config (see src/eval_config.json) or pass --config."
        )
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RunConfigError(f"run config {config_path} is not valid JSON: {exc}") from exc
    for key in ("provider", "model"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise RunConfigError(f"run config {config_path} must set a non-empty string '{key}'")

    try:
        opencode = json.loads(oc_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunConfigError(f"cannot read opencode config {oc_path}: {exc}") from exc
    providers = opencode.get("provider") or {}
    block = providers.get(raw["provider"])
    if block is None:
        raise RunConfigError(f"provider '{raw['provider']}' is not declared in {oc_path} (declared: {sorted(providers)})")
    if raw["model"] not in (block.get("models") or {}):
        raise RunConfigError(
            f"model '{raw['model']}' is not declared under provider '{raw['provider']}' in {oc_path} "
            f"(declared: {sorted(block.get('models') or {})})"
        )
    base_url = (block.get("options") or {}).get("baseURL")
    if not base_url:
        raise RunConfigError(f"provider '{raw['provider']}' in {oc_path} has no options.baseURL")

    return RunConfig(
        provider_key=raw["provider"], model=raw["model"], base_url=base_url,
        agent_call_timeout_seconds=int(raw.get("agent_call_timeout_seconds", 300)),
        ir_service_timeout_seconds=float(raw.get("ir_service_timeout_seconds", 120)),
        config_path=str(config_path), opencode_config_path=str(oc_path),
        note=str(raw.get("note", "")),
    )


def with_model_override(cfg: RunConfig, model_ref: str) -> RunConfig:
    """An EXPLICIT per-invocation override (`--model provider/model`),
    validated against opencode.json exactly like the config file is, and
    recorded in the manifest as `model_source: "cli_override"` -- never a
    silent switch."""
    provider, sep, model = model_ref.partition("/")
    if not sep or not provider or not model:
        raise RunConfigError(f"--model must look like 'provider/model' (got {model_ref!r})")
    opencode = json.loads(Path(cfg.opencode_config_path).read_text(encoding="utf-8"))
    block = (opencode.get("provider") or {}).get(provider)
    if block is None:
        raise RunConfigError(f"provider '{provider}' is not declared in {cfg.opencode_config_path}")
    if model not in (block.get("models") or {}):
        raise RunConfigError(f"model '{model}' is not declared under provider '{provider}' in {cfg.opencode_config_path}")
    base_url = (block.get("options") or {}).get("baseURL")
    if not base_url:
        raise RunConfigError(f"provider '{provider}' has no options.baseURL")
    from dataclasses import replace
    return replace(cfg, provider_key=provider, model=model, base_url=base_url)


# --------------------------------------------------------------------------- #
# Fingerprints
# --------------------------------------------------------------------------- #


def provider_fingerprint(opencode_config_path: Path | str, provider_key: str) -> str:
    """Hash of the selected provider block EXCLUDING credentials (`apiKey`),
    so a rotated key never changes the fingerprint but a changed base URL,
    package, or model list does."""
    block = json.loads(Path(opencode_config_path).read_text(encoding="utf-8"))["provider"][provider_key]
    scrubbed = json.loads(json.dumps(block))
    (scrubbed.get("options") or {}).pop("apiKey", None)
    return _sha256_bytes(json.dumps(scrubbed, sort_keys=True).encode())[:16]


def pipeline_fingerprint(root: Optional[Path | str] = None) -> dict[str, Any]:
    """Hash over everything that decides what the pipeline asks a model and
    how it validates the answer: the orchestrator/raw_schema/content_reader/
    reconstruction sources, every agent prompt file, and opencode.json (which
    carries each agent's tool denylist). Per-file hashes are kept so a
    reader can see WHICH file changed between two runs."""
    base = Path(root) if root else SRC_ROOT
    files: list[Path] = [base / rel for rel in _PIPELINE_CODE_FILES]
    files += sorted(base.glob(_AGENT_PROMPT_GLOB))
    files.append(base / "opencode.json")
    per_file = {}
    for f in files:
        per_file[str(f.relative_to(base))] = _sha256_bytes(f.read_bytes())[:12] if f.is_file() else "missing"
    combined = _sha256_bytes(json.dumps(per_file, sort_keys=True).encode())[:16]
    return {"sha": combined, "files": per_file}


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=20, check=True).stdout


def git_state(repo_root: Optional[Path | str] = None) -> dict[str, Any]:
    """Commit, dirty flag, and a hash of the uncommitted tracked diff plus the
    list of untracked (non-ignored) files -- so "same commit" can never hide
    "different working tree"."""
    root = Path(repo_root) if repo_root else REPO_ROOT
    try:
        commit = _git(["rev-parse", "HEAD"], root).strip()
        status = _git(["status", "--porcelain"], root)
        diff = _git(["diff", "HEAD"], root)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"commit": None, "dirty": None, "error": f"{type(exc).__name__}: {exc}"}
    untracked = sorted(line[3:] for line in status.splitlines() if line.startswith("??"))
    return {
        "commit": commit,
        "dirty": bool(status.strip()),
        "tracked_diff_sha256": _sha256_bytes(diff.encode())[:16] if diff else None,
        "untracked_files": untracked[:50],
    }


def opencode_cli_version() -> Optional[str]:
    try:
        out = subprocess.run(["opencode", "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout or "").strip() or None


# --------------------------------------------------------------------------- #
# Endpoint snapshot
# --------------------------------------------------------------------------- #


def probe_models(
    cfg: RunConfig, client: Optional[httpx.Client] = None, timeout: float = 15.0,
) -> dict[str, Any]:
    """`GET {baseURL}/models` -- the smallest safe request the endpoint
    offers. Sends no prompt content. Records reachability, latency, and the
    model ids the endpoint lists. It is NOT a quality benchmark and says
    nothing about the decode-time empty-response defect."""
    opencode = json.loads(Path(cfg.opencode_config_path).read_text(encoding="utf-8"))
    api_key = (((opencode.get("provider") or {}).get(cfg.provider_key) or {}).get("options") or {}).get("apiKey", "")
    url = cfg.base_url.rstrip("/") + "/models"
    own_client = client is None
    client = client or httpx.Client(timeout=timeout)
    started = time.perf_counter()
    try:
        resp = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        ids: list[str] = []
        try:
            body = resp.json()
            ids = [m.get("id") for m in (body.get("data") or []) if isinstance(m, dict) and m.get("id")]
        except ValueError:
            pass
        return {
            "url": url, "ok": resp.status_code == 200, "status": resp.status_code, "latency_ms": latency_ms,
            "model_ids": ids, "configured_model_listed": cfg.model in ids if ids else None, "error": None,
        }
    except httpx.HTTPError as exc:
        return {
            "url": url, "ok": False, "status": None,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "model_ids": [], "configured_model_listed": None, "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if own_client:
            client.close()


def require_model_available(cfg: RunConfig, probe: dict[str, Any]) -> Optional[str]:
    """Refuse to run when the endpoint listed its models successfully and the
    configured model is not among them. A failed/unparseable probe is NOT
    fatal (a flaky listing must not block a run) -- it returns a warning
    string for the manifest instead."""
    if probe.get("ok") and probe.get("model_ids"):
        if cfg.model not in probe["model_ids"]:
            raise ModelUnavailable(
                f"model '{cfg.model}' is not listed by {probe['url']} (listed: {probe['model_ids']}); "
                f"refusing to start a run against a model the endpoint does not offer."
            )
        return None
    return f"model-list probe inconclusive (ok={probe.get('ok')}, status={probe.get('status')}, error={probe.get('error')})"


# --------------------------------------------------------------------------- #
# Manifest metadata
# --------------------------------------------------------------------------- #


def build_manifest_metadata(
    cfg: RunConfig,
    *,
    model_source: str = "config",
    constants: Optional[dict[str, Any]] = None,
    probe: Optional[dict[str, Any]] = None,
    probe_warning: Optional[str] = None,
    ir_service: Optional[dict[str, Any]] = None,
    include_git: bool = True,
) -> dict[str, Any]:
    """Everything a reader needs to attribute and reproduce a run. Never
    contains an API key."""
    return {
        "run_config": {
            "config_path": cfg.config_path,
            "config_sha256": _sha256_bytes(Path(cfg.config_path).read_bytes())[:16],
            "model_source": model_source,
            "note": cfg.note,
        },
        "model_ref": cfg.model_ref,
        "provider": cfg.provider_key,
        "model_id": cfg.model,
        "base_url": cfg.base_url,
        "timeouts": {
            "agent_call_timeout_seconds": cfg.agent_call_timeout_seconds,
            "ir_service_timeout_seconds": cfg.ir_service_timeout_seconds,
        },
        "constants": dict(constants or {}),
        "provider_fingerprint": provider_fingerprint(cfg.opencode_config_path, cfg.provider_key),
        "pipeline_fingerprint": pipeline_fingerprint(),
        "git": git_state() if include_git else None,
        "opencode_cli_version": opencode_cli_version(),
        "endpoint_probe": probe,
        "endpoint_probe_warning": probe_warning,
        "ir_service": ir_service,
        "served_model_verifiable": False,
        "served_model_note": (
            "the opencode event stream carries no served-model field: only the REQUESTED model and the "
            "endpoint's model list at run start are recorded, not which model served each call"
        ),
    }
