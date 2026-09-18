"""Tests for pipeline/fingerprint.py -- the staleness-detection primitive
behind ir_service's /health endpoint and orchestrator.check_health."""

from pipeline.fingerprint import config_fingerprint, schema_fingerprint


def test_schema_fingerprint_is_stable_across_calls():
    assert schema_fingerprint() == schema_fingerprint()


def test_schema_fingerprint_is_a_short_hex_string():
    fp = schema_fingerprint()
    assert isinstance(fp, str)
    assert len(fp) == 16
    int(fp, 16)  # raises if not valid hex


def test_config_fingerprint_changes_with_content(tmp_path):
    f = tmp_path / "opencode.json"
    f.write_text("{}")
    fp1 = config_fingerprint(f)
    f.write_text('{"changed": true}')
    fp2 = config_fingerprint(f)
    assert fp1 != fp2


def test_config_fingerprint_missing_file():
    from pathlib import Path

    assert config_fingerprint(Path("/no/such/file.json")) == "missing"
