"""Persistence helpers for macro images.

Macros land under ``memory/{slug}/repairs/{repair_id}/macros/{ts}_{source}.{ext}``.
Two sources :

  - ``manual`` : tech drag-dropped or uploaded via the chat panel (Flow A)
  - ``capture`` : agent called ``cam_capture``, frontend snapped via
    getUserMedia (Flow B)

The path layout is mirrored on the frontend's replay route
(``GET /api/macros/{slug}/{repair_id}/{filename}``).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal

Source = Literal["manual", "capture"]

_EXT_FROM_MIME: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def persist_macro(
    *,
    memory_root: Path,
    slug: str,
    repair_id: str,
    source: str,
    bytes_: bytes,
    mime: str,
) -> Path:
    """Write ``bytes_`` under ``macros/{ts}_{source}.{ext}`` and return the path.

    Creates the macros directory if missing. Disambiguates same-second
    collisions with a numeric suffix.

    Raises :class:`ValueError` on unknown mime or invalid source.
    """
    if source not in ("manual", "capture"):
        raise ValueError(f"source must be 'manual' or 'capture', got {source!r}")
    ext = _EXT_FROM_MIME.get(mime.lower())
    if ext is None:
        raise ValueError(f"unsupported mime: {mime!r}")
    macros_dir = memory_root / slug / "repairs" / repair_id / "macros"
    macros_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    path = macros_dir / f"{ts}_{source}{ext}"
    counter = 1
    while path.exists():
        path = macros_dir / f"{ts}_{source}_{counter}{ext}"
        counter += 1
    path.write_bytes(bytes_)
    return path


def macro_path_for(
    *,
    memory_root: Path,
    slug: str,
    repair_id: str,
    filename: str,
) -> Path:
    """Resolve a stored macro path safely. Blocks path traversal.

    Raises :class:`ValueError` if the filename contains directory separators,
    leading dots, or escapes the macros directory after resolution.
    """
    if (
        "/" in filename
        or "\\" in filename
        or filename.startswith(".")
        or ".." in filename
    ):
        raise ValueError(f"invalid filename: {filename!r}")
    macros_dir = memory_root / slug / "repairs" / repair_id / "macros"
    candidate = macros_dir / filename
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"invalid filename: {filename!r}") from exc
    macros_resolved = macros_dir.resolve(strict=False)
    if not str(resolved).startswith(str(macros_resolved)):
        raise ValueError(f"invalid filename: {filename!r}")
    return candidate


def build_image_ref(
    *,
    path: Path,
    memory_root: Path,
    slug: str,
    repair_id: str,
    source: Source,
) -> dict:
    """Build the ``image_ref`` dict that lands in ``messages.jsonl``.

    The frontend resolves ``path`` (relative to
    ``memory/{slug}/repairs/{repair_id}/``) via the
    ``GET /api/macros/{slug}/{repair_id}/{filename}`` route on replay.
    """
    repair_root = memory_root / slug / "repairs" / repair_id
    relative = path.relative_to(repair_root)
    return {
        "type": "image_ref",
        "path": str(relative),
        "source": source,
    }
