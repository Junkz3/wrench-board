"""find_cousin_packs: sibling boards (same family) that carry usable data — the
agent's fallback knowledge source when no exact graph exists (T9a Phase B)."""
import pytest

from api.pipeline.device_registry import JsonDeviceRegistryStore, find_cousin_packs

pytestmark = pytest.mark.asyncio


def _seed_pack(memory_root, slug, *, graph=False, registry=False):
    d = memory_root / slug
    d.mkdir(parents=True, exist_ok=True)
    if graph:
        (d / "electrical_graph.json").write_text("{}", encoding="utf-8")
    if registry:
        base = d / "baseline"
        base.mkdir(exist_ok=True)
        (base / "registry.json").write_text("{}", encoding="utf-8")


@pytest.fixture
def store(tmp_path):
    return JsonDeviceRegistryStore(tmp_path)


async def test_returns_family_cousins_with_data(store, tmp_path):
    await store.upsert(canonical_key="820-2533", family="mbp15", aliases=[{"value": "820-2533", "kind": "board"}])
    await store.upsert(canonical_key="820-3787", family="mbp15", aliases=[{"value": "820-3787", "kind": "board"}])
    await store.upsert(canonical_key="820-9999", family="mbp15", aliases=[{"value": "820-9999", "kind": "board"}])
    await store.upsert(canonical_key="imac-board", family="imac", aliases=[{"value": "820-0000", "kind": "board"}])
    _seed_pack(tmp_path, "820-3787", graph=True)   # cousin with a graph → included
    _seed_pack(tmp_path, "820-9999")               # cousin, no data → excluded
    _seed_pack(tmp_path, "imac-board", graph=True)  # other family → excluded

    cousins = await find_cousin_packs(store, tmp_path, "820-2533")
    assert [c["slug"] for c in cousins] == ["820-3787"]
    assert cousins[0]["has_graph"] is True
    assert cousins[0]["family"] == "mbp15"


async def test_includes_registry_only_cousin_without_graph(store, tmp_path):
    await store.upsert(canonical_key="820-2533", family="mbp15", aliases=[{"value": "820-2533", "kind": "board"}])
    await store.upsert(canonical_key="820-3787", family="mbp15", aliases=[{"value": "820-3787", "kind": "board"}])
    _seed_pack(tmp_path, "820-3787", registry=True)

    cousins = await find_cousin_packs(store, tmp_path, "820-2533")
    assert [c["slug"] for c in cousins] == ["820-3787"]
    assert cousins[0]["has_graph"] is False


async def test_excludes_self(store, tmp_path):
    await store.upsert(canonical_key="820-2533", family="mbp15", aliases=[{"value": "820-2533", "kind": "board"}])
    _seed_pack(tmp_path, "820-2533", graph=True)
    assert await find_cousin_packs(store, tmp_path, "820-2533") == []


async def test_no_family_returns_empty(store, tmp_path):
    await store.upsert(canonical_key="820-2533", aliases=[{"value": "820-2533", "kind": "board"}])
    assert await find_cousin_packs(store, tmp_path, "820-2533") == []


async def test_unknown_slug_returns_empty(store, tmp_path):
    assert await find_cousin_packs(store, tmp_path, "nope") == []
