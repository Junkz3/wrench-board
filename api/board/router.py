"""HTTP router for board-file parsing — stateless: accepts an upload, returns parsed JSON."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

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
            board = parser.parse(data, file_hash=file_hash, board_id=board_id)
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
async def render_board(slug: str) -> dict:
    """Return the Three.js render payload for the active boardview of a slug.

    Resolution chain matches the WS / pin probes (`active_sources.json` →
    `board_assets/{slug}.<ext>` → `memory/{slug}/uploads/*-boardview-*`).
    Returns 404 when no boardview is on disk; 422 / 415 when the file
    fails to parse (same error envelope as `/api/board/parse`).
    """
    # Local imports — avoids dragging the pipeline package into the board
    # router's module-load graph (FastAPI registers them in opposite order).
    from api.board.render import to_render_payload
    from api.config import get_settings
    from api.pipeline import _find_boardview

    settings = get_settings()
    pack_dir = Path(settings.memory_root) / slug
    path = _find_boardview(slug, pack_dir)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail={
                "detail": "no-boardview",
                "message": f"no boardview on disk for slug={slug!r}",
            },
        )
    try:
        parser = parser_for(path)
        board = parser.parse_file(path)
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
    return to_render_payload(board)
