"""Tests for the board-delta auto-generation wired into POST /pipeline/repairs.

Strategy:
- Unit-test the pure decision helper `_should_autogenerate_delta` directly so
  the core logic is deterministic (no asyncio racing).
- Light integration tests via the TestClient to confirm the auto-generation task
  is scheduled (or not) under the different gate conditions.  We verify
  scheduling by patching ``asyncio.create_task`` in the repairs module so we
  can inspect the coroutine that was (or was not) submitted, without needing to
  drain the event loop across test sessions.

No network calls are made; ``generate_board_delta`` is patched at import time.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api import config as config_mod
from api.main import app
from api.pipeline import events
from api.pipeline.board_delta.schemas import DeltaBoard
from api.pipeline.routes.repairs import _should_autogenerate_delta


# ---------------------------------------------------------------------------
# Fixtures (mirror the shared conftest.py pattern)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_bus():
    events.reset()
    yield
    events.reset()


@pytest.fixture
def memory_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated tmp memory root; resets the settings singleton around the test."""
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    monkeypatch.setenv("CLOUD_DEVICE_REGISTRY_URL", "")
    monkeypatch.setenv("CLOUD_DEVICE_REGISTRY_TOKEN", "")
    yield tmp_path
    monkeypatch.setattr(config_mod, "_settings", None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Unit tests — pure decision helper
# ---------------------------------------------------------------------------


def test_should_autogenerate_true_when_all_conditions_met(tmp_path):
    """New repair + board_number + allowed + no existing delta → should generate."""
    result = _should_autogenerate_delta(
        is_new=True,
        board_number="820-02016",
        allow_expand=True,
        memory_root=tmp_path,
        slug="macbook-air-m1",
    )
    assert result is True


def test_should_not_generate_when_not_new(tmp_path):
    """Existing repair reuse (is_new=False) → skip regardless of board_number."""
    result = _should_autogenerate_delta(
        is_new=False,
        board_number="820-02016",
        allow_expand=True,
        memory_root=tmp_path,
        slug="macbook-air-m1",
    )
    assert result is False


def test_should_not_generate_when_no_board_number(tmp_path):
    """No board_number supplied → skip."""
    result = _should_autogenerate_delta(
        is_new=True,
        board_number=None,
        allow_expand=True,
        memory_root=tmp_path,
        slug="macbook-air-m1",
    )
    assert result is False


def test_should_not_generate_when_board_number_empty_string(tmp_path):
    """Empty board_number → skip (normalize_board_number returns '')."""
    result = _should_autogenerate_delta(
        is_new=True,
        board_number="",
        allow_expand=True,
        memory_root=tmp_path,
        slug="macbook-air-m1",
    )
    assert result is False


def test_should_not_generate_when_not_allowed(tmp_path):
    """allow_expand=False (managed, non-Pro tenant) → skip."""
    result = _should_autogenerate_delta(
        is_new=True,
        board_number="820-02016",
        allow_expand=False,
        memory_root=tmp_path,
        slug="macbook-air-m1",
    )
    assert result is False


def test_should_not_generate_when_delta_already_exists(tmp_path):
    """Delta already on disk → no respend."""
    # Write a real delta file so read_delta finds it.
    delta_dir = tmp_path / "macbook-air-m1" / "board_deltas"
    delta_dir.mkdir(parents=True)
    existing = DeltaBoard(
        device_label="MacBook Air M1",
        board_number="820-02016",
        coverage="thin",
    )
    (delta_dir / "820-02016.json").write_text(existing.model_dump_json(), encoding="utf-8")

    result = _should_autogenerate_delta(
        is_new=True,
        board_number="820-02016",
        allow_expand=True,
        memory_root=tmp_path,
        slug="macbook-air-m1",
    )
    assert result is False


def test_should_not_generate_when_delta_exists_unnormalized_key(tmp_path):
    """board_number with spaces/uppercase still hits the normalized file."""
    delta_dir = tmp_path / "macbook-air-m1" / "board_deltas"
    delta_dir.mkdir(parents=True)
    existing = DeltaBoard(
        device_label="MacBook Air M1",
        board_number="820-02016",
        coverage="rich",
    )
    (delta_dir / "820-02016.json").write_text(existing.model_dump_json(), encoding="utf-8")

    result = _should_autogenerate_delta(
        is_new=True,
        board_number=" 820 02016 ",  # un-normalized variant
        allow_expand=True,
        memory_root=tmp_path,
        slug="macbook-air-m1",
    )
    assert result is False


# ---------------------------------------------------------------------------
# Integration tests — POST /pipeline/repairs schedules the task correctly
# ---------------------------------------------------------------------------


async def _fake_pipeline(device_label, **kwargs):
    """Minimal stub for generate_knowledge_pack — emits no events, returns None."""
    pass


def _make_fixed_delta() -> DeltaBoard:
    return DeltaBoard(
        device_label="MacBook Air M1",
        board_number="820-02016",
        coverage="thin",
    )


def _capture_create_task():
    """Return a (mock, scheduled_coroutines) pair.

    The mock replaces ``asyncio.create_task`` in the repairs module.
    Each scheduled coroutine is closed immediately (to avoid "coroutine
    was never awaited" ResourceWarnings) and appended to the list.
    """
    scheduled: list = []

    def fake_create_task(coro, **kwargs):
        scheduled.append(coro)
        coro.close()  # suppress ResourceWarning
        # Return a no-op future so callers that inspect the task don't blow up.
        fut: asyncio.Future = asyncio.Future()
        fut.cancel()
        return fut

    return fake_create_task, scheduled


def test_new_repair_with_board_number_and_allowed_schedules_delta(memory_root, client):
    """New repair + board_number + allow_expand=True → a background task is scheduled."""
    fake_ct, scheduled = _capture_create_task()

    with (
        patch("api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)),
        patch("api.pipeline.routes.repairs.asyncio.create_task", new=fake_ct),
    ):
        res = client.post(
            "/pipeline/repairs",
            data={
                "device_label": "MacBook Air M1",
                "symptom": "no POST beep",
                "device_slug": "macbook-air-m1",
                "board_number": "820-02016",
                "allow_expand": "true",
            },
        )

    assert res.status_code == 200
    # The pipeline build also uses create_task (via _register_build) AND our
    # delta task. At minimum one create_task call must be the delta task.
    delta_tasks = [c for c in scheduled if "autogenerate_delta" in c.__qualname__]
    assert delta_tasks, (
        f"No delta task was scheduled. All scheduled coroutines: "
        f"{[c.__qualname__ for c in scheduled]}"
    )


def test_new_repair_with_board_number_not_allowed_skips_delta(memory_root, client):
    """allow_expand=False (non-Pro in managed) → NO delta background task scheduled."""
    fake_ct, scheduled = _capture_create_task()

    with (
        patch("api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)),
        patch("api.pipeline.routes.repairs.asyncio.create_task", new=fake_ct),
    ):
        res = client.post(
            "/pipeline/repairs",
            data={
                "device_label": "MacBook Air M1",
                "symptom": "no POST beep",
                "device_slug": "macbook-air-m1",
                "board_number": "820-02016",
                "allow_expand": "false",
            },
        )

    assert res.status_code == 200
    delta_tasks = [c for c in scheduled if "autogenerate_delta" in c.__qualname__]
    assert not delta_tasks, "Delta task should NOT be scheduled when allow_expand=False"


def test_new_repair_without_board_number_skips_delta(memory_root, client):
    """No board_number → no delta background task scheduled."""
    fake_ct, scheduled = _capture_create_task()

    with (
        patch("api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)),
        patch("api.pipeline.routes.repairs.asyncio.create_task", new=fake_ct),
    ):
        res = client.post(
            "/pipeline/repairs",
            data={
                "device_label": "MacBook Air M1",
                "symptom": "no POST beep",
                "device_slug": "macbook-air-m1",
                # board_number intentionally omitted
            },
        )

    assert res.status_code == 200
    delta_tasks = [c for c in scheduled if "autogenerate_delta" in c.__qualname__]
    assert not delta_tasks, "Delta task should NOT be scheduled when board_number is absent"


def test_new_repair_delta_already_on_disk_skips_generation(memory_root, client):
    """Delta already exists → no delta background task scheduled (no respend)."""
    # Pre-seed the delta on disk.
    delta_dir = memory_root / "macbook-air-m1" / "board_deltas"
    delta_dir.mkdir(parents=True)
    existing = DeltaBoard(device_label="MacBook Air M1", board_number="820-02016", coverage="rich")
    (delta_dir / "820-02016.json").write_text(existing.model_dump_json(), encoding="utf-8")

    fake_ct, scheduled = _capture_create_task()

    with (
        patch("api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)),
        patch("api.pipeline.routes.repairs.asyncio.create_task", new=fake_ct),
    ):
        res = client.post(
            "/pipeline/repairs",
            data={
                "device_label": "MacBook Air M1",
                "symptom": "no POST beep",
                "device_slug": "macbook-air-m1",
                "board_number": "820-02016",
                "allow_expand": "true",
            },
        )

    assert res.status_code == 200
    delta_tasks = [c for c in scheduled if "autogenerate_delta" in c.__qualname__]
    assert not delta_tasks, "Delta task should NOT be scheduled when delta already exists on disk"


def test_repair_returns_immediately_delta_is_background(memory_root, client):
    """The POST response must be immediate — the delta task does not block it.

    We verify this by checking that the create_task call happened (i.e., the
    coroutine was SUBMITTED but NOT awaited inline) during the request.
    """
    fake_ct, scheduled = _capture_create_task()

    with (
        patch("api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)),
        patch("api.pipeline.routes.repairs.asyncio.create_task", new=fake_ct),
    ):
        res = client.post(
            "/pipeline/repairs",
            data={
                "device_label": "MacBook Air M1",
                "symptom": "no POST beep",
                "device_slug": "macbook-air-m1",
                "board_number": "820-02016",
                "allow_expand": "true",
            },
        )

    assert res.status_code == 200
    # The delta coroutine must appear in the scheduled list: it was submitted via
    # create_task, NOT awaited.  If the code awaited it directly the mock would
    # not have been called and the POST would have blocked on the slow generator.
    delta_tasks = [c for c in scheduled if "autogenerate_delta" in c.__qualname__]
    assert delta_tasks, "Delta generation must be fire-and-forget via create_task, not awaited inline"
