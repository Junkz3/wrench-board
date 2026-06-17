"""Fast unit tests for the schematic renderer page-count cap.

No pdftoppm, no real PDF — pdfplumber.open is monkeypatched so these run in
milliseconds and stay in `make test`. The slow integration tests in
test_renderer.py exercise the real fixture path.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from api.pipeline.schematic.renderer import (
    SchematicPageLimitExceeded,
    _probe_pages,
    render_pages,
)


def _fake_page() -> MagicMock:
    p = MagicMock()
    p.width = 595.0
    p.height = 842.0
    p.chars = []
    p.lines = []
    return p


def _patch_pdf_and_cap(monkeypatch, page_count: int, cap: int) -> None:
    fake_pdf = MagicMock()
    fake_pdf.pages = [_fake_page() for _ in range(page_count)]

    @contextmanager
    def fake_open(_path):
        yield fake_pdf

    monkeypatch.setattr(
        "api.pipeline.schematic.renderer.pdfplumber.open", fake_open
    )
    monkeypatch.setattr(
        "api.pipeline.schematic.renderer.get_settings",
        lambda: type("S", (), {"pipeline_schematic_max_pages": cap})(),
    )


def test_probe_pages_raises_when_exceeding_cap(monkeypatch, tmp_path: Path):
    _patch_pdf_and_cap(monkeypatch, page_count=5, cap=3)
    pdf_path = tmp_path / "fake.pdf"
    pdf_path.touch()
    with pytest.raises(SchematicPageLimitExceeded) as exc_info:
        _probe_pages(pdf_path)
    assert "5 pages" in str(exc_info.value)
    assert "cap of 3" in str(exc_info.value)


def test_probe_pages_passes_when_within_cap(monkeypatch, tmp_path: Path):
    _patch_pdf_and_cap(monkeypatch, page_count=2, cap=200)
    pdf_path = tmp_path / "fake.pdf"
    pdf_path.touch()
    out = _probe_pages(pdf_path)
    assert len(out) == 2
    assert [meta["page"] for meta in out] == [1, 2]


def test_probe_pages_passes_when_exactly_at_cap(monkeypatch, tmp_path: Path):
    _patch_pdf_and_cap(monkeypatch, page_count=3, cap=3)
    pdf_path = tmp_path / "fake.pdf"
    pdf_path.touch()
    out = _probe_pages(pdf_path)
    assert len(out) == 3


def test_schematic_page_limit_exceeded_is_value_error_subclass():
    assert issubclass(SchematicPageLimitExceeded, ValueError)


def test_render_pages_uses_provided_metadata_without_reprobing(
    monkeypatch, tmp_path: Path
):
    """When metadata is supplied, render_pages must not parse the PDF itself.

    The orchestrator's single-pass extractor already produced the per-page
    width/height/scan counts; re-probing here would be the redundant parse we
    are removing. pdftoppm is stubbed to just drop the expected PNGs.
    """

    def _boom(_pdf_path):
        raise AssertionError("_probe_pages must not run when metadata is given")

    monkeypatch.setattr(
        "api.pipeline.schematic.renderer._probe_pages", _boom
    )

    def fake_run(cmd, **_kwargs):
        prefix = Path(cmd[-1])
        # page_count=2 → pdftoppm pads to 2 digits: page-01.png, page-02.png
        for n in (1, 2):
            (prefix.parent / f"page-{n:02d}.png").write_bytes(b"\x89PNG")
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(
        "api.pipeline.schematic.renderer.subprocess.run", fake_run
    )

    metadata = [
        {"page": 1, "width": 842.0, "height": 595.0, "char_count": 10, "line_count": 3},
        {"page": 2, "width": 595.0, "height": 842.0, "char_count": 0, "line_count": 0},
    ]
    pdf_path = tmp_path / "fake.pdf"
    pdf_path.touch()

    out = render_pages(pdf_path, tmp_path / "out", dpi=150, metadata=metadata)

    assert [r.page_number for r in out] == [1, 2]
    assert out[0].orientation == "landscape"  # width > height
    assert out[1].orientation == "portrait"
    assert out[1].is_scanned is True  # char_count == 0 and line_count == 0
