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


def probe_page_count(pdf_path: Path) -> int:
    """Pages pdfplumber/pdfminer can actually parse (0 if the PDF is unreadable).

    Deliberately tolerant: some XZZ-library PDFs carry non-standard objects that
    make `pdfplumber.open` yield 0 pages (or raise) even though poppler reads
    them — we treat both as "0, needs repair" rather than crashing.
    """
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            return len(pdf.pages)
    except Exception:  # noqa: BLE001 — any parse failure means "unreadable here"
        logger.warning("pdfplumber could not parse %s", pdf_path, exc_info=True)
        return 0


def repair_pdf_with_ghostscript(src: Path, dst: Path) -> bool:
    """Re-distill `src` → `dst` via ghostscript to normalise broken objects.

    Returns True when gs produced a non-empty file, False on any failure
    (including gs not being installed) — the caller decides whether a failed
    repair is fatal. Never raises: a missing `gs` binary is a degraded-mode
    signal, not a crash.
    """
    try:
        subprocess.run(
            [
                "gs", "-o", str(dst),
                "-sDEVICE=pdfwrite",
                "-dPDFSETTINGS=/prepress",
                "-dQUIET", "-dBATCH", "-dNOPAUSE",
                str(src),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        logger.warning(
            "ghostscript (gs) not installed — cannot repair %s "
            "(apt install ghostscript)", src,
        )
        return False
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "ghostscript repair failed on %s: %s", src, (exc.stderr or "").strip()[:300],
        )
        return False
    return Path(dst).is_file() and Path(dst).stat().st_size > 0


def ensure_renderable_pdf(pdf_path: Path, work_dir: Path) -> Path:
    """Return a PDF that pdfplumber AND pdftoppm can read — repairing if needed.

    If the original parses cleanly (pdfplumber sees ≥1 page) it is returned
    untouched. Otherwise we re-distill it through ghostscript (which fixes the
    non-standard objects that defeat pdfminer) and re-probe. If it STILL yields
    0 pages we raise — far better to fail the build loudly than to render 0
    pages and silently produce an empty pack (wasting Scout/writer tokens).

    The repaired copy lands at `work_dir/_repaired_source.pdf` so the caller
    can use it for BOTH rendering and grounding (both read the PDF via
    pdfplumber/poppler).
    """
    pdf_path = Path(pdf_path)
    if probe_page_count(pdf_path) > 0:
        return pdf_path

    logger.warning(
        "%s is unreadable by pdfplumber (0 pages) — attempting ghostscript repair",
        pdf_path,
    )
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    repaired = Path(work_dir) / "_repaired_source.pdf"
    if repair_pdf_with_ghostscript(pdf_path, repaired):
        n = probe_page_count(repaired)
        if n > 0:
            logger.info(
                "ghostscript repaired %s → %d pages (using %s)",
                pdf_path, n, repaired.name,
            )
            return repaired

    raise RuntimeError(
        f"{pdf_path} is unrenderable (0 pages) even after ghostscript repair — "
        "corrupt or unsupported PDF; aborting the build rather than producing "
        "an empty pack"
    )


def render_pages(
    pdf_path: Path,
    output_dir: Path,
    *,
    dpi: int = 200,
    metadata: list[dict] | None = None,
) -> list[RenderedPage]:
    """Render every page of `pdf_path` to `output_dir/page-XX.png`.

    Pages are numbered 1-based with zero-padded width matching the total page
    count (page-01.png ... page-12.png for a 12-page PDF — pdftoppm's default
    behaviour). Returns one `RenderedPage` per page in page-number order.

    `metadata` lets a caller pass the per-page probe result (page / width /
    height / char_count / line_count) it has already computed — e.g. via
    `grounding.extract_all_pages`, which parses the PDF once for both scan
    detection and grounding. When omitted, `_probe_pages` runs as before, so
    standalone callers are unaffected.
    """
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)

    metadata = metadata if metadata is not None else _probe_pages(pdf_path)
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


def render_one_page(
    pdf_path: Path,
    output_dir: Path,
    page_number: int,
    total_pages: int,
    *,
    dpi: int = 200,
    width_pt: float,
    height_pt: float,
    char_count: int,
    line_count: int,
) -> RenderedPage:
    """Rasterise exactly one page to `output_dir/page-NN.png` via pdftoppm.

    Unlike `render_pages` (one pdftoppm over the whole PDF), this renders a
    single page with `-singlefile -f N -l N`, so a caller can pipeline
    render → vision per page and overlap pdftoppm CPU with the OTPM-bound
    vision wait. `-singlefile` writes `<prefix>.png` with no page-number
    suffix, removing the padding ambiguity of the bulk path.

    Page metadata (dims + char/line counts for orientation + scan detection)
    is supplied by the caller, which already parsed it via pdfplumber — this
    function does no PDF parsing of its own. `page-NN` is zero-padded to the
    same width the bulk renderer uses so the web viewer's glob sorts identically.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    width = max(2, len(str(total_pages)))
    prefix = output_dir / f"page-{page_number:0{width}d}"
    try:
        subprocess.run(
            [
                "pdftoppm", "-png", "-singlefile", "-r", str(dpi),
                "-f", str(page_number), "-l", str(page_number),
                str(pdf_path), str(prefix),
            ],
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
            f"pdftoppm failed on {pdf_path} page {page_number}: "
            f"{exc.stderr.strip() or exc}"
        ) from exc

    png_path = output_dir / f"page-{page_number:0{width}d}.png"
    if not png_path.is_file():
        raise RuntimeError(
            f"pdftoppm did not produce expected PNG for page {page_number} "
            f"(looked at {png_path})"
        )
    return RenderedPage(
        page_number=page_number,
        png_path=png_path,
        orientation="landscape" if width_pt > height_pt else "portrait",
        is_scanned=char_count == 0 and line_count == 0,
        width_pt=width_pt,
        height_pt=height_pt,
    )


def _probe_pages(pdf_path: Path) -> list[dict]:
    cap = get_settings().pipeline_schematic_max_pages
    out: list[dict] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        n = len(pdf.pages)
        if n > cap:
            raise SchematicPageLimitExceeded(
                f"schematic has {n} pages, exceeds cap of {cap}"
            )
        for i, page in enumerate(pdf.pages, start=1):
            out.append(
                {
                    "page": i,
                    "width": float(page.width),
                    "height": float(page.height),
                    "char_count": len(page.chars),
                    "line_count": len(page.lines),
                }
            )
            # pdfplumber matérialise et CACHE le modèle objet de chaque page dès
            # qu'on touche .chars/.lines (~750 Mo sur un schéma vectoriel dense).
            # Sans flush, les N pages restent résidentes simultanément (~1 Go+) ;
            # on libère chaque page après lecture → pic ≈ 1 page, pas N. Les
            # valeurs sont déjà extraites avant le flush → sortie inchangée.
            page.flush_cache()
    return out
