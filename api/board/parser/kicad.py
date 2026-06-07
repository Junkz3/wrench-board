"""Parser for KiCad .kicad_pcb native PCB files.

Shells out to system Python 3 with pcbnew (installed with KiCad; NOT a
pip dependency) to extract the file contents as JSON, then converts to
our unified Board model. Populates every field including the rich
metadata ones (value, footprint, rotation_deg, pad_shape, pad_size).

System requirements:
- /usr/bin/python3 available
- pcbnew module available to system Python (installed via `apt install kicad`
  on Debian/Ubuntu, `brew install --cask kicad` on macOS, etc.)

Network boundary: this parser runs a subprocess, not in the request thread.
Keep KICAD_PARSE_TIMEOUT modest so a hung child is bounded.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from api.board.model import Board, Layer, Nail, Net, Part, Pin, Point
from api.board.parser.base import BoardParser, InvalidBoardFile, register
from api.board.parser.test_link import _GROUND_RE, _POWER_RE

_EXTRACT_SCRIPT = Path(__file__).parent / "_kicad_extract.py"
KICAD_PARSE_TIMEOUT = 30  # seconds — MNT Reform at ~400 KB parses in ~2s


class KicadSubprocessError(InvalidBoardFile):
    """Raised when the KiCad extractor subprocess fails (non-zero exit, stderr, timeout)."""


@register
class KicadPcbParser(BoardParser):
    extensions = (".kicad_pcb",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        # The extractor needs a real file path (pcbnew.LoadBoard doesn't accept
        # in-memory content). Write to a tmp file and pass the path.
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", delete=True) as tmp:
            tmp.write(raw)
            tmp.flush()
            return self._parse_path(Path(tmp.name), file_hash=file_hash, board_id=board_id)

    def parse_file(self, path: Path) -> Board:
        # Override the default to avoid the raw-bytes round-trip when we already
        # have a path — significantly faster for the 420 KB MNT Reform PCB.
        import hashlib

        raw = path.read_bytes()
        file_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
        return self._parse_path(path, file_hash=file_hash, board_id=path.stem)

    def _parse_path(self, path: Path, *, file_hash: str, board_id: str) -> Board:
        python3 = shutil.which("python3") or "/usr/bin/python3"
        try:
            result = subprocess.run(
                [python3, str(_EXTRACT_SCRIPT), str(path)],
                capture_output=True,
                timeout=KICAD_PARSE_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise KicadSubprocessError(
                f"KiCad extractor timed out after {KICAD_PARSE_TIMEOUT}s"
            ) from e

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise KicadSubprocessError(
                f"KiCad extractor failed (exit={result.returncode}): {stderr[:500]}"
            )

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise KicadSubprocessError(f"KiCad extractor emitted invalid JSON: {e}") from e

        return _json_to_board(data, file_hash=file_hash, board_id=board_id)


def _json_to_board(data: dict, *, file_hash: str, board_id: str) -> Board:
    outline = [Point(x=p["x"], y=p["y"]) for p in data.get("outline", [])]

    nets_src = data.get("nets", [])
    net_names = [n["name"] for n in nets_src]

    # Build parts with pin_refs computed from cumulative first_pin offsets.
    parts_src = data.get("parts", [])
    pins_src = data.get("pins", [])
    n_pins = len(pins_src)

    parts: list[Part] = []
    pins: list[Pin] = []

    for k, p in enumerate(parts_src):
        start = p["first_pin"]
        end = parts_src[k + 1]["first_pin"] if k + 1 < len(parts_src) else n_pins
        pin_refs = list(range(start, end))

        side = p["side"]
        layer = Layer.TOP if side == 1 else Layer.BOTTOM

        bbox_src = p["bbox"]
        bbox_lo = Point(
            x=min(bbox_src[0]["x"], bbox_src[1]["x"]),
            y=min(bbox_src[0]["y"], bbox_src[1]["y"]),
        )
        bbox_hi = Point(
            x=max(bbox_src[0]["x"], bbox_src[1]["x"]),
            y=max(bbox_src[0]["y"], bbox_src[1]["y"]),
        )

        parts.append(
            Part(
                refdes=p["refdes"],
                layer=layer,
                is_smd=p.get("is_smd", True),
                bbox=(bbox_lo, bbox_hi),
                pin_refs=pin_refs,
                value=p.get("value"),
                footprint=p.get("footprint"),
                rotation_deg=p.get("rotation_deg"),
            )
        )

        # Emit the pins for this part, assigning 1-based local indices
        for local_idx, pin_i in enumerate(pin_refs, start=1):
            pr = pins_src[pin_i]
            pin_side = pr["side"]
            pin_layer = Layer.TOP if pin_side == 1 else Layer.BOTTOM
            net_code = pr.get("net_code", 0)
            if net_code == 0:
                net_name: str | None = None
            else:
                # net_code may not align 1:1 with the nets list index
                # (KiCad gives non-contiguous codes); look up by code.
                net_name = _lookup_net_name(nets_src, net_code)

            pad_size_src = pr.get("pad_size")
            pad_size = tuple(pad_size_src) if pad_size_src else None

            pins.append(
                Pin(
                    part_refdes=p["refdes"],
                    index=local_idx,
                    pos=Point(x=pr["x"], y=pr["y"]),
                    net=net_name,
                    probe=None,
                    layer=pin_layer,
                    pad_shape=pr.get("pad_shape"),
                    pad_size=pad_size,
                    pad_rotation_deg=pr.get("pad_rotation_deg"),
                )
            )

    # Build Net list from names, grouping pins by name. Matches brd2.py pattern.
    refs_by_name: dict[str, list[int]] = {}
    for name in net_names:
        refs_by_name.setdefault(name, [])
    for i, pin in enumerate(pins):
        if pin.net is None:
            continue
        refs_by_name.setdefault(pin.net, []).append(i)

    nets: list[Net] = []
    for name in sorted(refs_by_name):
        nets.append(
            Net(
                name=name,
                pin_refs=refs_by_name[name],
                is_power=bool(_POWER_RE.match(name)),
                is_ground=bool(_GROUND_RE.match(name)),
            )
        )

    nails: list[Nail] = []  # KiCad test points handled separately; empty for now

    return Board(
        board_id=board_id,
        file_hash=file_hash,
        source_format="kicad_pcb",
        outline=outline,
        parts=parts,
        pins=pins,
        nets=nets,
        nails=nails,
    )


def _lookup_net_name(nets_src: list[dict], code: int) -> str | None:
    for n in nets_src:
        if n["code"] == code:
            return n["name"]
    return None
