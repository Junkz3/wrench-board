"""ATE BoardView `.bv` parser.

**Two on-disk shapes, one extension.** ATE BoardView `.bv` files come in two
unrelated encodings:

  * an ASCII Test_Link-shape payload (an optional ``BoardView <version>`` banner
    followed by ``Parts:`` / ``Pins:`` / ``Nails:`` blocks), and
  * a **binary Microsoft Access JET4 database** (magic ``00 01 00 00`` +
    ``"Standard Jet DB"``, 4 KB pages) — the native ATE output. The boardview
    lives in plain relational tables: ``Pin`` (Part, TB, Pin, Name, X, Y, Layer,
    Net), ``Nail`` (test points + nets), ``Layout`` (board-outline points).

This parser handles both. Binary input is dispatched to the read-only JET4
engine (``_jet_engine``); the decoded tables are mapped to a `Board` here. Only
genuinely-unrecognised binary (looks binary but is not JET4) still trips a clear
`ObfuscatedFileError` rather than silently producing an empty Board.
"""

from __future__ import annotations

from api.board.model import Board, Layer, Nail, Part, Pin, Point
from api.board.parser._ascii_boardview import (
    DialectMarkers,
    derive_nets,
    looks_like_binary,
    normalize_bbox,
    parse_test_link_shape,
)
from api.board.parser._jet_engine import is_jet4, read_jet_tables
from api.board.parser.base import BoardParser, ObfuscatedFileError, register


@register
class BVParser(BoardParser):
    extensions = (".bv",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        if looks_like_binary(raw):
            # Native ATE output is a JET4 (MS Access) database. Route it to the
            # read-only JET4 engine and map the relational tables to a Board.
            if is_jet4(raw):
                return _board_from_jet_tables(
                    read_jet_tables(raw), board_id=board_id, file_hash=file_hash
                )
            raise ObfuscatedFileError(
                "bv: this file looks like a binary ATE BoardView container "
                "but is not a JET4 (MS Access) database. Recognised binary "
                ".bv is the Standard Jet DB shape (magic 00 01 00 00). See "
                "docs/superpowers/specs/2026-04-25-boardview-formats-v1.md."
            )
        text = raw.decode("utf-8", errors="replace")
        return parse_test_link_shape(
            text,
            markers=DialectMarkers(),
            source_format="bv",
            board_id=board_id,
            file_hash=file_hash,
        )


# ---------------------------------------------------------------------------
# JET4-table → Board mapping
# ---------------------------------------------------------------------------


def _layer_from_tb(tb: str | None) -> Layer:
    """ATE side tag → Layer. ``(T)`` = top, ``(B)`` = bottom.

    The tag is a parenthesised side marker shared by the Pin and Nail tables.
    Anything we don't recognise (None, an internal-layer tag) conservatively
    falls back to TOP — matching the BRD2 parser's "never raise on side".
    """
    if tb and "B" in tb.upper():
        return Layer.BOTTOM
    return Layer.TOP


def _as_float(v) -> float:
    """Coerce a decoded cell to float (cells are already int/float, but a null
    or stray bytes blob defends against a malformed row)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _board_from_jet_tables(
    tables: dict[str, list[dict]], *, board_id: str, file_hash: str
) -> Board:
    """Map the decoded JET4 ``Pin`` / ``Nail`` / ``Layout`` tables to a `Board`.

    Data model recovered from real exports (cross-checked vs ``mdb-export``):

      * ``Pin``: one row per *pad*, columns Part (refdes), TB (side), Pin (pad
        number), Name (silkscreen pin label, e.g. "A2"), X / Y (mils), Layer,
        Net. Pads sharing a ``Part`` form one component; a component's side is
        constant across its pads, so we take it from the first pad. The bbox is
        the min/max of the component's pad coordinates (ATE ships no explicit
        part box).
      * ``Nail``: test points — Nail (probe id like "$0"), X / Y, TB (side),
        ``NetName`` (the human net) / ``NET`` (an internal ``#NNN`` id).
      * ``Layout``: ordered board-outline points (X / Y); ``R`` / ``Group`` are
        render hints we don't surface.

    The Pin table is authoritative for parts/pins/nets; nets are derived from
    the pins (same power/ground heuristics as every other dialect).
    """
    pin_rows = tables.get("Pin", [])
    nail_rows = tables.get("Nail", [])
    layout_rows = tables.get("Layout", [])

    pins: list[Pin] = []
    # Per-part accumulation: side (from first pad) + pad-coordinate extents for
    # the synthesised bbox + the ordered pin indexes into `pins`.
    part_side: dict[str, Layer] = {}
    part_xs: dict[str, list[float]] = {}
    part_ys: dict[str, list[float]] = {}
    part_pin_refs: dict[str, list[int]] = {}
    part_order: list[str] = []  # preserve first-seen order for determinism

    for row in pin_rows:
        refdes = row.get("Part")
        if not refdes:
            continue  # a pad with no owning part is unusable
        x = _as_float(row.get("X"))
        y = _as_float(row.get("Y"))
        net = row.get("Net")
        # ATE uses "(NC)" for an explicitly-unconnected pad — normalise to None
        # so it doesn't pollute the net list as a spurious net.
        if net == "(NC)":
            net = None
        layer = _layer_from_tb(row.get("TB"))
        # `Pin` is the pad number; fall back to the running per-part position.
        pin_no = row.get("Pin")
        idx = pin_no if isinstance(pin_no, int) else len(part_pin_refs.get(refdes, [])) + 1
        name = row.get("Name")

        pin_ref = len(pins)
        pins.append(
            Pin(
                part_refdes=refdes,
                index=idx,
                pos=Point(x=x, y=y),
                net=net,
                layer=layer,
                name=str(name) if name is not None else None,
            )
        )

        if refdes not in part_side:
            part_side[refdes] = layer
            part_xs[refdes] = []
            part_ys[refdes] = []
            part_pin_refs[refdes] = []
            part_order.append(refdes)
        part_xs[refdes].append(x)
        part_ys[refdes].append(y)
        part_pin_refs[refdes].append(pin_ref)

    parts: list[Part] = []
    for refdes in part_order:
        xs, ys = part_xs[refdes], part_ys[refdes]
        bbox = normalize_bbox(min(xs), min(ys), max(xs), max(ys))
        parts.append(
            Part(
                refdes=refdes,
                layer=part_side[refdes],
                # ATE `.bv` carries no through-hole flag; default SMD like BRD2.
                # Hard classification needs footprint/schematic data.
                is_smd=True,
                bbox=bbox,
                pin_refs=part_pin_refs[refdes],
            )
        )

    nets = derive_nets(pins)

    nails = _nails_from_rows(nail_rows)
    outline = [
        Point(x=_as_float(r.get("X")), y=_as_float(r.get("Y"))) for r in layout_rows
    ]

    return Board(
        board_id=board_id,
        file_hash=file_hash,
        source_format="bv",
        outline=outline,
        parts=parts,
        pins=pins,
        nets=nets,
        nails=nails,
    )


def _nails_from_rows(nail_rows: list[dict]) -> list[Nail]:
    """Map ``Nail`` rows to `Nail` objects.

    The probe id is a string like ``"$0"`` in the source; `Nail.probe` is an
    int, so we use the row's ordinal position (1-based) as a stable probe number
    — the human-meaningful identity is the net, which we preserve. Net name
    prefers the readable ``NetName``, falling back to the internal ``NET`` id.
    """
    out: list[Nail] = []
    for i, row in enumerate(nail_rows, start=1):
        net = row.get("NetName") or row.get("NET") or ""
        out.append(
            Nail(
                probe=i,
                pos=Point(x=_as_float(row.get("X")), y=_as_float(row.get("Y"))),
                layer=_layer_from_tb(row.get("TB")),
                net=str(net),
            )
        )
    return out
