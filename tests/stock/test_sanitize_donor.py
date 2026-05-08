"""Donor ID anti-hallucination guard. See spec §9."""

from api.agent.sanitize import _validate_donor_ids, sanitize_agent_text


def test_unknown_donor_id_wrapped(tmp_path, monkeypatch):
    monkeypatch.setattr("api.stock.store._memory_root", lambda: tmp_path / "memory")
    monkeypatch.setattr("api.stock.store._stock_root", lambda: tmp_path / "memory" / "_stock")
    text = "Tu as iphone-x-donor-2026-001 dans ton stock."
    out = _validate_donor_ids(text)
    assert "⟨?donor:invalid⟩" in out
    assert "iphone-x-donor-2026-001" not in out


def test_known_donor_id_passes_through(tmp_path, monkeypatch):
    monkeypatch.setattr("api.stock.store._memory_root", lambda: tmp_path / "memory")
    monkeypatch.setattr(
        "api.stock.store._stock_root", lambda: tmp_path / "memory" / "_stock"
    )
    (tmp_path / "memory" / "iphone-x").mkdir(parents=True)
    from api.stock.store import mark_donor

    donor_id = mark_donor(device_slug="iphone-x", label="X")
    text = f"Tu as {donor_id} dans ton stock."
    out = _validate_donor_ids(text)
    assert "⟨?donor:invalid⟩" not in out
    assert donor_id in out


def test_sanitize_agent_text_runs_donor_validation_when_no_board(tmp_path, monkeypatch):
    """The donor-id pass must run even on the board=None path."""
    monkeypatch.setattr("api.stock.store._memory_root", lambda: tmp_path / "memory")
    monkeypatch.setattr(
        "api.stock.store._stock_root", lambda: tmp_path / "memory" / "_stock"
    )
    text = "Source it from iphone-x-donor-2026-999."
    out, unknown = sanitize_agent_text(text, board=None)
    assert "⟨?donor:invalid⟩" in out
    assert unknown == []


def test_sanitize_fail_open_on_store_error(tmp_path, monkeypatch):
    """If the inventory store raises, donor IDs pass through unchanged
    (degraded mode — never block the agent's response on a missing store)."""

    def _raise() -> None:
        raise RuntimeError("simulated store unavailable")

    monkeypatch.setattr("api.stock.store.load_inventory", _raise)
    text = "iphone-x-donor-2026-001 should pass through."
    out = _validate_donor_ids(text)
    assert "iphone-x-donor-2026-001" in out
    assert "⟨?donor:invalid⟩" not in out
