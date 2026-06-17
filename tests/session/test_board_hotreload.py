"""Per-owner boardview resolution + mid-session hot-reload for the agent.

Regression suite for the "boardview désync" bug: the diagnostic agent loads
its board ONCE at WS open (`SessionState.from_device`). A boardview uploaded
or switched *after* the session opened was invisible to the `bv_*` tools — the
tech saw the board in the viewer (which reads disk live, per request) while the
agent reported `no-board-loaded`. Two defects, one root cause (the board-load
was never aligned with T9's per-owner live-graph model):

  1. Staleness — no reload mid-session; only a WS reconnect picked up an upload.
  2. Owner-blindness — `from_device(slug)` read the ROOT pin + scanned the SHARED
     uploads dir newest-first, so in managed multi-tenant it could load (or leak)
     another tenant's board.

The fix: `from_device(slug, owner_ref=...)` resolves STRICTLY the tenant's own
per-owner pin, remembers (slug, owner, source file), and `refresh_board_if_changed()`
re-resolves + reparses only when the active file changed — called on every `bv_*`
dispatch so an upload becomes visible without a reconnect.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from api.session.state import SessionState

FIXTURE_DIR = Path(__file__).parent.parent / "board" / "fixtures"
MINIMAL_BRD = FIXTURE_DIR / "minimal.brd"


def _seed_owner_boardview(
    memory_root: Path, slug: str, owner: str, filename: str, src: Path = MINIMAL_BRD
) -> None:
    """Drop a parseable boardview into the shared uploads dir and pin it for
    `owner` via the T9 per-owner pointer (`_sources/{owner}/active_sources.json`)."""
    uploads = memory_root / slug / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, uploads / filename)
    sdir = memory_root / slug / "_sources" / owner
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "active_sources.json").write_text(
        json.dumps({"boardview": {"filename": filename, "hash": None}}),
        encoding="utf-8",
    )


def _drop_upload(memory_root: Path, slug: str, filename: str, src: Path = MINIMAL_BRD) -> None:
    uploads = memory_root / slug / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, uploads / filename)


def test_from_device_owner_scoped_ignores_other_tenants_upload(tmp_path, monkeypatch):
    monkeypatch.setenv("WRENCH_BOARD_MEMORY_ROOT", str(tmp_path))
    slug = "dev-x"
    # Tenant A pins their own (older) board; tenant B has a NEWER unpinned file
    # sitting in the same shared uploads dir. Owner-blind resolution would grab
    # B's (newest-first); per-owner resolution must return A's.
    _seed_owner_boardview(tmp_path, slug, "tenantA", "20260101T000000Z-boardview-A.brd")
    _drop_upload(tmp_path, slug, "20260601T000000Z-boardview-B.brd")

    s = SessionState.from_device(slug, owner_ref="tenantA")

    assert s.board is not None
    assert s.board_source is not None
    assert s.board_source.name == "20260101T000000Z-boardview-A.brd"


def test_managed_session_without_active_boardview_has_no_board(tmp_path, monkeypatch):
    monkeypatch.setenv("WRENCH_BOARD_MEMORY_ROOT", str(tmp_path))
    s = SessionState.from_device("dev-x", owner_ref="tenantA")
    assert s.board is None


def test_refresh_loads_boardview_uploaded_after_session_open(tmp_path, monkeypatch):
    # THE reported bug. Session opens before the upload → board None. After the
    # tech uploads (per-owner pin written), a bv_* dispatch refreshes → board
    # loads without a WS reconnect.
    monkeypatch.setenv("WRENCH_BOARD_MEMORY_ROOT", str(tmp_path))
    slug = "dev-x"
    s = SessionState.from_device(slug, owner_ref="tenantA")
    assert s.board is None

    _seed_owner_boardview(tmp_path, slug, "tenantA", "20260101T000000Z-boardview-A.brd")
    changed = s.refresh_board_if_changed()

    assert changed is True
    assert s.board is not None
    assert s.board_source.name == "20260101T000000Z-boardview-A.brd"


def test_refresh_noop_when_unchanged_preserves_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("WRENCH_BOARD_MEMORY_ROOT", str(tmp_path))
    slug = "dev-x"
    _seed_owner_boardview(tmp_path, slug, "tenantA", "20260101T000000Z-boardview-A.brd")
    s = SessionState.from_device(slug, owner_ref="tenantA")
    assert s.board is not None
    loaded = s.board
    s.highlights.add("R1")

    changed = s.refresh_board_if_changed()

    assert changed is False
    assert s.board is loaded  # same object — not reparsed
    assert "R1" in s.highlights  # overlay untouched (set_board NOT called)


def test_refresh_reloads_when_active_boardview_switched(tmp_path, monkeypatch):
    # Tech switches the active pin to a different uploaded boardview mid-session.
    monkeypatch.setenv("WRENCH_BOARD_MEMORY_ROOT", str(tmp_path))
    slug = "dev-x"
    _seed_owner_boardview(tmp_path, slug, "tenantA", "20260101T000000Z-boardview-A.brd")
    s = SessionState.from_device(slug, owner_ref="tenantA")
    s.highlights.add("R1")

    # Switch the pin to a second file.
    _seed_owner_boardview(tmp_path, slug, "tenantA", "20260202T000000Z-boardview-A2.brd")
    changed = s.refresh_board_if_changed()

    assert changed is True
    assert s.board_source.name == "20260202T000000Z-boardview-A2.brd"
    assert s.highlights == set()  # board changed → overlay reset (set_board)


def test_self_host_owner_none_uses_root_chain_unchanged(tmp_path, monkeypatch):
    # Self-host (owner None) keeps the legacy slug-scoped uploads scan — a plain
    # `-boardview-` file with no per-owner pin must still load.
    monkeypatch.setenv("WRENCH_BOARD_MEMORY_ROOT", str(tmp_path))
    slug = "dev-x"
    _drop_upload(tmp_path, slug, "20260101T000000Z-boardview-local.brd")

    s = SessionState.from_device(slug)  # no owner_ref

    assert s.board is not None
    assert s.board_source.name == "20260101T000000Z-boardview-local.brd"
