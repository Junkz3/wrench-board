from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.stock.schemas import PartsIndex, PartsIndexEntry


@pytest.fixture
def client(tmp_path, monkeypatch):
    memory = tmp_path / "memory"
    (memory / "_stock").mkdir(parents=True)
    (memory / "iphone-x").mkdir()
    monkeypatch.setattr("api.stock.store._memory_root", lambda: memory)
    monkeypatch.setattr("api.stock.store._stock_root", lambda: memory / "_stock")
    monkeypatch.setattr("api.stock.search._memory_root", lambda: memory)
    # The router uses get_settings().memory_root in two places (parts list + has_pi check).
    # Patch the helper functions in router.py too:
    monkeypatch.setattr("api.stock.router._memory_root", lambda: memory, raising=False)
    monkeypatch.setattr("api.stock.tools._memory_root", lambda: memory, raising=False)

    idx = PartsIndex(
        schema_version="1.0", device_slug="iphone-x",
        generated_at=datetime.now(UTC),
        source_electrical_graph_hash="x" * 64,
        entries={
            "C1": PartsIndexEntry(
                refdes="C1", type="capacitor", kind="passive_c",
                value_canonical="0.1uF", value_raw="0.1uF", package="0402",
                mpn=None, voltage_rating=25.0, tolerance=None,
                role_in_design="decoupling",
                safety_class="tolerant_with_warning",
                criticality_in_design="low", pages=[1],
            ),
        },
    )
    (memory / "iphone-x" / "parts_index.json").write_text(idx.model_dump_json())
    return TestClient(app)


def test_create_donor_returns_donor_id(client):
    r = client.post("/api/stock/donors", json={
        "device_slug": "iphone-x",
        "label": "iPhone X test",
        "condition": "donor_only",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["donor_id"].startswith("iphone-x-donor-")


def test_create_donor_unknown_slug_returns_404(client):
    r = client.post("/api/stock/donors", json={
        "device_slug": "does-not-exist",
        "label": "ghost",
    })
    assert r.status_code == 404


def test_list_donors(client):
    client.post("/api/stock/donors", json={"device_slug": "iphone-x", "label": "A"})
    r = client.get("/api/stock/donors")
    assert r.status_code == 200
    body = r.json()
    assert len(body["donors"]) == 1
    assert body["donors"][0]["device_slug"] == "iphone-x"
    assert body["donors"][0]["parts_total"] == 1
    assert body["donors"][0]["parts_available"] == 1
    assert body["donors"][0]["has_parts_index"] is True


def test_stock_is_owner_scoped_across_tenants(client):
    """Multi-tenant front-door: the X-Owner-Ref header partitions the inventory.
    A tenant only ever sees / deletes / consumes its OWN donors. (Closes the
    cross-tenant stock leak.)"""
    A = {"X-Owner-Ref": "tenant-a"}
    B = {"X-Owner-Ref": "tenant-b"}

    created = client.post("/api/stock/donors", json={"device_slug": "iphone-x", "label": "A board"}, headers=A)
    assert created.status_code == 200
    a_donor = created.json()["donor_id"]

    # A sees its donor; B's inventory is empty.
    assert [d["donor_id"] for d in client.get("/api/stock/donors", headers=A).json()["donors"]] == [a_donor]
    assert client.get("/api/stock/donors", headers=B).json()["donors"] == []

    # B cannot read parts, delete, or consume A's donor — it does not exist for B.
    assert client.get(f"/api/stock/donors/{a_donor}/parts", headers=B).status_code == 404
    assert client.delete(f"/api/stock/donors/{a_donor}", headers=B).status_code == 404
    assert client.post(f"/api/stock/donors/{a_donor}/consume", json={"refdes": "C1"}, headers=B).status_code == 404

    # B's search never returns A's parts, even on a matching query.
    sB = client.post("/api/stock/search", json={"type": "capacitor"}, headers=B).json()
    assert sB.get("exact_matches", []) == []

    # A's donor is untouched and A can operate on it.
    assert client.delete(f"/api/stock/donors/{a_donor}", headers=A).status_code == 200


def test_stock_without_owner_ref_uses_the_global_inventory(client):
    """Standalone / self-host (no header) keeps the single global inventory —
    backward-compatible behaviour."""
    client.post("/api/stock/donors", json={"device_slug": "iphone-x", "label": "self-host"})
    assert len(client.get("/api/stock/donors").json()["donors"]) == 1
    # A scoped tenant does NOT see the global (ownerless) donor.
    assert client.get("/api/stock/donors", headers={"X-Owner-Ref": "tenant-a"}).json()["donors"] == []


def test_consume_endpoint(client):
    r = client.post("/api/stock/donors", json={"device_slug": "iphone-x", "label": "A"})
    donor_id = r.json()["donor_id"]
    r = client.post(f"/api/stock/donors/{donor_id}/consume",
                    json={"refdes": "C1", "notes": "test"})
    assert r.status_code == 200


def test_search_endpoint(client):
    client.post("/api/stock/donors", json={"device_slug": "iphone-x", "label": "A"})
    r = client.post("/api/stock/search", json={
        "type": "capacitor", "value_canonical": "0.1uF",
        "package": "0402", "requested_role": "decoupling",
    })
    assert r.status_code == 200
    body = r.json()
    assert len(body["exact_matches"]) == 1
    assert body["exact_matches"][0]["refdes"] == "C1"


def test_delete_donor(client):
    r = client.post("/api/stock/donors", json={"device_slug": "iphone-x", "label": "A"})
    donor_id = r.json()["donor_id"]
    r = client.delete(f"/api/stock/donors/{donor_id}")
    assert r.status_code == 200
    r = client.get("/api/stock/donors")
    assert len(r.json()["donors"]) == 0


def test_list_donor_parts(client):
    r = client.post("/api/stock/donors", json={"device_slug": "iphone-x", "label": "A"})
    donor_id = r.json()["donor_id"]
    r = client.get(f"/api/stock/donors/{donor_id}/parts")
    assert r.status_code == 200
    body = r.json()
    assert body["parts"][0]["refdes"] == "C1"
    assert body["parts"][0]["available"] is True
    # consume one and verify it flips
    client.post(f"/api/stock/donors/{donor_id}/consume", json={"refdes": "C1"})
    r = client.get(f"/api/stock/donors/{donor_id}/parts")
    body = r.json()
    assert body["parts"][0]["available"] is False


def test_tool_stock_list_donors_shape(client):
    client.post("/api/stock/donors", json={"device_slug": "iphone-x", "label": "A"})
    from api.stock.tools import stock_list_donors as tool
    out = tool()
    assert "donors" in out
    assert len(out["donors"]) == 1
    assert "parts_available" in out["donors"][0]


def test_tool_stock_search_shape(client):
    client.post("/api/stock/donors", json={"device_slug": "iphone-x", "label": "A"})
    from api.stock.tools import stock_search as tool
    out = tool({"type": "capacitor", "value_canonical": "0.1uF",
                "package": "0402", "requested_role": "decoupling"})
    assert "exact_matches" in out
    assert len(out["exact_matches"]) == 1
