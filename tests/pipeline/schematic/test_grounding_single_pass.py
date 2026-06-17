"""Tests for the single-pass page extractor `extract_all_pages`.

The orchestrator used to parse the PDF 118 times for a 117-page schematic:
once in `_probe_pages` (scan detection) and once per page in the grounding
loop. `extract_all_pages` opens the PDF a single time and derives BOTH the
render/scan metadata and the per-page grounding from one parse, so its output
must be byte-for-byte equivalent to the two legacy paths it replaces.

The equivalence test (slow) runs against the real MNT Reform fixture; the cap
test (fast) monkeypatches pdfplumber so it stays in `make test`.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from api.pipeline.schematic.grounding import (
    extract_all_pages,
    extract_grounding,
    extract_page_data,
)
from api.pipeline.schematic.renderer import (
    SchematicPageLimitExceeded,
    _probe_pages,
)

FIXTURE_PDF = Path("board_assets/mnt-reform-motherboard.pdf")


@pytest.mark.slow
def test_extract_all_pages_matches_legacy_per_page_paths():
    """One pass must equal _probe_pages + per-page extract_grounding."""
    if not FIXTURE_PDF.is_file():
        pytest.skip(f"missing fixture {FIXTURE_PDF}")

    combined = extract_all_pages(FIXTURE_PDF)
    legacy_meta = _probe_pages(FIXTURE_PDF)

    # Render/scan metadata identical to _probe_pages, in page order.
    assert [
        {
            "page": e.page,
            "width": e.width,
            "height": e.height,
            "char_count": e.char_count,
            "line_count": e.line_count,
        }
        for e in combined
    ] == legacy_meta

    # Per-page grounding identical to the per-page open path.
    assert len(combined) == len(legacy_meta)
    for e in combined:
        assert e.grounding == extract_grounding(FIXTURE_PDF, e.page)


@pytest.mark.slow
def test_extract_page_data_matches_extract_all_pages_per_page():
    """The single-page extractor must equal the matching extract_all_pages row."""
    if not FIXTURE_PDF.is_file():
        pytest.skip(f"missing fixture {FIXTURE_PDF}")

    combined = extract_all_pages(FIXTURE_PDF)
    for e in combined:
        one = extract_page_data(FIXTURE_PDF, e.page)
        assert one == e


@pytest.mark.slow
def test_extract_page_data_without_grounding_returns_meta_only():
    """with_grounding=False keeps scan metadata but drops the grounding."""
    if not FIXTURE_PDF.is_file():
        pytest.skip(f"missing fixture {FIXTURE_PDF}")

    full = extract_page_data(FIXTURE_PDF, 1)
    meta_only = extract_page_data(FIXTURE_PDF, 1, with_grounding=False)

    assert meta_only.grounding is None
    assert full.grounding is not None
    # Metadata (dims + scan counts) is identical regardless of grounding.
    assert (meta_only.page, meta_only.width, meta_only.height) == (
        full.page,
        full.width,
        full.height,
    )
    assert (meta_only.char_count, meta_only.line_count) == (
        full.char_count,
        full.line_count,
    )


def _patch_pdf_and_cap(monkeypatch, page_count: int, cap: int) -> None:
    def _fake_page() -> MagicMock:
        p = MagicMock()
        p.width = 595.0
        p.height = 842.0
        p.chars = []
        p.lines = []
        p.rects = []
        p.extract_words.return_value = []
        return p

    fake_pdf = MagicMock()
    fake_pdf.pages = [_fake_page() for _ in range(page_count)]

    @contextmanager
    def fake_open(_path):
        yield fake_pdf

    monkeypatch.setattr(
        "api.pipeline.schematic.grounding.pdfplumber.open", fake_open
    )
    monkeypatch.setattr(
        "api.pipeline.schematic.grounding.get_settings",
        lambda: type("S", (), {"pipeline_schematic_max_pages": cap})(),
    )


def test_extract_all_pages_raises_when_exceeding_cap(monkeypatch, tmp_path: Path):
    _patch_pdf_and_cap(monkeypatch, page_count=5, cap=3)
    pdf_path = tmp_path / "fake.pdf"
    pdf_path.touch()
    with pytest.raises(SchematicPageLimitExceeded) as exc_info:
        extract_all_pages(pdf_path)
    assert "5 pages" in str(exc_info.value)
    assert "cap of 3" in str(exc_info.value)


def test_extract_all_pages_passes_when_within_cap(monkeypatch, tmp_path: Path):
    _patch_pdf_and_cap(monkeypatch, page_count=2, cap=200)
    pdf_path = tmp_path / "fake.pdf"
    pdf_path.touch()
    out = extract_all_pages(pdf_path)
    assert [e.page for e in out] == [1, 2]
