"""Run configuration + manifest metadata (pipeline/run_config.py and the
orchestrator glue): explicit model/provider, endpoint snapshot, fingerprints,
git state, one-model-per-run guard, and that no credential is ever recorded.

Infrastructure only: none of this may change what a model is asked; that is
pinned by test_run_characterization.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from test_orchestrator import (  # noqa: F401  (env is a pytest fixture)
    PAPER_ID, _build_run_paper_invoke_sequence, env, make_invoke_sequence,
)

from pipeline import orchestrator, run_config, run_store

SECRET = "sk-super-secret-value"


def _opencode(tmp_path: Path, api_key: str = SECRET, base_url: str = "https://gateway.example/gpt/v1") -> Path:
    path = tmp_path / "opencode.json"
    path.write_text(json.dumps({
        "provider": {
            "prov-a": {"npm": "x", "options": {"baseURL": base_url, "apiKey": api_key}, "models": {"model-a": {}, "model-b": {}}},
            "prov-b": {"npm": "x", "options": {"baseURL": "https://gateway.example/other/v1", "apiKey": "k2"}, "models": {"model-c": {}}},
        },
    }), encoding="utf-8")
    return path


def _config(tmp_path: Path, **overrides) -> Path:
    body = {"provider": "prov-a", "model": "model-a", **overrides}
    path = tmp_path / "eval_config.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


# --------------------------------------------------------------------- #
# loading / validation: no silent default
# --------------------------------------------------------------------- #


def test_load_resolves_base_url_from_opencode_json_and_defaults_timeouts(tmp_path):
    cfg = run_config.load_run_config(_config(tmp_path), _opencode(tmp_path))
    assert (cfg.provider_key, cfg.model, cfg.model_ref) == ("prov-a", "model-a", "prov-a/model-a")
    assert cfg.base_url == "https://gateway.example/gpt/v1"
    assert cfg.agent_call_timeout_seconds == 300 and cfg.ir_service_timeout_seconds == 120.0


def test_timeouts_come_from_the_config_when_given(tmp_path):
    cfg = run_config.load_run_config(
        _config(tmp_path, agent_call_timeout_seconds=90, ir_service_timeout_seconds=30), _opencode(tmp_path),
    )
    assert cfg.agent_call_timeout_seconds == 90 and cfg.ir_service_timeout_seconds == 30.0


def test_missing_config_is_an_error_not_a_silent_default(tmp_path):
    with pytest.raises(run_config.RunConfigError, match="never chosen by a default in code"):
        run_config.load_run_config(tmp_path / "nope.json", _opencode(tmp_path))


@pytest.mark.parametrize("body, match", [
    ({"provider": "prov-a"}, "'model'"),
    ({"model": "model-a"}, "'provider'"),
    ({"provider": "ghost", "model": "model-a"}, "not declared"),
    ({"provider": "prov-a", "model": "ghost"}, "not declared under provider"),
    ({"provider": "prov-b", "model": "model-a"}, "not declared under provider"),  # a real model, wrong provider
])
def test_invalid_config_is_rejected(tmp_path, body, match):
    path = tmp_path / "eval_config.json"
    path.write_text(json.dumps(body))
    with pytest.raises(run_config.RunConfigError, match=match):
        run_config.load_run_config(path, _opencode(tmp_path))


def test_the_checked_in_evaluation_config_loads_and_names_the_recorded_model():
    cfg = run_config.load_run_config()  # real src/eval_config.json against real src/opencode.json
    assert cfg.model_ref == "jetstream-gpt/gpt-oss-120b"
    assert cfg.base_url == "https://llm.jetstream-cloud.org/gpt-oss-120b/v1"


def test_cli_override_is_validated_and_is_a_new_explicit_config(tmp_path):
    cfg = run_config.load_run_config(_config(tmp_path), _opencode(tmp_path))
    other = run_config.with_model_override(cfg, "prov-b/model-c")
    assert other.model_ref == "prov-b/model-c" and other.base_url == "https://gateway.example/other/v1"
    with pytest.raises(run_config.RunConfigError):
        run_config.with_model_override(cfg, "prov-b/model-a")
    with pytest.raises(run_config.RunConfigError, match="provider/model"):
        run_config.with_model_override(cfg, "model-a")


# --------------------------------------------------------------------- #
# fingerprints
# --------------------------------------------------------------------- #


def test_provider_fingerprint_ignores_the_api_key_but_not_the_base_url(tmp_path):
    a = run_config.provider_fingerprint(_opencode(tmp_path, api_key="one"), "prov-a")
    b = run_config.provider_fingerprint(_opencode(tmp_path, api_key="rotated"), "prov-a")
    c = run_config.provider_fingerprint(_opencode(tmp_path, base_url="https://elsewhere/v1"), "prov-a")
    assert a == b
    assert a != c


def test_pipeline_fingerprint_changes_when_an_agent_prompt_file_changes(tmp_path):
    root = tmp_path / "src"
    (root / "pipeline").mkdir(parents=True)
    (root / "opencode-config" / "agents").mkdir(parents=True)
    for rel in run_config._PIPELINE_CODE_FILES:
        (root / rel).write_text("x")
    (root / "opencode.json").write_text("{}")
    (root / "opencode-config" / "agents" / "extractor.md").write_text("prompt v1")
    before = run_config.pipeline_fingerprint(root)
    (root / "opencode-config" / "agents" / "extractor.md").write_text("prompt v2")
    after = run_config.pipeline_fingerprint(root)
    assert before["sha"] != after["sha"]
    assert before["files"]["opencode-config/agents/extractor.md"] != after["files"]["opencode-config/agents/extractor.md"]
    assert before["files"]["pipeline/orchestrator.py"] == after["files"]["pipeline/orchestrator.py"]


# --------------------------------------------------------------------- #
# git state
# --------------------------------------------------------------------- #


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_state_reports_commit_and_dirty_state(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "f.txt").write_text("one")
    git("add", "f.txt")
    git("commit", "-q", "-m", "c1")
    clean = run_config.git_state(tmp_path)
    assert clean["dirty"] is False and len(clean["commit"]) == 40 and clean["tracked_diff_sha256"] is None
    (tmp_path / "f.txt").write_text("two")
    (tmp_path / "new.txt").write_text("untracked")
    dirty = run_config.git_state(tmp_path)
    assert dirty["dirty"] is True and dirty["commit"] == clean["commit"]
    assert dirty["tracked_diff_sha256"] and dirty["untracked_files"] == ["new.txt"]


def test_git_state_outside_a_repo_is_recorded_as_an_error_not_a_crash(tmp_path):
    state = run_config.git_state(tmp_path)  # tmp_path is not a git repo
    assert state["commit"] is None and state["dirty"] is None and state["error"]


# --------------------------------------------------------------------- #
# endpoint probe
# --------------------------------------------------------------------- #


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _cfg(tmp_path):
    return run_config.load_run_config(_config(tmp_path), _opencode(tmp_path))


def test_probe_records_status_latency_and_listed_models_and_sends_the_configured_key(tmp_path):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "model-a"}, {"id": "other"}]})

    probe = run_config.probe_models(_cfg(tmp_path), client=_client(handler))
    assert probe["ok"] and probe["status"] == 200 and probe["model_ids"] == ["model-a", "other"]
    assert probe["configured_model_listed"] is True and probe["latency_ms"] >= 0
    assert seen["url"] == "https://gateway.example/gpt/v1/models" and seen["auth"] == f"Bearer {SECRET}"
    assert SECRET not in json.dumps(probe)  # the key is used, never recorded


def test_missing_configured_model_refuses_the_run(tmp_path):
    probe = run_config.probe_models(_cfg(tmp_path), client=_client(lambda r: httpx.Response(200, json={"data": [{"id": "other"}]})))
    with pytest.raises(run_config.ModelUnavailable, match="not listed"):
        run_config.require_model_available(_cfg(tmp_path), probe)


def test_an_inconclusive_probe_is_a_recorded_warning_not_a_refusal(tmp_path):
    def down(request):
        raise httpx.ConnectError("boom")

    probe = run_config.probe_models(_cfg(tmp_path), client=_client(down))
    assert probe["ok"] is False and "ConnectError" in probe["error"]
    warning = run_config.require_model_available(_cfg(tmp_path), probe)
    assert warning and "inconclusive" in warning

    server_error = run_config.probe_models(_cfg(tmp_path), client=_client(lambda r: httpx.Response(503, text="no")))
    assert run_config.require_model_available(_cfg(tmp_path), server_error)  # warning, no raise


# --------------------------------------------------------------------- #
# manifest metadata
# --------------------------------------------------------------------- #


def test_manifest_metadata_has_every_required_field_and_never_a_credential(tmp_path):
    cfg = _cfg(tmp_path)
    meta = run_config.build_manifest_metadata(
        cfg, constants={"MAX_EXTRACTION_ATTEMPTS": 3}, probe={"ok": True}, include_git=False,
    )
    for key in (
        "model_ref", "provider", "model_id", "base_url", "timeouts", "constants", "provider_fingerprint",
        "pipeline_fingerprint", "git", "opencode_cli_version", "endpoint_probe", "run_config",
        "served_model_verifiable", "served_model_note",
    ):
        assert key in meta, key
    assert meta["model_ref"] == "prov-a/model-a" and meta["base_url"] == "https://gateway.example/gpt/v1"
    assert meta["timeouts"] == {"agent_call_timeout_seconds": 300, "ir_service_timeout_seconds": 120.0}
    assert meta["served_model_verifiable"] is False
    assert meta["run_config"]["model_source"] == "config"
    assert SECRET not in json.dumps(meta)


def test_prepare_run_binds_the_configured_timeout_and_records_the_constants(tmp_path, monkeypatch):
    monkeypatch.setattr(run_config, "probe_models", lambda cfg, **kw: {"ok": True, "model_ids": [cfg.model], "url": "u"})
    monkeypatch.setattr(run_config, "git_state", lambda *a, **k: {"commit": "abc", "dirty": False})
    cfg_path = tmp_path / "eval_config.json"
    cfg_path.write_text(json.dumps({"provider": "jetstream-gpt", "model": "gpt-oss-120b", "agent_call_timeout_seconds": 77}))
    cfg, extra, invoke = orchestrator.prepare_run(config_path=str(cfg_path))
    assert cfg.model_ref == "jetstream-gpt/gpt-oss-120b"
    assert invoke.keywords == {"timeout": 77}
    assert extra["constants"]["MAX_EXTRACTION_ATTEMPTS"] == orchestrator.MAX_EXTRACTION_ATTEMPTS
    assert extra["run_config"]["model_source"] == "config"


def test_prepare_run_marks_a_cli_override_and_refuses_an_unlisted_model(tmp_path, monkeypatch):
    monkeypatch.setattr(run_config, "git_state", lambda *a, **k: {"commit": "abc", "dirty": False})
    monkeypatch.setattr(run_config, "probe_models", lambda cfg, **kw: {"ok": True, "model_ids": [cfg.model], "url": "u"})
    _, extra, _ = orchestrator.prepare_run(model_override="jetstream-scout/llama-4-scout")
    assert extra["run_config"]["model_source"] == "cli_override" and extra["model_ref"] == "jetstream-scout/llama-4-scout"

    monkeypatch.setattr(run_config, "probe_models", lambda cfg, **kw: {"ok": True, "model_ids": ["something-else"], "url": "u"})
    with pytest.raises(run_config.ModelUnavailable):
        orchestrator.prepare_run()


def test_orchestrator_has_no_default_model_constant():
    assert not hasattr(orchestrator, "DEFAULT_MODEL")


# --------------------------------------------------------------------- #
# run_paper glue
# --------------------------------------------------------------------- #


def test_run_paper_merges_manifest_extra_and_counts_calls_for_the_single_model(env):
    outcome = orchestrator.run_paper(
        paper_id=PAPER_ID, model="test-model", client=env["client"],
        invoke=make_invoke_sequence(_build_run_paper_invoke_sequence()), enable_ai_validation=False,
        manifest_extra={"model_ref": "test-model", "base_url": "https://x/v1", "provider": "p"},
    )
    manifest = run_store.load_run_manifest(outcome["run_id"])
    assert manifest["base_url"] == "https://x/v1" and manifest["provider"] == "p"
    calls = manifest["agent_calls"]
    assert calls["model"] == "test-model" and calls["total"] == sum(calls["by_agent"].values()) > 0
    assert set(calls["by_agent"]) <= {"extractor", "converter", "ir-validator"}


def test_a_call_requesting_a_different_model_aborts_the_run(env, monkeypatch):
    def bad_run_record(**kwargs):
        # a (hypothetical future) call site that requests a different model than the run is pinned to
        kwargs["invoke"]("extractor", "some-other-model", "prompt")

    monkeypatch.setattr(orchestrator, "run_record", bad_run_record)
    with pytest.raises(RuntimeError, match="mixed models within one run"):
        orchestrator.run_paper(
            paper_id=PAPER_ID, model="test-model", client=env["client"], invoke=make_invoke_sequence([]),
            enable_ai_validation=False, run_id="mixed_run",
        )
    assert run_store.load_run_manifest("mixed_run")["run_status"] == "failed"
