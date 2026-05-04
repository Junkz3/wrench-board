"""Validation contract for the timeout-related Settings fields.

Asserts that every `*_timeout_seconds` setting parameterising the agent
runtime / memory_stores HTTP fallback:

* defaults to the value previously hardcoded in the call site, so this
  refactor stays behavior-preserving;
* enforces strict positivity (Pydantic `gt=0`), preventing a value of 0
  from being silently accepted from the env (which would convert `wait_for`
  / `httpx` into a "fail immediately" flag in production).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.config import Settings


def _fresh_settings(**overrides) -> Settings:
    """Build a Settings instance ignoring the user's .env (env_file=None)."""
    base: dict = {"anthropic_api_key": "sk-test-only"}
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def test_timeout_defaults_match_pre_refactor_constants():
    s = _fresh_settings()
    # Stream watchdog (existing setting, included as a floor).
    assert s.ma_stream_event_timeout_seconds == 600.0
    # Forwarder + drain (formerly hardcoded inline literals).
    assert s.ma_forwarder_unwind_timeout_seconds == 2.0
    assert s.ma_session_drain_timeout_seconds == 5.0
    # Sub-agent flows (formerly default kwargs on helpers).
    assert s.ma_subagent_consultation_timeout_seconds == 120.0
    assert s.ma_curator_timeout_seconds == 180.0
    # Camera capture (formerly _CAPTURE_TIMEOUT_S = 30.0).
    assert s.ma_camera_capture_timeout_seconds == 30.0
    # Memory_stores HTTP fallback (formerly httpx.AsyncClient(timeout=30.0)).
    assert s.ma_memory_store_http_timeout_seconds == 30.0


@pytest.mark.parametrize(
    "field",
    [
        "ma_forwarder_unwind_timeout_seconds",
        "ma_session_drain_timeout_seconds",
        "ma_subagent_consultation_timeout_seconds",
        "ma_curator_timeout_seconds",
        "ma_camera_capture_timeout_seconds",
        "ma_memory_store_http_timeout_seconds",
    ],
)
def test_timeout_field_rejects_zero_and_negative(field: str):
    # Zero would turn `asyncio.wait_for` into an instant-fail; reject it.
    with pytest.raises(ValidationError):
        _fresh_settings(**{field: 0.0})
    with pytest.raises(ValidationError):
        _fresh_settings(**{field: -1.0})


def test_timeout_field_accepts_custom_positive_override():
    s = _fresh_settings(ma_session_drain_timeout_seconds=12.5)
    assert s.ma_session_drain_timeout_seconds == 12.5
