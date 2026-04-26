"""Tests for SessionState.from_device — auto-loads a board for a device slug."""

from pathlib import Path

import pytest

from api.session.state import SessionState


@pytest.fixture
def board_assets_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake board_assets/ directory, scoped to a tmp path."""
    assets = tmp_path / "board_assets"
    assets.mkdir()
    # Point SessionState.from_device at this dir via an env var the helper reads.
    monkeypatch.setenv("MICROSOLDER_BOARD_ASSETS", str(assets))
    return assets


def test_from_device_slug_with_no_file_returns_empty_session(board_assets_dir: Path) -> None:
    session = SessionState.from_device("does-not-exist")
    assert session.board is None


def test_from_device_prefers_kicad_pcb_over_brd(board_assets_dir: Path) -> None:
    """When both .kicad_pcb and .brd exist, .kicad_pcb wins."""
    repo_root = Path(__file__).resolve().parents[2]
    src = repo_root / "board_assets" / "mnt-reform-motherboard.kicad_pcb"
    if not src.exists():
        pytest.skip("fixture mnt-reform-motherboard.kicad_pcb not available")
    (board_assets_dir / "mnt-reform-motherboard.kicad_pcb").write_bytes(src.read_bytes())
    # Drop a bogus .brd next to it — if the helper picks .brd, parse will crash.
    (board_assets_dir / "mnt-reform-motherboard.brd").write_text("GARBAGE\n")

    session = SessionState.from_device("mnt-reform-motherboard")
    assert session.board is not None
    assert len(session.board.parts) > 10


def test_from_device_falls_back_to_brd_when_no_kicad_pcb(board_assets_dir: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    src = repo_root / "board_assets" / "mnt-reform-motherboard.brd"
    if not src.exists():
        pytest.skip("fixture mnt-reform-motherboard.brd not available")
    (board_assets_dir / "mnt-reform-motherboard.brd").write_bytes(src.read_bytes())

    session = SessionState.from_device("mnt-reform-motherboard")
    assert session.board is not None


def test_from_device_swallows_parse_errors(board_assets_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Corrupted file → session has no board, warning logged, no exception."""
    (board_assets_dir / "bogus.kicad_pcb").write_text("not a kicad file\n")
    import logging
    with caplog.at_level(logging.WARNING):
        session = SessionState.from_device("bogus")
    assert session.board is None
    assert any("board load failed" in rec.message.lower() for rec in caplog.records)
