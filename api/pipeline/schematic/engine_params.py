# SPDX-License-Identifier: Apache-2.0
"""Knob layer: load tunable engine params from JSON, with code-level fallback defaults.

The dual source design means: if engine_params.json is missing or partial,
the loader falls back to module-level DEFAULTS dicts. This guarantees
backward compatibility and lets a future microsolder-evolve loop tune
the JSON file (versioned, easily revertable) instead of patching source.

The numeric / boolean constants previously hard-coded at the top of
`simulator.py` and `hypothesize.py` now live here as defaults and in
`engine_params.json` as the authoritative override layer. Each consuming
module reads the merged params at import time and binds module-level
names so external imports (`from .simulator import TOLERANCE_OK`) keep
working — only the *source* of the value changes, not the surface.

Serialization quirks:
  - `PENALTY_WEIGHTS` is a tuple in code, serialized as a 2-list in JSON.
  - `_SCORE_VISIBILITY` is a `dict[tuple[str, str, str], float]` in code
    (tuple keys aren't JSON-representable), serialized as a list of
    `[kind, role, mode, multiplier]` rows in JSON.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_PARAMS_PATH = Path(__file__).parent / "engine_params.json"

# Numeric / boolean defaults. These mirror the values previously
# hard-coded as module-level constants in simulator.py and hypothesize.py.
# When engine_params.json is present, its keys override these defaults
# section by section (shallow merge per section).

SIMULATOR_DEFAULTS: dict = {
    "tolerance_ok": 0.9,
    "tolerance_uvlo": 0.5,
    "leaky_short_per_consumer_ma": 50.0,
}

HYPOTHESIZE_DEFAULTS: dict = {
    "penalty_weights": [10, 2],
    "top_k_single": 20,
    "max_results_default": 5,
    "two_fault_enabled": True,
    "max_pairs": 100,
    "score_visibility": [
        ["passive_c", "decoupling", "open", 0.5],
        ["passive_c", "bulk", "open", 0.5],
        ["passive_c", "filter", "open", 0.5],
        ["passive_r", "pull_up", "open", 0.5],
        ["passive_r", "pull_down", "open", 0.5],
    ],
}


@lru_cache(maxsize=1)
def load_params() -> dict:
    """Load engine params from JSON, merged onto defaults. Memoized.

    Returns a freshly-built dict on first call (subsequent calls return
    the cached object — callers must treat it as read-only or copy
    before mutating). Missing file or missing section falls back to the
    full defaults; partial sections override key-by-key.
    """
    params = {
        "simulator": dict(SIMULATOR_DEFAULTS),
        "hypothesize": dict(HYPOTHESIZE_DEFAULTS),
    }
    if _PARAMS_PATH.exists():
        on_disk = json.loads(_PARAMS_PATH.read_text())
        for section in ("simulator", "hypothesize"):
            if section in on_disk:
                params[section].update(on_disk[section])
    return params


def reset_cache() -> None:
    """For tests: clear the lru_cache so tests can swap _PARAMS_PATH."""
    load_params.cache_clear()
