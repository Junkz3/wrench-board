import json

from fastapi.testclient import TestClient

from api.main import app


def _seed_pack(root, slug, *, device_kind=None, graph=False):
    d = root / slug
    d.mkdir(parents=True)
    registry = {
        "device_label": slug,
        "taxonomy": {"brand": "ACME", "model": "X", "version": "1",
                     "form_factor": "card", "device_kind": device_kind},
        "components": [], "signals": [],
    }
    (d / "registry.json").write_text(json.dumps(registry))
    for f in ("knowledge_graph.json", "rules.json", "dictionary.json"):
        (d / f).write_text("{}")
    if graph:
        (d / "electrical_graph.json").write_text("{}")


def test_taxonomy_exposes_graph_and_kind(memory_root):
    _seed_pack(memory_root, "acme-x", device_kind="gpu_card", graph=True)
    with TestClient(app) as c:
        tree = c.get("/pipeline/taxonomy").json()
    entry = next(e for e in tree["brands"]["ACME"]["X"] if e["device_slug"] == "acme-x")
    assert entry["has_electrical_graph"] is True
    assert entry["device_kind"] == "gpu_card"
    assert entry["complete"] is True


def test_taxonomy_defaults_when_no_graph_no_kind(memory_root):
    _seed_pack(memory_root, "acme-y", device_kind=None, graph=False)
    with TestClient(app) as c:
        tree = c.get("/pipeline/taxonomy").json()
    entry = next(e for e in tree["brands"]["ACME"]["X"] if e["device_slug"] == "acme-y")
    assert entry["has_electrical_graph"] is False
    assert entry["device_kind"] is None
