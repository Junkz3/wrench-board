from api.tools.ws_events import (
    DrawArrow,
    Highlight,
    HighlightNet,
    UploadError,
)


def test_highlight_envelope_round_trip():
    e = Highlight(refdes=["U7"], color="accent")
    dumped = e.model_dump()
    assert dumped["type"] == "boardview.highlight"
    assert dumped["refdes"] == ["U7"]
    assert dumped["color"] == "accent"
    assert dumped["additive"] is False


def test_highlight_net_envelope_shape():
    e = HighlightNet(net="+3V3", pin_refs=[1, 2, 3])
    assert e.model_dump()["type"] == "boardview.highlight_net"


def test_upload_error_envelope():
    e = UploadError(reason="obfuscated", message="refused")
    dumped = e.model_dump()
    assert dumped["reason"] == "obfuscated"


def test_draw_arrow_serializes_alias_over_wire():
    e = DrawArrow(from_=(0, 0), to=(10, 10), id="arr-1")
    dumped = e.model_dump()
    assert "from" in dumped
    assert "from_" not in dumped
    assert dumped["from"] == (0, 0)
    assert dumped["to"] == (10, 10)
