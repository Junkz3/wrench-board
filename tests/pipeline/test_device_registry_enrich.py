"""register_from_registry: post-build enrichment of a fiche from the Registry's
DeviceTaxonomy — the bridge that lets cross-facet inputs (board# ↔ model) dedupe."""
import pytest

from api.pipeline.device_registry import (
    JsonDeviceRegistryStore,
    register_from_registry,
    resolve_device,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def store(tmp_path):
    return JsonDeviceRegistryStore(tmp_path)


async def test_enriches_fiche_with_discovered_facets(store):
    # Pack was built under a marketing-only slug (no board# was typed).
    await store.upsert(canonical_key="macbook-pro-a1286",
                       aliases=[{"value": "MacBook Pro A1286", "kind": "marketing"}])
    registry = {
        "device_label": "MacBook Pro 15-inch (2011)",
        "taxonomy": {"brand": "Apple", "model": "MacBook Pro 15",
                     "version": "A1286 / 820-2533", "form_factor": "logic board",
                     "device_kind": "laptop"},
    }
    await register_from_registry(store, "macbook-pro-a1286", registry)

    # A later input by the board# now resolves to the SAME pack — cross-facet dedup.
    res = await resolve_device("820-2533", store)
    assert res["canonical_slug"] == "macbook-pro-a1286"
    res2 = await resolve_device("A1286", store)
    assert res2["canonical_slug"] == "macbook-pro-a1286"


async def test_sets_family_from_brand_model(store):
    registry = {"device_label": "X", "taxonomy": {"brand": "Apple", "model": "MacBook Pro 15",
                                                  "version": "A1398"}}
    await store.upsert(canonical_key="820-3787", aliases=[{"value": "820-3787", "kind": "board"}])
    ident = await register_from_registry(store, "820-3787", registry)
    assert ident["family"] == "apple-macbook-pro-15"


async def test_conflicting_strong_id_does_not_raise(store):
    # board 820-2533 already owned elsewhere; enriching another fiche with it must
    # degrade (best-effort), never break the pipeline.
    await store.upsert(canonical_key="owner", aliases=[{"value": "820-2533", "kind": "board"}])
    await store.upsert(canonical_key="newpack", aliases=[{"value": "New", "kind": "marketing"}])
    registry = {"device_label": "thing", "taxonomy": {"brand": "Apple", "model": "MBP",
                                                      "version": "820-2533"}}
    ident = await register_from_registry(store, "newpack", registry)  # must not raise
    assert ident is None or ident["canonicalKey"] == "newpack"
