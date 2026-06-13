"""Cloud-backed device registry adapter (managed mode) + store factory."""
import pytest

from api.pipeline.device_registry import (
    CloudDeviceRegistryStore,
    DeviceRegistryConflict,
    JsonDeviceRegistryStore,
    get_device_registry_store,
)

pytestmark = pytest.mark.asyncio


class _FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


def _fake_client_factory(captured, *, status=200, payload=None):
    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, headers=None, json=None):
            captured.append({"method": "POST", "url": url, "headers": headers, "json": json})
            return _FakeResp(status, payload)

        async def get(self, url, *, headers=None, params=None):
            captured.append({"method": "GET", "url": url, "headers": headers, "params": params})
            return _FakeResp(status, payload)

    return _FakeClient


def _patch_httpx(monkeypatch, captured, *, status=200, payload=None):
    from api.pipeline import device_registry as dr
    monkeypatch.setattr(dr.httpx, "AsyncClient", _fake_client_factory(captured, status=status, payload=payload))


async def test_lookup_posts_contract_with_bearer(monkeypatch):
    captured = []
    _patch_httpx(monkeypatch, captured, payload={"candidates": [{"canonicalKey": "820-2533"}]})
    store = CloudDeviceRegistryStore("https://cloud.example/", "tok")
    out = await store.lookup(["820-2533"])
    assert out == [{"canonicalKey": "820-2533"}]
    call = captured[0]
    assert call["url"] == "https://cloud.example/internal/device-registry/lookup"
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert call["json"] == {"tokens": ["820-2533"]}


async def test_upsert_returns_identity(monkeypatch):
    captured = []
    _patch_httpx(monkeypatch, captured, payload={"identity": {"canonicalKey": "820-2533", "status": "active"}})
    store = CloudDeviceRegistryStore("https://cloud.example", "tok")
    out = await store.upsert(canonical_key="820-2533", family="mbp15",
                             aliases=[{"value": "820-2533", "kind": "board"}])
    assert out["canonicalKey"] == "820-2533"
    assert captured[0]["url"] == "https://cloud.example/internal/device-registry/identities"
    assert captured[0]["json"]["canonicalKey"] == "820-2533"
    assert captured[0]["json"]["aliases"] == [{"value": "820-2533", "kind": "board"}]


async def test_upsert_409_raises_conflict(monkeypatch):
    captured = []
    _patch_httpx(monkeypatch, captured, status=409, payload={"error": {"code": "CONFLICT"}})
    store = CloudDeviceRegistryStore("https://cloud.example", "tok")
    with pytest.raises(DeviceRegistryConflict):
        await store.upsert(canonical_key="other", aliases=[{"value": "820-2533", "kind": "board"}])


async def test_merge_posts_camelcase(monkeypatch):
    captured = []
    _patch_httpx(monkeypatch, captured, payload={"identity": {"canonicalKey": "820-2533"}})
    store = CloudDeviceRegistryStore("https://cloud.example", "tok")
    await store.merge(source_key="macbook-pro-a1286", target_key="820-2533", by="op", reason="same")
    assert captured[0]["json"] == {
        "sourceKey": "macbook-pro-a1286", "targetKey": "820-2533", "by": "op", "reason": "same",
    }


async def test_list_by_family_reads_cousins(monkeypatch):
    captured = []
    _patch_httpx(monkeypatch, captured, payload={"cousins": [{"canonicalKey": "820-3787"}]})
    store = CloudDeviceRegistryStore("https://cloud.example", "tok")
    out = await store.list_by_family("mbp15")
    assert out == [{"canonicalKey": "820-3787"}]
    assert captured[0]["url"] == "https://cloud.example/internal/device-registry/family/mbp15"


async def test_factory_picks_cloud_when_configured(monkeypatch, tmp_path):
    from api.pipeline import device_registry as dr

    class _S:
        cloud_device_registry_url = "https://cloud.example"
        cloud_device_registry_token = "tok"

    monkeypatch.setattr(dr, "get_settings", lambda: _S())
    assert isinstance(get_device_registry_store(tmp_path), CloudDeviceRegistryStore)


async def test_factory_falls_back_to_json(monkeypatch, tmp_path):
    from api.pipeline import device_registry as dr

    class _S:
        cloud_device_registry_url = ""
        cloud_device_registry_token = ""

    monkeypatch.setattr(dr, "get_settings", lambda: _S())
    assert isinstance(get_device_registry_store(tmp_path), JsonDeviceRegistryStore)
