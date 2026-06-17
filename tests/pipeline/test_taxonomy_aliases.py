"""GET /pipeline/taxonomy carries each pack's carnet aliases so the new-repair
autocomplete matches by board# / Apple model / EMC, not just the label (T9a Phase B)."""
import asyncio
import json

from api.pipeline.device_registry import JsonDeviceRegistryStore


def _build_pack(memory_root, slug, *, brand, model, version):
    d = memory_root / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "registry.json").write_text(
        json.dumps({"device_label": f"{brand} {model}",
                    "taxonomy": {"brand": brand, "model": model, "version": version}}),
        encoding="utf-8",
    )


def test_taxonomy_entry_includes_carnet_aliases(memory_root, client):
    _build_pack(memory_root, "820-2533", brand="Apple", model="MacBook Pro 15", version="A1286")
    store = JsonDeviceRegistryStore(memory_root)
    asyncio.run(store.upsert(canonical_key="820-2533", aliases=[
        {"value": "820-2533", "kind": "board"},
        {"value": "A1286", "kind": "apple_model"},
        {"value": "K19i", "kind": "codename"},
    ]))

    res = client.get("/pipeline/taxonomy")
    assert res.status_code == 200
    entry = res.json()["brands"]["Apple"]["MacBook Pro 15"][0]
    assert "820-2533" in entry["aliases"]
    assert "A1286" in entry["aliases"]
    assert "K19i" in entry["aliases"]


def test_taxonomy_entry_aliases_empty_without_carnet(memory_root, client):
    _build_pack(memory_root, "some-pack", brand="MNT", model="Reform", version="r2")
    res = client.get("/pipeline/taxonomy")
    entry = res.json()["brands"]["MNT"]["Reform"][0]
    assert entry["aliases"] == []
