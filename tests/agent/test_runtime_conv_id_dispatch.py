"""Anti-regression : conv_id name discipline in _forward_session_to_ws.

Original bug (commit 6bd6628) : the inner code that calls save_board_state
after a bv_* tool dispatch referenced `resolved_conv_id` — a name that's
bound in the sibling _forward_ws_to_session scope, NOT in the
_forward_session_to_ws scope. Every bv_* fire raised NameError.

We cannot easily integration-test this without spinning up a full MA
event loop fixture (the call is buried in a ~270 LOC async function with
many MA-shaped event branches). Instead we lock the discipline at the
source level : the source of _forward_session_to_ws must NOT mention the
name `resolved_conv_id`. If you need a value from the parent scope, pass
it as an argument or rename the local — never reach out into a sibling
function's locals through a closure that doesn't have it.
"""

from __future__ import annotations

import inspect

from api.agent import runtime_managed


def test_forward_session_to_ws_source_does_not_reference_resolved_conv_id():
    """The closure that saves board state must use the locally-bound
    `conv_id`, not the sibling-scope `resolved_conv_id`.

    Failure here means someone re-introduced the bug from 6bd6628.
    """
    src = inspect.getsource(runtime_managed._forward_session_to_ws)
    assert "resolved_conv_id" not in src, (
        "Found 'resolved_conv_id' in _forward_session_to_ws source — that name "
        "is bound only in _forward_ws_to_session and reaching for it from this "
        "function raises NameError on every bv_* tool fire. Use the local "
        "`conv_id` instead. (Original bug : commit 6bd6628.)"
    )


def test_forward_session_to_ws_calls_save_board_state_with_conv_id():
    """Belt-and-suspenders : the source still calls save_board_state and
    passes conv_id (kwarg). If someone removes the call entirely or
    renames the kwarg, the smoke / live runtime catches it, but this
    is the cheap unit-level safety net.
    """
    src = inspect.getsource(runtime_managed._forward_session_to_ws)
    assert "save_board_state(" in src, (
        "save_board_state call disappeared from _forward_session_to_ws — "
        "board overlay snapshots after bv_* mutations will no longer persist."
    )
    assert "conv_id=conv_id" in src, (
        "save_board_state call no longer passes conv_id=conv_id keyword. "
        "Verify the snapshot keying — board state is per-conv, so dropping "
        "conv_id collapses snapshots from different convs into one slot."
    )
