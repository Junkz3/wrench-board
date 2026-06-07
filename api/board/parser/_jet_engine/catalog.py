"""JET4 catalog + table-definition (TDEF) parsing.

Two jobs:

  * `table_columns(raw, tdef_page)` — decode one TABLE-DEFINITION page (type 2)
    into the table's column layout (name, JET type, fixed/variable, fixed
    offset), in storage / col_id order.
  * `list_tables(raw)` — read the system catalog table **MSysObjects** to map
    each *user* table name → its TDEF root page (skipping `MSys*`).

TDEF page layout (the fields we need):

    0x10  record count (4-byte LE)        — informational
    0x2D  total column count (2-byte LE)
    0x33  real-index count (4-byte LE)    — index definitions precede the cols
    0x3F  start of the real-index definition block (12 bytes each)
    ...   then `ncol` column definitions, 25 bytes each
    ...   then `ncol` column names: each a 2-byte length + UCS-2 LE name

Column definition (25 bytes), fields we read:
    [0]      JET column type code
    [5:7]    column id (its position in the record's null bitmask & storage order)
    [7:9]    variable-column index (position among the var columns)
    [15]     flags — bit 0x01 set = fixed-length column
    [21:23]  fixed-data offset (only meaningful for fixed columns)

MSysObjects is the catalog table; its own TDEF root page is the well-known
**page 2** in every JET4 file. Each catalog row carries `Id` (= the described
object's TDEF root page), `Name`, and `Type` (1 = a real table).

Derived: recovered from real `.bv` files; cross-checked against `mdb-schema`
/ `mdb-export`. No third-party reader code copied.
"""

from __future__ import annotations

import struct

from api.board.parser._jet_engine.pages import JET4_PAGE_SIZE
from api.board.parser._jet_engine.rows import data_pages_by_owner, read_rows

# Well-known TDEF root page of the MSysObjects system catalog in every JET4 db.
_MSYSOBJECTS_TDEF_PAGE = 2

# TDEF header field offsets.
_TDEF_NUM_COLS = 0x2D
_TDEF_NUM_REAL_IDX = 0x33
_TDEF_COLDEF_BASE = 0x3F
_REAL_IDX_ENTRY_LEN = 12
_COLDEF_LEN = 25

# Catalog object type code for a real (non-system) table.
_OBJTYPE_TABLE = 1


def table_columns(raw: bytes, tdef_page: int) -> list[dict]:
    """Decode the column layout of the table whose TDEF root page is `tdef_page`.

    Returns a list of column dicts {name, type, col_id, var_idx, fixed, foff}
    sorted by `col_id` — the order columns occupy in a record's fixed region and
    null bitmask. `rows.read_rows` consumes exactly this shape.
    """
    base = tdef_page * JET4_PAGE_SIZE
    p = raw[base : base + JET4_PAGE_SIZE]
    num_cols = struct.unpack_from("<H", p, _TDEF_NUM_COLS)[0]
    num_real_idx = struct.unpack_from("<I", p, _TDEF_NUM_REAL_IDX)[0]

    # Column definitions follow the real-index definition block.
    coldef_base = _TDEF_COLDEF_BASE + num_real_idx * _REAL_IDX_ENTRY_LEN

    defs: list[dict] = []
    for c in range(num_cols):
        cd = p[coldef_base + c * _COLDEF_LEN : coldef_base + (c + 1) * _COLDEF_LEN]
        col_type = cd[0]
        col_id = struct.unpack_from("<H", cd, 5)[0]
        var_idx = struct.unpack_from("<H", cd, 7)[0]
        flags = cd[15]
        foff = struct.unpack_from("<H", cd, 21)[0]
        defs.append(
            {
                "type": col_type,
                "col_id": col_id,
                "var_idx": var_idx,
                "fixed": bool(flags & 0x01),
                "foff": foff,
            }
        )

    # Column names follow the definition block: 2-byte length + UCS-2 LE name,
    # one per column, in definition order.
    name_off = coldef_base + num_cols * _COLDEF_LEN
    for c in range(num_cols):
        nlen = struct.unpack_from("<H", p, name_off)[0]
        name_off += 2
        defs[c]["name"] = p[name_off : name_off + nlen].decode(
            "utf-16-le", errors="replace"
        )
        name_off += nlen

    # Storage / record order is by col_id, which need not match definition order.
    defs.sort(key=lambda d: d["col_id"])
    return defs


def list_tables(raw: bytes) -> dict[str, int]:
    """Return `{user_table_name: tdef_root_page}` for every non-system table.

    Reads MSysObjects (catalog) rows, keeps only rows whose `Type == 1` (a real
    table) and whose `Name` does not start with `MSys`, mapping `Name -> Id`
    (the table's TDEF root page).
    """
    cat_cols = table_columns(raw, _MSYSOBJECTS_TDEF_PAGE)
    pages_by_owner = data_pages_by_owner(raw)

    catalog_rows: list[dict] = []
    for dp in pages_by_owner.get(_MSYSOBJECTS_TDEF_PAGE, []):
        catalog_rows.extend(read_rows(raw, dp, cat_cols))

    tables: dict[str, int] = {}
    for r in catalog_rows:
        name = r.get("Name")
        if not name or name.startswith("MSys"):
            continue
        if r.get("Type") != _OBJTYPE_TABLE:
            continue
        root = r.get("Id")
        if isinstance(root, int) and root > 0:
            tables[name] = root
    return tables
