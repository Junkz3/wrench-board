"""Unit tests for the read-only JET4 (MS Access) engine behind the binary `.bv`
parser.

These tests build JET4 page bytes BY HAND so CI never depends on a third-party
corpus file. The byte layout exercised here is the one derived from real ATE
BoardView `.bv` exports (no third-party code was copied), cross-checked
against a reference exporter's row counts during development:

  * 4 KB pages, page-type byte at offset 0.
  * Data page (type 1): owner-TDEF page at offset 4, row count at offset 12,
    a row-offset table of 2-byte little-endian entries starting at offset 14.
    Each row offset carries the record start in its low 13 bits; bit 0x8000
    flags a deleted row, bit 0x4000 a "lookup" pointer (both skipped).
  * Record: 2-byte column count, fixed-length columns packed from offset 2 by
    their per-column fixed offset, then the variable-length text region; at the
    record tail (read backwards): the null bitmask (ceil(ncol/8) bytes), the
    2-byte variable-column count, and a reversed table of var-column boundary
    offsets.
  * Text columns are UCS-2 LE, optionally compressed: a leading 0xFF 0xFE marker
    switches to single-byte mode (toggled again by another 0xFF 0xFE).
"""

from __future__ import annotations

import struct

from api.board.parser._jet_engine import (
    catalog,
    pages,
    read_jet_tables,
    rows,
)

# ---------------------------------------------------------------------------
# Hand-built page helpers
# ---------------------------------------------------------------------------


def _data_page(owner_tdef: int, records: list[bytes]) -> bytes:
    """Assemble a 4 KB JET4 data page (type 1) holding `records`.

    Records are laid out from the END of the page downward (the real format
    packs them against the page tail); the row-offset table at the page head
    points to each record's start, in declaration order.
    """
    page = bytearray(pages.JET4_PAGE_SIZE)
    page[0] = 1  # page type: data
    struct.pack_into("<I", page, 4, owner_tdef)
    struct.pack_into("<H", page, 12, len(records))

    cursor = pages.JET4_PAGE_SIZE
    offsets: list[int] = []
    for rec in records:
        cursor -= len(rec)
        page[cursor : cursor + len(rec)] = rec
        offsets.append(cursor)
    for i, off in enumerate(offsets):
        struct.pack_into("<H", page, 14 + 2 * i, off)
    return bytes(page)


def _record(
    ncol: int,
    fixed: bytes,
    var_segments: list[bytes],
    null_col_ids: set[int] | None = None,
) -> bytes:
    """Build one JET4 record from a fixed-data blob + ordered var segments.

    `fixed` is the packed fixed-column bytes (placed right after the 2-byte
    column count). The var segments are concatenated after it; the tail then
    carries the reversed boundary table, the var count, and the null bitmask.
    """
    null_col_ids = null_col_ids or set()
    head = struct.pack("<H", ncol) + fixed
    var_region = b"".join(var_segments)

    # Boundary offsets are absolute record offsets: start of each var segment
    # plus the end-of-data boundary. Stored REVERSED in the record tail.
    boundaries: list[int] = []
    pos = len(head)
    for seg in var_segments:
        boundaries.append(pos)
        pos += len(seg)
    boundaries.append(pos)  # EOD

    tail = b"".join(struct.pack("<H", b) for b in reversed(boundaries))
    tail += struct.pack("<H", len(var_segments))

    nmask_len = (ncol + 7) // 8
    mask = bytearray(nmask_len)
    for cid in range(ncol):
        if cid not in null_col_ids:
            mask[cid // 8] |= 1 << (cid % 8)

    return head + var_region + tail + bytes(mask)


def _tdef_page(coldefs: list[dict], num_real_idx: int = 0) -> bytes:
    """Assemble a minimal JET4 table-definition page (type 2) for `coldefs`.

    `coldefs` is a list of {name, type, col_id, var_idx, fixed, foff}. We write
    the column-count + real-index-count header fields the reader needs, then the
    25-byte column definitions, then the 2-byte-length-prefixed UCS-2 names. The
    real-index block (if any) is written as zeroed 12-byte entries — the reader
    only skips over it, never decodes it.
    """
    page = bytearray(pages.JET4_PAGE_SIZE)
    page[0] = 2  # page type: table definition
    struct.pack_into("<H", page, 0x2D, len(coldefs))
    struct.pack_into("<I", page, 0x33, num_real_idx)

    coldef_base = 0x3F + num_real_idx * 12
    # Column defs are written in DEFINITION order; the reader sorts by col_id.
    for i, c in enumerate(coldefs):
        cd = bytearray(25)
        cd[0] = c["type"]
        struct.pack_into("<H", cd, 5, c["col_id"])
        struct.pack_into("<H", cd, 7, c.get("var_idx", 0))
        cd[15] = 0x01 if c["fixed"] else 0x00
        struct.pack_into("<H", cd, 21, c.get("foff", 0))
        page[coldef_base + i * 25 : coldef_base + (i + 1) * 25] = cd

    name_off = coldef_base + len(coldefs) * 25
    for c in coldefs:
        nm = c["name"].encode("utf-16-le")
        struct.pack_into("<H", page, name_off, len(nm))
        name_off += 2
        page[name_off : name_off + len(nm)] = nm
        name_off += len(nm)
    return bytes(page)


def _msysobjects_coldefs() -> list[dict]:
    """A minimal MSysObjects column layout sufficient for `list_tables`.

    Only Id (Long @0), Name (Text), and Type (Integer @4) matter to the reader;
    real catalogs carry ~17 columns but the engine reads these three by name.
    """
    return [
        {"name": "Id", "type": 4, "col_id": 0, "var_idx": 0, "fixed": True, "foff": 0},
        {"name": "Type", "type": 3, "col_id": 1, "var_idx": 0, "fixed": True, "foff": 4},
        {"name": "Name", "type": 10, "col_id": 2, "var_idx": 0, "fixed": False, "foff": 0},
    ]


# ---------------------------------------------------------------------------
# pages.py
# ---------------------------------------------------------------------------


def test_is_jet4_accepts_standard_jet_db_header():
    raw = b"\x00\x01\x00\x00" + b"Standard Jet DB" + b"\x00" * 100
    # version byte 0x01 lives at offset 0x14
    raw = bytearray(raw)
    raw[0x14] = 0x01
    assert pages.is_jet4(bytes(raw)) is True


def test_is_jet4_rejects_ascii_and_short_input():
    assert pages.is_jet4(b"BoardView 1.5.0\nParts: 0\n") is False
    assert pages.is_jet4(b"\x00\x01\x00\x00") is False  # too short for the header


def test_page_at_and_page_type():
    p0 = bytes([0]) + b"\x00" * (pages.JET4_PAGE_SIZE - 1)
    p1 = bytes([1]) + b"\x00" * (pages.JET4_PAGE_SIZE - 1)
    raw = p0 + p1
    assert pages.page_at(raw, 0) == p0
    assert pages.page_at(raw, 1) == p1
    assert pages.page_type(p1) == 1
    assert pages.page_type(p0) == 0


# ---------------------------------------------------------------------------
# rows.decode_jet_text
# ---------------------------------------------------------------------------


def test_decode_jet_text_plain_ucs2():
    assert rows.decode_jet_text("U0700".encode("utf-16-le")) == "U0700"


def test_decode_jet_text_compressed_single_byte():
    # 0xFF 0xFE marker -> single-byte mode for the rest.
    blob = b"\xff\xfe" + b"GND"
    assert rows.decode_jet_text(blob) == "GND"


def test_decode_jet_text_compressed_then_toggled_back_to_ucs2():
    # single-byte "AB", toggle, then UCS-2 "C".
    blob = b"\xff\xfe" + b"AB" + b"\xff\xfe" + "C".encode("utf-16-le")
    assert rows.decode_jet_text(blob) == "ABC"


def test_decode_jet_text_empty():
    assert rows.decode_jet_text(b"") == ""


# ---------------------------------------------------------------------------
# rows.read_rows — fixed + variable columns, deleted-row skipping, nulls
# ---------------------------------------------------------------------------


def _pin_coldefs() -> list[dict]:
    """The Pin table column layout (storage / col_id order), as decoded from
    real `.bv` TDEF pages: Part(text), TB(text), Pin(long@0), Name(text),
    X(single@4), Y(single@8), Layer(int@12), Net(text).

    `var_idx` is each VARIABLE column's 0-based rank AMONG variable columns in
    storage (col_id) order — exactly how a real JET4 TDEF records it, and how
    `_record` packs the var segments. The variable columns here are Part, TB,
    Name, Net (col_ids 0,1,3,7) → var_idx 0,1,2,3; fixed columns' var_idx is
    irrelevant (0)."""
    return [
        {"name": "Part", "type": 10, "col_id": 0, "var_idx": 0, "fixed": False, "foff": 0},
        {"name": "TB", "type": 10, "col_id": 1, "var_idx": 1, "fixed": False, "foff": 0},
        {"name": "Pin", "type": 4, "col_id": 2, "var_idx": 0, "fixed": True, "foff": 0},
        {"name": "Name", "type": 10, "col_id": 3, "var_idx": 2, "fixed": False, "foff": 0},
        {"name": "X", "type": 6, "col_id": 4, "var_idx": 0, "fixed": True, "foff": 4},
        {"name": "Y", "type": 6, "col_id": 5, "var_idx": 0, "fixed": True, "foff": 8},
        {"name": "Layer", "type": 3, "col_id": 6, "var_idx": 0, "fixed": True, "foff": 12},
        {"name": "Net", "type": 10, "col_id": 7, "var_idx": 3, "fixed": False, "foff": 0},
    ]


def test_read_rows_decodes_fixed_and_text_columns():
    defs = _pin_coldefs()
    # fixed region: Pin(long)=1 @0, X(single)=4.5 @4, Y(single)=4.6 @8, Layer(int)=1 @12
    fixed = struct.pack("<i", 1) + struct.pack("<f", 4.5) + struct.pack("<f", 4.6)
    fixed += struct.pack("<h", 1)
    var = [
        "U0700".encode("utf-16-le"),
        "(T)".encode("utf-16-le"),
        "A2".encode("utf-16-le"),
        "(NC)".encode("utf-16-le"),
    ]
    rec = _record(8, fixed, var)
    page = _data_page(owner_tdef=33, records=[rec])
    out = rows.read_rows(page + b"\x00" * pages.JET4_PAGE_SIZE, 0, defs)
    assert len(out) == 1
    r = out[0]
    assert r["Part"] == "U0700"
    assert r["TB"] == "(T)"
    assert r["Pin"] == 1
    assert r["Name"] == "A2"
    assert abs(r["X"] - 4.5) < 1e-5
    assert abs(r["Y"] - 4.6) < 1e-5
    assert r["Layer"] == 1
    assert r["Net"] == "(NC)"


def test_read_rows_skips_deleted_rows():
    defs = _pin_coldefs()
    fixed = struct.pack("<i", 7) + struct.pack("<f", 0.0) + struct.pack("<f", 0.0)
    fixed += struct.pack("<h", 2)
    rec = _record(
        8,
        fixed,
        [
            "R1".encode("utf-16-le"),
            "(B)".encode("utf-16-le"),
            "1".encode("utf-16-le"),
            "GND".encode("utf-16-le"),
        ],
    )
    page = bytearray(_data_page(owner_tdef=33, records=[rec]))
    # Flip the deleted bit on row 0's offset entry.
    off = struct.unpack_from("<H", page, 14)[0]
    struct.pack_into("<H", page, 14, off | 0x8000)
    out = rows.read_rows(bytes(page), 0, defs)
    assert out == []


def test_read_rows_handles_null_text_column():
    defs = _pin_coldefs()
    fixed = struct.pack("<i", 1) + struct.pack("<f", 1.0) + struct.pack("<f", 2.0)
    fixed += struct.pack("<h", 1)
    # Net (col_id 7) is null -> not present in the var region.
    var = [
        "U1".encode("utf-16-le"),
        "(T)".encode("utf-16-le"),
        "1".encode("utf-16-le"),
    ]
    rec = _record(8, fixed, var, null_col_ids={7})
    page = _data_page(owner_tdef=33, records=[rec])
    out = rows.read_rows(page, 0, defs)
    assert out[0]["Net"] is None
    assert out[0]["Part"] == "U1"


# ---------------------------------------------------------------------------
# catalog.py — TDEF column decode + table listing
# ---------------------------------------------------------------------------


def test_table_columns_decodes_names_types_and_order():
    defs = _pin_coldefs()
    tdef = _tdef_page(defs)
    raw = b"\x00" * pages.JET4_PAGE_SIZE  # page 0 placeholder
    raw += tdef  # this TDEF lands at page index 1
    cols = catalog.table_columns(raw, 1)
    assert [c["name"] for c in cols] == [
        "Part", "TB", "Pin", "Name", "X", "Y", "Layer", "Net"
    ]
    pin_col = next(c for c in cols if c["name"] == "Pin")
    assert pin_col["fixed"] is True and pin_col["type"] == 4 and pin_col["foff"] == 0
    part_col = next(c for c in cols if c["name"] == "Part")
    assert part_col["fixed"] is False and part_col["type"] == 10


def _build_min_jet4() -> bytes:
    """Build a complete minimal JET4 image: header + MSysObjects catalog +
    one user table ("Pin") with two rows, all at known page indexes.

    Page map:
        0  db header (type 0)
        1  Pin TDEF       (type 2)
        2  MSysObjects TDEF (type 2) — the well-known catalog root page
        3  MSysObjects data page (owner=2): one row describing table "Pin"
        4  Pin data page  (owner=1): two Pin rows
    """
    # Page 0 — db header (only the JET4 signature matters to is_jet4).
    header = bytearray(pages.JET4_PAGE_SIZE)
    header[:4] = b"\x00\x01\x00\x00"
    header[4 : 4 + len(b"Standard Jet DB")] = b"Standard Jet DB"
    header[0x14] = 0x01

    pin_tdef = _tdef_page(_pin_coldefs())  # page 1
    msys_tdef = _tdef_page(_msysobjects_coldefs())  # page 2

    # Catalog row: Id=1 (Pin's TDEF page), Type=1 (table), Name="Pin".
    cat_fixed = struct.pack("<i", 1) + struct.pack("<h", 1)
    cat_rec = _record(3, cat_fixed, ["Pin".encode("utf-16-le")])
    cat_data = _data_page(owner_tdef=2, records=[cat_rec])  # page 3

    # Two Pin rows.
    def pin_rec(part, tb, pin, name, x, y, layer, net):
        fx = struct.pack("<i", pin) + struct.pack("<f", x) + struct.pack("<f", y)
        fx += struct.pack("<h", layer)
        return _record(
            8,
            fx,
            [
                part.encode("utf-16-le"),
                tb.encode("utf-16-le"),
                name.encode("utf-16-le"),
                net.encode("utf-16-le"),
            ],
        )

    pin_data = _data_page(  # page 4
        owner_tdef=1,
        records=[
            pin_rec("R1", "(T)", 1, "1", 1.0, 2.0, 1, "GND"),
            pin_rec("R1", "(T)", 2, "2", 1.5, 2.0, 1, "+3V3"),
        ],
    )
    return bytes(header) + pin_tdef + msys_tdef + cat_data + pin_data


def test_list_tables_finds_user_table_via_catalog():
    raw = _build_min_jet4()
    assert pages.is_jet4(raw) is True
    tables = catalog.list_tables(raw)
    assert tables == {"Pin": 1}


def test_read_jet_tables_end_to_end():
    raw = _build_min_jet4()
    out = read_jet_tables(raw)
    assert set(out) == {"Pin"}
    assert len(out["Pin"]) == 2
    r0 = out["Pin"][0]
    assert r0["Part"] == "R1" and r0["Pin"] == 1 and r0["Net"] == "GND"
    assert abs(r0["X"] - 1.0) < 1e-5
    assert out["Pin"][1]["Net"] == "+3V3"
