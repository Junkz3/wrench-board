"""Integration tests for api.pipeline.schematic.renderer.

Uses the real MNT Reform v2.5 PDF fixture (committed under board_assets/).
Marked `slow` at the module level: rendering 12 A4 pages via pdftoppm is
the single slowest fixture in the suite (~30 s on a modern laptop, dominated
by pdftoppm CPU), so we keep it out of `make test` and only run it under
`make test-all`. Move back to the fast path once the render pipeline is
optimized (cached vector extraction, fewer pages, lower dpi for the smoke
checks).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.pipeline.schematic.renderer import (
    PdftoppmNotAvailableError,
    RenderedPage,
    render_pages,
)

pytestmark = pytest.mark.slow

FIXTURE_PDF = Path("board_assets/mnt-reform-motherboard.pdf")


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> list[RenderedPage]:
    if not FIXTURE_PDF.is_file():
        pytest.skip(f"missing fixture {FIXTURE_PDF}")
    out = tmp_path_factory.mktemp("mnt_render")
    return render_pages(FIXTURE_PDF, out, dpi=150)


def test_render_pages_emits_one_png_per_page(rendered: list[RenderedPage]):
    assert len(rendered) == 12
    assert [r.page_number for r in rendered] == list(range(1, 13))


def test_rendered_pngs_exist_and_are_non_trivial(rendered: list[RenderedPage]):
    for r in rendered:
        assert r.png_path.is_file()
        # Every A4 at 150 dpi should comfortably exceed 50 KB — anything
        # smaller suggests pdftoppm wrote a blank or failed silently.
        assert r.png_path.stat().st_size > 50_000, r.png_path


def test_mnt_fixture_pages_are_native_vectors_not_scans(rendered: list[RenderedPage]):
    for r in rendered:
        assert r.is_scanned is False, r


def test_orientation_is_detected_per_page(rendered: list[RenderedPage]):
    # MNT v2.5 mixes portrait and landscape — denser sheets (regulators, PCIe,
    # display) are printed landscape. All we care about is that every page got
    # a valid orientation consistent with its bbox.
    for r in rendered:
        if r.width_pt > r.height_pt:
            assert r.orientation == "landscape", r
        else:
            assert r.orientation == "portrait", r
    kinds = {r.orientation for r in rendered}
    assert kinds.issubset({"portrait", "landscape"})
    assert kinds  # at least one


def test_render_pages_raises_on_missing_pdf(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        render_pages(tmp_path / "nope.pdf", tmp_path / "out")


def test_pdftoppm_not_available_error_is_exported():
    # Smoke-check the error class is importable — used by callers to distinguish
    # environment-setup failures from logic errors.
    assert issubclass(PdftoppmNotAvailableError, RuntimeError)
