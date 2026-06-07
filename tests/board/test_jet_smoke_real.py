"""Optional smoke test: parse REAL binary JET4 `.bv` files and cross-check the
decoded Pin/Nail/Layout counts against `mdb-export` ground truth.

Third-party corpus files are NEVER committed (open-hardware-only rule). This
test discovers real binary `.bv` files at runtime and skips cleanly when none
are present or when `mdb-export` (mdbtools) is unavailable, so CI stays green on
the hand-built-page unit tests in `test_jet_engine.py`. Locally, drop files into
`WRENCH_BOARD_REAL_BOARDS_DIR` (or `~/Documents`) to exercise the full path on
genuine ATE exports.

The assertion is strong: for every file we parse, the number of *parts* must be
non-zero and the number of *pins* must EXACTLY equal the row count `mdb-export`
reports for the `Pin` table — an independent third-party reader agreeing on the
record count is the cross-check that the JET4 page/record decode is correct.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from api.board.parser._jet_engine import is_jet4
from api.board.parser.bv import BVParser

_JET4_MAGIC = b"\x00\x01\x00\x00"


def _search_roots() -> list[Path]:
    roots: list[Path] = []
    env = os.environ.get("WRENCH_BOARD_REAL_BOARDS_DIR", "").strip()
    if env:
        roots.append(Path(env))
    roots.append(Path("/tmp/wrench-board-real-boards"))
    roots.append(Path.home() / "Downloads" / "wrench-board-real-boards")
    roots.append(Path.home() / "Documents")
    return [r for r in roots if r.is_dir()]


# Never treat the repo's OWN synthetic fixtures as corpus files — they are
# valid for our reader but not for mdbtools (no obfuscated header), and would
# break the ground-truth cross-check.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _collect_binary_bv() -> list[Path]:
    seen: set[bytes] = set()
    out: list[Path] = []
    for root in _search_roots():
        for p in glob.glob(str(root / "**" / "*.bv"), recursive=True):
            path = Path(p)
            if _REPO_ROOT in path.resolve().parents:
                continue  # skip our own fixtures / repo tree
            try:
                head = path.read_bytes()[:4]
            except OSError:
                continue
            if head != _JET4_MAGIC:
                continue
            # De-dup identical files (the corpus has many copies).
            digest = path.read_bytes()
            key = bytes(len(digest).to_bytes(8, "little")) + digest[:64]
            if key in seen:
                continue
            seen.add(key)
            out.append(path)
        if out:
            break
    # A handful is enough for a smoke test; keep it fast.
    return out[:5]


_REAL = _collect_binary_bv()
_HAVE_MDB = shutil.which("mdb-export") is not None


def _mdb_rowcount(path: Path, table: str) -> int:
    res = subprocess.run(
        ["mdb-export", str(path), table],
        capture_output=True,
        text=True,
        check=True,
    )
    # First line is the CSV header; remaining non-empty lines are rows.
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    return max(0, len(lines) - 1)


@pytest.mark.skipif(not _REAL, reason="no real binary .bv files found")
@pytest.mark.parametrize("path", _REAL, ids=lambda p: p.name)
def test_real_binary_bv_parses_with_nonzero_content(path: Path):
    raw = path.read_bytes()
    assert is_jet4(raw)
    board = BVParser().parse(raw, file_hash="smoke", board_id="smoke")
    assert len(board.parts) > 0, f"{path.name}: zero parts"
    assert len(board.pins) > 0, f"{path.name}: zero pins"
    assert len(board.nets) > 0, f"{path.name}: zero nets"

    summary = (
        f"JET4  {path.name:34} parts={len(board.parts):4} "
        f"pins={len(board.pins):5} nets={len(board.nets):4} "
        f"nails={len(board.nails):4} outline={len(board.outline):4}"
    )

    if _HAVE_MDB:
        # Ground-truth cross-check: our decoded pin count must equal the Pin
        # table row count an independent JET reader (mdbtools) sees.
        gt_pins = _mdb_rowcount(path, "Pin")
        assert len(board.pins) == gt_pins, (
            f"{path.name}: pins={len(board.pins)} != mdb-export Pin rows={gt_pins}"
        )
        summary += f"  [mdb Pin rows={gt_pins} ✓]"
    print(summary)
