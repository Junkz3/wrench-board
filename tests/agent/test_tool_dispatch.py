"""Unit tests for the dispatch table extracted from ``runtime_managed``.

The legacy ``_dispatch_tool`` waterfall lived inside ``runtime_managed.py``
and captured a closure over the WebSocket task locals — it was effectively
untestable without a full MA-session fixture. The dispatch refactor moved
the routing into ``tool_dispatch.py`` behind a ``ToolContext`` dataclass,
which makes the resolution surface trivially unit-testable.

Coverage focus: the routing rules (profile_* prefix, exact-name lookup,
bv_* fallback, unknown-tool error) — the per-tool handlers themselves are
already covered indirectly by the runtime_managed.* tests and the
tools.py / dispatch_bv.py suites.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from api.agent.tool_dispatch import (
    _HANDLERS,
    ToolContext,
    dispatch_tool,
)


def _ctx(tmp_path: Path) -> ToolContext:
    """Build a minimal ToolContext for routing tests.

    Only ``device_slug``, ``memory_root`` and ``session`` need real-ish
    values for the unknown-tool / pattern-fallback assertions; everything
    else can be a no-op MagicMock since the routing layer never touches
    those fields directly.
    """
    return ToolContext(
        device_slug="test-device",
        memory_root=tmp_path,
        client=MagicMock(),
        session=MagicMock(board=None),
    )


@pytest.mark.asyncio
async def test_unknown_tool_returns_structured_error(tmp_path: Path):
    """An unknown tool name must return the legacy
    ``{"ok": False, "reason": "unknown-tool", "error": "..."}`` shape so
    the agent can recover gracefully instead of stalling on a None result.
    """
    result = await dispatch_tool("not_a_real_tool", {}, _ctx(tmp_path))

    assert result == {
        "ok": False,
        "reason": "unknown-tool",
        "error": "unknown tool: not_a_real_tool",
    }


@pytest.mark.asyncio
async def test_unknown_profile_tool_returns_profile_scoped_error(
    tmp_path: Path,
):
    """``profile_*`` names that don't match the three known profile tools
    must return the profile-scoped error shape (the legacy ``_dispatch_tool``
    distinguished this case from the generic ``unknown tool`` error).
    """
    result = await dispatch_tool("profile_made_up", {}, _ctx(tmp_path))

    assert result["ok"] is False
    assert result["reason"] == "unknown-tool"
    assert "profile" in result["error"]


@pytest.mark.asyncio
async def test_unknown_bv_tool_falls_back_to_dispatch_bv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A ``bv_*`` name not in the protocol-bridge registry must hit
    ``dispatch_bv``. The fallback is the one piece of routing logic that
    isn't a registry lookup, so it gets its own test.
    """
    captured: dict = {}

    def fake_dispatch_bv(session, name, payload):
        captured["called"] = True
        captured["name"] = name
        captured["payload"] = payload
        return {"ok": True, "_via": "fake_bv"}

    monkeypatch.setattr(
        "api.agent.tool_dispatch.dispatch_bv", fake_dispatch_bv,
    )

    result = await dispatch_tool(
        "bv_brand_new_tool", {"refdes": "U1"}, _ctx(tmp_path),
    )

    assert result == {"ok": True, "_via": "fake_bv"}
    assert captured == {
        "called": True,
        "name": "bv_brand_new_tool",
        "payload": {"refdes": "U1"},
    }


def test_handler_registry_covers_every_named_tool():
    """Sanity contract: the ``_HANDLERS`` table must enumerate every named
    tool the legacy waterfall handled (excluding the prefix-fallback
    branches: ``profile_*`` and the catch-all ``bv_*``).

    Lock the list here so a future PR that adds a tool to ``manifest.py``
    but forgets to register it in ``_HANDLERS`` fails loudly.
    """
    expected = {
        # bv_* protocol bridges intercepted before generic dispatch_bv.
        "bv_propose_protocol",
        "bv_update_protocol",
        "bv_record_step_result",
        "bv_get_protocol",
        # mb_* memory-bank surface.
        "mb_get_component",
        "mb_get_rules_for_symptoms",
        "mb_record_finding",
        "mb_record_session_log",
        "mb_schematic_graph",
        "mb_hypothesize",
        "mb_record_measurement",
        "mb_list_measurements",
        "mb_compare_measurements",
        "mb_observations_from_measurements",
        "mb_set_observation",
        "mb_clear_observations",
        "mb_validate_finding",
        "mb_expand_knowledge",
    }
    assert set(_HANDLERS.keys()) == expected, (
        "_HANDLERS drifted from the locked tool list — update both this "
        "set and the manifest if you genuinely added a tool"
    )


def test_tool_context_field_order_matches_runtime_managed_shim():
    """The ``ToolContext`` field order must mirror the legacy
    ``_dispatch_tool`` keyword arguments. The runtime's thin shim
    constructs the dataclass keyword-only so a reordering wouldn't hard-
    crash, but a positional-arg test elsewhere (or a future helper that
    builds a ``ToolContext(*args)``) relies on the order — keep it stable.
    """
    fields = list(ToolContext.__dataclass_fields__.keys())
    assert fields == [
        "device_slug",
        "memory_root",
        "client",
        "session",
        "session_id",
        "repair_id",
        "session_mirrors",
        "conv_id",
    ]
