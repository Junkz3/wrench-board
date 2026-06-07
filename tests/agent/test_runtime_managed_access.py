from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_memory_store_attached_read_only(monkeypatch, tmp_path):
    """Session-create payload must attach the memory store with access='read_only'."""
    from api.agent import runtime_managed as rm

    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    ws.receive_text = AsyncMock(side_effect=Exception("stop early"))

    class FakeSettings:
        anthropic_api_key = "sk-test"
        anthropic_max_retries = 5
        memory_root = str(tmp_path)
        ma_memory_store_enabled = True
    monkeypatch.setattr(rm, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(rm, "load_managed_ids", lambda: {"environment_id": "env_x"})
    monkeypatch.setattr(rm, "get_agent", lambda ids, tier: {"id": "agent_x", "version": 1, "model": "claude-haiku-4-5"})

    async def fake_ensure(client, slug):
        return "memstore_999"
    monkeypatch.setattr(rm, "ensure_memory_store", fake_ensure)

    captured = {}
    class FakeSessions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            raise Exception("stop here")  # bail before streaming
        async def retrieve(self, sid):
            raise Exception("none")
    class FakeBeta:
        sessions = FakeSessions()
    class FakeClient:
        beta = FakeBeta()
    monkeypatch.setattr(rm, "AsyncAnthropic", lambda **_kw: FakeClient())

    # Run until session create raises; we only care about the payload.
    try:
        await rm.run_diagnostic_session_managed(ws, "demo", tier="fast")
    except Exception:
        pass

    assert "resources" in captured, "memory store must be attached"
    resource = captured["resources"][0]
    assert resource["type"] == "memory_store"
    assert resource["access"] == "read_only", f"expected read_only, got {resource['access']!r}"
