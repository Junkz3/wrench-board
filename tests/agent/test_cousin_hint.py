import pytest

from api.agent.cousin_hint import build_cousin_line
from api.pipeline.device_registry import JsonDeviceRegistryStore

pytestmark = pytest.mark.asyncio


async def test_line_names_cousin_with_graph(memory_root):
    store = JsonDeviceRegistryStore(memory_root)
    await store.upsert(canonical_key="820-2533", family="mbp15",
                       aliases=[{"value": "820-2533", "kind": "board"}])
    await store.upsert(canonical_key="820-3787", family="mbp15",
                       aliases=[{"value": "820-3787", "kind": "board"}])
    d = memory_root / "820-3787"
    d.mkdir(parents=True, exist_ok=True)
    (d / "electrical_graph.json").write_text("{}", encoding="utf-8")

    line = await build_cousin_line("820-2533")
    assert line is not None
    assert "820-3787" in line
    assert "indicative" in line.lower()


async def test_none_when_no_cousins(memory_root):
    store = JsonDeviceRegistryStore(memory_root)
    await store.upsert(canonical_key="820-2533", family="mbp15",
                       aliases=[{"value": "820-2533", "kind": "board"}])
    assert await build_cousin_line("820-2533") is None


async def test_none_on_unknown_device(memory_root):
    assert await build_cousin_line("nope") is None
