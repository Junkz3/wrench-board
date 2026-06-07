from api.board.model import Board, Layer, Part, Pin, Point, Trace
from api.board.render import to_render_payload


def test_render_payload_fits_to_outline_when_present():
    board = Board(
        board_id="outline-fit",
        file_hash="sha256:test",
        source_format="tvw",
        outline=[
            Point(x=0, y=0),
            Point(x=1000, y=0),
            Point(x=1000, y=500),
            Point(x=0, y=500),
        ],
        parts=[],
        pins=[],
        nets=[],
        nails=[],
        traces=[
            Trace(
                a=Point(x=20_000_000, y=20_000_000),
                b=Point(x=21_000_000, y=20_000_000),
                layer=1,
            )
        ],
    )

    payload = to_render_payload(board)

    assert payload["board_offset_x"] == 0
    assert payload["board_offset_y"] == 0
    assert payload["board_width"] == 25.4
    assert payload["board_height"] == 12.7
    assert len(payload["outline"]) == 4


def test_render_payload_uses_spatial_data_without_outline():
    board = Board(
        board_id="no-outline-fit",
        file_hash="sha256:test",
        source_format="brd",
        outline=[],
        parts=[],
        pins=[],
        nets=[],
        nails=[],
        traces=[
            Trace(
                a=Point(x=0, y=0),
                b=Point(x=1000, y=500),
                layer=1,
            )
        ],
    )

    payload = to_render_payload(board)

    assert payload["board_offset_x"] == 0
    assert payload["board_offset_y"] == 0
    assert payload["board_width"] == 25.4
    assert payload["board_height"] == 12.7


def test_synthesizes_board_edge_from_extent_when_no_outline_or_traces():
    # Assembly-only exports (e.g. CPD) carry no outline polygon and
    # nothing on layer 28. The renderer must still draw a board: a convex
    # hull of the placed pins/parts so the viewer shows a substrate.
    pins = [
        Pin(part_refdes="R1", index=1, pos=Point(x=0, y=0), net="GND",
            layer=Layer.TOP),
        Pin(part_refdes="R1", index=2, pos=Point(x=1000, y=0), net="N1",
            layer=Layer.TOP),
        Pin(part_refdes="C1", index=1, pos=Point(x=1000, y=800), net="N1",
            layer=Layer.TOP),
        Pin(part_refdes="C1", index=2, pos=Point(x=0, y=800), net="GND",
            layer=Layer.TOP),
    ]
    board = Board(
        board_id="synth-edge",
        file_hash="sha256:test",
        source_format="cad",
        outline=[],
        parts=[
            Part(refdes="R1", layer=Layer.TOP, is_smd=True,
                 bbox=(Point(x=0, y=0), Point(x=1000, y=0)), pin_refs=[0, 1]),
        ],
        pins=pins,
        nets=[],
        nails=[],
        traces=[],  # nothing on layer 28
    )

    payload = to_render_payload(board)
    outline = payload["outline"]
    # The viewer accepts a flat point list or a {"polygons": [...]} wrapper.
    polys = outline["polygons"] if isinstance(outline, dict) else [outline]
    assert polys and all(len(p) >= 3 for p in polys)
    pts = [pt for poly in polys for pt in poly]
    xs = [p["x"] for p in pts]
    ys = [p["y"] for p in pts]
    # Hull spans the full placed extent (0..1000 mils -> 0..25.4 mm).
    assert min(xs) == 0 and max(xs) == 25.4
    assert min(ys) == 0 and max(ys) == 20.32
