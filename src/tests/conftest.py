import sys
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent.parent / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))


import pytest


@pytest.fixture(autouse=True)
def _no_provider_cooldown(monkeypatch):
    """Step B waits TABLE_PROVIDER_COOLDOWN_SECONDS between provider-failed rounds; tests never sleep."""
    from pipeline import orchestrator

    monkeypatch.setattr(orchestrator, "TABLE_PROVIDER_COOLDOWN_SECONDS", 0)
    # The provider-outage counter is process-global and keyed by run id, and tests reuse run ids ("r1", "run1"): a test
    # that spent a provider budget must never leave the next one in fast-fail mode.
    orchestrator._PROVIDER_OUTAGE.clear()
