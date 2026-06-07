"""Anti-régression: scope hygiene of `_forward_session_to_ws`.

`run_diagnostic_session_managed` defines a local `resolved_conv_id` (the
canonicalized conversation id, possibly migrated/created lazily). The
function passes the resolved id explicitly to `_forward_session_to_ws`
under the parameter name `conv_id`. Inside `_forward_session_to_ws`,
the variable `resolved_conv_id` is NOT in scope — referencing it is a
NameError that fires at runtime the first time a `bv_*` tool dispatches
(`save_board_state(... conv_id=resolved_conv_id)` was the historical
offender, fixed in commit 6bd6628).

This test pins the invariant statically: the source of
`_forward_session_to_ws` must not mention `resolved_conv_id` anywhere.
If a future edit reintroduces the name, this test fails before the
real WS path even gets a chance to crash live.
"""

from __future__ import annotations

import inspect

from api.agent import runtime_managed


def test_forward_session_to_ws_does_not_reference_resolved_conv_id():
    """`resolved_conv_id` is in `run_diagnostic_session_managed`'s scope only.

    `_forward_session_to_ws` is a top-level function (NOT nested) — it
    receives the canonicalized id via its `conv_id` parameter. Referencing
    `resolved_conv_id` inside its body is a guaranteed NameError at
    runtime that only fires once a tool actually dispatches.
    """
    source = inspect.getsource(runtime_managed._forward_session_to_ws)
    assert "resolved_conv_id" not in source, (
        "_forward_session_to_ws references `resolved_conv_id`, which is "
        "not in its scope (it lives in `run_diagnostic_session_managed`). "
        "Use the `conv_id` parameter instead — the canonicalized id is "
        "already passed in via that name. NameError will fire at runtime "
        "as soon as the offending code path executes (typically the first "
        "bv_* tool dispatch via save_board_state). See commit 6bd6628 for "
        "the original fix."
    )


def test_forward_session_to_ws_takes_conv_id_parameter():
    """Defensive: the function must keep its `conv_id` keyword parameter so
    the caller in run_diagnostic_session_managed can route the resolved id."""
    sig = inspect.signature(runtime_managed._forward_session_to_ws)
    assert "conv_id" in sig.parameters, (
        "_forward_session_to_ws must accept `conv_id` — the caller in "
        "run_diagnostic_session_managed passes the resolved id via this "
        "keyword. Renaming or removing it would break the bv_* dispatch "
        "path that calls save_board_state(... conv_id=conv_id, ...)."
    )
