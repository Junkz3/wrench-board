from pathlib import Path
from api.pipeline.board_delta.schemas import DeltaBoard
from api.pipeline.board_delta.store import normalize_board_number, write_delta, read_delta, delta_path


def test_normalize_is_stable_and_filesystem_safe():
    assert normalize_board_number("  820-02016 ") == "820-02016"
    assert normalize_board_number("820_02016") == "820-02016"
    assert normalize_board_number("820/02016") == "820-02016"
    assert normalize_board_number("CFI-1200") == "cfi-1200"  # lowercased
    assert normalize_board_number("../etc") == "etc"  # traversal stripped


def test_write_then_read_roundtrip(tmp_path: Path):
    d = DeltaBoard(device_label="MacBook Air M1", board_number="820-02016", coverage="thin",
                   repair_pitfalls=[{"title": "x", "detail": "y", "source_url": "http://z"}])
    write_delta(memory_root=tmp_path, device_slug="macbook-air-m1", delta=d)
    p = delta_path(tmp_path, "macbook-air-m1", "820-02016")
    assert p.exists()
    back = read_delta(memory_root=tmp_path, device_slug="macbook-air-m1", board_number="820-02016")
    assert back is not None and back.coverage == "thin"


def test_read_missing_returns_none(tmp_path: Path):
    assert read_delta(memory_root=tmp_path, device_slug="x", board_number="nope") is None
