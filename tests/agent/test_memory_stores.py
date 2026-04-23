"""Tests for `api.agent.memory_stores` — the shared Managed-Agents memory
store helper used by `memory_seed.py`, `field_reports.py`, and opened
directly by `runtime_managed.py` at session start.

The SDK doesn't yet expose `client.beta.memory_stores` on 0.96.0 so today
every call falls through to the HTTP path. These tests pin both paths so
the behaviour doesn't regress once the SDK catches up.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from api import config as config_mod
from api.agent import memory_stores


@pytest.fixture(autouse=True)
def reset_settings_cache(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    yield
    monkeypatch.setattr(config_mod, "_settings", None)


class _FakeHttpResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | str):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        if isinstance(self._payload, dict):
            return self._payload
        raise ValueError("no json body")


class _FakeHttpClient:
    """Minimal stand-in for httpx.AsyncClient used as an async context
    manager. Records every call and returns the scripted response."""

    def __init__(self, response: _FakeHttpResponse):
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def post(self, url: str, *, headers: dict, json: dict):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.response


def _client_without_surface() -> MagicMock:
    """Return a client whose `.beta` has no `memory_stores` attribute."""
    # Using a class instance rather than MagicMock — MagicMock auto-vivifies.
    class _Beta:
        pass

    class _Client:
        beta = _Beta()

    return _Client()  # type: ignore[return-value]


async def test_ensure_creates_store_via_http_when_sdk_absent(tmp_path, monkeypatch):
    fake_resp = _FakeHttpResponse(200, {"id": "memstore_abc123"})
    fake_http = _FakeHttpClient(fake_resp)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: fake_http)

    client = _client_without_surface()
    store_id = await memory_stores.ensure_memory_store(client, "demo-pi")

    assert store_id == "memstore_abc123"
    assert len(fake_http.calls) == 1
    call = fake_http.calls[0]
    assert call["url"].endswith("/memory_stores")
    assert call["headers"]["anthropic-beta"] == "managed-agents-2026-04-01"
    assert call["json"]["name"] == "microsolder-demo-pi"
    # The id is persisted so the next call doesn't hit the network.
    meta = (tmp_path / "demo-pi" / "managed.json").read_text()
    assert "memstore_abc123" in meta


async def test_ensure_reuses_cached_store_id(tmp_path, monkeypatch):
    (tmp_path / "demo-pi").mkdir()
    (tmp_path / "demo-pi" / "managed.json").write_text(
        '{"memory_store_id": "memstore_cached", "device_slug": "demo-pi"}'
    )
    fake_http = _FakeHttpClient(_FakeHttpResponse(500, "should not be called"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: fake_http)

    client = _client_without_surface()
    store_id = await memory_stores.ensure_memory_store(client, "demo-pi")

    assert store_id == "memstore_cached"
    assert fake_http.calls == []


async def test_ensure_prefers_sdk_surface_when_present(monkeypatch):
    sdk_create = AsyncMock(return_value=MagicMock(id="memstore_from_sdk"))
    client = MagicMock()
    client.beta.memory_stores.create = sdk_create

    # HTTP path must NOT be hit when the SDK surface works.
    fake_http = _FakeHttpClient(_FakeHttpResponse(500, "unreachable"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: fake_http)

    store_id = await memory_stores.ensure_memory_store(client, "demo-pi")
    assert store_id == "memstore_from_sdk"
    sdk_create.assert_awaited_once()
    assert fake_http.calls == []


async def test_ensure_returns_none_on_http_failure(tmp_path, monkeypatch):
    fake_http = _FakeHttpClient(_FakeHttpResponse(403, "beta_not_active"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: fake_http)

    client = _client_without_surface()
    store_id = await memory_stores.ensure_memory_store(client, "denied-device")
    assert store_id is None
    # No managed.json persisted on failure — the next call can retry.
    assert not (tmp_path / "denied-device" / "managed.json").exists()


async def test_upsert_via_http_when_sdk_absent(monkeypatch):
    fake_resp = _FakeHttpResponse(200, {"content_sha256": "deadbeef"})
    fake_http = _FakeHttpClient(fake_resp)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: fake_http)

    client = _client_without_surface()
    sha = await memory_stores.upsert_memory(
        client,
        store_id="memstore_x",
        path="/knowledge/rules.json",
        content='{"rules": []}',
    )

    assert sha == "deadbeef"
    assert len(fake_http.calls) == 1
    call = fake_http.calls[0]
    assert call["url"].endswith("/memory_stores/memstore_x/memories")
    assert call["json"] == {
        "path": "/knowledge/rules.json",
        "content": '{"rules": []}',
    }


async def test_upsert_prefers_sdk_write_method(monkeypatch):
    sdk_write = AsyncMock(return_value=MagicMock(content_sha256="sha_from_sdk"))
    client = MagicMock()
    # .write is the public-beta canonical name — must be tried first.
    client.beta.memory_stores.memories.write = sdk_write

    fake_http = _FakeHttpClient(_FakeHttpResponse(500, "unreachable"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: fake_http)

    sha = await memory_stores.upsert_memory(
        client,
        store_id="memstore_y",
        path="/field_reports/r1.md",
        content="note",
    )
    assert sha == "sha_from_sdk"
    sdk_write.assert_awaited_once_with(
        memory_store_id="memstore_y",
        path="/field_reports/r1.md",
        content="note",
    )
    assert fake_http.calls == []


async def test_upsert_returns_none_on_http_failure(monkeypatch):
    fake_http = _FakeHttpClient(_FakeHttpResponse(413, "payload_too_large"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: fake_http)

    client = _client_without_surface()
    sha = await memory_stores.upsert_memory(
        client,
        store_id="memstore_x",
        path="/oversize.txt",
        content="x" * 10,
    )
    assert sha is None
