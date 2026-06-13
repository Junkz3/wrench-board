"""JET4 data-page row decoding + the text-column codec.

A JET4 **data page** (type 1) packs variable-length records against the page
tail and indexes them with a row-offset table at the page head:

    offset 0      page type (1)
    offset 4      owner table-definition page (4-byte LE) — which table owns
                  these rows; we use this to group data pages by table without
                  walking the TDEF usage map.
    offset 12     row count (2-byte LE)
    offset 14..   row-offset table: one 2-byte LE entry per row. Low 13 bits =
                  record start within the page; bit 0x8000 = deleted row,
                  bit 0x4000 = "lookup" pointer. Both flagged kinds are skipped.

Each **record** is self-describing:

    [0:2]   column count (2-byte LE)
    [2:..]  fixed-length columns, each at `2 + col.foff`
    [..]    variable-length (text/memo) data region
    ── tail, read BACKWARDS ──
    [-nmask:]              null bitmask, ceil(ncol/8) bytes (bit set = present)
    [..-2]                 variable-column count (2-byte LE)
    [.. table ..]          (nvar+1) boundary offsets (2-byte LE) **reversed**:
                           reversed → ascending var-segment starts + EOD.

`decode_jet_text` handles JET4's optional text compression: text is UCS-2 LE,
but a leading `0xFF 0xFE` marker switches to single-byte (one byte per char)
mode; a second `0xFF 0xFE` toggles back to UCS-2. Real ATE `.bv` exports we
sampled store text uncompressed, but the compressed form is legal JET4 and a
foreign export could use it, so we decode it rather than mis-read it.

Column `type` codes used by `.bv` tables:
    3  Integer  (2-byte signed)
    4  Long     (4-byte signed)
    6  Single   (4-byte IEEE float)
    7  Double   (8-byte IEEE float)
    10 Text     (variable, UCS-2 / compressed)

Derived: layout recovered from real files + cross-checked against
`mdb-export`; no third-party reader code copied.
"""

from __future__ import annotations

import struct
from collections import defaultdict

from api.board.parser._jet_engine.pages import JET4_PAGE_SIZE, page_count

# JET column type codes (subset present in `.bv` Pin/Nail/Layout tables).
_T_INT = 3
_T_LONG = 4
_T_SINGLE = 6
_T_DOUBLE = 7
_T_TEXT = 10

_ROW_OFFSET_MASK = 0x1FFF  # low 13 bits = record start
_ROW_DELETED = 0x8000
_ROW_LOOKUP = 0x4000


def decode_jet_text(raw: bytes) -> str:
    """Decode a JET4 text-column blob (UCS-2 LE, optionally compressed)."""
    if not raw:
        return ""
    # Uncompressed (the common case): straight UCS-2 LE.
    if raw[:2] != b"\xff\xfe":
        return raw.decode("utf-16-le", errors="replace")
    # Compressed: a 0xFF 0xFE marker switches to single-byte mode; each further
    # 0xFF 0xFE toggles the mode. We start *compressed* right after the marker.
    out: list[str] = []
    i = 2
    compressed = True
    n = len(raw)
    while i < n:
        if raw[i : i + 2] == b"\xff\xfe":
            compressed = not compressed
            i += 2
            continue
        if compressed:
            # Single-byte: the source byte is a Latin-1 code unit.
            out.append(chr(raw[i]))
            i += 1
        else:
            out.append(raw[i : i + 2].decode("utf-16-le", errors="replace"))
            i += 2
    return "".join(out)


def _decode_fixed(rec: bytes, col_type: int, base: int):
    """Decode one fixed-length column starting at `base` in the record."""
    if col_type == _T_LONG:
        return struct.unpack_from("<i", rec, base)[0]
    if col_type == _T_INT:
        return struct.unpack_from("<h", rec, base)[0]
    if col_type == _T_SINGLE:
        return struct.unpack_from("<f", rec, base)[0]
    if col_type == _T_DOUBLE:
        return struct.unpack_from("<d", rec, base)[0]
    # Any other fixed type (byte, currency, …) is not used by `.bv` tables;
    # surface None rather than guess a width.
    return None


def _decode_record(rec: bytes, coldefs: list[dict]) -> dict | None:
    """Decode a single record's bytes into a {column_name: value} dict.

    `coldefs` is the table's column layout in STORAGE (col_id) order, each a
    dict {name, type, col_id, fixed, foff}. Returns None for a record too short
    to hold its own header (corrupt / truncated row).
    """
    if len(rec) < 2:
        return None

    ncol = struct.unpack_from("<H", rec, 0)[0]
    nmask_len = (ncol + 7) // 8
    if len(rec) < nmask_len + 2:
        return None
    null_mask = rec[len(rec) - nmask_len :]

    def is_present(col_id: int) -> bool:
        # Bit set in the mask = the column is PRESENT (non-null). JET4 stores
        # the null bitmask "inverted" relative to the usual convention.
        if col_id >= nmask_len * 8:
            return True
        return bool((null_mask[col_id // 8] >> (col_id % 8)) & 1)

    nvar = sum(1 for c in coldefs if not c["fixed"])

    # Recover the reversed var-boundary table from the record tail.
    var_bounds: list[int] = []
    if nvar:
        body_end = len(rec) - nmask_len
        stored_nvar = struct.unpack_from("<H", rec, body_end - 2)[0]
        # Trust the per-table column count; the stored count should match but a
        # mismatch on a corrupt row would otherwise mis-slice — clamp to it.
        eff_nvar = stored_nvar if stored_nvar == nvar else nvar
        table_start = body_end - 2 - 2 * (eff_nvar + 1)
        if table_start < 0:
            return None
        vals = [
            struct.unpack_from("<H", rec, table_start + 2 * k)[0]
            for k in range(eff_nvar + 1)
        ]
        var_bounds = vals[::-1]  # reversed → ascending segment boundaries + EOD

    row: dict = {}
    for col in coldefs:
        if col["fixed"]:
            base = 2 + col["foff"]
            if base + 8 > len(rec) and col["type"] == _T_DOUBLE:
                row[col["name"]] = None
            else:
                try:
                    row[col["name"]] = _decode_fixed(rec, col["type"], base)
                except struct.error:
                    row[col["name"]] = None
            continue

        # Variable-length (text) column. JET4 places each variable value at the
        # slot given by its `var_idx` (its rank among variable columns), which is
        # NOT necessarily col_id/storage order. Index the boundary table by the
        # decoded `var_idx` rather than a running counter, so an export where the
        # two orders diverge still slices its text columns correctly. (On this
        # corpus var_idx is monotonic with col_id, so both agree — but honoring
        # the real field removes the silent-mis-slice trap for foreign files.)
        vi = col["var_idx"]
        if not is_present(col["col_id"]):
            row[col["name"]] = None
            continue
        if vi < 0 or vi + 1 >= len(var_bounds):
            row[col["name"]] = None
            continue
        a, b = var_bounds[vi], var_bounds[vi + 1]
        seg = rec[a:b] if 0 <= a <= b <= len(rec) else b""
        if col["type"] == _T_TEXT:
            row[col["name"]] = decode_jet_text(seg)
        else:
            row[col["name"]] = seg
    return row


def read_rows(raw: bytes, data_page: int, coldefs: list[dict]) -> list[list]:
    """Decode every live record on the data page at index `data_page`.

    Returns a list of {column_name: value} dicts (the README in the package
    `__init__` describes the tuple-of-rows shape used at the engine boundary;
    here we keep names so the parser maps by column, not position). Deleted and
    lookup rows are skipped.
    """
    start = data_page * JET4_PAGE_SIZE
    page = raw[start : start + JET4_PAGE_SIZE]
    if len(page) < 14:
        return []
    nrows = struct.unpack_from("<H", page, 12)[0]
    # Row-offset entries are at the head; each row spans [offs[j], offs[j-1]) —
    # records pack downward from the page tail, so row 0's end is the page end.
    offsets = [struct.unpack_from("<H", page, 14 + 2 * j)[0] for j in range(nrows)]

    out: list[dict] = []
    for j in range(nrows):
        flag = offsets[j]
        if flag & (_ROW_DELETED | _ROW_LOOKUP):
            continue
        rec_start = flag & _ROW_OFFSET_MASK
        rec_end = (offsets[j - 1] & _ROW_OFFSET_MASK) if j > 0 else JET4_PAGE_SIZE
        if not (0 <= rec_start <= rec_end <= JET4_PAGE_SIZE):
            continue
        rec = page[rec_start:rec_end]
        decoded = _decode_record(rec, coldefs)
        if decoded is not None:
            out.append(decoded)
    return out


def data_pages_by_owner(raw: bytes) -> dict[int, list[int]]:
    """Map each table-definition page → the list of data-page indexes it owns.

    Every JET4 data page records its owner TDEF page at offset 4. Grouping by
    that field lets us collect a table's rows without parsing the TDEF's
    usage-map page-pointer chain — the data pages declare their own ownership,
    and cross-checking decoded counts against `mdb-export` confirms this finds
    every page (no rows missed).
    """
    by: dict[int, list[int]] = defaultdict(list)
    for i in range(page_count(raw)):
        base = i * JET4_PAGE_SIZE
        if raw[base] == 1:  # data page
            owner = struct.unpack_from("<I", raw, base + 4)[0]
            by[owner].append(i)
    return dict(by)
