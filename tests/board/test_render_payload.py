from api.board.model import Board, Point, Trace
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
