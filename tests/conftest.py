"""Shared test fixtures.

Both fixtures lived as near-identical copies in ~10 endpoint test modules
under `tests/pipeline/`; centralising them here removes the maintenance
hazard (a divergent edit silently changing one file's setup vs another).

A test module that needs a different shape — e.g. the schematic API tests
which also seed `ANTHROPIC_API_KEY` — can still define its own fixture
with the same name; pytest closes over the closest-scoped definition.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import config as config_mod
from api.main import app


@pytest.fixture
def client() -> TestClient:
    """FastAPI TestClient bound to the live `api.main.app`."""
    return TestClient(app)


@pytest.fixture
def memory_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Redirect `Settings.memory_root` to an isolated tmp dir per test.

    Resets the cached `_settings` singleton on entry and exit so
    `get_settings()` rereads the env in the next test.
    """
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    # Hermetic: never let an operator's cloud-registry config (set in .env for
    # live managed-mode testing) turn these tests into calls to a live cloud —
    # force the local JSON device-registry store under tmp_path. setenv to ""
    # (not delenv): pydantic-settings re-reads the .env FILE, so a process-env
    # override is what actually wins; "" is falsy → get_device_registry_store
    # picks JSON.
    monkeypatch.setenv("CLOUD_DEVICE_REGISTRY_URL", "")
    monkeypatch.setenv("CLOUD_DEVICE_REGISTRY_TOKEN", "")
    yield tmp_path
    monkeypatch.setattr(config_mod, "_settings", None)
