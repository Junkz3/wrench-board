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


async def test_upsert_falls_back_to_update_on_409_path_conflict(monkeypatch):
    """True upsert: 409 path-conflict on POST → POST to /memories/{id}.

    The MA Memory API does NOT auto-replace on path collision — it returns
    409 memory_path_conflict_error with the conflicting_memory_id. Our
    upsert helper must detect that and fall back to an update against the
    existing memory id (POST, not PATCH — the live endpoint rejects PATCH).
    """
    create_resp = _FakeHttpResponse(
        409,
        {
            "type": "error",
            "error": {
                "type": "memory_path_conflict_error",
                "message": "path occupied",
                "conflicting_memory_id": "mem_existing_001",
                "conflicting_path": "/patterns/x.md",
            },
        },
    )
    update_resp = _FakeHttpResponse(200, {"content_sha256": "newsha"})

    sequence = iter([create_resp, update_resp])
    captured: list[tuple[str, str, dict]] = []

    class _Seq:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_e):
            return None
        async def post(self, url, *, headers, json):
            captured.append(("POST", url, json))
            return next(sequence)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: _Seq())

    client = _client_without_surface()
    sha = await memory_stores.upsert_memory(
        client, store_id="memstore_x", path="/patterns/x.md", content="new",
    )

    assert sha == "newsha"
    assert len(captured) == 2, "Expected create then update"
    _, create_url, create_body = captured[0]
    _, update_url, update_body = captured[1]
    assert create_url.endswith("/memory_stores/memstore_x/memories")
    assert create_body == {"path": "/patterns/x.md", "content": "new"}
    assert update_url.endswith("/memory_stores/memstore_x/memories/mem_existing_001")
    assert update_body == {"content": "new"}


# ---------------------------------------------------------------------------
# Layered architecture: ensure_global_store + ensure_repair_store
# ---------------------------------------------------------------------------


async def test_ensure_global_store_creates_once(tmp_path, monkeypatch):
    """Global store is created on first call, reused on second (no HTTP hit)."""
    fake_resp = _FakeHttpResponse(200, {"id": "memstore_global_patterns"})
    fake_http = _FakeHttpClient(fake_resp)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: fake_http)

    client = _client_without_surface()
    sid1 = await memory_stores.ensure_global_store(
        client, kind="patterns", description="Test patterns store",
    )
    sid2 = await memory_stores.ensure_global_store(
        client, kind="patterns", description="Test patterns store",
    )

    assert sid1 == "memstore_global_patterns"
    assert sid2 == "memstore_global_patterns"
    assert len(fake_http.calls) == 1, "Second call must reuse cached id"

    registry_file = tmp_path / "_managed" / "global.json"
    assert registry_file.exists()
    import json as _json
    registry = _json.loads(registry_file.read_text())
    assert registry["patterns"]["memory_store_id"] == "memstore_global_patterns"
    assert registry["patterns"]["name"] == "microsolder-global-patterns"


async def test_ensure_global_store_kind_validation():
    """Unknown kinds raise ValueError before any I/O."""
    client = _client_without_surface()
    with pytest.raises(ValueError, match="Unknown global store kind"):
        await memory_stores.ensure_global_store(
            client, kind="bogus", description="x",
        )


async def test_ensure_global_store_separate_kinds(tmp_path, monkeypatch):
    """patterns and playbooks live as distinct entries in the same registry."""
    responses = iter([
        _FakeHttpResponse(200, {"id": "memstore_patterns_001"}),
        _FakeHttpResponse(200, {"id": "memstore_playbooks_001"}),
    ])

    class _MultiHttp:
        def __init__(self):
            self.calls = []
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_e):
            return None
        async def post(self, url, *, headers, json):
            self.calls.append({"url": url, "json": json})
            return next(responses)

    fake_http = _MultiHttp()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: fake_http)

    client = _client_without_surface()
    p_id = await memory_stores.ensure_global_store(
        client, kind="patterns", description="P",
    )
    pb_id = await memory_stores.ensure_global_store(
        client, kind="playbooks", description="PB",
    )

    assert p_id == "memstore_patterns_001"
    assert pb_id == "memstore_playbooks_001"
    assert len(fake_http.calls) == 2

    import json as _json
    registry = _json.loads(
        (tmp_path / "_managed" / "global.json").read_text()
    )
    assert set(registry.keys()) == {"patterns", "playbooks"}


async def test_ensure_repair_store_per_repair(tmp_path, monkeypatch):
    """Per-repair store is created once per (slug, repair_id) tuple."""
    create_log: list[str] = []

    class _PerCallHttp:
        def __init__(self):
            self.calls = []
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_e):
            return None
        async def post(self, url, *, headers, json):
            create_log.append(json["name"])
            self.calls.append({"json": json})
            return _FakeHttpResponse(200, {"id": f"memstore_{json['name']}"})

    fake_http = _PerCallHttp()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: fake_http)

    client = _client_without_surface()
    a1 = await memory_stores.ensure_repair_store(
        client, device_slug="iphone-x", repair_id="R1",
    )
    a2 = await memory_stores.ensure_repair_store(
        client, device_slug="iphone-x", repair_id="R1",
    )
    b = await memory_stores.ensure_repair_store(
        client, device_slug="iphone-x", repair_id="R2",
    )

    assert a1 == a2, "Same (slug, repair_id) must reuse the same store"
    assert a1 != b, "Different repair_id must yield a distinct store"
    assert create_log == [
        "microsolder-repair-iphone-x-R1",
        "microsolder-repair-iphone-x-R2",
    ]

    import json as _json
    marker = _json.loads(
        (tmp_path / "iphone-x" / "repairs" / "R1" / "managed.json").read_text()
    )
    assert marker["memory_store_id"] == a1
    assert marker["device_slug"] == "iphone-x"
    assert marker["repair_id"] == "R1"


async def test_ensure_repair_store_returns_none_on_http_failure(
    tmp_path, monkeypatch
):
    fake_http = _FakeHttpClient(_FakeHttpResponse(403, "denied"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: fake_http)

    client = _client_without_surface()
    sid = await memory_stores.ensure_repair_store(
        client, device_slug="iphone-x", repair_id="R-bad",
    )
    assert sid is None
    assert not (
        tmp_path / "iphone-x" / "repairs" / "R-bad" / "managed.json"
    ).exists()
