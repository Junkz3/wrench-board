"""Unit tests for per-repair JSONL chat history."""

from __future__ import annotations

import json

import pytest

from api import config as config_mod
from api.agent.chat_history import (
    append_event,
    build_ctx_tag,
    ensure_conversation,
    load_events,
    strip_ctx_tag,
    touch_status,
)


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch):
    monkeypatch.setattr(config_mod, "_settings", None)
    yield
    monkeypatch.setattr(config_mod, "_settings", None)


def _ensure_conv(tmp_path, slug="demo-pi", repair="r1", tier="fast") -> str:
    """Helper: resolve/create the default conversation the tests write into."""
    conv_id, _ = ensure_conversation(
        device_slug=slug, repair_id=repair, conv_id=None, tier=tier,
        memory_root=tmp_path,
    )
    return conv_id


def test_append_then_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_BACKEND", "jsonl")
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))

    conv_id = _ensure_conv(tmp_path)

    append_event(
        device_slug="demo-pi",
        repair_id="r1",
        conv_id=conv_id,
        event={"role": "user", "content": "Pas de son"},
        memory_root=tmp_path,
    )
    append_event(
        device_slug="demo-pi",
        repair_id="r1",
        conv_id=conv_id,
        event={"role": "assistant", "content": [{"type": "text", "text": "OK"}]},
        memory_root=tmp_path,
    )

    events = load_events(
        device_slug="demo-pi", repair_id="r1", conv_id=conv_id,
        memory_root=tmp_path,
    )
    assert len(events) == 2
    assert events[0]["role"] == "user"
    assert events[0]["content"] == "Pas de son"
    assert events[1]["role"] == "assistant"


def test_load_returns_empty_when_no_history(tmp_path):
    events = load_events(
        device_slug="nobody", repair_id="never-happened",
        conv_id="deadbeef", memory_root=tmp_path,
    )
    assert events == []


def test_append_is_noop_without_repair_id(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_BACKEND", "jsonl")
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    append_event(
        device_slug="demo-pi",
        repair_id=None,
        conv_id="whatever",
        event={"role": "user", "content": "Pas de son"},
        memory_root=tmp_path,
    )
    # Nothing should have been written under demo-pi/repairs/.
    assert not (tmp_path / "demo-pi" / "repairs").exists()


def test_append_is_noop_when_backend_not_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_BACKEND", "managed_agents")
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    append_event(
        device_slug="demo-pi",
        repair_id="r1",
        conv_id="whatever",
        event={"role": "user", "content": "Pas de son"},
        memory_root=tmp_path,
    )
    assert not (tmp_path / "demo-pi" / "repairs" / "r1").exists()


def test_load_skips_malformed_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_BACKEND", "jsonl")
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    # Seed a conversation-scoped messages.jsonl directly (legacy migration is
    # covered in test_conversations.py — here we just test the load path).
    conv_id = _ensure_conv(tmp_path)
    d = tmp_path / "demo-pi" / "repairs" / "r1" / "conversations" / conv_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "messages.jsonl").write_text(
        '{"ts":"t1","event":{"role":"user","content":"ok"}}\n'
        "not-json\n"
        '{"ts":"t2","event":{"role":"assistant","content":"reply"}}\n',
        encoding="utf-8",
    )
    events = load_events(
        device_slug="demo-pi", repair_id="r1", conv_id=conv_id,
        memory_root=tmp_path,
    )
    assert len(events) == 2  # middle line dropped


def test_touch_status_updates_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_BACKEND", "jsonl")
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    # Seed a metadata file like /pipeline/repairs creates.
    repairs_dir = tmp_path / "demo-pi" / "repairs"
    repairs_dir.mkdir(parents=True)
    meta_path = repairs_dir / "r1.json"
    meta_path.write_text(json.dumps({
        "repair_id": "r1",
        "device_slug": "demo-pi",
        "device_label": "Demo Pi",
        "symptom": "no boot",
        "status": "open",
        "created_at": "2026-04-22T12:00:00+00:00",
    }))

    touch_status(
        device_slug="demo-pi",
        repair_id="r1",
        status="in_progress",
        memory_root=tmp_path,
    )
    updated = json.loads(meta_path.read_text())
    assert updated["status"] == "in_progress"
    assert "status_updated_at" in updated


def test_touch_status_noop_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    # Should not raise.
    touch_status(
        device_slug="nobody",
        repair_id="never",
        status="closed",
        memory_root=tmp_path,
    )


def test_cost_persists_alongside_event(tmp_path, monkeypatch):
    """append_event with `cost` kwarg writes it on the record so replay can
    re-emit it and the lifetime cost chip accumulates correctly.
    """
    from api.agent.chat_history import (
        append_event,
        load_events,
        load_events_with_costs,
    )

    monkeypatch.setenv("CHAT_HISTORY_BACKEND", "jsonl")
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))

    conv_id = _ensure_conv(tmp_path)

    append_event(
        device_slug="demo-pi", repair_id="r1", conv_id=conv_id,
        event={"role": "user", "content": "hello"},
        memory_root=tmp_path,
    )
    append_event(
        device_slug="demo-pi", repair_id="r1", conv_id=conv_id,
        event={"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        cost={"model": "claude-haiku-4-5", "cost_usd": 0.023, "priced": True},
        memory_root=tmp_path,
    )

    # load_events (legacy) still returns just events.
    plain = load_events(
        device_slug="demo-pi", repair_id="r1", conv_id=conv_id, memory_root=tmp_path
    )
    assert len(plain) == 2
    assert plain[0]["role"] == "user"

    # load_events_with_costs surfaces per-record cost.
    records = load_events_with_costs(
        device_slug="demo-pi", repair_id="r1", conv_id=conv_id, memory_root=tmp_path,
    )
    assert len(records) == 2
    user_event, user_cost = records[0]
    assistant_event, assistant_cost = records[1]
    assert user_cost is None
    assert assistant_cost is not None
    assert assistant_cost["cost_usd"] == 0.023
    assert assistant_cost["model"] == "claude-haiku-4-5"


def test_save_and_load_ma_session_id_per_tier(tmp_path, monkeypatch):
    from api.agent.chat_history import load_ma_session_id, save_ma_session_id

    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    rdir = tmp_path / "demo-pi" / "repairs"
    rdir.mkdir(parents=True)
    meta_path = rdir / "r1.json"
    meta_path.write_text(json.dumps({
        "repair_id": "r1",
        "device_slug": "demo-pi",
        "device_label": "Demo Pi",
        "symptom": "no boot",
        "status": "open",
        "created_at": "2026-04-22T12:00:00+00:00",
        "ma_session_id": "legacy_session_pre_tier_storage",
    }))

    conv_id = _ensure_conv(tmp_path)

    # Legacy top-level ma_session_id is IGNORED by the new loader.
    assert load_ma_session_id(
        device_slug="demo-pi", repair_id="r1", conv_id=conv_id, tier="fast",
        memory_root=tmp_path,
    ) is None

    save_ma_session_id(
        device_slug="demo-pi", repair_id="r1", conv_id=conv_id,
        session_id="sesn_fast_A", tier="fast",
        memory_root=tmp_path,
    )
    save_ma_session_id(
        device_slug="demo-pi", repair_id="r1", conv_id=conv_id,
        session_id="sesn_normal_B", tier="normal",
        memory_root=tmp_path,
    )

    assert load_ma_session_id(
        device_slug="demo-pi", repair_id="r1", conv_id=conv_id, tier="fast",
        memory_root=tmp_path,
    ) == "sesn_fast_A"
    assert load_ma_session_id(
        device_slug="demo-pi", repair_id="r1", conv_id=conv_id, tier="normal",
        memory_root=tmp_path,
    ) == "sesn_normal_B"
    assert load_ma_session_id(
        device_slug="demo-pi", repair_id="r1", conv_id=conv_id, tier="deep",
        memory_root=tmp_path,
    ) is None


def _write_repair_meta(tmp_path, slug: str, rid: str, payload: dict) -> None:
    target = tmp_path / slug / "repairs" / f"{rid}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")


def test_build_ctx_tag_includes_label_and_plainte_init(tmp_path):
    _write_repair_meta(
        tmp_path,
        "iphone-x",
        "r42",
        {
            "device_label": "iPhone X",
            "symptom": "pas de boot, écran noir",
        },
    )
    tag = build_ctx_tag(
        device_slug="iphone-x", repair_id="r42", memory_root=tmp_path
    )
    # Wording is deliberately passive — `plainte_init` (not `symptôme`) and
    # quoted so the agent reads it as opening-sheet metadata, not as a fresh
    # symptom declaration that would re-trigger mb_get_rules_for_symptoms.
    assert (
        tag
        == '[ctx · device=iPhone X (iphone-x) · plainte_init="pas de boot, écran noir"]'
    )


def test_build_ctx_tag_falls_back_to_slug_when_no_meta(tmp_path):
    tag = build_ctx_tag(
        device_slug="iphone-x", repair_id="r-missing", memory_root=tmp_path
    )
    # No meta file → label defaults to slug, no plainte_init segment.
    assert tag == "[ctx · device=iphone-x (iphone-x)]"


def test_build_ctx_tag_omits_plainte_init_when_empty(tmp_path):
    _write_repair_meta(
        tmp_path, "macbook-air", "r1", {"device_label": "MacBook Air", "symptom": "  "}
    )
    tag = build_ctx_tag(
        device_slug="macbook-air", repair_id="r1", memory_root=tmp_path
    )
    assert tag == "[ctx · device=MacBook Air (macbook-air)]"


def test_build_ctx_tag_returns_none_without_repair_id(tmp_path):
    assert (
        build_ctx_tag(device_slug="iphone-x", repair_id=None, memory_root=tmp_path)
        is None
    )


def test_strip_ctx_tag_peels_leading_tag():
    text = (
        '[ctx · device=iPhone X (iphone-x) · plainte_init="écran noir"]\n\n'
        "Salut, j'ai un souci"
    )
    assert strip_ctx_tag(text) == "Salut, j'ai un souci"


def test_strip_ctx_tag_no_op_when_absent():
    text = "Salut, juste un message normal"
    assert strip_ctx_tag(text) == text


def test_strip_ctx_tag_returns_empty_on_tag_only():
    assert strip_ctx_tag("[ctx · device=foo]") == ""


# `_strip_intro_wrapper` was removed when the layered MA memory architecture
# replaced the LLM-driven session-resume summary path (2026-04-26). The
# function it tested no longer exists; the agent now self-orients from the
# per-repair scribe mount, and there's no longer a Haiku summarization that
# needs the prior intro stripped from transcript lines.
