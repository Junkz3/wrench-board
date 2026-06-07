"""GET /api/macros/{slug}/{repair_id}/{filename} serves macro images."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import config, main


def _seed_macro(memory_root: Path, slug: str, repair_id: str, filename: str, content: bytes) -> Path:
    macros_dir = memory_root / slug / "repairs" / repair_id / "macros"
    macros_dir.mkdir(parents=True, exist_ok=True)
    path = macros_dir / filename
    path.write_bytes(content)
    return path


@pytest.fixture
def temp_memory_root(monkeypatch, tmp_path: Path) -> Path:
    """Patch settings.memory_root to a tmp dir for the duration of the test."""
    settings = config.get_settings()
    original = settings.memory_root
    # Pydantic settings instances are mutable on the model_dump side ; assign
    # via __setattr__ to bypass any frozen wrapper.
    object.__setattr__(settings, "memory_root", str(tmp_path))
    try:
        yield tmp_path
    finally:
        object.__setattr__(settings, "memory_root", original)


def test_macros_route_serves_jpeg(temp_memory_root: Path):
    bytes_ = b"\xff\xd8\xff\xe0fake_jpeg"
    _seed_macro(temp_memory_root, "iphone-x", "R1", "1745704812_manual.jpg", bytes_)

    with TestClient(main.app) as client:
        res = client.get("/api/macros/iphone-x/R1/1745704812_manual.jpg")
    assert res.status_code == 200
    assert res.content == bytes_
    assert res.headers["content-type"] == "image/jpeg"


def test_macros_route_404_on_missing(temp_memory_root: Path):
    with TestClient(main.app) as client:
        res = client.get("/api/macros/iphone-x/R1/does_not_exist.png")
    assert res.status_code == 404


def test_macros_route_blocks_path_traversal(temp_memory_root: Path):
    with TestClient(main.app) as client:
        res = client.get("/api/macros/iphone-x/R1/..%2F..%2Fetc%2Fpasswd")
    # FastAPI URL-decodes %2F into / before routing — the request typically
    # 404s because the path no longer matches the route. Either 400 (our
    # validation kicked) or 404 (no route match) is acceptable — both block.
    assert res.status_code in (400, 404)
