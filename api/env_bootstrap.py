"""Bridge a repo-root `.env` into os.environ at process start.

pydantic-settings (`api.config.Settings`) reads `.env` into the Settings object,
but does NOT export it into os.environ. A few modules read configuration
straight from os.environ at import time — notably the XZZ board-parser engine
(`api/board/parser/_xzz_engine/xzz_file.py`), which loads its DES master key from
`WRENCH_BOARD_XZZ_KEY` when the module is imported. Without this bridge the key
sits in `.env` but never reaches the parser, and `.pcb` decryption fails at
runtime even though the key is configured.

`load_env_file` parses `.env` and `setdefault`s each key into os.environ — it
never overrides a value already present in the real environment, so the shell /
container env still wins. Stdlib-only and idempotent. It has NO import-time side
effect: `api/__init__.py` calls it (skipped under pytest) so os.environ is
populated before any `api.board.*` parser module is imported.
"""

from __future__ import annotations

import os
from pathlib import Path

# Repo root is two levels up from this file (api/env_bootstrap.py → api/ → root).
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _parse_env(text: str) -> dict[str, str]:
    """Parse dotenv-style `KEY=VALUE` lines. Skips blanks and `#` comments,
    tolerates an `export ` prefix, splits on the FIRST `=` (values may contain
    `=`, e.g. base64), and strips a single layer of matching surrounding quotes.
    Malformed lines (no `=`, empty key) are skipped rather than raising."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def load_env_file(path: Path | None = None) -> int:
    """setdefault each key from the dotenv file at `path` (default: repo-root
    `.env`) into os.environ. Returns the count of keys actually applied (those
    not already set). A missing/unreadable file is a no-op returning 0."""
    env_path = path or _ENV_PATH
    try:
        text = env_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    applied = 0
    for key, value in _parse_env(text).items():
        if key not in os.environ:
            os.environ[key] = value
            applied += 1
    return applied
