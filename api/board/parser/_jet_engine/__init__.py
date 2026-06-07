"""Read-only JET4 (MS Access 2000–2003) engine for binary ATE BoardView `.bv`.

Real ATE BoardView `.bv` exports are not text — they are Microsoft Access JET4
databases (magic `00 01 00 00` + "Standard Jet DB", 4 KB pages). The boardview
lives in plain relational tables: `Pin` (Part, TB, Pin, Name, X, Y, Layer, Net),
`Nail` (test points + nets), `Layout` (board-outline points). This package is a
minimal, dependency-free reader that extracts those tables; `bv.py` maps them to
the `Board` model.

Layering: `pages` (page primitives) → `catalog` (table name → root page) +
`rows` (data-page record decode). `read_jet_tables` ties them together.

Derived: the JET4 layout was recovered from real files and cross-checked
against `mdb-export`; no third-party reader code was copied.
"""

from __future__ import annotations

from api.board.parser._jet_engine.catalog import list_tables, table_columns
from api.board.parser._jet_engine.pages import is_jet4
from api.board.parser._jet_engine.rows import data_pages_by_owner, read_rows


def read_jet_tables(raw: bytes) -> dict[str, list[dict]]:
    """Return `{user_table_name: [row_dict, ...]}` for every non-system table.

    Walks the MSysObjects catalog to map each user table to its
    table-definition (root) page, derives that table's column layout, then
    decodes all data pages the table owns. System tables (`MSys*`) are skipped.
    """
    tables = list_tables(raw)  # name -> TDEF root page
    pages_by_owner = data_pages_by_owner(raw)
    result: dict[str, list[dict]] = {}
    for name, tdef_page in tables.items():
        coldefs = table_columns(raw, tdef_page)
        rows: list[dict] = []
        for dp in pages_by_owner.get(tdef_page, []):
            rows.extend(read_rows(raw, dp, coldefs))
        result[name] = rows
    return result


__all__ = [
    "is_jet4",
    "list_tables",
    "table_columns",
    "read_rows",
    "read_jet_tables",
    "data_pages_by_owner",
]
