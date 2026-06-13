from __future__ import annotations

from api.agent.manifest import render_system_prompt
from api.session.state import SessionState


def test_prompt_includes_cousin_block_when_provided():
    session = SessionState()
    prompt = render_system_prompt(
        session,
        device_slug="820-2533",
        cousin_line=(
            "No schematic for this board — sibling 820-3787 (mbp15 family) has one; "
            "treat it as indicative, not authoritative."
        ),
    )
    assert "sibling 820-3787" in prompt
    assert "indicative" in prompt


def test_prompt_has_no_cousin_block_by_default():
    session = SessionState()
    prompt = render_system_prompt(session, device_slug="820-2533")
    assert "sibling" not in prompt
