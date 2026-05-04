"""TVW production-binary magic detection.

Three Pascal-encoded constant strings appear at the start of every
production-binary TVW. Detecting the combination is far more
discriminating than the previous non-printable-byte heuristic — fewer
false positives on ASCII rotation-cipher TVWs that happen to carry
binary garbage.
"""
from __future__ import annotations

# Pascal-string constants. Each tuple is (length_byte, magic_bytes).
_MAGIC_1 = (0x13, b"O95w-28ps49m 02v9o.")  # 19 bytes
_MAGIC_2 = (0x07, b"G5u9k8s")              # 7 bytes
_MAGIC_3 = (0x08, b"B!Z@6sob")             # 8 bytes


def is_production_binary(raw: bytes) -> bool:
    """Return True iff `raw` opens with the 3 production-binary magic Pascal strings.

    Layout:
        @0x00  byte 0x13 + "O95w-28ps49m 02v9o."  (magic 1)
        @0x14  uint32 LE = 1                       (format version)
        @0x18  byte 0x07 + "G5u9k8s"              (magic 2)
        @0x20  byte 0x08 + "B!Z@6sob"             (magic 3)
    """
    if len(raw) < 64:
        return False
    if raw[0] != _MAGIC_1[0] or raw[1:1 + _MAGIC_1[0]] != _MAGIC_1[1]:
        return False
    off = 1 + _MAGIC_1[0]
    # uint32 version = 1
    if raw[off:off + 4] != b"\x01\x00\x00\x00":
        return False
    off += 4
    if raw[off] != _MAGIC_2[0] or raw[off + 1:off + 1 + _MAGIC_2[0]] != _MAGIC_2[1]:
        return False
    off += 1 + _MAGIC_2[0]
    if raw[off] != _MAGIC_3[0] or raw[off + 1:off + 1 + _MAGIC_3[0]] != _MAGIC_3[1]:
        return False
    return True
