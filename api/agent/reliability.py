# SPDX-License-Identifier: Apache-2.0
"""Helper shared by runtime_direct and runtime_managed.

Reads memory/{slug}/simulator_reliability.json and formats a one-liner
suitable for injection into the system prompt. Returns None when the
file is missing (normal for devices whose pack hasn't been benched yet)
or corrupt (logged).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("microsolder.agent.reliability")


def _memory_root() -> Path:
    """Isolated so tests can patch it."""
    return Path("memory")


def load_reliability_line(device_slug: str) -> str | None:
    """Return a single-line summary of the simulator reliability for this
    device, or None when unknown."""
    path = _memory_root() / device_slug / "simulator_reliability.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "[reliability] failed to load %s: %s — ignoring",
            path,
            exc,
        )
        return None
    try:
        return (
            f"Simulator reliability for {data['device_slug']}: "
            f"score={data['score']:.2f} "
            f"(self_mrr={data['self_mrr']:.2f}, "
            f"cascade_recall={data['cascade_recall']:.2f}, "
            f"n={data['n_scenarios']} scenarios, "
            f"as of {data['source_run_date']}). "
            "Treat top-ranked hypotheses with proportional caution."
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "[reliability] malformed %s: %s — ignoring",
            path,
            exc,
        )
        return None
