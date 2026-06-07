"""Render-side synthesis of a side-by-side 'both' layout for overlay formats.

Overlay boardview formats (.cad / .fz / .tvw) place TOP and BOTTOM components
at the SAME physical X/Y (the honest stacked layout). The viewer's 'both' mode
draws every entity at its raw coordinate with no per-side offset, so on these
boards the two faces pile on top of each other ("ça mélange les deux côtés").

XZZ (.pcb) ships the two faces SIDE BY SIDE in coordinate space, and the viewer
already renders that cleanly. So the fix is to make overlay boards produce the
same side-by-side payload XZZ produces: keep the top face in place, mirror the
bottom face and shift it to the right. Then the viewer's existing (tested)
dual-outline handling — camera recentre, side-flip chevrons — just works.

These tests pin that synthesis: it fires ONLY when the two faces actually
overlap, and it leaves genuinely side-by-side boards untouched.
"""

from api.board.model import Board, Layer, Part, Pin, Point
from api.board.render import to_render_payload

_HALF = 20.0  # mils — half the body size of the synthetic test parts


def _part(refdes: str, layer: Layer, cx: float, cy: float, pin_index: int) -> Part:
    return Part(
        refdes=refdes,
        layer=layer,
        is_smd=True,
        bbox=(Point(x=cx - _HALF, y=cy - _HALF), Point(x=cx + _HALF, y=cy + _HALF)),
        pin_refs=[pin_index],
    )


def _board(parts, pins, *, outline_max=3000.0):
    return Board(
        board_id="synth",
        file_hash="sha256:x",
        source_format="tvw",
        outline=[
            Point(x=0, y=0),
            Point(x=outline_max, y=0),
            Point(x=outline_max, y=outline_max),
            Point(x=0, y=outline_max),
        ],
        parts=parts,
        pins=pins,
        nets=[],
        nails=[],
    )


def _by_side(components):
    top = [c for c in components if c["_side"] == "top"]
    bot = [c for c in components if c["_side"] == "bottom"]
    return top, bot


def test_overlapping_faces_are_split_side_by_side():
    # Top face (U1,U3) and bottom face (U2,U4) span the SAME region -> overlay.
    pins = [
        Pin(part_refdes="U1", index=1, pos=Point(x=800, y=800), layer=Layer.TOP),
        Pin(part_refdes="U2", index=1, pos=Point(x=800, y=800), layer=Layer.BOTTOM),
        Pin(part_refdes="U3", index=1, pos=Point(x=1600, y=1600), layer=Layer.TOP),
        Pin(part_refdes="U4", index=1, pos=Point(x=1600, y=1600), layer=Layer.BOTTOM),
    ]
    parts = [
        _part("U1", Layer.TOP, 800, 800, 0),
        _part("U2", Layer.BOTTOM, 800, 800, 1),
        _part("U3", Layer.TOP, 1600, 1600, 2),
        _part("U4", Layer.BOTTOM, 1600, 1600, 3),
    ]
    payload = to_render_payload(_board(parts, pins))

    top, bot = _by_side(payload["components"])
    assert len(top) == 2 and len(bot) == 2

    top_right = max(c["x"] + c["width"] for c in top)
    bot_left = min(c["x"] for c in bot)
    # The bottom face must be shifted clear of the top face — no overlap.
    assert bot_left >= top_right, (
        f"bottom face not separated: bot_left={bot_left} top_right={top_right}"
    )

    # The viewer keys 'both' handling off dual_outline metadata.
    assert "dual_outline" in payload
    assert payload["dual_outline"]["axis"] == "x"

    # A bottom part's pin must travel WITH the part (stay attached).
    u2_pin = next(p for p in payload["pins"] if p["id"] == "U2_P1")
    assert u2_pin["x"] >= top_right

    # Two board outlines now (top + mirrored bottom).
    outline = payload["outline"]
    assert isinstance(outline, dict) and len(outline["polygons"]) == 2


def test_already_side_by_side_board_is_left_untouched():
    # Top face on the left, bottom face on the right — already disjoint.
    pins = [
        Pin(part_refdes="U1", index=1, pos=Point(x=1000, y=1000), layer=Layer.TOP),
        Pin(part_refdes="U3", index=1, pos=Point(x=2000, y=2000), layer=Layer.TOP),
        Pin(part_refdes="U2", index=1, pos=Point(x=8000, y=1000), layer=Layer.BOTTOM),
        Pin(part_refdes="U4", index=1, pos=Point(x=9000, y=2000), layer=Layer.BOTTOM),
    ]
    parts = [
        _part("U1", Layer.TOP, 1000, 1000, 0),
        _part("U3", Layer.TOP, 2000, 2000, 1),
        _part("U2", Layer.BOTTOM, 8000, 1000, 2),
        _part("U4", Layer.BOTTOM, 9000, 2000, 3),
    ]
    payload = to_render_payload(_board(parts, pins, outline_max=10000.0))

    # No synthetic split — the board already reads side-by-side.
    assert "dual_outline" not in payload
    u2 = next(c for c in payload["components"] if c["id"] == "U2")
    # Bottom part keeps its real coordinate (≈ 8000 mils -> mm), not shifted.
    assert abs(u2["x"] + u2["width"] / 2 - 8000 * 0.0254) < 0.1


def test_single_sided_board_is_not_split():
    pins = [
        Pin(part_refdes="U1", index=1, pos=Point(x=1000, y=1000), layer=Layer.TOP),
        Pin(part_refdes="U2", index=1, pos=Point(x=2000, y=2000), layer=Layer.TOP),
    ]
    parts = [
        _part("U1", Layer.TOP, 1000, 1000, 0),
        _part("U2", Layer.TOP, 2000, 2000, 1),
    ]
    payload = to_render_payload(_board(parts, pins))
    assert "dual_outline" not in payload
