"""Verify that a validation.start payload round-trips through chat_history
with a source=trigger marker."""

from pathlib import Path

from api.agent.chat_history import append_event, load_events


def test_trigger_event_persists_with_source_marker(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path / "memory"))
    # Reload settings to pick up the patched MEMORY_ROOT.
    from api import config
    monkeypatch.setattr(config, "_settings", None, raising=False)

    trigger_text = (
        "[Action tech — Marquer fix] "
        "L'utilisateur vient de confirmer que la repair r1 est résolue."
    )
    append_event(
        device_slug="demo",
        repair_id="r1",
        conv_id="c1",
        event={
            "role": "user",
            "content": trigger_text,
            "source": "trigger",
            "trigger_kind": "validation.start",
        },
        memory_root=tmp_path / "memory",
    )
    events = load_events(
        device_slug="demo", repair_id="r1", conv_id="c1",
        memory_root=tmp_path / "memory",
    )
    assert len(events) == 1
    assert events[0]["role"] == "user"
    assert events[0].get("source") == "trigger"
    assert events[0].get("trigger_kind") == "validation.start"
    assert "Marquer fix" in events[0]["content"]
