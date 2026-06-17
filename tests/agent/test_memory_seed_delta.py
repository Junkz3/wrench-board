from pathlib import Path
from api.pipeline.board_delta.schemas import DeltaBoard
from api.pipeline.board_delta.store import write_delta
from api.agent.memory_seed import build_board_delta_block


def test_block_none_when_no_delta(tmp_path: Path):
    assert build_board_delta_block(memory_root=tmp_path, device_slug="x", board_number=None) is None


def test_block_none_when_coverage_none(tmp_path: Path):
    write_delta(memory_root=tmp_path, device_slug="x",
                delta=DeltaBoard(device_label="X", board_number="b1", coverage="none"))
    assert build_board_delta_block(memory_root=tmp_path, device_slug="x", board_number="b1") is None


def test_block_text_when_rich(tmp_path: Path):
    write_delta(memory_root=tmp_path, device_slug="x",
                delta=DeltaBoard(device_label="X", board_number="b1", coverage="rich",
                                 signature_ics=[{"part": "ISL9240", "role": "charger", "source_url": "http://z"}]))
    block = build_board_delta_block(memory_root=tmp_path, device_slug="x", board_number="b1")
    assert block is not None
    assert "ISL9240" in block
    assert "not validated" in block.lower()
