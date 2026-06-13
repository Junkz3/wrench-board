"""JSON device-registry store (self-host). Honors the same contract as the
cloud's Postgres-backed registry — async, camelCase identity shape."""
import pytest

from api.pipeline.device_registry import (
    DeviceRegistryConflict,
    JsonDeviceRegistryStore,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def store(tmp_path):
    return JsonDeviceRegistryStore(tmp_path)


async def test_upsert_and_lookup_by_any_alias(store):
    await store.upsert(
        canonical_key="820-2533",
        family="macbook-pro-15",
        provenance={"source": "scout"},
        aliases=[
            {"value": "820-2533", "kind": "board"},
            {"value": "A1286", "kind": "apple_model"},
            {"value": "K19i", "kind": "codename"},
            {"value": 'MacBook Pro 15" 2011', "kind": "marketing"},
        ],
    )
    by_board = await store.lookup(["820-2533"])
    by_model = await store.lookup(["a1286"])
    by_marketing = await store.lookup(['MacBook Pro 15" 2011'])
    assert [i["canonicalKey"] for i in by_board] == ["820-2533"]
    assert [i["canonicalKey"] for i in by_model] == ["820-2533"]
    assert [i["canonicalKey"] for i in by_marketing] == ["820-2533"]
    assert by_board[0]["facets"]["board"] == ["820-2533"]
    assert by_board[0]["family"] == "macbook-pro-15"


async def test_upsert_idempotent_unions(store):
    await store.upsert(canonical_key="820-2533", aliases=[{"value": "820-2533", "kind": "board"}])
    await store.upsert(canonical_key="820-2533", aliases=[{"value": "EMC 2353-1", "kind": "emc"}])
    assert len(await store.list()) == 1
    by_emc = await store.lookup(["emc 2353-1"])
    assert by_emc[0]["facets"]["board"] == ["820-2533"]
    assert by_emc[0]["facets"]["emc"] == ["EMC 2353-1"]


async def test_soft_token_fans_out(store):
    await store.upsert(canonical_key="820-2533", family="mbp15", aliases=[
        {"value": "820-2533", "kind": "board"}, {"value": "MacBook Pro 15", "kind": "marketing"}])
    await store.upsert(canonical_key="820-3787", family="mbp15", aliases=[
        {"value": "820-3787", "kind": "board"}, {"value": "MacBook Pro 15", "kind": "marketing"}])
    candidates = await store.lookup(["MacBook Pro 15"])
    assert sorted(i["canonicalKey"] for i in candidates) == ["820-2533", "820-3787"]


async def test_strong_alias_collision_raises(store):
    await store.upsert(canonical_key="820-2533", aliases=[{"value": "820-2533", "kind": "board"}])
    with pytest.raises(DeviceRegistryConflict):
        await store.upsert(canonical_key="other", aliases=[{"value": "820-2533", "kind": "board"}])


async def test_same_strong_alias_same_identity_ok(store):
    await store.upsert(canonical_key="820-2533", aliases=[{"value": "820-2533", "kind": "board"}])
    again = await store.upsert(canonical_key="820-2533", aliases=[{"value": "820-2533", "kind": "board"}])
    assert again["canonicalKey"] == "820-2533"


async def test_merge_repoints_and_tombstones(store):
    await store.upsert(canonical_key="macbook-pro-a1286", aliases=[{"value": "A1286", "kind": "apple_model"}])
    await store.upsert(canonical_key="820-2533", aliases=[{"value": "820-2533", "kind": "board"}])
    target = await store.merge(source_key="macbook-pro-a1286", target_key="820-2533", by="op")
    assert target["canonicalKey"] == "820-2533"
    assert "A1286" in target["facets"]["apple_model"]
    assert [i["canonicalKey"] for i in await store.lookup(["a1286"])] == ["820-2533"]
    src = await store.get_by_canonical_key("macbook-pro-a1286")
    assert src["status"] == "merged"
    assert src["mergedInto"] == "820-2533"


async def test_merge_conflicting_strong_ids_raises(store):
    await store.upsert(canonical_key="820-2533", aliases=[{"value": "820-2533", "kind": "board"}])
    await store.upsert(canonical_key="820-3787", aliases=[{"value": "820-3787", "kind": "board"}])
    with pytest.raises(DeviceRegistryConflict):
        await store.merge(source_key="820-2533", target_key="820-3787")


async def test_revoke_excludes_and_frees_alias(store):
    await store.upsert(canonical_key="820-2533", aliases=[{"value": "820-2533", "kind": "board"}])
    await store.revoke(canonical_key="820-2533", by="op")
    assert await store.lookup(["820-2533"]) == []
    # the strong alias is freed — re-registerable on a new identity
    again = await store.upsert(canonical_key="new", aliases=[{"value": "820-2533", "kind": "board"}])
    assert again["canonicalKey"] == "new"


async def test_list_by_family(store):
    await store.upsert(canonical_key="820-2533", family="mbp15", aliases=[{"value": "820-2533", "kind": "board"}])
    await store.upsert(canonical_key="820-3787", family="mbp15", aliases=[{"value": "820-3787", "kind": "board"}])
    await store.upsert(canonical_key="820-9999", family="imac", aliases=[{"value": "820-9999", "kind": "board"}])
    cousins = await store.list_by_family("mbp15")
    assert sorted(i["canonicalKey"] for i in cousins) == ["820-2533", "820-3787"]


async def test_persists_across_instances(tmp_path):
    s1 = JsonDeviceRegistryStore(tmp_path)
    await s1.upsert(canonical_key="820-2533", aliases=[{"value": "820-2533", "kind": "board"}])
    s2 = JsonDeviceRegistryStore(tmp_path)
    assert [i["canonicalKey"] for i in await s2.lookup(["820-2533"])] == ["820-2533"]
