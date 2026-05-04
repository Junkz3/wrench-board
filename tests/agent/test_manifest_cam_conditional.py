"""cam_capture must be in the manifest only when session.has_camera is True."""

from __future__ import annotations

from api.agent.manifest import build_tools_manifest
from api.session.state import SessionState


def _tool_names(manifest):
    return {t["name"] for t in manifest}


def test_cam_capture_absent_when_no_camera():
    session = SessionState()
    assert session.has_camera is False
    names = _tool_names(build_tools_manifest(session))
    assert "cam_capture" not in names


def test_cam_capture_present_when_camera_available():
    session = SessionState()
    session.has_camera = True
    names = _tool_names(build_tools_manifest(session))
    assert "cam_capture" in names


def test_cam_capture_independent_of_board():
    """cam_capture is gated on has_camera, not on board presence."""
    session = SessionState()
    session.has_camera = True
    # No board loaded — bv_* should be absent, but cam_capture still present.
    names = _tool_names(build_tools_manifest(session))
    assert "cam_capture" in names
    assert "bv_highlight" not in names  # confirm baseline
