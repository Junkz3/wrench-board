"""Production-binary `.tvw` magic detection.

Every production-binary `.tvw` opens with the same first Pascal string — the
format's signature, stored as encoded bytes — immediately followed by a
`uint32 = 1` format version. That pair is the **stable, vendor-independent**
signature of the format and is what we key on.

The two Pascal strings that follow (vendor + build/date) are **vendor-specific**
and therefore NOT part of the magic: only the header string *content* differs;
the file header reader (`walker._read_file_header`), the layer/dcode/pin readers
and every trailing-section scanner parse them identically across vendors. So the
magic check accepts the shared signature + version prefix and lets the
(vendor-agnostic) walker decode the body. Pinning the magic to one vendor's
build strings is what made some variants fail as an "Unknown TVW variant" — see
`tvw.py` and the corpus scan.
"""
from __future__ import annotations

# Stable signature: the format's signature Pascal string (stored encoded).
_MAGIC_SIG = (0x13, b"O95w-28ps49m 02v9o.")  # 0x13 length + 19 bytes
_VERSION_LE = b"\x01\x00\x00\x00"            # uint32 LE format version == 1


def is_production_binary(raw: bytes) -> bool:
    """Return True iff `raw` opens with the production-binary signature (the
    format's magic Pascal string + version), regardless of the emitting vendor.

    Layout:
        @0x00  byte 0x13 + "O95w-28ps49m 02v9o."  (encoded signature)
        @0x14  uint32 LE = 1                       (format version)
        @0x18  byte len + vendor Pascal string     (vendor-specific — not checked)
        ...    byte len + build/date Pascal string (vendor-specific — not checked)

    Keying on the version-tagged signature (not the vendor strings) keeps every
    production-binary `.tvw` in scope; the walker's header reader decodes the
    vendor/date strings and the rest of the grammar is identical across vendors.
    """
    if len(raw) < 64:
        return False
    if raw[0] != _MAGIC_SIG[0] or raw[1:1 + _MAGIC_SIG[0]] != _MAGIC_SIG[1]:
        return False
    off = 1 + _MAGIC_SIG[0]
    if raw[off:off + 4] != _VERSION_LE:
        return False
    return True
