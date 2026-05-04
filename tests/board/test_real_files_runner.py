"""Optional real-file smoke runner.

Runs the new parsers against real-world boardview files that the user
drops into one of these directories (scanned in order, first match wins):

  1. Path from env var `WRENCH_BOARD_REAL_BOARDS_DIR`
  2. `/tmp/wrench-board-real-boards` (convenient scratch area)
  3. `~/Downloads/wrench-board-real-boards`

Files must never be committed — the open-hardware-only rule keeps
proprietary content out of the repo. At runtime, any brand is fair
game.

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
    env = os.environ.get("WRENCH_BOARD_REAL_BOARDS_DIR", "").strip()
    if env:
        out.append(Path(env))
    out.append(Path("/tmp/wrench-board-real-boards"))
    out.append(Path.home() / "Downloads" / "wrench-board-real-boards")
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
        print(f"REAL  {path.name:30} KNOWN fz-key-missing (set WRENCH_BOARD_FZ_KEY)")
        return
    except ObfuscatedFileError as exc:
        # Binary TVW is the documented known limitation.
        if "binary-layout" in str(exc) or path.suffix.lower() == ".tvw":
            print(f"REAL  {path.name:30} KNOWN binary-layout (by design)")
            return
        raise
    except BoardParserError as exc:
        pytest.fail(f"{path.name}: unexpected parser error: {exc}")

    # Parse succeeded — apply the full consistency-invariants suite on the
    # real data so a regression in any parser surfaces here, not later in
    # the agent or UI layer.
    assert board.parts, f"{path.name}: 0 parts"
    assert board.pins, f"{path.name}: 0 pins"

    # --- string invariants ---
    refdes_set: set[str] = set()
    for part in board.parts:
        assert isinstance(part.refdes, str) and part.refdes, (
            f"{path.name}: part has empty/non-str refdes"
        )
        assert part.refdes not in refdes_set, (
            f"{path.name}: duplicate refdes {part.refdes!r}"
        )
        refdes_set.add(part.refdes)

    # --- pin → part cross-resolution ---
    for i, pin in enumerate(board.pins):
        assert pin.part_refdes in refdes_set, (
            f"{path.name}: pin {i} refers to unknown part {pin.part_refdes!r}"
        )

    # --- parts[k].pin_refs ↔ pins[ref].part_refdes consistency ---
    for part in board.parts:
        for ref in part.pin_refs:
            assert 0 <= ref < len(board.pins)
            assert board.pins[ref].part_refdes == part.refdes

    # --- pin.index positive (connectors/sockets legitimately repeat
    # indices: a USB connector has front + back pads sharing the same
    # logical pin number, a CPU socket has multiple test pads per BGA
    # ball — not duplicates, real physical reality) ---
    for part in board.parts:
        idxs = [board.pins[r].index for r in part.pin_refs]
        assert all(i >= 1 for i in idxs), (
            f"{path.name}: {part.refdes} non-positive pin.index"
        )

    # --- bbox normalised (min, max) ---
    for part in board.parts:
        lo, hi = part.bbox
        assert lo.x <= hi.x and lo.y <= hi.y, (
            f"{path.name}: {part.refdes} bbox not normalised"
        )

    # --- net → pin cross-resolution ---
    net_names: set[str] = set()
    for net in board.nets:
        assert isinstance(net.name, str) and net.name, (
            f"{path.name}: empty net name"
        )
        net_names.add(net.name)
        for ref in net.pin_refs:
            assert 0 <= ref < len(board.pins)
            assert board.pins[ref].net == net.name, (
                f"{path.name}: net {net.name!r} ref={ref} mismatch with "
                f"pin.net={board.pins[ref].net!r}"
            )

    # --- every named pin.net resolves to a Net entry ---
    for i, pin in enumerate(board.pins):
        if pin.net is not None:
            assert pin.net in net_names, (
                f"{path.name}: pin {i} net={pin.net!r} not in board.nets"
            )

    # --- pin position must be int (Point.x/y are int mils) ---
    for pin in board.pins:
        assert isinstance(pin.pos.x, int) and isinstance(pin.pos.y, int), (
            f"{path.name}: non-int pin position"
        )

    print(
        f"REAL  {path.name:30} PASS  "
        f"parts={len(board.parts)} pins={len(board.pins)} "
        f"nets={len(board.nets)} nails={len(board.nails)}"
    )
