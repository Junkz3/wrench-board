"""JET4 page primitives — size, format detection, page slicing & typing.

A JET4 (MS Access 2000–2003) database is a flat array of fixed 4 KB pages. The
first page (index 0) is the database header; every later page carries a 1-byte
*page type* at offset 0 that tells the reader what it holds:

    0x00  database definition (the header page itself)
    0x01  data page       — holds table rows (see rows.py)
    0x02  table definition — column layout for one table (see catalog/rows)
    0x03  intermediate index page
    0x04  leaf index page
    0x05  page-usage bitmap

We only ever need the header (to confirm the format), the catalog data pages,
and the user-table data pages. Index / usage pages are walked structurally
elsewhere, never decoded here.

Note: the constants and offsets below were recovered by inspecting
real `.bv` exports byte-by-byte and cross-checking decoded row counts against
`mdb-export`. No third-party JET-reader source was consulted or copied.
"""

from __future__ import annotations

# Every JET4 page is exactly 4096 bytes. (JET3 / Access 97 used 2 KB pages and a
# different obfuscation scheme — out of scope; those `.bv` files don't appear in
# the corpus and would carry a different version byte.)
JET4_PAGE_SIZE = 4096

# The fixed file signature: 4 magic bytes, then the ASCII tag "Standard Jet DB"
# at offset 4. Offset 0x14 holds the JET version byte (0x01 = JET4 / Access 2000+).
_MAGIC = b"\x00\x01\x00\x00"
_JET_TAG = b"Standard Jet DB"
_JET_TAG_OFFSET = 4
_VERSION_OFFSET = 0x14
_VERSION_JET4 = 0x01


def is_jet4(raw: bytes) -> bool:
    """True iff `raw` begins with a JET4 database header.

    We require the full signature (magic + "Standard Jet DB" tag + the JET4
    version byte) rather than the 4 magic bytes alone: the bare magic
    `00 01 00 00` is too weak to dispatch a parser on, and a non-JET4 version
    byte must NOT be decoded by this engine (its page layout differs).
    """
    if len(raw) < _VERSION_OFFSET + 1:
        return False
    if raw[:4] != _MAGIC:
        return False
    tag = raw[_JET_TAG_OFFSET : _JET_TAG_OFFSET + len(_JET_TAG)]
    if tag != _JET_TAG:
        return False
    return raw[_VERSION_OFFSET] == _VERSION_JET4


def page_count(raw: bytes) -> int:
    """Number of whole 4 KB pages in `raw` (a trailing partial page is ignored —
    real files are always page-aligned, but corpus dumps occasionally carry a
    few stray trailing bytes)."""
    return len(raw) // JET4_PAGE_SIZE


def page_at(raw: bytes, index: int) -> bytes:
    """Return the 4 KB page at `index`. Raises IndexError past the last page."""
    start = index * JET4_PAGE_SIZE
    end = start + JET4_PAGE_SIZE
    if start < 0 or end > len(raw):
        raise IndexError(f"page {index} out of range (have {page_count(raw)} pages)")
    return raw[start:end]


def page_type(page: bytes) -> int:
    """The page-type byte (offset 0). 1 = data, 2 = table definition, etc."""
    return page[0] if page else -1
