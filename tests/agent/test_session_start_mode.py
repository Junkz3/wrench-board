"""Tests for the explicit Managed Agents session-start state machine.

Each test pins one of the five disjoint modes the runtime can land in,
so a future refactor that subtly changes the FRESH_RECOVERED_LOST vs
FRESH_RECOVERED_AGENT_BUMP boundary is caught immediately.
"""
from __future__ import annotations

from api.agent.session_start_mode import (
    SessionStartDecision,
    SessionStartMode,
    decide_session_start_mode,
)


def test_fresh_new_when_no_prior_session_id():
    decision = decide_session_start_mode(
        reused_session_id=None,
        retrieved_session_agent_id=None,
        current_agent_id="agent_current_001",
        retrieve_failed=False,
    )
    assert decision == SessionStartDecision(mode=SessionStartMode.FRESH_NEW)
    assert decision.prior_session_id is None


def test_fresh_recovered_lost_when_retrieve_fails():
    """MA retrieve raised (archived / expired / outage) — must rebuild fresh.

    The prior_session_id must surface so the frontend can show
    `context_lost` with a "we lost session sesn_xyz" message.
    """
    decision = decide_session_start_mode(
        reused_session_id="sesn_archived_999",
        retrieved_session_agent_id=None,
        current_agent_id="agent_current_001",
        retrieve_failed=True,
    )
    assert decision.mode == SessionStartMode.FRESH_RECOVERED_LOST
    assert decision.prior_session_id == "sesn_archived_999"


def test_fresh_recovered_agent_bump_when_agent_id_changed():
    """Overnight agent-evolve loop bumped agent_id — silent UI.

    The user shouldn't see a "session lost" alert just because we
    rotated to a new agent version. The JSONL replay handles the
    visual continuity.
    """
    decision = decide_session_start_mode(
        reused_session_id="sesn_old_111",
        retrieved_session_agent_id="agent_v1_yesterday",
        current_agent_id="agent_v2_today",
        retrieve_failed=False,
    )
    assert decision.mode == SessionStartMode.FRESH_RECOVERED_AGENT_BUMP
    assert decision.prior_session_id == "sesn_old_111"


def test_resumed_when_retrieve_ok_and_agent_id_matches():
    decision = decide_session_start_mode(
        reused_session_id="sesn_alive_222",
        retrieved_session_agent_id="agent_current_001",
        current_agent_id="agent_current_001",
        retrieve_failed=False,
    )
    assert decision.mode == SessionStartMode.RESUMED
    assert decision.prior_session_id == "sesn_alive_222"


def test_resumed_when_session_has_no_agent_binding():
    """Defensive: MA should always return an agent-bound session, but if
    it ever returns one without an agent.id field, we treat it as the
    current agent (no drift evidence) and resume."""
    decision = decide_session_start_mode(
        reused_session_id="sesn_alive_333",
        retrieved_session_agent_id=None,
        current_agent_id="agent_current_001",
        retrieve_failed=False,
    )
    assert decision.mode == SessionStartMode.RESUMED


def test_modes_are_disjoint_string_values():
    """Each mode has a unique string representation — guards against
    accidental Enum value clobbering during refactors. Also the values
    are usable in logger.info("mode=%s", decision.mode) without an
    explicit str cast since SessionStartMode subclasses StrEnum."""
    values = {m.value for m in SessionStartMode}
    assert len(values) == len(list(SessionStartMode))
    # StrEnum: f-string interpolation gives the value directly, not the
    # qualified name. This is what makes the enum drop-in safe in
    # logger format strings without an explicit `.value` access.
    assert f"{SessionStartMode.FRESH_NEW}" == "fresh_new"
    assert SessionStartMode.FRESH_NEW.value == "fresh_new"
