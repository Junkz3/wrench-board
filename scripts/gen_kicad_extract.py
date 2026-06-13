#!/usr/bin/env python3
"""Generate a `<file>.extract.json` sidecar for a .kicad_pcb board.

Run this where KiCad (pcbnew) IS installed; commit the sidecar next to the
board file. Slim deploys without pcbnew (the prod Docker image) then parse the
board from the sidecar — KicadPcbParser trusts it only when the embedded
source hash matches the .kicad_pcb bytes, so a stale sidecar is ignored, never
silently served.

Usage:
    python3 scripts/gen_kicad_extract.py board_assets/mnt-reform-motherboard.kicad_pcb
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

_EXTRACT_SCRIPT = Path(__file__).parent.parent / "api" / "board" / "parser" / "_kicad_extract.py"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    pcb = Path(sys.argv[1])
    if not pcb.is_file():
        print(f"not a file: {pcb}", file=sys.stderr)
        return 2

    result = subprocess.run(
        [sys.executable, str(_EXTRACT_SCRIPT), str(pcb)],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        print(result.stderr.decode("utf-8", errors="replace"), file=sys.stderr)
        return result.returncode

    digest = "sha256:" + hashlib.sha256(pcb.read_bytes()).hexdigest()
    sidecar = pcb.parent / (pcb.name + ".extract.json")
    sidecar.write_text(json.dumps({
        "source_sha256": digest,
        "extract": json.loads(result.stdout),
    }, separators=(",", ":")))
    print(f"wrote {sidecar} ({sidecar.stat().st_size} bytes) for {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
