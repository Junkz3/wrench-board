"""HTTP router for board-file parsing — stateless: accepts an upload, returns parsed JSON."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Header, HTTPException, UploadFile

from api.board.parser.base import (
    BoardParserError,
    InvalidBoardFile,
    MalformedHeaderError,
    MissingFZKeyError,
    ObfuscatedFileError,
    PinPartMismatchError,
    UnsupportedFormatError,
    parser_for,
)
from api.config import get_settings

router = APIRouter(prefix="/api/board", tags=["board"])

_UPLOAD_CHUNK = 1 << 20  # 1 MB

# Process-local cache for /render. Boardview parsing is the heaviest
# synchronous work the API does (a 5 MB .tvw takes ~5 s to parse +
# serialise on a 5700X3D), and `renderDashboardData` hits this endpoint
# every time the user opens a repair dashboard — just to count
# net_diagnostics for the "diagnostic-ready" badge. Caching by
# (resolved file path, mtime) gives byte-exact invalidation: if the
# tech swaps the active pin or re-uploads a board, the new file has a
# new path or mtime and misses the cache. Bounded to MAX_ENTRIES so a
# long-running process doesn't grow unbounded under heavy device churn.
_RENDER_CACHE_MAX_ENTRIES = 16
_render_cache: dict[tuple[str, float], dict] = {}


@router.post("/parse")
async def parse_board(file: UploadFile = File(...)) -> dict:  # noqa: B008
    name = file.filename or "upload.brd"
    suffix = Path(name).suffix or ".brd"

    max_bytes = get_settings().board_upload_max_bytes
    # Cheap upfront check on the declared Content-Length. It can be absent or
    # lied about, so we still enforce the authoritative chunked check below.
    declared = getattr(file, "size", None)
    if declared is not None and declared > max_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "detail": "file-too-large",
                "max_bytes": max_bytes,
                "message": f"upload exceeds {max_bytes} bytes",
            },
        )

    # Authoritative read: abort the stream as soon as we cross max_bytes so a
    # malicious client can't force us to buffer the whole payload.
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail={
                    "detail": "file-too-large",
                    "max_bytes": max_bytes,
                    "message": f"upload exceeds {max_bytes} bytes",
                },
            )
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(
            status_code=400,
            detail={"detail": "empty-file", "message": "uploaded file is empty"},
        )

    board_id = Path(name).stem
    file_hash = "sha256:" + hashlib.sha256(data).hexdigest()

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        path = Path(tmp.name)
        try:
            parser = parser_for(path)
            # Offload le parse CPU-lourd hors de l'event-loop : sur le moteur
            # async single-worker, un parse sync inline gèle TOUTES les requêtes
            # (diags, /health, WS) le temps du parse, et les parses concurrents
            # se sérialisent. to_thread garde le loop responsive (cf. main.py:78).
            board = await asyncio.to_thread(
                parser.parse, data, file_hash=file_hash, board_id=board_id
            )
        except NotImplementedError as e:
            # Defensive: surface unimplemented parser branches as 501 rather
            # than letting them propagate as a generic 500.
            raise HTTPException(
                status_code=501,
                detail={"detail": "parser-not-implemented", "message": str(e)},
            ) from e
        except UnsupportedFormatError as e:
            raise HTTPException(
                status_code=415,
                detail={"detail": "unsupported-format", "message": str(e)},
            ) from e
        except MissingFZKeyError as e:
            # Specific 422 so the frontend can prompt the tech for a key
            # rather than dumping a generic invalid-board message.
            raise HTTPException(
                status_code=422,
                detail={"detail": "fz-key-missing", "message": str(e)},
            ) from e
        except ObfuscatedFileError as e:
            raise HTTPException(
                status_code=422,
                detail={"detail": "obfuscated", "message": str(e)},
            ) from e
        except MalformedHeaderError as e:
            raise HTTPException(
                status_code=422,
                detail={"detail": "malformed-header", "field": e.field, "message": str(e)},
            ) from e
        except PinPartMismatchError as e:
            raise HTTPException(
                status_code=422,
                detail={"detail": "pin-part-mismatch", "pin_index": e.pin_index, "message": str(e)},
            ) from e
        except InvalidBoardFile as e:
            raise HTTPException(
                status_code=422,
                detail={"detail": "invalid-board-file", "message": str(e)},
            ) from e
        except BoardParserError as e:
            raise HTTPException(
                status_code=422,
                detail={"detail": "parse-error", "message": str(e)},
            ) from e
        except OSError as e:
            raise HTTPException(
                status_code=400,
                detail={"detail": "io-error", "message": str(e)},
            ) from e

    return board.model_dump()


@router.get("/render")
async def render_board(
    slug: str,
    x_owner_ref: str | None = Header(default=None, alias="X-Owner-Ref"),
) -> dict:
    """Return the Three.js render payload for the active boardview of a slug.

    Tenant-scopé (T9) : le cloud injecte `X-Owner-Ref` (= tenant_id) sur le trafic
    proxifié. Managé → STRICTEMENT le boardview épinglé par CE tenant ; un tenant
    sans boardview actif obtient 404 (jamais le board d'un autre — c'était la fuite).
    Self-host (en-tête absent) → chaîne globale historique (`active_sources.json` →
    `board_assets/{slug}.<ext>` → `memory/{slug}/uploads/*-boardview-*`), inchangé.
    Returns 404 when no boardview is on disk; 422 / 415 when the file fails to parse.
    """
    # Local imports — avoids dragging the pipeline package into the board
    # router's module-load graph (FastAPI registers them in opposite order).
    from api.board.render import to_render_payload
    from api.config import get_settings
    from api.pipeline import _find_owner_boardview

    settings = get_settings()
    pack_dir = Path(settings.memory_root) / slug
    path = _find_owner_boardview(slug, pack_dir, x_owner_ref)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail={
                "detail": "no-boardview",
                "message": f"no boardview on disk for slug={slug!r}",
            },
        )
    try:
        mtime = path.stat().st_mtime
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail={"detail": "io-error", "message": str(e)},
        ) from e
    cache_key = (str(path), mtime)
    cached = _render_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        parser = parser_for(path)
        # Offload hors event-loop (cf. /parse) : un render de gros boardview ne
        # doit pas geler le moteur ni sérialiser avec les autres requêtes.
        board = await asyncio.to_thread(parser.parse_file, path)
    except UnsupportedFormatError as e:
        raise HTTPException(
            status_code=415,
            detail={"detail": "unsupported-format", "message": str(e)},
        ) from e
    except (
        ObfuscatedFileError,
        MalformedHeaderError,
        PinPartMismatchError,
        MissingFZKeyError,
        InvalidBoardFile,
        BoardParserError,
    ) as e:
        raise HTTPException(
            status_code=422,
            detail={"detail": "parse-error", "message": str(e)},
        ) from e
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail={"detail": "io-error", "message": str(e)},
        ) from e
    payload = to_render_payload(board)
    # Bound the cache before inserting. FIFO-style trim is enough here —
    # workloads alternate between a few active devices, not random churn.
    if len(_render_cache) >= _RENDER_CACHE_MAX_ENTRIES:
        # Drop the oldest entry; dict preserves insertion order.
        _render_cache.pop(next(iter(_render_cache)))
    _render_cache[cache_key] = payload
    return payload
