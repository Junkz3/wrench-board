"""`.fz` boardview parser.

Two flavours of `.fz` exist in the field and this parser dispatches
between them:

1. **FZ-zlib**. 4-byte LE int32 size header followed directly by a
   zlib stream that decompresses to a pipe-delimited (`!`) section
   format with `A!schema` / `S!data` rows. Implemented in
   `_fz_zlib.py`. No key required.

2. **FZ-xor**. The same FZ-zlib container wrapped in a 16-byte
   sliding-window RC6-shaped cipher keyed by a 44 × uint32 expanded
   key. Decrypt with `_fz_engine.cipher.decrypt_fz_xor`, then hand the
   plaintext back through `parse_fz_zlib`.

Dispatch: peek bytes 4-5 — a zlib magic (`78 9c` / `78 da` / `78 01`)
routes to FZ-zlib directly; otherwise the bytes go through the XOR
decrypt first and the result must surface a zlib magic at offset 4 of
the recovered plaintext (or the file is rejected as malformed).

The cipher key is loaded from `WRENCH_BOARD_FZ_KEY` (see
`_fz_engine.cipher`). FZ-zlib parsing works without the key; FZ-xor
files raise a clear error when the key is unset.
"""

from __future__ import annotations

from api.board.model import Board
from api.board.parser._fz_engine.cipher import (
    FZ_KEY_ENV,
    FZKeyNotConfigured,
    KEY_WORDS,
    decrypt_fz_xor,
)
from api.board.parser._fz_zlib import looks_like_fz_zlib, parse_fz_zlib
from api.board.parser.base import BoardParser, InvalidBoardFile, register

_KEY_WORDS_LEN = 44


@register
class FZParser(BoardParser):
    extensions = (".fz",)

    def __init__(self, key: tuple[int, ...] | None = None):
        if key is not None and len(key) != _KEY_WORDS_LEN:
            key = None
        self.key = key if key is not None else KEY_WORDS

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        if looks_like_fz_zlib(raw):
            return parse_fz_zlib(raw, file_hash=file_hash, board_id=board_id, source_format="fz")
        try:
            plain = decrypt_fz_xor(raw, self.key)
        except FZKeyNotConfigured as exc:
            raise InvalidBoardFile(str(exc)) from exc
        if not looks_like_fz_zlib(plain):
            raise InvalidBoardFile(
                "fz-xor: decryption did not surface the expected zlib container "
                "(bytes 4-5 are not a zlib magic). Either the file is corrupt "
                "or it uses a different key — set "
                f"{FZ_KEY_ENV} to override."
            )
        return parse_fz_zlib(plain, file_hash=file_hash, board_id=board_id, source_format="fz")
