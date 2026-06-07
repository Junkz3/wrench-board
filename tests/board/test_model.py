"""Pydantic model for Board and its components."""

from api.board.model import Board, Layer, Nail, Net, Part, Pin, Point


def test_layer_bitflag():
    assert Layer.TOP.value == 1
    assert Layer.BOTTOM.value == 2
    assert (Layer.TOP | Layer.BOTTOM) == Layer.BOTH


def test_point_is_integer_mils():
    p = Point(x=100, y=200)
    assert p.x == 100
    assert p.y == 200


def test_part_bbox_from_two_points():
    part = Part(
        refdes="U7",
        layer=Layer.TOP,
        is_smd=True,
        bbox=(Point(x=0, y=0), Point(x=100, y=50)),
        pin_refs=[0, 1, 2, 3],
    )
    assert part.refdes == "U7"
    assert part.layer == Layer.TOP
    assert part.is_smd is True
    assert part.bbox[0].x == 0
    assert part.bbox[0].y == 0
    assert part.bbox[1].x == 100
    assert part.bbox[1].y == 50
    assert part.pin_refs == [0, 1, 2, 3]


def test_pin_with_optional_net():
    pin = Pin(
        part_refdes="U7",
        index=1,
        pos=Point(x=10, y=20),
        net=None,
        probe=None,
        layer=Layer.TOP,
    )
    assert pin.net is None
    assert pin.probe is None

    pin_with_net = Pin(
        part_refdes="U7",
        index=2,
        pos=Point(x=30, y=40),
        net="+3V3",
        probe=7,
        layer=Layer.TOP,
    )
    assert pin_with_net.net == "+3V3"
    assert pin_with_net.probe == 7


def _sample_board() -> Board:
    pins = [
        Pin(part_refdes="R1", index=1, pos=Point(x=0, y=0), net="+3V3", layer=Layer.TOP),
        Pin(part_refdes="R1", index=2, pos=Point(x=10, y=0), net="GND", layer=Layer.TOP),
    ]
    parts = [
        Part(
            refdes="R1",
            layer=Layer.TOP,
            is_smd=True,
            bbox=(Point(x=0, y=0), Point(x=10, y=5)),
            pin_refs=[0, 1],
        ),
    ]
    nets = [
        Net(name="+3V3", pin_refs=[0], is_power=True, is_ground=False),
        Net(name="GND", pin_refs=[1], is_power=False, is_ground=True),
    ]
    return Board(
        board_id="test",
        file_hash="sha256:deadbeef",
        source_format="brd",
        outline=[Point(x=0, y=0), Point(x=100, y=0), Point(x=100, y=50), Point(x=0, y=50)],
        parts=parts,
        pins=pins,
        nets=nets,
        nails=[],
    )


def test_net_flags():
    n = Net(name="+3V3", pin_refs=[0, 1], is_power=True, is_ground=False)
    assert n.is_power is True
    assert n.is_ground is False


def test_nail_model():
    nail = Nail(probe=1, pos=Point(x=100, y=200), layer=Layer.TOP, net="+3V3")
    assert nail.probe == 1
    assert nail.pos.x == 100
    assert nail.pos.y == 200
    assert nail.layer == Layer.TOP
    assert nail.net == "+3V3"


def test_board_indexes_built_after_construction():
    board = _sample_board()
    assert board.part_by_refdes("R1") is not None
    assert board.part_by_refdes("R1").refdes == "R1"
    assert board.part_by_refdes("missing") is None
    assert board.net_by_name("+3V3").is_power is True


def test_board_is_json_serializable_without_private_indexes():
    board = _sample_board()
    dumped = board.model_dump()
    # private indexes must not leak into serialization
    assert "_refdes_index" not in dumped
    assert "_net_index" not in dumped


def test_board_model_copy_rebuilds_indexes():
    """model_copy must not leave stale indexes, otherwise part_by_refdes lies."""
    board = _sample_board()

    new_parts = [
        Part(
            refdes="U99",
            layer=Layer.TOP,
            is_smd=True,
            bbox=(Point(x=0, y=0), Point(x=1, y=1)),
            pin_refs=[],
        )
    ]
    copy = board.model_copy(update={"parts": new_parts})

    assert copy.part_by_refdes("U99") is not None
    assert copy.part_by_refdes("R1") is None  # old index entry must be gone


def test_part_rich_fields_default_to_none():
    p = Part(
        refdes="U1",
        layer=Layer.TOP,
        is_smd=True,
        bbox=(Point(x=0, y=0), Point(x=100, y=100)),
        pin_refs=[0],
    )
    assert p.value is None
    assert p.footprint is None
    assert p.rotation_deg is None


def test_part_rich_fields_accept_values():
    p = Part(
        refdes="U1",
        layer=Layer.TOP,
        is_smd=True,
        bbox=(Point(x=0, y=0), Point(x=100, y=100)),
        pin_refs=[0],
        value="iMX8MP-SoM",
        footprint="Connector_PinSocket:SoM-BTB-400",
        rotation_deg=90.0,
    )
    assert p.value == "iMX8MP-SoM"
    assert p.footprint == "Connector_PinSocket:SoM-BTB-400"
    assert p.rotation_deg == 90.0


def test_pin_rich_fields_default_to_none():
    pin = Pin(
        part_refdes="U1",
        index=1,
        pos=Point(x=10, y=20),
        layer=Layer.TOP,
    )
    assert pin.pad_shape is None
    assert pin.pad_size is None


def test_pin_rich_fields_accept_values():
    pin = Pin(
        part_refdes="U1",
        index=1,
        pos=Point(x=10, y=20),
        layer=Layer.TOP,
        pad_shape="rect",
        pad_size=(40, 20),
    )
    assert pin.pad_shape == "rect"
    assert pin.pad_size == (40, 20)
