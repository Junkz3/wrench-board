# SPDX-License-Identifier: Apache-2.0
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
    ObfuscatedFileError,
    PinPartMismatchError,
    UnsupportedFormatError,
    parser_for,
)

router = APIRouter(prefix="/api/board", tags=["board"])


@router.post("/parse")
async def parse_board(file: UploadFile = File(...)) -> dict:  # noqa: B008
    name = file.filename or "upload.brd"
    suffix = Path(name).suffix or ".brd"
    data = await file.read()
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
            # Stub parser: format extension is registered but the concrete
            # parser is not yet implemented. Surface as 501 so the frontend
            # can show a "coming soon" message instead of a generic error.
            raise HTTPException(
                status_code=501,
                detail={"detail": "parser-not-implemented", "message": str(e)},
            ) from e
        except UnsupportedFormatError as e:
            raise HTTPException(
                status_code=415,
                detail={"detail": "unsupported-format", "message": str(e)},
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
