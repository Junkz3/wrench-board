"""ASUS TSICT `.asc` parser.

ASUS's internal viewer writes a directory of five files: `format.asc`
(outline), `parts.asc` (components), `pins.asc` (pin placements),
`nails.asc` (test points), and `nets.asc` (net catalogue). Lines
inside each file follow the Test_Link grammar. Redistributed ASUS
boards in the community are packaged two ways:

1. **Combined single file** — the five sections concatenated with
   their Test_Link block markers (`Format:` / `Parts:` / `Pins:` /
   `Nails:`). This is the shape most repair techs upload.
2. **Directory** — the five sub-files next to each other. When the
   user uploads one of them (say `pins.asc`), we pick up the others
   from the same directory and synthesize the combined payload.

Both paths route through the shared Test_Link helper once the text
stream is assembled. No code copied from any external codebase.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from api.board.model import Board
from api.board.parser._ascii_boardview import DialectMarkers, parse_test_link_shape
from api.board.parser.base import BoardParser, InvalidBoardFile, register

# Sub-file names, in block order. The block marker emitted for each
# mirrors the canonical Test_Link spelling so the helper recognises
# them whether we read a combined file or a directory.
_SUBFILES: tuple[tuple[str, str], ...] = (
    ("format.asc", "Format:"),
    ("parts.asc", "Parts:"),
    ("pins.asc", "Pins:"),
    ("nails.asc", "Nails:"),
)

_COMBINED_MARKERS = ("Format:", "Parts:", "Pins:", "Nails:", "Components:")


def _looks_combined(text: str) -> bool:
    """True if the payload already contains at least two Test_Link markers —
    a strong signal that this is the combined single-file form rather than
    one of the split sub-files."""
    hits = sum(1 for m in _COMBINED_MARKERS if m in text)
    return hits >= 2


def _assemble_from_directory(dir_path: Path) -> str:
    """Build a combined Test_Link payload from the five `*.asc` sub-files.

    Files that don't exist are skipped (the block is simply absent —
    the helper treats a missing block with no count as empty). Counts
    are inferred from the body line count so the helper can validate.
    """
    out: list[str] = []
    counts = []
    bodies = []
    for fname, _marker in _SUBFILES:
        path = dir_path / fname
        if not path.exists():
            counts.append(0)
            bodies.append([])
            continue
        lines = [ln for ln in path.read_text(errors="replace").splitlines() if ln.strip()]
        counts.append(len(lines))
        bodies.append(lines)

    out.append(f"var_data: {counts[0]} {counts[1]} {counts[2]} {counts[3]}")
    for (_fname, marker), body in zip(_SUBFILES, bodies, strict=True):
        if not body:
            continue
        out.append(marker)
        out.extend(body)
    return "\n".join(out) + "\n"


@register
class ASCParser(BoardParser):
    extensions = (".asc",)

    def parse_file(self, path: Path) -> Board:
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        if _looks_combined(text):
            file_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
            return self._parse_combined_text(
                text, file_hash=file_hash, board_id=path.stem
            )
        # Split-directory case: synthesize from siblings. Reject up-front
        # if none of the expected sub-files exist (other than the one we
        # were handed) — otherwise we'd silently return an empty Board
        # for a `.asc` that wasn't TSICT at all.
        expected = {name for name, _ in _SUBFILES}
        siblings = {
            p.name for p in path.parent.iterdir() if p.is_file() and p.name in expected
        }
        if not siblings & expected:
            raise InvalidBoardFile(
                "asc: payload is not a combined TSICT boardview and no "
                "TSICT sub-files (format.asc, parts.asc, pins.asc, nails.asc) "
                "were found next to it."
            )
        assembled = _assemble_from_directory(path.parent)
        # Hash the assembled payload so the file_hash is deterministic
        # across runs even though the input came from multiple files.
        file_hash = "sha256:" + hashlib.sha256(assembled.encode("utf-8")).hexdigest()
        return self._parse_combined_text(
            assembled, file_hash=file_hash, board_id=path.parent.name or path.stem
        )

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        text = raw.decode("utf-8", errors="replace")
        if not _looks_combined(text):
            raise InvalidBoardFile(
                "asc: payload does not contain a combined TSICT boardview. "
                "Upload the concatenated file or place the five sub-files "
                "(format.asc, parts.asc, pins.asc, nails.asc, nets.asc) "
                "together on disk."
            )
        return self._parse_combined_text(text, file_hash=file_hash, board_id=board_id)

    def _parse_combined_text(
        self, text: str, *, file_hash: str, board_id: str
    ) -> Board:
        return parse_test_link_shape(
            text,
            markers=DialectMarkers(
                header_count_marker="var_data:",
                outline_markers=("Format:", "Outline:"),
                parts_markers=("Parts:", "Components:"),
                pins_markers=("Pins:",),
                nails_markers=("Nails:", "TestPoints:"),
            ),
            source_format="asc",
            board_id=board_id,
            file_hash=file_hash,
        )
