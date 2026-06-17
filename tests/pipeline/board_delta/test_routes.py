from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient
from api.main import app
from api.pipeline.board_delta.schemas import DeltaBoard


@pytest.fixture
def memroot(tmp_path: Path):
    settings = MagicMock()
    settings.anthropic_api_key = "sk-ant-stub"
    settings.memory_root = str(tmp_path)
    settings.anthropic_model_main = "claude-sonnet-4-6"
    with patch("api.pipeline.routes.board_delta.get_settings", return_value=settings):
        yield tmp_path


@pytest.mark.asyncio
async def test_post_generates_stores_and_meters(memroot):
    fixed = DeltaBoard(device_label="MacBook Air M1", board_number="820-02016", coverage="rich",
                       repair_pitfalls=[{"title": "x", "detail": "y", "source_url": "http://z"}])

    async def fake_gen(**_):
        return fixed

    meter = MagicMock()
    with (
        patch("api.pipeline.routes.board_delta.generate_board_delta", new=fake_gen),
        patch("api.pipeline.routes.board_delta.report_delta_usage", new=meter),
    ):
        client = TestClient(app)
        r = client.post("/pipeline/packs/macbook-air-m1/board-delta",
                        data={"device_label": "MacBook Air M1", "board_number": "820-02016"},
                        headers={"X-Owner-Ref": "t1"})
    assert r.status_code == 200
    assert r.json()["coverage"] == "rich"
    assert (memroot / "macbook-air-m1" / "board_deltas" / "820-02016.json").exists()
    assert meter.called
    assert meter.call_args.kwargs["kind"] == "delta"
    assert meter.call_args.kwargs["owner_ref"] == "t1"


def test_get_missing_delta_404(memroot):
    client = TestClient(app)
    r = client.get("/pipeline/packs/macbook-air-m1/board-delta/nope")
    assert r.status_code == 404
