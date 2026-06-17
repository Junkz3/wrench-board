"""POST /pipeline/resolve-device — the cloud resolves a free label to a canonical
device identity (or gets the ambiguous candidate menu) BEFORE gating (T9a Phase B)."""
import asyncio

from api.pipeline.device_registry import JsonDeviceRegistryStore


def test_resolve_endpoint_flags_ambiguous(memory_root, client):
    store = JsonDeviceRegistryStore(memory_root)
    asyncio.run(store.upsert(canonical_key="820-2533", family="mbp15", aliases=[
        {"value": "820-2533", "kind": "board"}, {"value": "MacBook Pro 15", "kind": "marketing"}]))
    asyncio.run(store.upsert(canonical_key="820-3787", family="mbp15", aliases=[
        {"value": "820-3787", "kind": "board"}, {"value": "MacBook Pro 15", "kind": "marketing"}]))

    res = client.post("/pipeline/resolve-device", json={"device_label": "MacBook Pro 15"})
    assert res.status_code == 200
    body = res.json()
    assert body["ambiguous"] is True
    assert sorted(c["device_slug"] for c in body["candidates"]) == ["820-2533", "820-3787"]


def test_resolve_endpoint_returns_board_canonical(memory_root, client):
    res = client.post("/pipeline/resolve-device", json={"device_label": "MacBook Pro A1286 820-2533"})
    body = res.json()
    assert body["canonical_slug"] == "820-2533"
    assert body["ambiguous"] is False
    assert body["candidates"] == []


def test_resolve_endpoint_honors_explicit_slug(memory_root, client):
    res = client.post("/pipeline/resolve-device", json={"device_label": "whatever", "device_slug": "pinned"})
    body = res.json()
    assert body["canonical_slug"] == "pinned"
    assert body["ambiguous"] is False
