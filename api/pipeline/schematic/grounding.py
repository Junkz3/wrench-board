"""pdfplumber-based grounding dump for the vision prompt.

On native-vector PDFs (KiCad, Altium, Cadence exports) pdfplumber extracts
every printed string with its bounding box, every wire segment, every
component rectangle — deterministically and for free. The vision LLM then
receives this dump as ground truth and only has to resolve the topology
(which pin connects to which net via which wire) rather than guessing
refdes spellings, pin numbers, or rail labels.

This collapses the hallucination failure mode we measured on Haiku without
grounding (invented rails, misread MPNs, hallucinated pin numbers).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

from api.config import get_settings
from api.pipeline.schematic.renderer import SchematicPageLimitExceeded

# Regexes used to bucket candidates. Deliberately permissive — the vision
# pass is told to only use these as a truth set, not as the final list.
_REFDES_RE = re.compile(r"^(?:U|R|C|L|D|Q|J|Y|TP|H|SW|F|FB|BT|K|M|X|Z)\d{1,4}[A-Z]?$")
# Allow nets starting with digit ("30V_GATE") or plus ("+5V", "+3V3") as well
# as letters ("GND", "VCCIO"). Require at least one letter somewhere so pure
# numeric grid-labels ("1", "2", "3") get filtered out.
_NET_RE = re.compile(r"^[+]?[A-Z0-9][A-Z0-9_+/.-]{1,40}$")
_HAS_LETTER_RE = re.compile(r"[A-Z]")
_VALUE_RE = re.compile(
    r"""^(
        \d+(?:\.\d+)?[kKmMμuµnpfGM]?[HFΩRr]? |  # 4.7k, 100nF, 68uH, 100Ω, 4R7
        \d+[vV]\d* |                             # 5V, 3V3
        \d+\.\d+[vV] |                           # 3.3V
        \d+(?:\.\d+)?[mM]?[aAwW] |              # 1A, 100mA, 1/16W (handled elsewhere)
        [A-Z]{2,}\d+[A-Z0-9-]*                  # LM2677SX-5, TLV1117-18
    )$""",
    re.VERBOSE,
)


@dataclass(frozen=True)
class PageGrounding:
    page: int
    refdes: list[str]
    net_labels: list[str]
    values: list[tuple[str, float, float]]  # (text, x, y)
    sheet_file: str | None
    sheet_title: str | None
    wire_count: int
    rect_count: int
    # PDF page dimensions in points (1 pt = 1/72 inch). The web viewer uses
    # these to scale pdfplumber-native bboxes onto the rasterised PNG.
    page_width: float = 0.0
    page_height: float = 0.0
    # Every refdes occurrence on the page, with its pdfplumber bbox in points
    # (x0, top, x1, bottom). A refdes can repeat — symbol + netlist + note —
    # so this is a flat list, not a dict. The viewer overlays one highlight
    # rectangle per entry when the user searches for that refdes.
    refdes_anchors: list[tuple[str, float, float, float, float]] = field(
        default_factory=list
    )


@dataclass(frozen=True)
class PageExtract:
    """One page's render/scan metadata + grounding, from a single PDF parse.

    `page`/`width`/`height`/`char_count`/`line_count` mirror the dict
    `renderer._probe_pages` produces (scan detection + orientation), so a
    caller can hand them to `render_pages(metadata=...)` and skip a second
    pdfplumber pass. `grounding` is the same `PageGrounding` the per-page
    `extract_grounding` returns.
    """

    page: int
    width: float
    height: float
    char_count: int
    line_count: int
    grounding: PageGrounding | None


def extract_grounding(pdf_path: Path, page_number: int) -> PageGrounding:
    """Run pdfplumber on one page and bucket its texts for the vision prompt."""
    with pdfplumber.open(str(pdf_path)) as pdf:
        if not (1 <= page_number <= len(pdf.pages)):
            raise ValueError(
                f"page {page_number} out of range (PDF has {len(pdf.pages)} pages)"
            )
        return _grounding_from_page(pdf.pages[page_number - 1], page_number)


def _grounding_from_page(page, page_number: int) -> PageGrounding:
    """Bucket one already-open pdfplumber page into a PageGrounding.

    Shared by the single-page `extract_grounding` and the single-pass
    `extract_all_pages` so both paths produce byte-for-byte identical output
    from one place. Touches `page.extract_words` / `page.lines` / `page.rects`
    once; the caller owns opening the PDF and (for the multi-page pass)
    flushing the page cache afterwards.
    """
    words = page.extract_words(x_tolerance=2, y_tolerance=2)
    wire_count = len(page.lines)
    rect_count = len(page.rects)
    page_width = float(page.width)
    page_height = float(page.height)

    tokens = [w["text"] for w in words]

    # Split tokens that pdfplumber glued together when text overlaps wires,
    # e.g. 'PWR_FLAG+1V5' -> ['PWR_FLAG', '+1V5']. Common enough on dense
    # regulator sheets that we handle it explicitly.
    split_tokens: list[str] = []
    for t in tokens:
        if "+" in t and t[0] != "+":
            parts = t.split("+")
            split_tokens.append(parts[0])
            split_tokens.extend("+" + p for p in parts[1:] if p)
        else:
            split_tokens.append(t)
    tokens = split_tokens

    refdes = sorted({t for t in tokens if _REFDES_RE.match(t)})
    # Preserve the full bbox of every refdes occurrence for the PDF viewer's
    # highlight overlay. pdfplumber coordinates use a top-left origin in
    # points, matching the rasterised PNG once scaled by page_width /
    # page_height. Token-split candidates (the '+' splitter above) are
    # retained — a refdes never contains '+' so the split can't break one.
    refdes_anchors: list[tuple[str, float, float, float, float]] = [
        (
            w["text"],
            float(w["x0"]),
            float(w["top"]),
            float(w["x1"]),
            float(w["bottom"]),
        )
        for w in words
        if _REFDES_RE.match(w["text"])
    ]
    # A net label is an uppercase / digit-prefixed token with at least one
    # letter, that's not a refdes, that's substantive (≥3 chars), and isn't
    # an obvious resistor-shorthand value (e.g. '581R', '85R').
    net_candidates = {
        t
        for t in tokens
        if _NET_RE.match(t)
        and not _REFDES_RE.match(t)
        and _HAS_LETTER_RE.search(t)
        and len(t) >= 3
        and not re.match(r"^\d+[RrKkMm]$", t)
    }
    # Drop KiCad title-block noise words
    noise = {
        "FILE",
        "SHEET",
        "REV",
        "DATE",
        "SIZE",
        "LICENSE",
        "ENGINEER",
        "CERN",
        "OHL",
        "HTTPS",
        "MNT",
        "RESEARCH",
        "GMBH",
        "KICAD",
        "EDA",
        "REFORM",
        "POWER",
        "PCIE",
        "USB",
        "TITLE",
    }
    net_labels = sorted({t for t in net_candidates if t not in noise})

    values: list[tuple[str, float, float]] = []
    for w in words:
        t = w["text"]
        if _REFDES_RE.match(t) or t in noise:
            continue
        if _VALUE_RE.match(t):
            values.append((t, float(w["x0"]), float(w["top"])))

    # Pull sheet file / title from the title-block texts.
    sheet_file: str | None = None
    sheet_title: str | None = None
    full_text = " ".join(tokens)
    m = re.search(r"([A-Za-z0-9_-]+\.kicad_sch)", full_text)
    if m:
        sheet_file = m.group(1)
    # Title line usually reads like "Title: Reform 2 Regulators"
    m = re.search(r"Title\s*:?\s*([A-Z][A-Za-z0-9 ._-]{3,60})", full_text)
    if m:
        sheet_title = m.group(1).strip()

    return PageGrounding(
        page=page_number,
        refdes=refdes,
        net_labels=net_labels,
        values=values,
        sheet_file=sheet_file,
        sheet_title=sheet_title,
        wire_count=wire_count,
        rect_count=rect_count,
        page_width=page_width,
        page_height=page_height,
        refdes_anchors=refdes_anchors,
    )


def extract_page_data(
    pdf_path: Path, page_number: int, *, with_grounding: bool = True
) -> PageExtract:
    """Open the PDF and extract one page's scan metadata (+ optional grounding).

    The single-page sibling of `extract_all_pages`, for the streaming ingest
    pipeline that processes pages one at a time. `with_grounding=False` skips
    the word/refdes extraction and returns metadata only (orientation + scan
    detection), for callers that render without a grounding prompt.
    """
    with pdfplumber.open(str(pdf_path)) as pdf:
        if not (1 <= page_number <= len(pdf.pages)):
            raise ValueError(
                f"page {page_number} out of range (PDF has {len(pdf.pages)} pages)"
            )
        page = pdf.pages[page_number - 1]
        grounding = _grounding_from_page(page, page_number) if with_grounding else None
        return PageExtract(
            page=page_number,
            width=float(page.width),
            height=float(page.height),
            char_count=len(page.chars),
            line_count=len(page.lines),
            grounding=grounding,
        )


def extract_all_pages(pdf_path: Path) -> list[PageExtract]:
    """Open the PDF once and extract every page's scan metadata + grounding.

    Replaces the old two-pass cost (a full `_probe_pages` parse for scan
    detection, then one `pdfplumber.open` per page in the grounding loop) with
    a single parse per page that feeds both. The page count cap
    (`pipeline_schematic_max_pages`) is enforced up front, before any per-page
    extraction, so an oversized PDF fails fast.

    `page.flush_cache()` is called after each page: pdfplumber materialises and
    caches the full object model on first touch (~hundreds of MB on a dense
    vector sheet); without the flush all N pages stay resident at once. Every
    value is read before the flush, so output is unchanged.
    """
    cap = get_settings().pipeline_schematic_max_pages
    out: list[PageExtract] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        n = len(pdf.pages)
        if n > cap:
            raise SchematicPageLimitExceeded(
                f"schematic has {n} pages, exceeds cap of {cap}"
            )
        for i, page in enumerate(pdf.pages, start=1):
            grounding = _grounding_from_page(page, i)
            out.append(
                PageExtract(
                    page=i,
                    width=float(page.width),
                    height=float(page.height),
                    char_count=len(page.chars),
                    line_count=len(page.lines),
                    grounding=grounding,
                )
            )
            page.flush_cache()
    return out


def format_grounding_for_prompt(g: PageGrounding) -> str:
    """Render a PageGrounding as compact text suitable for inlining in a prompt.

    The vision model is told this is ground truth pulled deterministically
    from the PDF's vector layer — it must not invent refdes, net labels, or
    values outside these sets.
    """
    lines = [
        "GROUNDING — vector-layer extract (pdfplumber). This is ground truth.",
        f"page: {g.page}",
        f"sheet_file: {g.sheet_file or 'unknown'}",
        f"sheet_title: {g.sheet_title or 'unknown'}",
        f"wire_count: {g.wire_count}",
        f"rect_count: {g.rect_count}",
        "",
        f"REFDES ({len(g.refdes)}) — every refdes you emit MUST be in this set:",
        "  " + ", ".join(g.refdes) if g.refdes else "  (none)",
        "",
        f"NET_LABELS ({len(g.net_labels)}) — every net label you emit MUST be in this set:",
        "  " + ", ".join(g.net_labels) if g.net_labels else "  (none)",
        "",
        f"VALUE_TOKENS ({len(g.values)}) — values printed on the page, with position:",
    ]
    for text, x, y in g.values[:200]:
        lines.append(f"  {text!r} @ ({x:.0f},{y:.0f})")
    if len(g.values) > 200:
        lines.append(f"  ... +{len(g.values) - 200} more")
    return "\n".join(lines)
