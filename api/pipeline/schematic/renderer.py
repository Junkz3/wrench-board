"""PDF → per-page PNG renderer + lightweight metadata.

Uses poppler's `pdftoppm` CLI (already installed on any machine that runs the
diagnostic pipeline; no Python-only dependency). pdfplumber is used strictly
as a utility here — to count chars/lines per page (scan detection) and to
probe orientation. No text extraction is fed into the vision prompt; that
decision lives in the pipeline architecture notes.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pdfplumber

from api.config import get_settings

logger = logging.getLogger("wrench_board.pipeline.schematic.renderer")


@dataclass(frozen=True)
class RenderedPage:
    page_number: int                       # 1-based
    png_path: Path
    orientation: Literal["portrait", "landscape"]
    is_scanned: bool                       # True when pdfplumber finds no text/vectors
    width_pt: float
    height_pt: float


class PdftoppmNotAvailableError(RuntimeError):
    pass


class SchematicPageLimitExceeded(ValueError):
    """Raised when an uploaded schematic exceeds `pipeline_schematic_max_pages`."""


def render_pages(
    pdf_path: Path,
    output_dir: Path,
    *,
    dpi: int = 200,
) -> list[RenderedPage]:
    """Render every page of `pdf_path` to `output_dir/page-XX.png`.

    Pages are numbered 1-based with zero-padded width matching the total page
    count (page-01.png ... page-12.png for a 12-page PDF — pdftoppm's default
    behaviour). Returns one `RenderedPage` per page in page-number order.
    """
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)

    metadata = _probe_pages(pdf_path)
    page_count = len(metadata)

    prefix = output_dir / "page"
    try:
        subprocess.run(
            ["pdftoppm", "-png", "-r", str(dpi), str(pdf_path), str(prefix)],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise PdftoppmNotAvailableError(
            "pdftoppm not found — install poppler-utils (apt install poppler-utils)."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"pdftoppm failed on {pdf_path}: {exc.stderr.strip() or exc}"
        ) from exc

    width = max(2, len(str(page_count)))  # pdftoppm pads to 2 digits minimum
    rendered: list[RenderedPage] = []
    for meta in metadata:
        candidate = output_dir / f"page-{meta['page']:0{width}d}.png"
        if not candidate.exists():
            # Fallback: some pdftoppm versions pad only when needed.
            fallback = output_dir / f"page-{meta['page']}.png"
            if fallback.exists():
                candidate = fallback
            else:
                raise RuntimeError(
                    f"pdftoppm did not produce expected PNG for page {meta['page']} "
                    f"(looked at {candidate} and {fallback})"
                )
        rendered.append(
            RenderedPage(
                page_number=meta["page"],
                png_path=candidate,
                orientation="landscape" if meta["width"] > meta["height"] else "portrait",
                is_scanned=meta["char_count"] == 0 and meta["line_count"] == 0,
                width_pt=meta["width"],
                height_pt=meta["height"],
            )
        )

    scanned = sum(1 for r in rendered if r.is_scanned)
    if scanned:
        logger.warning(
            "%d / %d pages detected as scanned (no extractable text/vectors) — "
            "vision pass will run without grounding",
            scanned,
            len(rendered),
        )
    return rendered


def _probe_pages(pdf_path: Path) -> list[dict]:
    cap = get_settings().pipeline_schematic_max_pages
    with pdfplumber.open(str(pdf_path)) as pdf:
        n = len(pdf.pages)
        if n > cap:
            raise SchematicPageLimitExceeded(
                f"schematic has {n} pages, exceeds cap of {cap}"
            )
        return [
            {
                "page": i,
                "width": float(page.width),
                "height": float(page.height),
                "char_count": len(page.chars),
                "line_count": len(page.lines),
            }
            for i, page in enumerate(pdf.pages, start=1)
        ]
