"""client.capabilities frame must update session.has_camera."""

from __future__ import annotations

from api.agent.runtime_managed import _handle_client_capabilities
from api.session.state import SessionState


def test_capabilities_sets_has_camera_true():
    session = SessionState()
    assert session.has_camera is False
    _handle_client_capabilities(
        session, {"type": "client.capabilities", "camera_available": True}
    )
    assert session.has_camera is True


def test_capabilities_sets_has_camera_false():
    session = SessionState()
    session.has_camera = True
    _handle_client_capabilities(
        session, {"type": "client.capabilities", "camera_available": False}
    )
    assert session.has_camera is False


def test_capabilities_missing_camera_field_defaults_false():
    session = SessionState()
    session.has_camera = True
    _handle_client_capabilities(session, {"type": "client.capabilities"})
    assert session.has_camera is False


def test_capabilities_non_bool_camera_field_coerces_safely():
    session = SessionState()
    _handle_client_capabilities(
        session, {"type": "client.capabilities", "camera_available": "yes"}
    )
    # Truthy non-bool → True (we coerce via bool())
    assert session.has_camera is True
    _handle_client_capabilities(
        session, {"type": "client.capabilities", "camera_available": None}
    )
    assert session.has_camera is False
