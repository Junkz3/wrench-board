"""Pydantic model for Board and its components."""

from api.board.model import Layer, Part, Pin, Point


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
