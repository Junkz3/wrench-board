"""Tests for the engine_params knob layer.

The loader reads engine_params.json and merges it onto module-level
DEFAULTS dicts. The two sources must stay in sync; the consuming
modules (simulator, hypothesize) bind their constants from the loader.
These tests pin all four invariants.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.pipeline.schematic import engine_params
from api.pipeline.schematic.engine_params import (
    _PARAMS_PATH,
    HYPOTHESIZE_DEFAULTS,
    SIMULATOR_DEFAULTS,
    load_params,
    reset_cache,
)


def _on_disk_params() -> dict:
    return json.loads(Path(_PARAMS_PATH).read_text())


def test_defaults_match_json():
    """engine_params.json and the module-level DEFAULTS must agree.

    Two-source design only works if the sources stay in sync. Drift here
    would either break loader fallback (file gone → unexpected values)
    or hide JSON edits (loader silently overrides them with stale
    defaults). The test compares both sections key-by-key.
    """
    on_disk = _on_disk_params()
    assert on_disk["simulator"] == SIMULATOR_DEFAULTS
    assert on_disk["hypothesize"] == HYPOTHESIZE_DEFAULTS


def test_load_params_returns_defaults_when_json_absent(monkeypatch, tmp_path):
    """A missing engine_params.json must yield exactly the DEFAULTS."""
    missing = tmp_path / "absent.json"
    monkeypatch.setattr(engine_params, "_PARAMS_PATH", missing)
    reset_cache()
    try:
        loaded = load_params()
        assert loaded["simulator"] == SIMULATOR_DEFAULTS
        assert loaded["hypothesize"] == HYPOTHESIZE_DEFAULTS
    finally:
        reset_cache()


def test_load_params_overrides_partial(monkeypatch, tmp_path):
    """A partial JSON overrides only the listed keys; others stay at default."""
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"simulator": {"leaky_short_per_consumer_ma": 99.0}}))
    monkeypatch.setattr(engine_params, "_PARAMS_PATH", partial)
    reset_cache()
    try:
        loaded = load_params()
        # Override took effect.
        assert loaded["simulator"]["leaky_short_per_consumer_ma"] == 99.0
        # Untouched keys stay at default.
        assert loaded["simulator"]["tolerance_ok"] == SIMULATOR_DEFAULTS["tolerance_ok"]
        assert loaded["simulator"]["tolerance_uvlo"] == SIMULATOR_DEFAULTS["tolerance_uvlo"]
        # Hypothesize section was absent from the override → full defaults.
        assert loaded["hypothesize"] == HYPOTHESIZE_DEFAULTS
    finally:
        reset_cache()


def test_load_params_memoized():
    """load_params is wrapped in lru_cache — two calls must return the
    same object, otherwise the cache isn't doing its job."""
    reset_cache()
    a = load_params()
    b = load_params()
    assert a is b


def test_constants_match_loaded_params():
    """Each module-level constant in simulator + hypothesize must equal
    the value the loader returned. Catches accidental drift between the
    knob layer and the consuming modules (e.g. someone hard-coding a
    value back instead of binding from _params)."""
    # Re-import via load_params to get the canonical merged values.
    reset_cache()
    params = load_params()
    sim = params["simulator"]
    hyp = params["hypothesize"]

    from api.pipeline.schematic import hypothesize as hypothesize_mod
    from api.pipeline.schematic import simulator as simulator_mod

    # Simulator
    assert simulator_mod.TOLERANCE_OK == sim["tolerance_ok"]
    assert simulator_mod.TOLERANCE_UVLO == sim["tolerance_uvlo"]
    assert simulator_mod.LEAKY_SHORT_PER_CONSUMER_MA == sim["leaky_short_per_consumer_ma"]

    # Hypothesize
    assert hypothesize_mod.PENALTY_WEIGHTS == tuple(hyp["penalty_weights"])
    assert hypothesize_mod.TOP_K_SINGLE == hyp["top_k_single"]
    assert hypothesize_mod.MAX_RESULTS_DEFAULT == hyp["max_results_default"]
    assert hypothesize_mod.TWO_FAULT_ENABLED == hyp["two_fault_enabled"]
    assert hypothesize_mod.MAX_PAIRS == hyp["max_pairs"]

    # _SCORE_VISIBILITY: JSON stores [kind, role, mode, mult] rows;
    # the module re-keys them to a tuple → float dict.
    expected_visibility = {
        (kind, role, mode): float(mult) for kind, role, mode, mult in hyp["score_visibility"]
    }
    assert hypothesize_mod._SCORE_VISIBILITY == expected_visibility


def test_load_params_returns_independent_default_dicts(monkeypatch, tmp_path):
    """Mutating the loader's return value must NOT poison the DEFAULTS.

    The loader builds its result from `dict(SIMULATOR_DEFAULTS)` /
    `dict(HYPOTHESIZE_DEFAULTS)` so callers can't accidentally bleed
    edits back into the source-of-truth defaults. Same regression
    surface as a global mutable default arg.
    """
    missing = tmp_path / "absent.json"
    monkeypatch.setattr(engine_params, "_PARAMS_PATH", missing)
    reset_cache()
    try:
        loaded = load_params()
        original = SIMULATOR_DEFAULTS["tolerance_ok"]
        # Note: the result IS memoized so this same dict is returned to
        # subsequent callers. The invariant being tested here is that
        # SIMULATOR_DEFAULTS itself isn't shared by reference — caller
        # mutation may persist across load_params() calls (lru_cache
        # contract) but never leaks into the source defaults.
        loaded["simulator"]["tolerance_ok"] = 999.0
        assert SIMULATOR_DEFAULTS["tolerance_ok"] == original
    finally:
        reset_cache()


@pytest.fixture(autouse=True)
def _isolate_cache():
    """Ensure each test starts with a fresh cache (defends against
    cross-test contamination via the lru_cache)."""
    reset_cache()
    yield
    reset_cache()
