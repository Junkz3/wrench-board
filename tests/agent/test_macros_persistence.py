"""Persistence helpers for macro images (Flow A + Flow B)."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.agent.macros import (
    build_image_ref,
    macro_path_for,
    persist_macro,
)


def test_persist_macro_writes_jpeg(tmp_path: Path):
    bytes_data = b"\xff\xd8\xff\xe0fake_jpeg_payload"
    path = persist_macro(
        memory_root=tmp_path,
        slug="iphone-x",
        repair_id="R1",
        source="manual",
        bytes_=bytes_data,
        mime="image/jpeg",
    )
    assert path.exists()
    assert path.suffix == ".jpg"
    assert path.read_bytes() == bytes_data
    assert path.parent.name == "macros"
    assert "_manual." in path.name


def test_persist_macro_png_extension_from_mime(tmp_path: Path):
    path = persist_macro(
        memory_root=tmp_path, slug="iphone-x", repair_id="R1",
        source="capture", bytes_=b"\x89PNG\r\n\x1a\n", mime="image/png",
    )
    assert path.suffix == ".png"
    assert "_capture." in path.name


def test_persist_macro_rejects_unknown_mime(tmp_path: Path):
    with pytest.raises(ValueError, match="unsupported mime"):
        persist_macro(
            memory_root=tmp_path, slug="iphone-x", repair_id="R1",
            source="manual", bytes_=b"x", mime="application/pdf",
        )


def test_persist_macro_rejects_invalid_source(tmp_path: Path):
    with pytest.raises(ValueError, match="source"):
        persist_macro(
            memory_root=tmp_path, slug="iphone-x", repair_id="R1",
            source="foo", bytes_=b"x", mime="image/png",
        )


def test_persist_macro_disambiguates_same_second_collisions(tmp_path: Path):
    """Two captures in the same second must not overwrite each other."""
    p1 = persist_macro(
        memory_root=tmp_path, slug="iphone-x", repair_id="R1",
        source="capture", bytes_=b"first", mime="image/jpeg",
    )
    p2 = persist_macro(
        memory_root=tmp_path, slug="iphone-x", repair_id="R1",
        source="capture", bytes_=b"second", mime="image/jpeg",
    )
    assert p1 != p2
    assert p1.read_bytes() == b"first"
    assert p2.read_bytes() == b"second"


def test_macro_path_for_resolves_under_macros_dir(tmp_path: Path):
    path = macro_path_for(
        memory_root=tmp_path, slug="iphone-x", repair_id="R1",
        filename="1745704812_manual.png",
    )
    assert path == tmp_path / "iphone-x" / "repairs" / "R1" / "macros" / "1745704812_manual.png"


def test_macro_path_for_blocks_path_traversal(tmp_path: Path):
    with pytest.raises(ValueError, match="invalid filename"):
        macro_path_for(
            memory_root=tmp_path, slug="iphone-x", repair_id="R1",
            filename="../../etc/passwd",
        )
    with pytest.raises(ValueError, match="invalid filename"):
        macro_path_for(
            memory_root=tmp_path, slug="iphone-x", repair_id="R1",
            filename="/etc/passwd",
        )
    with pytest.raises(ValueError, match="invalid filename"):
        macro_path_for(
            memory_root=tmp_path, slug="iphone-x", repair_id="R1",
            filename=".hidden",
        )


def test_build_image_ref_shape(tmp_path: Path):
    macros_dir = tmp_path / "iphone-x" / "repairs" / "R1" / "macros"
    macros_dir.mkdir(parents=True)
    img_path = macros_dir / "1745704812_manual.png"
    img_path.touch()
    ref = build_image_ref(
        path=img_path,
        memory_root=tmp_path,
        slug="iphone-x",
        repair_id="R1",
        source="manual",
    )
    assert ref == {
        "type": "image_ref",
        "path": "macros/1745704812_manual.png",
        "source": "manual",
    }
