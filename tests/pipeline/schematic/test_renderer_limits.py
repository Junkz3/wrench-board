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
