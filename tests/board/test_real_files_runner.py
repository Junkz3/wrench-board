"""Optional real-file smoke runner.

Runs the new parsers against real-world boardview files that the user
drops into one of these directories (scanned in order, first match wins):

  1. Path from env var `MICROSOLDER_REAL_BOARDS_DIR`
  2. `/tmp/microsolder-real-boards` (convenient scratch area)
  3. `~/Downloads/microsolder-real-boards`

Files must never be committed — CLAUDE.md hard-rule #4 keeps proprietary
content out of the repo. At runtime, any brand is fair game (per the
Open-hardware-rule-is-repo-only memory note).

If no directory exists or is empty, every test is skipped cleanly.

For each file found this runner:
  - Dispatches to the right parser by extension
  - Asserts the parse either succeeds or raises a DOCUMENTED known
    limitation (binary TVW, missing FZ key, combined-form ASC required)
  - Emits a summary line the user can eyeball in pytest's output:
      REAL  minimal.bv          PASS  parts=42  pins=180  nets=12
      REAL  prod.tvw            KNOWN binary-layout (by design)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from api.board.parser.base import (
    BoardParserError,
    MissingFZKeyError,
    ObfuscatedFileError,
    parser_for,
)

_KNOWN_EXTS = {".bv", ".gr", ".cad", ".cst", ".f2b", ".bdv", ".tvw", ".fz", ".asc",
               ".brd", ".brd2", ".kicad_pcb"}


def _candidate_dirs() -> list[Path]:
    out: list[Path] = []
    env = os.environ.get("MICROSOLDER_REAL_BOARDS_DIR", "").strip()
    if env:
        out.append(Path(env))
    out.append(Path("/tmp/microsolder-real-boards"))
    out.append(Path.home() / "Downloads" / "microsolder-real-boards")
    return out


def _collect_real_files() -> list[Path]:
    for d in _candidate_dirs():
        if d.is_dir():
            files = [
                p for p in sorted(d.iterdir())
                if p.is_file() and p.suffix.lower() in _KNOWN_EXTS
            ]
            if files:
                return files
    return []


_REAL_FILES = _collect_real_files()


@pytest.mark.skipif(not _REAL_FILES, reason="no real files in any candidate dir")
@pytest.mark.parametrize("path", _REAL_FILES, ids=lambda p: p.name)
def test_real_file_parses_or_raises_known_limitation(path: Path):
    """Every real file must either parse cleanly or raise one of the
    known-limitation error classes. Anything else is a real bug."""
    parser = parser_for(path)
    try:
        board = parser.parse_file(path)
    except MissingFZKeyError:
        print(f"REAL  {path.name:30} KNOWN fz-key-missing (set MICROSOLDER_FZ_KEY)")
        return
    except ObfuscatedFileError as exc:
        # Binary TVW is the documented known limitation.
        if "binary-layout" in str(exc) or path.suffix.lower() == ".tvw":
            print(f"REAL  {path.name:30} KNOWN binary-layout (by design)")
            return
        raise
    except BoardParserError as exc:
        pytest.fail(f"{path.name}: unexpected parser error: {exc}")

    # Parse succeeded — sanity-check the topology on the real data.
    assert board.parts, f"{path.name}: 0 parts"
    assert board.pins, f"{path.name}: 0 pins"
    # pin → part cross-resolution
    refdes = {p.refdes for p in board.parts}
    for pin in board.pins:
        assert pin.part_refdes in refdes, (
            f"{path.name}: pin refers to unknown part {pin.part_refdes}"
        )
    # net → pin cross-resolution
    for net in board.nets:
        for ref in net.pin_refs:
            assert 0 <= ref < len(board.pins)
            assert board.pins[ref].net == net.name

    print(
        f"REAL  {path.name:30} PASS  "
        f"parts={len(board.parts)} pins={len(board.pins)} "
        f"nets={len(board.nets)} nails={len(board.nails)}"
    )
