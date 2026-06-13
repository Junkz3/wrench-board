"""resolve_device: free text → canonical identity (extract → lookup → adopt/create)."""
import pytest

from api.pipeline.device_identity import slugify_label
from api.pipeline.device_registry import JsonDeviceRegistryStore, resolve_device

pytestmark = pytest.mark.asyncio


@pytest.fixture
def store(tmp_path):
    return JsonDeviceRegistryStore(tmp_path)


async def test_new_device_with_board_creates_canonical(store):
    res = await resolve_device("A1286 820-2533", store)
    assert res["canonical_slug"] == "820-2533"  # board# is the canonical key
    assert res["created"] is True
    assert res["ambiguous"] is False
    # registered for next time
    assert [i["canonicalKey"] for i in await store.lookup(["820-2533"])] == ["820-2533"]


async def test_variant_sharing_board_resolves_to_same(store):
    await resolve_device("820-2533", store)
    res = await resolve_device("board 820-2533 won't power", store)
    assert res["canonical_slug"] == "820-2533"
    assert res["created"] is False


async def test_adopts_existing_fiche_by_shared_soft_token(store):
    # Simulate a post-Scout-enriched fiche that knows A1286 maps to 820-2533.
    await store.upsert(canonical_key="820-2533", aliases=[
        {"value": "820-2533", "kind": "board"},
        {"value": "A1286", "kind": "apple_model"},
    ])
    res = await resolve_device("MacBook Pro A1286", store)
    assert res["canonical_slug"] == "820-2533"
    assert res["created"] is False


async def test_broad_marketing_term_is_ambiguous_not_merged(store):
    await store.upsert(canonical_key="820-2533", family="mbp15", aliases=[
        {"value": "820-2533", "kind": "board"}, {"value": "MacBook Pro 15", "kind": "marketing"}])
    await store.upsert(canonical_key="820-3787", family="mbp15", aliases=[
        {"value": "820-3787", "kind": "board"}, {"value": "MacBook Pro 15", "kind": "marketing"}])
    res = await resolve_device("MacBook Pro 15", store)
    assert res["ambiguous"] is True
    assert len(res["candidates"]) == 2
    # falls back to a fresh input-derived slug — never silently picks a cousin
    assert res["canonical_slug"] == slugify_label("MacBook Pro 15")


async def test_ambiguous_does_not_register_a_fiche(store):
    # A broad term that fans out must NOT persist a guessed 3rd fiche — we don't
    # know which board the tech means; the caller will disambiguate.
    await store.upsert(canonical_key="820-2533", family="mbp15", aliases=[
        {"value": "820-2533", "kind": "board"}, {"value": "MacBook Pro 15", "kind": "marketing"}])
    await store.upsert(canonical_key="820-3787", family="mbp15", aliases=[
        {"value": "820-3787", "kind": "board"}, {"value": "MacBook Pro 15", "kind": "marketing"}])
    res = await resolve_device("MacBook Pro 15", store)
    assert res["ambiguous"] is True
    assert len(await store.list()) == 2  # no new fiche registered


async def test_no_signal_falls_back_to_slug(store):
    res = await resolve_device("Some Weird Gadget", store)
    assert res["canonical_slug"] == slugify_label("Some Weird Gadget")
    assert res["created"] is True


async def test_device_slug_override_used_when_no_match(store):
    res = await resolve_device("Some Weird Gadget", store, device_slug="preexisting-pack")
    assert res["canonical_slug"] == "preexisting-pack"
