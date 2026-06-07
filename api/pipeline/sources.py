"""Versioned input source management for a knowledge pack.

The technician can upload multiple schematic PDFs and multiple boardview
files for the same device. This module pins which version is **active**
per kind, and (for schematic_pdf) caches the derived artefacts by content
hash so switching back to a previously-compiled PDF is instantaneous
instead of re-paying the vision pipeline.

On-disk layout under `memory/{slug}/`:

    uploads/                                 # all uploaded versions, timestamped
        20260423T120000Z-schematic_pdf-rev1.pdf
        20260424T130000Z-schematic_pdf-rev2.pdf
        20260424T140000Z-boardview-iphone-x.brd
    active_sources.json                      # { schematic_pdf: "rev2.pdf",
                                             #   boardview:     "iphone-x.brd" }
    .cache_schematic/{sha256-16}/            # hashed cache of derived artefacts
        schematic.pdf
        schematic_pages/...
        schematic_graph.json
        electrical_graph.json

The "active" file is materialised in two places: as the filename pin in
`active_sources.json`, and (for schematic_pdf) as the canonical source
at `memory/{slug}/schematic.pdf` plus its derived files. The detection
helpers in `api/pipeline/__init__.py` and `api/session/state.py` read
`active_sources.json` first; legacy packs without that file fall back to
"newest in uploads" + "in-repo board_assets" lookup.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("wrench_board.pipeline.sources")

ACTIVE_FILE = "active_sources.json"
CACHE_DIR_NAME = ".cache_schematic"
SCHEMATIC_KIND = "schematic_pdf"
BOARDVIEW_KIND = "boardview"
KNOWN_KINDS = (SCHEMATIC_KIND, BOARDVIEW_KIND)

# Artefacts produced by `ingest_schematic` that must travel with each
# cached version. `schematic.pdf` is the source; the rest are derived.
_SCHEMATIC_DERIVED_FILES = ("schematic_graph.json", "electrical_graph.json")
_SCHEMATIC_DERIVED_DIRS = ("schematic_pages",)


@dataclass(frozen=True)
class UploadVersion:
    """One entry in `memory/{slug}/uploads/` for a given kind."""

    filename: str
    timestamp: str
    original_name: str
    size_bytes: int
    is_active: bool

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "timestamp": self.timestamp,
            "original_name": self.original_name,
            "size_bytes": self.size_bytes,
            "is_active": self.is_active,
        }


def read_active(pack_dir: Path) -> dict[str, str | None]:
    """Return the active filename per kind, or empty dict if pin file absent.

    Auto-init on read: if the file is missing but uploads exist, picks
    the newest upload per kind and persists that decision so subsequent
    reads are stable.
    """
    pin_file = pack_dir / ACTIVE_FILE
    if pin_file.exists():
        try:
            data = json.loads(pin_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            # Drop unknown kinds defensively.
            return {k: data.get(k) for k in KNOWN_KINDS if data.get(k)}
        except (OSError, json.JSONDecodeError):
            logger.warning("malformed %s, treating as empty", pin_file, exc_info=True)
            return {}

    # No pin file yet — auto-init from the newest upload per kind.
    inferred = _infer_initial_active(pack_dir)
    if inferred:
        try:
            write_active(pack_dir, inferred)
        except OSError:
            logger.warning("could not persist auto-init pins for %s", pack_dir, exc_info=True)
    return inferred


def write_active(pack_dir: Path, pins: dict[str, str | None]) -> None:
    """Persist the kind → filename map. Filters unknown kinds."""
    pack_dir.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in pins.items() if k in KNOWN_KINDS and v}
    (pack_dir / ACTIVE_FILE).write_text(
        json.dumps(clean, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _infer_initial_active(pack_dir: Path) -> dict[str, str]:
    """Best-effort initial pins from the newest upload per kind."""
    out: dict[str, str] = {}
    for kind in KNOWN_KINDS:
        uploads = list_uploads_for_kind(pack_dir, kind)
        if uploads:
            # list_uploads_for_kind returns newest-first via filename sort.
            out[kind] = uploads[0]["filename"]
    return out


def list_uploads_for_kind(pack_dir: Path, kind: str) -> list[dict]:
    """Return upload entries for a kind, newest-first.

    Reads the on-disk filenames directly; the `is_active` flag isn't
    populated here (callers that care join with `read_active`).
    """
    uploads_dir = pack_dir / "uploads"
    if not uploads_dir.exists():
        return []
    items: list[dict] = []
    for path in sorted(uploads_dir.iterdir(), reverse=True):
        if not path.is_file() or path.name.endswith(".description.txt"):
            continue
        name = path.name
        marker = f"-{kind}-"
        idx = name.find(marker)
        if idx < 0:
            continue
        timestamp = name[:idx]
        original = name[idx + len(marker):]
        items.append({
            "filename": name,
            "timestamp": timestamp,
            "original_name": original,
            "size_bytes": path.stat().st_size,
        })
    return items


def list_versions(pack_dir: Path, kind: str) -> list[UploadVersion]:
    """Like `list_uploads_for_kind` but joined with the active pin."""
    pins = read_active(pack_dir)
    active = pins.get(kind)
    return [
        UploadVersion(
            filename=u["filename"],
            timestamp=u["timestamp"],
            original_name=u["original_name"],
            size_bytes=u["size_bytes"],
            is_active=(u["filename"] == active),
        )
        for u in list_uploads_for_kind(pack_dir, kind)
    ]


def resolve_path(pack_dir: Path, kind: str) -> Path | None:
    """Return the absolute path of the active upload for a kind, or None."""
    pins = read_active(pack_dir)
    active = pins.get(kind)
    if not active:
        return None
    candidate = pack_dir / "uploads" / active
    return candidate if candidate.exists() else None


# ─── Schematic cache (hash-keyed) ───────────────────────────────────────

def hash_pdf(pdf_path: Path) -> str:
    """Return the first 16 chars of sha256(pdf_bytes) — stable cache key."""
    h = hashlib.sha256()
    with pdf_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def cache_dir_for(pack_dir: Path, pdf_hash: str) -> Path:
    return pack_dir / CACHE_DIR_NAME / pdf_hash


def is_cached(pack_dir: Path, pdf_hash: str) -> bool:
    """True when every required artefact for this hash exists in cache."""
    cdir = cache_dir_for(pack_dir, pdf_hash)
    if not cdir.exists():
        return False
    if not (cdir / "schematic.pdf").exists():
        return False
    return all((cdir / f).exists() for f in _SCHEMATIC_DERIVED_FILES)


def write_through_cache(pack_dir: Path, pdf_hash: str) -> None:
    """Snapshot the in-place schematic artefacts into the hashed cache.

    Called after a fresh ingestion so the same PDF can be switched back
    to instantly later. Idempotent — safe to call repeatedly on the same
    hash; existing files are overwritten.
    """
    cdir = cache_dir_for(pack_dir, pdf_hash)
    cdir.mkdir(parents=True, exist_ok=True)
    src_pdf = pack_dir / "schematic.pdf"
    if src_pdf.exists():
        shutil.copyfile(src_pdf, cdir / "schematic.pdf")
    for f in _SCHEMATIC_DERIVED_FILES:
        src = pack_dir / f
        if src.exists():
            shutil.copyfile(src, cdir / f)
    for d in _SCHEMATIC_DERIVED_DIRS:
        src_dir = pack_dir / d
        if src_dir.exists():
            dst_dir = cdir / d
            if dst_dir.exists():
                shutil.rmtree(dst_dir)
            shutil.copytree(src_dir, dst_dir)
    logger.info("[sources] cache write-through for hash=%s in %s", pdf_hash, pack_dir.name)


def restore_from_cache(pack_dir: Path, pdf_hash: str) -> bool:
    """Copy cached artefacts into place, overwriting whatever was there.

    Returns True on success. Caller should `is_cached()` first.
    """
    cdir = cache_dir_for(pack_dir, pdf_hash)
    if not is_cached(pack_dir, pdf_hash):
        return False
    shutil.copyfile(cdir / "schematic.pdf", pack_dir / "schematic.pdf")
    for f in _SCHEMATIC_DERIVED_FILES:
        src = cdir / f
        if src.exists():
            shutil.copyfile(src, pack_dir / f)
    for d in _SCHEMATIC_DERIVED_DIRS:
        src_dir = cdir / d
        if src_dir.exists():
            dst_dir = pack_dir / d
            if dst_dir.exists():
                shutil.rmtree(dst_dir)
            shutil.copytree(src_dir, dst_dir)
    logger.info("[sources] cache restore for hash=%s in %s", pdf_hash, pack_dir.name)
    return True


def clear_in_place_schematic(pack_dir: Path) -> None:
    """Drop the in-place schematic.pdf + derived files (used before re-ingestion).

    Cache copies are untouched — only the canonical paths get cleared so
    detection helpers correctly report `has_schematic_pdf=False` while the
    rebuild is in flight.
    """
    for f in ("schematic.pdf", *_SCHEMATIC_DERIVED_FILES):
        target = pack_dir / f
        if target.exists():
            target.unlink()
    for d in _SCHEMATIC_DERIVED_DIRS:
        target = pack_dir / d
        if target.exists():
            shutil.rmtree(target)
