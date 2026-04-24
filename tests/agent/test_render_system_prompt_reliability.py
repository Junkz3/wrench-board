# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from unittest.mock import patch

from api.agent.manifest import render_system_prompt
from api.session.state import SessionState


def test_prompt_omits_line_when_reliability_unknown():
    session = SessionState()
    with patch(
        "api.agent.manifest.load_reliability_line",
        return_value=None,
    ):
        prompt = render_system_prompt(session, device_slug="test-device")
    assert "Simulator reliability" not in prompt


def test_prompt_includes_line_when_reliability_known():
    session = SessionState()
    with patch(
        "api.agent.manifest.load_reliability_line",
        return_value="Simulator reliability for test-device: score=0.78 ...",
    ):
        prompt = render_system_prompt(session, device_slug="test-device")
    assert "Simulator reliability for test-device" in prompt
    assert "0.78" in prompt
