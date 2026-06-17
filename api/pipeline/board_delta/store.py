from __future__ import annotations
import re
from pathlib import Path
from api.pipeline.board_delta.schemas import DeltaBoard

_SEP = re.compile(r"[\s_/]+")
_UNSAFE = re.compile(r"[^a-z0-9.-]+")


def normalize_board_number(raw: str) -> str:
    """Stable, filesystem-safe key. Lowercase, separators -> '-', strip path bits."""
    s = (raw or "").strip().lower()
    s = _SEP.sub("-", s)
    s = s.replace("..", "")
    s = _UNSAFE.sub("", s)
    return s.strip("-.")


def delta_path(memory_root: Path, device_slug: str, board_number: str) -> Path:
    return Path(memory_root) / device_slug / "board_deltas" / f"{normalize_board_number(board_number)}.json"


def write_delta(*, memory_root: Path, device_slug: str, delta: DeltaBoard) -> Path:
    p = delta_path(memory_root, device_slug, delta.board_number)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(delta.model_dump_json(indent=2), encoding="utf-8")
    return p


def read_delta(*, memory_root: Path, device_slug: str, board_number: str) -> DeltaBoard | None:
    p = delta_path(memory_root, device_slug, board_number)
    if not p.exists():
        return None
    return DeltaBoard.model_validate_json(p.read_text(encoding="utf-8"))
