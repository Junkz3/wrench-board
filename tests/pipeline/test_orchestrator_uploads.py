# SPDX-License-Identifier: Apache-2.0
"""Tests for the uploads scanner that the pipeline orchestrator runs
before Scout. Covers shape (kinds extracted, most-recent-wins for
schematic/boardview, accumulation for datasheets), edge cases (empty
dir, missing dir, malformed filenames), and helpers."""

from __future__ import annotations

from pathlib import Path

from api.pipeline.orchestrator import (
    UploadedDocuments,
    scan_uploads,
)


def test_scan_missing_directory_returns_empty(tmp_path: Path) -> None:
    out = scan_uploads(tmp_path / "does-not-exist")
    assert out == UploadedDocuments()
    assert out.is_empty()


def test_scan_empty_directory_returns_empty(tmp_path: Path) -> None:
    d = tmp_path / "uploads"
    d.mkdir()
    out = scan_uploads(d)
    assert out.is_empty()


def test_scan_groups_files_by_kind(tmp_path: Path) -> None:
    d = tmp_path / "uploads"
    d.mkdir()
    (d / "20260424T120000Z-schematic_pdf-reform2.pdf").write_bytes(b"%PDF stub")
    (d / "20260424T120100Z-boardview-reform2.kicad_pcb").write_bytes(b"(kicad_pcb stub)")
    (d / "20260424T120200Z-datasheet-lm2677.pdf").write_bytes(b"%PDF stub2")
    (d / "20260424T120300Z-datasheet-atsaml21.pdf").write_bytes(b"%PDF stub3")
    (d / "20260424T120400Z-notes-tech-notes.txt").write_text("bench-side context")
    (d / "manually-dropped-thing.pdf").write_bytes(b"orphan")

    out = scan_uploads(d)

    assert out.schematic_pdf is not None
    assert out.schematic_pdf.name == "20260424T120000Z-schematic_pdf-reform2.pdf"
    assert out.boardview is not None
    assert out.boardview.name == "20260424T120100Z-boardview-reform2.kicad_pcb"
    assert {p.name for p in out.datasheets} == {
        "20260424T120200Z-datasheet-lm2677.pdf",
        "20260424T120300Z-datasheet-atsaml21.pdf",
    }
    assert {p.name for p in out.notes} == {"20260424T120400Z-notes-tech-notes.txt"}
    # Filename without the {ts}-{kind}-{name} pattern lands in `other`.
    assert any(p.name == "manually-dropped-thing.pdf" for p in out.other)


def test_scan_picks_most_recent_schematic_and_boardview(tmp_path: Path) -> None:
    d = tmp_path / "uploads"
    d.mkdir()
    (d / "20260420T100000Z-schematic_pdf-old.pdf").write_bytes(b"old")
    (d / "20260424T100000Z-schematic_pdf-new.pdf").write_bytes(b"new")
    (d / "20260420T100000Z-boardview-old.brd").write_bytes(b"old")
    (d / "20260424T100000Z-boardview-new.brd").write_bytes(b"new")

    out = scan_uploads(d)

    assert out.schematic_pdf is not None
    assert out.schematic_pdf.name.endswith("schematic_pdf-new.pdf")
    assert out.boardview is not None
    assert out.boardview.name.endswith("boardview-new.brd")


def test_scan_ignores_subdirectories(tmp_path: Path) -> None:
    d = tmp_path / "uploads"
    d.mkdir()
    sub = d / "nested"
    sub.mkdir()
    (sub / "20260424T100000Z-schematic_pdf-stash.pdf").write_bytes(b"nested")
    out = scan_uploads(d)
    assert out.schematic_pdf is None


