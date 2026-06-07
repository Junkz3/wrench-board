"""Explicit state machine for how a Managed Agents session was started.

Replaces the booleans `resumed` + `stale_agent_recovery` + the implicit
"reused_session_id and not resumed" branch in `runtime_managed.py`. The
five paths into a session were previously expressed as combinations of
two flags, which read fine inline but made reasoning about the WS event
contract (which `session_*` event the frontend should expect, when to
emit `context_lost`, when to replay JSONL vs MA history) brittle.

Five modes, exhaustive and disjoint:

* `FRESH_NEW` — no prior session id on disk for this conv. First user
  message in a brand new conversation. UI: `session_ready`.
* `FRESH_RECOVERED_LOST` — a prior session id existed but
  `client.beta.sessions.retrieve` failed (archived, expired, MA outage).
  We create a new session; the prior chat history was lost. UI:
  `context_lost` with the on-disk state snapshot.
* `FRESH_RECOVERED_AGENT_BUMP` — the prior session is bound to an
  agent_id different from the current bootstrap (overnight evolve loop
  bumped the SYSTEM_PROMPT or manifest). We create a new session on the
  current agent; chat history reads from the JSONL mirror so the tech
  doesn't see a fresh-session UI alert. UI: silent (no event), JSONL
  replay handles the visual.
* `RESUMED` — the prior session retrieves fine and is bound to the
  current agent_id. UI: `session_resumed` + MA-history replay.
* `RESUMED_BUT_EMPTY` — like RESUMED, but `events.list()` returned no
  user/agent events (likely compacted out). The agent has no
  conversational memory. UI: `session_resumed` then `context_lost` once
  the empty replay is observed; we inject a synthetic state block so
  the agent re-orients.

The transition `RESUMED → RESUMED_BUT_EMPTY` happens after the replay
attempt — it's a runtime observation, not a startup decision. It is
modeled as a separate constant for clarity even though only the
post-replay code path constructs it.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SessionStartMode(StrEnum):
    """Five disjoint modes a session can start in.

    Each value's docstring above (in the module header) explains the
    triggering condition + expected WS event contract. Values are str
    so loggers and tests can reference them by name without an import.
    """

    FRESH_NEW = "fresh_new"
    FRESH_RECOVERED_LOST = "fresh_recovered_lost"
    FRESH_RECOVERED_AGENT_BUMP = "fresh_recovered_agent_bump"
    RESUMED = "resumed"
    RESUMED_BUT_EMPTY = "resumed_but_empty"


@dataclass(frozen=True, slots=True)
class SessionStartDecision:
    """Outcome of `decide_session_start_mode`.

    `mode` drives the WS event contract and the recap-injection branch.
    `prior_session_id` is non-None when MA had a session id on disk for
    this conv (one of the FRESH_RECOVERED_* or RESUMED* modes); it's
    surfaced on `context_lost` so the frontend can render "we lost
    session sesn_xyz, here's your snapshot".
    """

    mode: SessionStartMode
    prior_session_id: str | None = None


def decide_session_start_mode(
    *,
    reused_session_id: str | None,
    retrieved_session_agent_id: str | None,
    current_agent_id: str,
    retrieve_failed: bool,
) -> SessionStartDecision:
    """Classify a session start into one of the four startup modes.

    `RESUMED_BUT_EMPTY` is NOT returned here — it's a post-replay
    observation that runs after `decide_session_start_mode` and only
    when the initial mode was `RESUMED`. The caller transitions to
    `RESUMED_BUT_EMPTY` once it observes an empty `events.list()`.

    Args:
        reused_session_id: The MA session id stored on disk for this
            (device, repair, conv, tier), or None if the conv is brand
            new. Drives the FRESH_NEW vs FRESH_RECOVERED_* / RESUMED
            split.
        retrieved_session_agent_id: The `agent.id` returned by
            `client.beta.sessions.retrieve(reused_session_id)`. None
            if `retrieve_failed=True` or if MA returned a session
            without an agent binding (defensive — should not happen in
            practice).
        current_agent_id: The agent_id from `managed_ids.json` for the
            current tier. Used to detect overnight agent-bump drift.
        retrieve_failed: True if `client.beta.sessions.retrieve` raised
            (archived / expired / outage). Implies we cannot resume
            even if we wanted to.

    Returns:
        A `SessionStartDecision` with the mode + the prior session id
        (when applicable).
    """
    if not reused_session_id:
        return SessionStartDecision(mode=SessionStartMode.FRESH_NEW)

    if retrieve_failed:
        return SessionStartDecision(
            mode=SessionStartMode.FRESH_RECOVERED_LOST,
            prior_session_id=reused_session_id,
        )

    if (
        retrieved_session_agent_id
        and retrieved_session_agent_id != current_agent_id
    ):
        return SessionStartDecision(
            mode=SessionStartMode.FRESH_RECOVERED_AGENT_BUMP,
            prior_session_id=reused_session_id,
        )

    return SessionStartDecision(
        mode=SessionStartMode.RESUMED,
        prior_session_id=reused_session_id,
    )
