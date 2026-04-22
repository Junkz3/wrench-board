"""Every stub boardview parser must: (1) register its extension so the
dispatcher routes to it, and (2) raise NotImplementedError with a clear
message pointing to the roadmap when parse() is called. This catches
silent regressions where a stub gets deleted or loses its registry entry."""

from __future__ import annotations

import pytest

from api.board.parser.base import BoardParser, parser_for

STUB_EXTENSIONS = [
    (".fz", "PCB Repair Tool"),
    (".bdv", "HONHAN BoardViewer"),
    (".asc", "ASUS TSICT"),
    (".bv", "ATE BoardView"),
    (".gr", "BoardView R5.0"),
    (".cst", "IBM Lenovo CAST"),
    (".tvw", "Tebo IctView"),
    (".f2b", "Unisoft ProntoPLACE"),
    (".cad", "generic .cad"),
]


@pytest.mark.parametrize("ext,format_name", STUB_EXTENSIONS)
def test_stub_parser_registered_for_extension(tmp_path, ext, format_name):
    """parser_for dispatches to the stub by extension."""
    path = tmp_path / f"test{ext}"
    path.write_bytes(b"anything")
    p = parser_for(path)
    assert isinstance(p, BoardParser), f"{ext} did not resolve to a BoardParser"
    assert ext in p.extensions, f"{ext} parser.extensions={p.extensions}"


@pytest.mark.parametrize("ext,format_name", STUB_EXTENSIONS)
def test_stub_parser_raises_not_implemented_with_roadmap_ref(tmp_path, ext, format_name):
    """parse() raises NotImplementedError including the format name and the
    roadmap path, so the user / HTTP layer can surface a clear message."""
    path = tmp_path / f"sample{ext}"
    path.write_bytes(b"dummy payload")
    p = parser_for(path)
    with pytest.raises(NotImplementedError) as exc:
        p.parse_file(path)
    msg = str(exc.value)
    assert format_name in msg, f"format name missing from error: {msg}"
    assert ext in msg, f"extension missing from error: {msg}"
    assert "roadmap" in msg.lower(), f"roadmap ref missing from error: {msg}"
