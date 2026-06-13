"""Unit tests for the ghostscript PDF-repair fallback in the renderer.

Some real-world schematic PDFs (notably parts of the XZZ board library) carry
non-standard objects that pdfplumber/pdfminer cannot parse — `pdfplumber.open`
yields 0 pages even though poppler's pdfinfo reads them fine. Left unhandled,
`ingest_schematic` rendered 0 pages and silently built an EMPTY pack (wasting
Scout/writer tokens on nothing — observed live on an iPhone 8 schematic,
2026-06-12).

`ensure_renderable_pdf` closes that hole: if pdfplumber sees 0 pages it
re-distills the PDF through ghostscript (which normalises the broken objects)
and re-probes; if it STILL sees 0 pages it raises rather than letting the
pipeline produce an empty pack.

The probe + gs calls are stubbed — these are fast unit tests, no real PDF.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.pipeline.schematic import renderer


def _touch(p: Path) -> Path:
    p.write_bytes(b"%PDF-1.4\n")
    return p


def test_returns_original_when_already_readable(tmp_path, monkeypatch):
    pdf = _touch(tmp_path / "ok.pdf")
    monkeypatch.setattr(renderer, "probe_page_count", lambda _p: 12)
    called = {"gs": False}
    monkeypatch.setattr(
        renderer, "repair_pdf_with_ghostscript",
        lambda *a, **k: called.__setitem__("gs", True) or True,
    )

    out = renderer.ensure_renderable_pdf(pdf, tmp_path)

    assert out == pdf
    assert called["gs"] is False  # no repair attempted on a healthy PDF


def test_repairs_when_pdfplumber_sees_zero_pages(tmp_path, monkeypatch):
    pdf = _touch(tmp_path / "broken.pdf")
    # 0 pages on the original, 80 after ghostscript rewrites the repaired file.
    counts = iter([0, 80])
    monkeypatch.setattr(renderer, "probe_page_count", lambda _p: next(counts))

    def _fake_gs(src, dst):
        Path(dst).write_bytes(b"%PDF-1.4\nrepaired\n")
        return True

    monkeypatch.setattr(renderer, "repair_pdf_with_ghostscript", _fake_gs)

    out = renderer.ensure_renderable_pdf(pdf, tmp_path)

    assert out != pdf
    assert out.is_file()


def test_raises_when_still_zero_after_repair(tmp_path, monkeypatch):
    pdf = _touch(tmp_path / "hopeless.pdf")
    monkeypatch.setattr(renderer, "probe_page_count", lambda _p: 0)  # always 0
    monkeypatch.setattr(
        renderer, "repair_pdf_with_ghostscript",
        lambda src, dst: Path(dst).write_bytes(b"%PDF\n") or True,
    )

    with pytest.raises(RuntimeError, match="unrenderable|0 pages|repair"):
        renderer.ensure_renderable_pdf(pdf, tmp_path)


def test_raises_when_ghostscript_unavailable(tmp_path, monkeypatch):
    pdf = _touch(tmp_path / "broken.pdf")
    monkeypatch.setattr(renderer, "probe_page_count", lambda _p: 0)
    # gs not installed → repair reports failure
    monkeypatch.setattr(renderer, "repair_pdf_with_ghostscript", lambda src, dst: False)

    with pytest.raises(RuntimeError, match="unrenderable|0 pages|repair"):
        renderer.ensure_renderable_pdf(pdf, tmp_path)


def test_repair_pdf_with_ghostscript_invokes_gs(tmp_path, monkeypatch):
    """The repair helper shells out to gs with pdfwrite and reports success
    from the produced file, and reports failure (no raise) when gs is absent."""
    calls = {}

    class _OK:
        def __init__(self, *a, **k):
            calls["argv"] = a[0]
            # simulate gs writing the output
            dst = a[0][a[0].index("-o") + 1]
            Path(dst).write_bytes(b"%PDF-1.4\nout\n")

    monkeypatch.setattr(renderer.subprocess, "run", _OK)
    dst = tmp_path / "out.pdf"
    ok = renderer.repair_pdf_with_ghostscript(tmp_path / "in.pdf", dst)
    assert ok is True
    assert calls["argv"][0] == "gs"
    assert "pdfwrite" in " ".join(calls["argv"])

    def _missing(*a, **k):
        raise FileNotFoundError("gs")

    monkeypatch.setattr(renderer.subprocess, "run", _missing)
    assert renderer.repair_pdf_with_ghostscript(tmp_path / "in.pdf", dst) is False
