import json

from fastapi.testclient import TestClient

from api.main import app


def test_pending_kind_returns_state_when_present(memory_root):
    pack = memory_root / "dev-x"
    pack.mkdir(parents=True)
    (pack / "pending_kind.json").write_text(json.dumps({
        "status": "needs_confirmation", "resolved_kind": None,
        "user_declared": "laptop_logic_board", "graph_inferred": "gpu_card",
        "confidence": 0.9, "evidence": "NVVDD + GDDR rails",
    }))
    with TestClient(app) as c:
        r = c.get("/pipeline/packs/dev-x/pending-kind")
    assert r.status_code == 200
    body = r.json()
    assert body["user_declared"] == "laptop_logic_board"
    assert body["graph_inferred"] == "gpu_card"


def test_pending_kind_404_when_none(memory_root):
    (memory_root / "dev-y").mkdir(parents=True)
    with TestClient(app) as c:
        r = c.get("/pipeline/packs/dev-y/pending-kind")
    assert r.status_code == 404


def test_pending_kind_404_when_pack_missing(memory_root):
    with TestClient(app) as c:
        r = c.get("/pipeline/packs/nope-missing/pending-kind")
    assert r.status_code == 404
    assert "No pack" in r.json()["detail"]
