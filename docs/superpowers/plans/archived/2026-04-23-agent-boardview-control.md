# Agent Boardview Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the diagnostic agent to the existing PCB renderer so tool calls mutate the canvas end-to-end (tech types « highlight U7 » → agent calls `bv_highlight` → backend dispatches + emits WS event → frontend animates the canvas).

**Architecture:** 13 cabling gaps between layers that already exist. Backend: dynamic per-session tool manifest (`mb_*` always, `bv_*` when board loaded), scission of tool_result (text → agent) and WS event (visual → frontend), post-hoc refdes sanitizer. Frontend: early stub + pending buffer for `window.Boardview`, real API exposed from `brd_viewer.js`, split user/agent state in the renderer so the tech's selection and the agent's highlights coexist.

**Tech stack:** Python 3.11+, FastAPI, anthropic SDK (messages.create + Managed Agents beta), Pydantic v2, pytest + pytest-asyncio, WebSocket, vanilla JS (ES modules, no build step), D3.js v7.

**Reference spec:** `docs/superpowers/specs/2026-04-23-agent-boardview-control-design.md`.

**Commit decomposition (3 commits minimum, per CLAUDE.md commit hygiene):**
- Group A → `feat(agent): bv_* tools + dynamic manifest + mb_* aggregation + sanitizer`
- Group B → `feat(web): window.Boardview public API + agent state split`
- Group C → `docs: rewrite Hard Rule #5 (tool-boundary verification + post-hoc sanitizer)`

An optional Task A7b (re-bootstrap the Managed Agents with the new tool set) may add a 4th small commit on `managed_ids.json`, ask Alexis first.

Use `git commit -m "..." -- path1 path2` (explicit paths) because parallel agents may be active on this repo.

**Managed vs Direct runtime note:** tools on Managed Agents are registered at agent-bootstrap time (cf. `scripts/bootstrap_managed_agent.py`), not at session-create time. The per-session `build_tools_manifest(session)` applies cleanly to the direct runtime only (`DIAGNOSTIC_MODE=direct`). For managed (the default), the agent sees a fixed superset baked in at bootstrap — missing `bv_*` until A7b runs, and the dispatch handles runtime unavailability by returning `{ok: false, reason: "no-board-loaded"}` when appropriate.

---

## File Structure

**Created files:**
- `api/agent/sanitize.py` — `sanitize_agent_text(text, board)` regex + validation. ~30 lines.
- `api/agent/manifest.py` — `MB_TOOLS`, `BV_TOOLS`, `build_tools_manifest(session)`, `render_system_prompt(session, device_slug)`. ~200 lines (mostly static schemas).
- `api/agent/dispatch_bv.py` — `BV_DISPATCH` mapping + `dispatch_bv(session, name, payload)` wrapper. ~50 lines.
- `tests/agent/test_sanitize.py` — sanitizer unit tests.
- `tests/agent/test_manifest_dynamic.py` — manifest + system prompt unit tests.
- `tests/agent/test_dispatch_bv.py` — dispatch unit tests (12 `bv_*` tools × happy path + error paths).
- `tests/agent/test_mb_aggregation.py` — the 4 presence cases of restructured `mb_get_component`.
- `tests/agent/test_session_from_device.py` — `SessionState.from_device` helper tests.
- `tests/agent/test_ws_flow.py` — integration test (mock AsyncAnthropic, walk one tool_use + event emission + sanitize).

**Modified files:**
- `api/session/state.py` — add `schematic: Any = None` field + `@classmethod from_device(device_slug)`. ~40 added lines.
- `api/agent/tools.py` — restructure `mb_get_component` return shape (breaking). Keep `mb_get_rules_for_symptoms`, `mb_list_findings`, `mb_record_finding` unchanged.
- `api/agent/runtime_direct.py` — replace static `SYSTEM_PROMPT_DIRECT` + `TOOLS` with dynamic builders; wire `dispatch_bv`; emit WS events; wrap text blocks through `sanitize_agent_text`. ~60 lines changed.
- `api/agent/runtime_managed.py` — wire `dispatch_bv`; emit WS events; wrap text blocks through `sanitize_agent_text`. ~40 lines changed. (No system prompt injection — agent carries it server-side.)
- `tests/agent/test_tools.py` — migrate assertions from flat `result["role"]` to nested `result["memory_bank"]["role"]`.
- `web/js/main.js` — early-load stub for `window.Boardview` + pending buffer. ~8 lines added.
- `web/js/llm.js` — listener that routes `payload.type.startsWith("boardview.")` to `window.Boardview.apply(payload)`. ~3 lines added inside `ws.addEventListener("message", …)`.
- `web/brd_viewer.js` — split `state.selectedPart/selectedPinIdx` under `state.user.*`, add `state.agent = {...}`, expose `window.Boardview` public API, drain `__pending`. ~200 lines changed/added (the big frontend piece).
- `CLAUDE.md` — rewrite Hard Rule #5.

**Unchanged (read-only references):**
- `api/tools/boardview.py` — 12 handlers stay as-is.
- `api/tools/ws_events.py` — 14 Pydantic envelopes stay as-is.
- `api/board/*` — parser, model, validator untouched.
- `api/pipeline/*` — knowledge pipeline untouched.

---

# Group A — Backend (ends in commit 1)

## Task A1: SessionState.from_device helper + schematic field

**Files:**
- Modify: `api/session/state.py`
- Create: `tests/agent/test_session_from_device.py`

**Context:** `SessionState` currently has no way to auto-load a board from a device slug. Runtimes need a one-liner that produces a ready session. If the board file is missing or parsing fails, we don't raise — we return `SessionState()` (board=None) and log a warning. The agent will simply not get `bv_*` tools in its manifest. Also add `schematic: Any = None` as a future-proofing hook (the `sch_*` family reads it).

- [ ] **Step 1: Write the failing tests**

Create `tests/agent/test_session_from_device.py`:

```python
"""Tests for SessionState.from_device — auto-loads a board for a device slug."""

from pathlib import Path

import pytest

from api.session.state import SessionState


@pytest.fixture
def board_assets_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake board_assets/ directory, scoped to a tmp path."""
    assets = tmp_path / "board_assets"
    assets.mkdir()
    # Point SessionState.from_device at this dir via an env var the helper reads.
    monkeypatch.setenv("WRENCH_BOARD_BOARD_ASSETS", str(assets))
    return assets


def test_from_device_slug_with_no_file_returns_empty_session(board_assets_dir: Path) -> None:
    session = SessionState.from_device("does-not-exist")
    assert session.board is None
    assert session.schematic is None


def test_from_device_prefers_kicad_pcb_over_brd(board_assets_dir: Path) -> None:
    """When both .kicad_pcb and .brd exist, .kicad_pcb wins."""
    # Copy real fixtures — these come from the repo's board_assets/.
    repo_root = Path(__file__).resolve().parents[2]
    src = repo_root / "board_assets" / "mnt-reform-motherboard.kicad_pcb"
    if not src.exists():
        pytest.skip("fixture mnt-reform-motherboard.kicad_pcb not available")
    (board_assets_dir / "mnt-reform-motherboard.kicad_pcb").write_bytes(src.read_bytes())
    # Drop a bogus .brd next to it — if the helper picks .brd, parse will crash.
    (board_assets_dir / "mnt-reform-motherboard.brd").write_text("GARBAGE\n")

    session = SessionState.from_device("mnt-reform-motherboard")
    assert session.board is not None
    # Sanity: at least one part present in a real MNT Reform board.
    assert len(session.board.parts) > 10


def test_from_device_falls_back_to_brd_when_no_kicad_pcb(board_assets_dir: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    src = repo_root / "board_assets" / "mnt-reform-motherboard.brd"
    if not src.exists():
        pytest.skip("fixture mnt-reform-motherboard.brd not available")
    (board_assets_dir / "mnt-reform-motherboard.brd").write_bytes(src.read_bytes())

    session = SessionState.from_device("mnt-reform-motherboard")
    assert session.board is not None


def test_from_device_swallows_parse_errors(board_assets_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Corrupted file → session has no board, warning logged, no exception."""
    (board_assets_dir / "bogus.kicad_pcb").write_text("not a kicad file\n")
    import logging
    with caplog.at_level(logging.WARNING):
        session = SessionState.from_device("bogus")
    assert session.board is None
    assert any("board load failed" in rec.message.lower() for rec in caplog.records)


def test_schematic_field_defaults_to_none() -> None:
    assert SessionState().schematic is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/agent/test_session_from_device.py -v`
Expected: FAIL — `SessionState.from_device` doesn't exist, `schematic` field missing.

- [ ] **Step 3: Implement `SessionState.from_device` and `schematic` field**

Edit `api/session/state.py`:

```python
"""Per-session state for the boardview panel."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from api.board.model import Board
from api.board.parser.base import parser_for

logger = logging.getLogger("wrench_board.session")

Side = Literal["top", "bottom"]

# Extension priority: richer formats first. If both exist for a slug,
# .kicad_pcb wins.
_BOARD_EXT_PRIORITY = (".kicad_pcb", ".brd")


def _board_assets_root() -> Path:
    """Root of board_assets/. Overridable via WRENCH_BOARD_BOARD_ASSETS env for tests."""
    override = os.environ.get("WRENCH_BOARD_BOARD_ASSETS")
    if override:
        return Path(override)
    # api/session/state.py → ../../board_assets
    return Path(__file__).resolve().parents[2] / "board_assets"


@dataclass
class SessionState:
    board: Board | None = None
    schematic: Any = None  # Hook for future sch_* tool family; not populated here.
    layer: Side = "top"
    highlights: set[str] = field(default_factory=set)
    net_highlight: str | None = None
    annotations: dict[str, dict[str, Any]] = field(default_factory=dict)
    arrows: dict[str, dict[str, Any]] = field(default_factory=dict)
    dim_unrelated: bool = False
    filter_prefix: str | None = None
    layer_visibility: dict[Side, bool] = field(
        default_factory=lambda: {"top": True, "bottom": True}
    )

    def set_board(self, board: Board) -> None:
        """Load a new board and reset all view state."""
        self.board = board
        self.layer = "top"
        self.highlights = set()
        self.net_highlight = None
        self.annotations = {}
        self.arrows = {}
        self.dim_unrelated = False
        self.filter_prefix = None
        self.layer_visibility = {"top": True, "bottom": True}

    @classmethod
    def from_device(cls, device_slug: str) -> SessionState:
        """Build a session for a device, auto-loading the board if available.

        Priority: .kicad_pcb first, then .brd. If no file is found or parsing
        fails, returns an empty SessionState — the agent will simply not get
        the `bv_*` tool family in its manifest.
        """
        root = _board_assets_root()
        for ext in _BOARD_EXT_PRIORITY:
            candidate = root / f"{device_slug}{ext}"
            if not candidate.exists():
                continue
            try:
                parser = parser_for(candidate)
                board = parser.parse_file(candidate)
                session = cls()
                session.set_board(board)
                return session
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "board load failed for %s (%s): %s", device_slug, candidate.name, exc
                )
                return cls()  # fall through with empty session
        return cls()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/agent/test_session_from_device.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Run the full board/session test suite to make sure nothing regressed**

Run: `.venv/bin/pytest tests/board/ tests/agent/ -v`
Expected: all existing tests still PASS.

---

## Task A2: Refdes sanitizer module

**Files:**
- Create: `api/agent/sanitize.py`
- Create: `tests/agent/test_sanitize.py`

**Context:** Post-hoc guard against hallucinated refdes in outbound agent text. Regex `\b[A-Z]{1,3}\d{1,4}\b` catches refdes-shaped tokens. When a board is loaded, each match is validated against `board.part_by_refdes`; unknown ones are wrapped as `⟨?U999⟩` in the delivered text. No-op when `board is None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/agent/test_sanitize.py`:

```python
"""Tests for sanitize_agent_text — post-hoc refdes guard."""

from api.agent.sanitize import sanitize_agent_text
from api.board.model import Board, Layer, Part, Point


def _board_with_parts(refdeses: list[str]) -> Board:
    parts = [
        Part(
            refdes=r,
            layer=Layer.TOP,
            is_smd=True,
            bbox=(Point(x=0, y=0), Point(x=10, y=10)),
            pin_refs=[],
        )
        for r in refdeses
    ]
    return Board(
        board_id="test", file_hash="sha256:x", source_format="test",
        outline=[], parts=parts, pins=[], nets=[], nails=[],
    )


def test_noop_when_board_is_none() -> None:
    text = "Check U7 and U999 please"
    clean, unknown = sanitize_agent_text(text, None)
    assert clean == text
    assert unknown == []


def test_wraps_unknown_refdes_and_keeps_known() -> None:
    board = _board_with_parts(["U7"])
    clean, unknown = sanitize_agent_text("Check U7 and U999 please", board)
    assert clean == "Check U7 and ⟨?U999⟩ please"
    assert unknown == ["U999"]


def test_multiple_unknown_refdes_all_wrapped() -> None:
    board = _board_with_parts(["C1"])
    clean, unknown = sanitize_agent_text("U1, U2, C1, R3 are suspect", board)
    assert "⟨?U1⟩" in clean
    assert "⟨?U2⟩" in clean
    assert "C1" in clean  # known, not wrapped
    assert "⟨?R3⟩" in clean
    assert set(unknown) == {"U1", "U2", "R3"}


def test_does_not_match_net_names_with_underscore() -> None:
    board = _board_with_parts([])
    clean, unknown = sanitize_agent_text("HDMI_D0 and VDD_3V3 are rails", board)
    assert clean == "HDMI_D0 and VDD_3V3 are rails"
    assert unknown == []


def test_does_not_match_lowercase() -> None:
    board = _board_with_parts([])
    clean, unknown = sanitize_agent_text("the u7 part is mentioned", board)
    assert clean == "the u7 part is mentioned"
    assert unknown == []


def test_flags_refdes_shaped_protocol_names() -> None:
    """Tokens like USB3 match the pattern; flagged when absent. Known limitation."""
    board = _board_with_parts([])
    clean, unknown = sanitize_agent_text("USB3 is fine", board)
    assert clean == "⟨?USB3⟩ is fine"
    assert unknown == ["USB3"]


def test_empty_text() -> None:
    board = _board_with_parts(["U1"])
    clean, unknown = sanitize_agent_text("", board)
    assert clean == ""
    assert unknown == []


def test_refdes_at_string_boundaries() -> None:
    board = _board_with_parts(["U1"])
    clean, unknown = sanitize_agent_text("U999", board)
    assert clean == "⟨?U999⟩"
    assert unknown == ["U999"]
    clean, unknown = sanitize_agent_text("U1", board)
    assert clean == "U1"
    assert unknown == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/agent/test_sanitize.py -v`
Expected: FAIL — `api.agent.sanitize` module doesn't exist.

- [ ] **Step 3: Implement sanitizer**

Create `api/agent/sanitize.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Post-hoc refdes sanitizer.

Second layer of defense against hallucinated component IDs. The first
layer is tool discipline (mb_get_component returns {found: false} for
unknown refdes); this layer scans outbound agent text and wraps
refdes-shaped tokens that don't resolve on the current board.
"""

from __future__ import annotations

import re

from api.board.model import Board
from api.board.validator import is_valid_refdes

REFDES_RE = re.compile(r"\b[A-Z]{1,3}\d{1,4}\b")


def sanitize_agent_text(text: str, board: Board | None) -> tuple[str, list[str]]:
    """Return (clean_text, unknown_refdes_list).

    If board is None, no ground truth exists — returns text unchanged.
    """
    if board is None:
        return text, []

    unknown: list[str] = []

    def _wrap(match: re.Match[str]) -> str:
        token = match.group(0)
        if is_valid_refdes(board, token):
            return token
        unknown.append(token)
        return f"⟨?{token}⟩"

    return REFDES_RE.sub(_wrap, text), unknown
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/agent/test_sanitize.py -v`
Expected: PASS (8 tests).

---

## Task A3: Restructure `mb_get_component` (BREAKING)

**Files:**
- Modify: `api/agent/tools.py`
- Create: `tests/agent/test_mb_aggregation.py`
- Modify: `tests/agent/test_tools.py`

**Context:** Current `mb_get_component` returns a flat object (`{found, canonical_name, role, package, kind, aliases, ...}`). Spec §5.1 restructures into sections: `{found, canonical_name, memory_bank: {...} | null, board: {...} | null, closest_matches: [...]}`. The 4 explicit presence cases (cf. §5.1). Existing tests in `tests/agent/test_tools.py` assert the flat form — migrate them.

- [ ] **Step 1: Write the failing tests for the new aggregated behavior**

Create `tests/agent/test_mb_aggregation.py`:

```python
"""Tests for the 4 presence cases of restructured mb_get_component."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.agent.tools import mb_get_component
from api.board.model import Board, Layer, Part, Pin, Point
from api.session.state import SessionState


@pytest.fixture
def seeded_memory(tmp_path: Path) -> Path:
    """Memory root with U7 (pmic) and C29 (cap) in the registry and dictionary."""
    slug_dir = tmp_path / "testdev"
    slug_dir.mkdir()
    (slug_dir / "registry.json").write_text(json.dumps({
        "components": [
            {"canonical_name": "U7", "aliases": ["pmic"], "kind": "pmic",
             "description": "Power management IC"},
            {"canonical_name": "C29", "aliases": [], "kind": "capacitor",
             "description": "Bulk cap"},
        ]
    }))
    (slug_dir / "dictionary.json").write_text(json.dumps({
        "entries": [
            {"canonical_name": "U7", "role": "PMIC", "package": "QFN-24",
             "typical_failure_modes": ["short"]},
            {"canonical_name": "C29", "role": "decoupling", "package": "0402",
             "typical_failure_modes": []},
        ]
    }))
    (slug_dir / "rules.json").write_text(json.dumps({"rules": []}))
    return tmp_path


def _session_with_parts(refdeses: list[str]) -> SessionState:
    parts = [
        Part(refdes=r, layer=Layer.TOP, is_smd=True,
             bbox=(Point(x=0, y=0), Point(x=10, y=10)),
             pin_refs=[i * 2 for i in range(4)])
        for i, r in enumerate(refdeses)
    ]
    pins = []
    for i, r in enumerate(refdeses):
        for pin_idx in range(4):
            pins.append(Pin(
                part_refdes=r, index=pin_idx + 1,
                pos=Point(x=i * 20, y=pin_idx * 5),
                net="VDD" if pin_idx == 0 else None,
                layer=Layer.TOP,
            ))
    board = Board(
        board_id="b", file_hash="sha256:x", source_format="test",
        outline=[], parts=parts, pins=pins, nets=[], nails=[],
    )
    session = SessionState()
    session.set_board(board)
    return session


def test_case1_memory_and_board_both_present(seeded_memory: Path) -> None:
    session = _session_with_parts(["U7", "C29"])
    result = mb_get_component(
        device_slug="testdev", refdes="U7",
        memory_root=seeded_memory, session=session,
    )
    assert result["found"] is True
    assert result["canonical_name"] == "U7"
    assert result["memory_bank"] is not None
    assert result["memory_bank"]["role"] == "PMIC"
    assert result["memory_bank"]["package"] == "QFN-24"
    assert result["board"] is not None
    assert result["board"]["side"] == "top"
    assert result["board"]["pin_count"] == 4


def test_case2_memory_only_no_session(seeded_memory: Path) -> None:
    """Session=None → memory_bank populated, board is None."""
    result = mb_get_component(
        device_slug="testdev", refdes="U7",
        memory_root=seeded_memory, session=None,
    )
    assert result["found"] is True
    assert result["memory_bank"] is not None
    assert result["memory_bank"]["role"] == "PMIC"
    assert result["board"] is None


def test_case2_memory_only_refdes_absent_from_board(seeded_memory: Path) -> None:
    session = _session_with_parts(["C29"])  # no U7 on the board
    result = mb_get_component(
        device_slug="testdev", refdes="U7",
        memory_root=seeded_memory, session=session,
    )
    assert result["found"] is True
    assert result["memory_bank"] is not None
    assert result["board"] is None


def test_case3_board_only_no_memory_entry(seeded_memory: Path) -> None:
    """R1 is on the board but has no registry/dictionary entry."""
    session = _session_with_parts(["U7", "R1"])
    result = mb_get_component(
        device_slug="testdev", refdes="R1",
        memory_root=seeded_memory, session=session,
    )
    assert result["found"] is True
    assert result["canonical_name"] == "R1"
    assert result["memory_bank"] is None
    assert result["board"] is not None


def test_case4_neither_source_has_refdes(seeded_memory: Path) -> None:
    session = _session_with_parts(["U7"])
    result = mb_get_component(
        device_slug="testdev", refdes="U999",
        memory_root=seeded_memory, session=session,
    )
    assert result["found"] is False
    assert "closest_matches" in result
    assert "memory_bank" not in result
    assert "board" not in result


def test_closest_matches_merges_memory_and_board(seeded_memory: Path) -> None:
    """closest_matches is the union of memory bank and board candidates."""
    session = _session_with_parts(["U7", "U12"])
    result = mb_get_component(
        device_slug="testdev", refdes="U99",
        memory_root=seeded_memory, session=session,
    )
    assert result["found"] is False
    # Both U7 (from memory) and U12 (from board) can appear as candidates.
    matches = set(result["closest_matches"])
    assert "U7" in matches


def test_no_schematic_key_ever(seeded_memory: Path) -> None:
    """schematic key is never present (api/vision/ stub)."""
    session = _session_with_parts(["U7"])
    result = mb_get_component(
        device_slug="testdev", refdes="U7",
        memory_root=seeded_memory, session=session,
    )
    assert "schematic" not in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/agent/test_mb_aggregation.py -v`
Expected: FAIL — `mb_get_component` doesn't accept `session`, returns flat form.

- [ ] **Step 3: Implement restructured `mb_get_component`**

Edit `api/agent/tools.py`, replace the whole `mb_get_component` function (keep `_load_pack` and the other `mb_*` functions untouched):

```python
def mb_get_component(
    *,
    device_slug: str,
    refdes: str,
    memory_root: Path,
    session: SessionState | None = None,
) -> dict[str, Any]:
    """Return component info, aggregated from memory bank + parsed board.

    Response shape (cf. spec §5.1, 4 presence cases):
      - case 1: {found: true, canonical_name, memory_bank: {...}, board: {...}}
      - case 2: {found: true, canonical_name, memory_bank: {...}, board: null}
      - case 3: {found: true, canonical_name, memory_bank: null, board: {...}}
      - case 4: {found: false, closest_matches: [...]}  # no memory_bank/board keys
    """
    pack = _load_pack(device_slug, memory_root)
    reg_by_name = {c["canonical_name"]: c for c in pack["registry"].get("components", [])}
    dct_by_name = {e["canonical_name"]: e for e in pack["dictionary"].get("entries", [])}

    memory_section: dict[str, Any] | None = None
    if refdes in reg_by_name:
        reg = reg_by_name[refdes]
        dct = dct_by_name.get(refdes, {})
        memory_section = {
            "role": dct.get("role"),
            "package": dct.get("package"),
            "aliases": reg.get("aliases", []),
            "kind": reg.get("kind", "unknown"),
            "typical_failure_modes": dct.get("typical_failure_modes", []),
            "description": reg.get("description", ""),
        }

    board_section: dict[str, Any] | None = None
    if session is not None and session.board is not None:
        part = session.board.part_by_refdes(refdes)
        if part is not None:
            # Collect nets connected to this part's pins.
            pin_indexes = set(part.pin_refs)
            connected_nets: list[str] = []
            for net in session.board.nets:
                if set(net.pin_refs) & pin_indexes:
                    connected_nets.append(net.name)
            side = "top" if part.layer & 1 else "bottom"
            bbox = part.bbox
            board_section = {
                "side": side,
                "pin_count": len(part.pin_refs),
                "bbox": [[bbox[0].x, bbox[0].y], [bbox[1].x, bbox[1].y]],
                "nets": connected_nets,
            }

    if memory_section is None and board_section is None:
        # Case 4: unknown on both sides. Union of candidates.
        prefix = refdes[0].upper() if refdes else ""
        mem_candidates = sorted(c for c in reg_by_name if prefix and c.startswith(prefix))
        board_candidates: list[str] = []
        if session is not None and session.board is not None:
            from api.board.validator import suggest_similar
            board_candidates = suggest_similar(session.board, refdes, k=5)
        merged = list(dict.fromkeys(mem_candidates + board_candidates))[:5]
        return {
            "found": False,
            "error": "not_found",
            "queried_refdes": refdes,
            "closest_matches": merged,
            "hint": f"No refdes {refdes!r} on device {device_slug!r}.",
        }

    return {
        "found": True,
        "canonical_name": refdes,
        "memory_bank": memory_section,
        "board": board_section,
    }
```

Add the import at the top of `api/agent/tools.py`:

```python
from api.session.state import SessionState
```

- [ ] **Step 4: Run the new aggregation tests to verify they pass**

Run: `.venv/bin/pytest tests/agent/test_mb_aggregation.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Migrate existing tests in `tests/agent/test_tools.py`**

The 3 `mb_get_component` tests there assert the flat form. Update them to the new nested form — they don't pass a `session`, so only `memory_bank` is populated.

Edit `tests/agent/test_tools.py`, replace the 3 test functions that touch `mb_get_component`:

```python
def test_mb_get_component_found(seeded_memory_root):
    result = mb_get_component(
        device_slug="demo-pi", refdes="U7", memory_root=seeded_memory_root,
    )
    assert result["found"] is True
    assert result["canonical_name"] == "U7"
    assert result["memory_bank"] is not None
    assert result["memory_bank"]["role"] == "PMIC"
    assert result["memory_bank"]["package"] == "QFN-24"
    assert result["memory_bank"]["kind"] == "pmic"
    assert result["board"] is None  # no session passed


def test_mb_get_component_not_found_suggests_closest(seeded_memory_root):
    result = mb_get_component(
        device_slug="demo-pi", refdes="U999", memory_root=seeded_memory_root,
    )
    assert result["found"] is False
    assert result["error"] == "not_found"
    assert "closest_matches" in result
    assert "U7" in result["closest_matches"]
    assert "memory_bank" not in result  # case 4: no memory_bank/board keys
    assert "board" not in result


def test_mb_get_component_empty_refdes_returns_not_found(seeded_memory_root):
    result = mb_get_component(
        device_slug="demo-pi", refdes="", memory_root=seeded_memory_root,
    )
    assert result["found"] is False
    assert result["error"] == "not_found"
```

- [ ] **Step 6: Run the migrated tests to verify they pass**

Run: `.venv/bin/pytest tests/agent/test_tools.py -v`
Expected: all PASS (migrated `mb_get_component` tests + unchanged `mb_get_rules_for_symptoms` tests).

---

## Task A4: Tool manifest + system prompt module

**Files:**
- Create: `api/agent/manifest.py`
- Create: `tests/agent/test_manifest_dynamic.py`

**Context:** Single source of truth for all tool JSON schemas (the 4 `MB_TOOLS` already defined inline in `runtime_direct.py` + 12 new `BV_TOOLS`) and the dynamic builders that produce the per-session manifest and (direct-runtime only) the system prompt. Using `is not None` patterns so schematic can slot in later without breaking the builder.

- [ ] **Step 1: Write the failing tests**

Create `tests/agent/test_manifest_dynamic.py`:

```python
"""Tests for build_tools_manifest and render_system_prompt."""

from api.agent.manifest import BV_TOOLS, MB_TOOLS, build_tools_manifest, render_system_prompt
from api.board.model import Board, Layer, Part, Point
from api.session.state import SessionState


def _session_with_board() -> SessionState:
    parts = [Part(refdes="U7", layer=Layer.TOP, is_smd=True,
                  bbox=(Point(x=0, y=0), Point(x=10, y=10)), pin_refs=[])]
    board = Board(board_id="b", file_hash="sha256:x", source_format="t",
                  outline=[], parts=parts, pins=[], nets=[], nails=[])
    s = SessionState()
    s.set_board(board)
    return s


def test_mb_tools_has_four_entries() -> None:
    assert len(MB_TOOLS) == 4
    names = {t["name"] for t in MB_TOOLS}
    assert names == {
        "mb_get_component", "mb_get_rules_for_symptoms",
        "mb_list_findings", "mb_record_finding",
    }


def test_bv_tools_has_twelve_entries() -> None:
    assert len(BV_TOOLS) == 12
    names = {t["name"] for t in BV_TOOLS}
    assert names == {
        "bv_highlight", "bv_focus", "bv_reset_view", "bv_flip",
        "bv_annotate", "bv_dim_unrelated", "bv_highlight_net",
        "bv_show_pin", "bv_draw_arrow", "bv_measure",
        "bv_filter_by_type", "bv_layer_visibility",
    }


def test_every_tool_has_name_description_input_schema() -> None:
    for tool in MB_TOOLS + BV_TOOLS:
        assert isinstance(tool["name"], str) and tool["name"]
        assert isinstance(tool["description"], str) and tool["description"]
        assert isinstance(tool["input_schema"], dict)
        assert tool["input_schema"].get("type") == "object"
        assert "properties" in tool["input_schema"]


def test_manifest_without_board_has_only_mb_tools() -> None:
    session = SessionState()  # board=None
    manifest = build_tools_manifest(session)
    names = {t["name"] for t in manifest}
    assert names == {t["name"] for t in MB_TOOLS}
    assert len(manifest) == 4


def test_manifest_with_board_adds_bv_tools() -> None:
    session = _session_with_board()
    manifest = build_tools_manifest(session)
    names = {t["name"] for t in manifest}
    assert names == {t["name"] for t in MB_TOOLS} | {t["name"] for t in BV_TOOLS}
    assert len(manifest) == 16


def test_manifest_has_no_sch_tools_regardless_of_session() -> None:
    session = _session_with_board()
    manifest = build_tools_manifest(session)
    assert not any(t["name"].startswith("sch_") for t in manifest)


def test_render_system_prompt_mentions_boardview_when_available() -> None:
    session = _session_with_board()
    prompt = render_system_prompt(session, device_slug="demo-pi")
    assert "boardview" in prompt.lower()
    assert "demo-pi" in prompt


def test_render_system_prompt_mentions_boardview_absent_when_no_board() -> None:
    session = SessionState()
    prompt = render_system_prompt(session, device_slug="demo-pi")
    # Should signal that the boardview tools are unavailable.
    assert "boardview" in prompt.lower()
    # memory bank is always available — check it's mentioned positively.
    assert "memory bank" in prompt.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/agent/test_manifest_dynamic.py -v`
Expected: FAIL — `api.agent.manifest` module doesn't exist.

- [ ] **Step 3: Implement manifest module**

Create `api/agent/manifest.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Tool manifest + system prompt builders for the diagnostic agent.

- MB_TOOLS: the always-on memory-bank family (4 tools). Was previously
  inlined in runtime_direct.py.
- BV_TOOLS: the boardview control family (12 tools), exposed only when
  a board is loaded in the session.
- build_tools_manifest(session): produces the per-session manifest
  passed to Anthropic's messages.create or the Managed Agent definition.
- render_system_prompt(session, device_slug): DIRECT-runtime only; the
  Managed-runtime prompt is carried by the agent server-side.
"""

from __future__ import annotations

from api.session.state import SessionState

MB_TOOLS: list[dict] = [
    {
        "name": "mb_get_component",
        "description": (
            "Look up a component by refdes on the current device. Returns "
            "aggregated info: {found, canonical_name, memory_bank: {...}|null, "
            "board: {...}|null} when found. For unknown refdes returns "
            "{found: false, closest_matches: [...]}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {"type": "string", "description": "e.g. U7, C29, J3100"},
            },
            "required": ["refdes"],
        },
    },
    {
        "name": "mb_get_rules_for_symptoms",
        "description": (
            "Find diagnostic rules matching a list of symptoms, ranked by "
            "symptom overlap + rule confidence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symptoms": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["symptoms"],
        },
    },
    {
        "name": "mb_list_findings",
        "description": (
            "Return prior confirmed findings (field reports) for the current "
            "device, newest first. Cross-session memory — check on open."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
                "filter_refdes": {"type": "string"},
            },
        },
    },
    {
        "name": "mb_record_finding",
        "description": (
            "Persist a confirmed repair finding so future sessions see it. "
            "Only when the technician explicitly confirms the cause."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {"type": "string"},
                "symptom": {"type": "string"},
                "confirmed_cause": {"type": "string"},
                "mechanism": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["refdes", "symptom", "confirmed_cause"],
        },
    },
]


BV_TOOLS: list[dict] = [
    {
        "name": "bv_highlight",
        "description": "Highlight one or more components on the PCB canvas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                },
                "color": {"type": "string", "enum": ["accent", "warn", "mute"], "default": "accent"},
                "additive": {"type": "boolean", "default": False},
            },
            "required": ["refdes"],
        },
    },
    {
        "name": "bv_focus",
        "description": "Pan/zoom the PCB canvas to a specific component. Auto-flips the board if the component is on the hidden side.",
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {"type": "string"},
                "zoom": {"type": "number", "default": 2.5},
            },
            "required": ["refdes"],
        },
    },
    {
        "name": "bv_reset_view",
        "description": "Reset the PCB canvas: clear all highlights, annotations, arrows, dim, filter. The technician's manual selection is preserved.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "bv_flip",
        "description": "Flip the visible PCB side (top ↔ bottom).",
        "input_schema": {
            "type": "object",
            "properties": {"preserve_cursor": {"type": "boolean", "default": False}},
        },
    },
    {
        "name": "bv_annotate",
        "description": "Attach a text label to a component on the canvas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {"type": "string"},
                "label": {"type": "string"},
            },
            "required": ["refdes", "label"],
        },
    },
    {
        "name": "bv_dim_unrelated",
        "description": "Visually dim all components not currently highlighted — focuses the technician's attention.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "bv_highlight_net",
        "description": "Highlight every pin on a given net (rail/signal tracing).",
        "input_schema": {
            "type": "object",
            "properties": {"net": {"type": "string"}},
            "required": ["net"],
        },
    },
    {
        "name": "bv_show_pin",
        "description": "Point to a specific pin of a component (e.g. for a probe instruction).",
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {"type": "string"},
                "pin": {"type": "integer", "minimum": 1},
            },
            "required": ["refdes", "pin"],
        },
    },
    {
        "name": "bv_draw_arrow",
        "description": "Draw an arrow between two components (e.g. to show a signal path).",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_refdes": {"type": "string"},
                "to_refdes": {"type": "string"},
            },
            "required": ["from_refdes", "to_refdes"],
        },
    },
    {
        "name": "bv_measure",
        "description": "Return the physical distance (mm) between two components' centers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes_a": {"type": "string"},
                "refdes_b": {"type": "string"},
            },
            "required": ["refdes_a", "refdes_b"],
        },
    },
    {
        "name": "bv_filter_by_type",
        "description": "Show only components whose refdes starts with a given prefix (e.g. 'C' for capacitors).",
        "input_schema": {
            "type": "object",
            "properties": {"prefix": {"type": "string"}},
            "required": ["prefix"],
        },
    },
    {
        "name": "bv_layer_visibility",
        "description": "Toggle visibility of a PCB layer (top or bottom).",
        "input_schema": {
            "type": "object",
            "properties": {
                "layer": {"type": "string", "enum": ["top", "bottom"]},
                "visible": {"type": "boolean"},
            },
            "required": ["layer", "visible"],
        },
    },
]


def build_tools_manifest(session: SessionState) -> list[dict]:
    """Return the tools list for `session`, exposing `bv_*` only when board is loaded."""
    manifest: list[dict] = list(MB_TOOLS)
    if session.board is not None:
        manifest.extend(BV_TOOLS)
    # Future: if session.schematic is not None: manifest.extend(SCH_TOOLS)
    return manifest


def render_system_prompt(session: SessionState, *, device_slug: str) -> str:
    """Build the system prompt for the DIRECT runtime only.

    The Managed runtime carries its prompt server-side via managed_ids.json
    and doesn't call this function.
    """
    boardview_status = "✅" if session.board is not None else "❌ (no board file loaded)"
    return f"""\
You are a calm, methodical board-level diagnostics assistant for a
microsoldering technician. Tu tutoies, en français, direct et pédagogique.

Device courant : {device_slug}.

Capabilities for this session:
  - memory bank ✅ (mb_get_component, mb_get_rules_for_symptoms,
    mb_list_findings, mb_record_finding)
  - boardview {boardview_status}
  - schematic ❌ (not yet parsed)

RÈGLE ANTI-HALLUCINATION : tu NE mentionnes JAMAIS un refdes (U7, C29,
J3100…) sans l'avoir validé via mb_get_component. Si le tool retourne
{{found: false, closest_matches: [...]}}, tu proposes une des
closest_matches ou tu demandes clarification — JAMAIS d'invention. Les
refdes non validés seront automatiquement wrapped ⟨?U999⟩ dans la
réponse finale (sanitizer post-hoc) — signal de debug, pas d'excuse.

Quand l'utilisateur décrit des symptômes, consulte d'abord mb_list_findings
(historique cross-session de ce device), puis mb_get_rules_for_symptoms.
Quand il demande un composant, appelle mb_get_component — il agrège
memory bank + board (topologie, nets connectés) en un seul appel. Si la
boardview est disponible, enchaîne bv_focus + bv_highlight pour MONTRER
le suspect au tech. Quand l'utilisateur confirme la cause, appelle
mb_record_finding pour l'archiver.
"""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/agent/test_manifest_dynamic.py -v`
Expected: PASS (8 tests).

---

## Task A5: `bv_*` dispatch module

**Files:**
- Create: `api/agent/dispatch_bv.py`
- Create: `tests/agent/test_dispatch_bv.py`

**Context:** Maps the 12 public `bv_*` tool names to their existing handlers in `api/tools/boardview.py`, and provides `dispatch_bv(session, name, payload)` with exception trapping so a runtime call never crashes the WS loop.

- [ ] **Step 1: Write the failing tests**

Create `tests/agent/test_dispatch_bv.py`:

```python
"""Tests for dispatch_bv — the bv_* tool router."""

from __future__ import annotations

import pytest

from api.agent.dispatch_bv import BV_DISPATCH, dispatch_bv
from api.board.model import Board, Layer, Part, Pin, Point
from api.session.state import SessionState


def _session_simple() -> SessionState:
    parts = [
        Part(refdes="U7", layer=Layer.TOP, is_smd=True,
             bbox=(Point(x=0, y=0), Point(x=20, y=20)), pin_refs=[0, 1]),
        Part(refdes="C29", layer=Layer.BOTTOM, is_smd=True,
             bbox=(Point(x=100, y=100), Point(x=110, y=110)), pin_refs=[2, 3]),
    ]
    pins = [
        Pin(part_refdes="U7", index=1, pos=Point(x=5, y=5), layer=Layer.TOP),
        Pin(part_refdes="U7", index=2, pos=Point(x=15, y=15), layer=Layer.TOP),
        Pin(part_refdes="C29", index=1, pos=Point(x=105, y=105), layer=Layer.BOTTOM),
        Pin(part_refdes="C29", index=2, pos=Point(x=108, y=108), layer=Layer.BOTTOM),
    ]
    board = Board(board_id="t", file_hash="sha256:x", source_format="t",
                  outline=[], parts=parts, pins=pins, nets=[], nails=[])
    s = SessionState()
    s.set_board(board)
    return s


def test_bv_dispatch_has_twelve_entries() -> None:
    assert len(BV_DISPATCH) == 12
    assert set(BV_DISPATCH.keys()) == {
        "bv_highlight", "bv_focus", "bv_reset_view", "bv_flip",
        "bv_annotate", "bv_dim_unrelated", "bv_highlight_net",
        "bv_show_pin", "bv_draw_arrow", "bv_measure",
        "bv_filter_by_type", "bv_layer_visibility",
    }


def test_dispatch_unknown_tool_returns_error() -> None:
    session = _session_simple()
    result = dispatch_bv(session, "bv_nonexistent", {})
    assert result["ok"] is False
    assert result["reason"] == "unknown-tool"


def test_dispatch_bv_highlight_known_refdes() -> None:
    session = _session_simple()
    result = dispatch_bv(session, "bv_highlight", {"refdes": "U7"})
    assert result["ok"] is True
    assert result["event"] is not None
    assert result["event"].type == "boardview.highlight"
    assert result["event"].refdes == ["U7"]


def test_dispatch_bv_highlight_unknown_refdes_no_event() -> None:
    session = _session_simple()
    result = dispatch_bv(session, "bv_highlight", {"refdes": "U999"})
    assert result["ok"] is False
    assert result["reason"] == "unknown-refdes"
    assert "event" not in result
    assert result["suggestions"]  # Levenshtein gave us something


def test_dispatch_bv_focus_auto_flip_when_layer_opposite() -> None:
    """Session layer=top, part on bottom → event.auto_flipped is True."""
    session = _session_simple()
    assert session.layer == "top"
    result = dispatch_bv(session, "bv_focus", {"refdes": "C29"})
    assert result["ok"] is True
    assert result["event"].auto_flipped is True
    assert session.layer == "bottom"


def test_dispatch_bv_reset_view_clears_agent_state() -> None:
    session = _session_simple()
    dispatch_bv(session, "bv_highlight", {"refdes": "U7"})
    assert session.highlights == {"U7"}
    result = dispatch_bv(session, "bv_reset_view", {})
    assert result["ok"] is True
    assert session.highlights == set()


def test_dispatch_bv_measure_returns_distance() -> None:
    session = _session_simple()
    result = dispatch_bv(session, "bv_measure",
                        {"refdes_a": "U7", "refdes_b": "C29"})
    assert result["ok"] is True
    assert result["event"].distance_mm > 0


def test_dispatch_bv_flip_toggles_layer() -> None:
    session = _session_simple()
    assert session.layer == "top"
    result = dispatch_bv(session, "bv_flip", {})
    assert result["ok"] is True
    assert session.layer == "bottom"
    assert result["event"].new_side == "bottom"


def test_dispatch_catches_handler_exception() -> None:
    """Malformed payload must return {ok: false, reason: handler-exception}."""
    session = _session_simple()
    # Missing required 'refdes' → TypeError from handler kwargs.
    result = dispatch_bv(session, "bv_highlight", {})
    assert result["ok"] is False
    assert result["reason"] == "handler-exception"


def test_dispatch_bv_annotate_requires_valid_refdes() -> None:
    session = _session_simple()
    result = dispatch_bv(session, "bv_annotate",
                        {"refdes": "U999", "label": "suspect"})
    assert result["ok"] is False
    assert result["reason"] == "unknown-refdes"


def test_dispatch_bv_highlight_net_unknown_net() -> None:
    session = _session_simple()
    result = dispatch_bv(session, "bv_highlight_net", {"net": "NO_SUCH_NET"})
    assert result["ok"] is False
    assert result["reason"] == "unknown-net"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/agent/test_dispatch_bv.py -v`
Expected: FAIL — `api.agent.dispatch_bv` module doesn't exist.

- [ ] **Step 3: Implement dispatch module**

Create `api/agent/dispatch_bv.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Dispatch router for the bv_* tool family.

Maps the public names (exposed to Claude in the manifest) to the existing
handlers in api/tools/boardview.py. Each handler returns a dict that may
contain {ok, summary, event, reason, suggestions}.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from api.session.state import SessionState
from api.tools import boardview as bv

logger = logging.getLogger("wrench_board.agent.dispatch_bv")


BV_DISPATCH: dict[str, Callable[..., dict[str, Any]]] = {
    "bv_highlight":        bv.highlight_component,
    "bv_focus":            bv.focus_component,
    "bv_reset_view":       bv.reset_view,
    "bv_flip":             bv.flip_board,
    "bv_annotate":         bv.annotate,
    "bv_dim_unrelated":    bv.dim_unrelated,
    "bv_highlight_net":    bv.highlight_net,
    "bv_show_pin":         bv.show_pin,
    "bv_draw_arrow":       bv.draw_arrow,
    "bv_measure":          bv.measure_distance,
    "bv_filter_by_type":   bv.filter_by_type,
    "bv_layer_visibility": bv.layer_visibility,
}


def dispatch_bv(session: SessionState, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Route a bv_* tool call to its handler. Traps any exception.

    Returns {ok: false, reason: "unknown-tool"} if the name isn't in BV_DISPATCH.
    Returns {ok: false, reason: "handler-exception", error: str(exc)} if the
    handler raises (e.g. malformed payload).
    """
    handler = BV_DISPATCH.get(name)
    if handler is None:
        return {"ok": False, "reason": "unknown-tool"}
    try:
        return handler(session, **payload)
    except Exception as exc:  # noqa: BLE001 — intentional catch-all at dispatch boundary
        logger.exception("bv_* handler %s raised", name)
        return {"ok": False, "reason": "handler-exception", "error": str(exc)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/agent/test_dispatch_bv.py -v`
Expected: PASS (11 tests).

---

## Task A6: Wire the DIRECT runtime

**Files:**
- Modify: `api/agent/runtime_direct.py`

**Context:** Replace the static module-level `TOOLS` and `SYSTEM_PROMPT_DIRECT` with per-session dynamic versions; route `bv_*` tool uses through `dispatch_bv`; emit the Pydantic `event` on the WS after each successful `bv_*` dispatch; run every outbound agent text block through `sanitize_agent_text`; build the session via `SessionState.from_device(device_slug)` at connection open.

- [ ] **Step 1: Open the file and understand the current shape**

Run: `wc -l api/agent/runtime_direct.py` (should be ~246 lines before changes).
Read the whole file; the loop at lines ~194-243 is what we modify.

- [ ] **Step 2: Rewrite `api/agent/runtime_direct.py`**

Full replacement (this is the cleanest way — the delta is touching every block):

```python
# SPDX-License-Identifier: Apache-2.0
"""Fallback diagnostic runtime using `messages.create` (no Managed Agents).

Keeps the WebSocket protocol identical to `runtime_managed`, so the frontend
doesn't care which mode is active. Activated with env var
`DIAGNOSTIC_MODE=direct`; used when the Managed Agents beta is unavailable
or when we want a lighter-weight path for local demos.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from anthropic import AsyncAnthropic
from fastapi import WebSocket, WebSocketDisconnect

from api.agent.dispatch_bv import dispatch_bv
from api.agent.manifest import build_tools_manifest, render_system_prompt
from api.agent.sanitize import sanitize_agent_text
from api.agent.tools import (
    mb_get_component,
    mb_get_rules_for_symptoms,
    mb_list_findings,
    mb_record_finding,
)
from api.config import get_settings
from api.session.state import SessionState

logger = logging.getLogger("wrench_board.agent.direct")


async def _dispatch_mb_tool(
    name: str,
    payload: dict,
    device_slug: str,
    memory_root: Path,
    client: AsyncAnthropic,
    session: SessionState,
    session_id: str | None = None,
) -> dict:
    """Run one of the mb_* memory-bank tools. Pass `session` so mb_get_component can aggregate."""
    if name == "mb_get_component":
        return mb_get_component(
            device_slug=device_slug,
            refdes=payload.get("refdes", ""),
            memory_root=memory_root,
            session=session,
        )
    if name == "mb_get_rules_for_symptoms":
        return mb_get_rules_for_symptoms(
            device_slug=device_slug,
            symptoms=payload.get("symptoms", []),
            memory_root=memory_root,
            max_results=payload.get("max_results", 5),
        )
    if name == "mb_list_findings":
        return mb_list_findings(
            device_slug=device_slug,
            memory_root=memory_root,
            limit=payload.get("limit", 20),
            filter_refdes=payload.get("filter_refdes"),
        )
    if name == "mb_record_finding":
        return await mb_record_finding(
            client=client,
            device_slug=device_slug,
            refdes=payload.get("refdes", ""),
            symptom=payload.get("symptom", ""),
            confirmed_cause=payload.get("confirmed_cause", ""),
            memory_root=memory_root,
            mechanism=payload.get("mechanism"),
            notes=payload.get("notes"),
            session_id=session_id,
        )
    return {"ok": False, "reason": "unknown-tool"}


async def run_diagnostic_session_direct(
    ws: WebSocket, device_slug: str, tier: str = "fast"
) -> None:
    """Run a direct-mode diagnostic session over `ws` for `device_slug`.

    Protocol on the wire (same as `runtime_managed`):
      - Client sends `{"type": "message", "text": "..."}`
      - Server emits `{"type": "message", "role": "assistant", "text": "..."}`,
        `{"type": "tool_use", "name": ..., "input": ...}`, and
        `{"type": "boardview.<verb>", ...}` events.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        await ws.accept()
        await ws.send_json({"type": "error", "text": "ANTHROPIC_API_KEY not set"})
        await ws.close()
        return

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    memory_root = Path(settings.memory_root)
    session = SessionState.from_device(device_slug)
    tier_to_model = {
        "fast": "claude-haiku-4-5",
        "normal": "claude-sonnet-4-6",
        "deep": "claude-opus-4-7",
    }
    model = tier_to_model.get(tier, settings.anthropic_model_main)
    await ws.accept()
    await ws.send_json({
        "type": "session_ready",
        "mode": "direct",
        "device_slug": device_slug,
        "tier": tier,
        "model": model,
        "board_loaded": session.board is not None,
    })

    system_prompt = render_system_prompt(session, device_slug=device_slug)
    tools = build_tools_manifest(session)

    messages: list[dict] = []
    try:
        while True:
            raw = await ws.receive_text()
            try:
                user_text = (json.loads(raw).get("text") or "").strip()
            except json.JSONDecodeError:
                user_text = raw.strip()
            if not user_text:
                continue

            messages.append({"role": "user", "content": user_text})
            while True:
                response = await client.messages.create(
                    model=model,
                    max_tokens=8000,
                    system=system_prompt,
                    messages=messages,
                    tools=tools,
                )

                # Emit text + tool_use blocks in the order the model produced
                # them. The frontend relies on this ordering.
                for block in response.content:
                    if block.type == "text":
                        clean, unknown = sanitize_agent_text(block.text, session.board)
                        if unknown:
                            logger.warning(
                                "sanitizer wrapped unknown refdes: %s", unknown
                            )
                        await ws.send_json(
                            {"type": "message", "role": "assistant", "text": clean}
                        )

                if response.stop_reason != "tool_use":
                    messages.append({"role": "assistant", "content": response.content})
                    break

                messages.append({"role": "assistant", "content": response.content})
                tool_results: list[dict] = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    await ws.send_json(
                        {"type": "tool_use", "name": block.name, "input": block.input}
                    )
                    # Route mb_* vs bv_*.
                    if block.name.startswith("bv_"):
                        result = dispatch_bv(session, block.name, block.input or {})
                    else:
                        result = await _dispatch_mb_tool(
                            block.name, block.input or {}, device_slug,
                            memory_root, client, session,
                        )
                    # Emit the WS event if the dispatch succeeded and produced one.
                    event = result.get("event")
                    if result.get("ok") and event is not None:
                        await ws.send_json(event.model_dump(by_alias=True))
                    # tool_result to the agent: strip `event` (visual-only) and
                    # convert any leftover Pydantic model to JSON-safe dict.
                    result_for_agent = {k: v for k, v in result.items() if k != "event"}
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result_for_agent, default=str),
                        }
                    )
                messages.append({"role": "user", "content": tool_results})
    except WebSocketDisconnect:
        logger.info("[Diag-Direct] WS closed for device=%s", device_slug)
```

- [ ] **Step 3: Run the full agent test suite**

Run: `.venv/bin/pytest tests/agent/ -v`
Expected: all previously-passing tests still PASS (Task A1-A5 tests + migrated `test_tools.py`).

- [ ] **Step 4: Smoke-test the direct runtime imports**

Run: `.venv/bin/python -c "from api.agent.runtime_direct import run_diagnostic_session_direct; print('OK')"`
Expected: output `OK`.

---

## Task A7: Wire the MANAGED runtime

**Files:**
- Modify: `api/agent/runtime_managed.py`

**Context:** Same wiring as A6, but **no** `render_system_prompt` (the MA agent carries its prompt server-side via `managed_ids.json`). The dispatch still needs to route `bv_*` → `dispatch_bv`. Events are emitted from `_forward_session_to_ws` after each successful dispatch. The tools manifest is attached to the MA at session creation time via `session_kwargs["tools"]` — cf. SDK: pass tools on session create, not per-message.

- [ ] **Step 1: Understand the current managed runtime structure**

Read `api/agent/runtime_managed.py` carefully. Key points:
- `_dispatch_tool(name, payload, ...)` at line ~45 currently only handles `mb_*`.
- `_forward_session_to_ws` at line ~232 reacts to MA events (`agent.message`, `agent.thinking`, `agent.custom_tool_use`, `session.status_idle` with `requires_action`).
- The MA dispatch is two-step: MA first emits `agent.custom_tool_use`, then pauses; when `requires_action` arrives, we look up cached events by ID and send back `user.custom_tool_result`.

- [ ] **Step 2: Modify the runtime — three changes**

**Change 1**: Update the imports at the top of `api/agent/runtime_managed.py`:

```python
from api.agent.dispatch_bv import dispatch_bv
from api.agent.manifest import build_tools_manifest
from api.agent.sanitize import sanitize_agent_text
from api.session.state import SessionState
```

**Change 2**: Update `_dispatch_tool` to route `bv_*` and accept a `session: SessionState` parameter. Replace the existing function body with:

```python
async def _dispatch_tool(
    name: str,
    payload: dict,
    device_slug: str,
    memory_root: Path,
    client: AsyncAnthropic,
    session: SessionState,
    session_id: str | None = None,
) -> dict:
    """Run a custom tool locally and return the raw result.

    Routes bv_* → dispatch_bv (synchronous), mb_* → their Python handlers.
    The returned dict may contain a Pydantic `event` field — the caller is
    responsible for emitting it on the WS and stripping it from the agent
    tool_result.
    """
    if name.startswith("bv_"):
        return dispatch_bv(session, name, payload)
    if name == "mb_get_component":
        return mb_get_component(
            device_slug=device_slug, refdes=payload.get("refdes", ""),
            memory_root=memory_root, session=session,
        )
    if name == "mb_get_rules_for_symptoms":
        return mb_get_rules_for_symptoms(
            device_slug=device_slug, symptoms=payload.get("symptoms", []),
            memory_root=memory_root, max_results=payload.get("max_results", 5),
        )
    if name == "mb_list_findings":
        return mb_list_findings(
            device_slug=device_slug, memory_root=memory_root,
            limit=payload.get("limit", 20),
            filter_refdes=payload.get("filter_refdes"),
        )
    if name == "mb_record_finding":
        return await mb_record_finding(
            client=client, device_slug=device_slug,
            refdes=payload.get("refdes", ""), symptom=payload.get("symptom", ""),
            confirmed_cause=payload.get("confirmed_cause", ""),
            memory_root=memory_root, mechanism=payload.get("mechanism"),
            notes=payload.get("notes"), session_id=session_id,
        )
    return {"ok": False, "reason": "unknown-tool", "error": f"unknown tool: {name}"}
```

**Change 3**: In `run_diagnostic_session_managed`, build a `SessionState` from the device. Insert this line right after `memory_store_id = await ensure_memory_store(client, device_slug)` (around line ~119):

```python
    session_state = SessionState.from_device(device_slug)
```

Then add `"board_loaded": session_state.board is not None` to the `session_ready` `ws.send_json` payload (around line ~161).

**IMPORTANT — tools manifest for Managed Agents is baked at agent-bootstrap time, not per-session.** The `session_kwargs` passed to `client.beta.sessions.create(...)` does NOT accept a `tools` override — the agent's tool set is fixed when `client.beta.agents.create(tools=...)` runs in `scripts/bootstrap_managed_agent.py`. Consequence: for the MA agent to see the `bv_*` tools, the bootstrap script must register all 16 tools (4 `mb_*` + 12 `bv_*`) and the MA agent versions must be re-bootstrapped. This is Task A7b below. **Do NOT add `"tools": ...` to `session_kwargs` — it will be rejected by the SDK.**

What that means for the dispatch behavior: the MA agent sees `bv_*` in its tools (once re-bootstrapped), BUT when `session.board is None` the agent can still try to call them. The dispatch in `dispatch_bv` already handles this case — the individual handlers check `_no_board(session)` and return `{ok: false, reason: "no-board-loaded"}`, which the agent reads from `user.custom_tool_result` and adjusts. Slightly worse UX than direct mode (where the manifest filters out impossible tools), but functional and safe.

**Change 4**: Pass `session_state` down into `_forward_session_to_ws` and apply the sanitizer + event emission.

Locate the task creation block and change:

```python
        emit_task = asyncio.create_task(
            _forward_session_to_ws(
                ws, client, session.id, device_slug, memory_root, events_by_id,
                session_state,
            ),
            name="session->ws",
        )
```

Then update the signature and body of `_forward_session_to_ws`:

```python
async def _forward_session_to_ws(
    ws: WebSocket,
    client: AsyncAnthropic,
    session_id: str,
    device_slug: str,
    memory_root: Path,
    events_by_id: dict[str, Any],
    session_state: SessionState,
) -> None:
    """Stream session events to the WS and dispatch custom tool calls."""
    stream_ctx = await client.beta.sessions.events.stream(session_id)
    async with stream_ctx as stream:
        async for event in stream:
            etype = getattr(event, "type", None)

            if etype == "agent.message":
                for block in getattr(event, "content", None) or []:
                    if getattr(block, "type", None) == "text":
                        clean, unknown = sanitize_agent_text(
                            block.text, session_state.board
                        )
                        if unknown:
                            logger.warning(
                                "sanitizer wrapped unknown refdes: %s", unknown
                            )
                        await ws.send_json(
                            {"type": "message", "role": "assistant", "text": clean}
                        )

            elif etype == "agent.thinking":
                text = getattr(event, "text", "") or ""
                if text:
                    await ws.send_json({"type": "thinking", "text": text})

            elif etype == "agent.custom_tool_use":
                events_by_id[event.id] = event
                await ws.send_json(
                    {
                        "type": "tool_use",
                        "name": getattr(event, "name", None),
                        "input": getattr(event, "input", {}) or {},
                    }
                )

            elif etype == "session.status_idle":
                stop = getattr(event, "stop_reason", None)
                stop_type = getattr(stop, "type", None) if stop is not None else None
                if stop_type != "requires_action":
                    continue
                event_ids = getattr(stop, "event_ids", None) or []
                for eid in event_ids:
                    tool_event = events_by_id.get(eid)
                    if tool_event is None:
                        logger.warning(
                            "[Diag-MA] requires_action for unknown event id %s", eid
                        )
                        continue
                    name = getattr(tool_event, "name", "")
                    payload = getattr(tool_event, "input", {}) or {}
                    result = await _dispatch_tool(
                        name, payload, device_slug, memory_root, client,
                        session_state, session_id,
                    )
                    # Emit the WS event if the dispatch succeeded.
                    bv_event = result.get("event")
                    if result.get("ok") and bv_event is not None:
                        await ws.send_json(bv_event.model_dump(by_alias=True))
                    result_for_agent = {k: v for k, v in result.items() if k != "event"}
                    await client.beta.sessions.events.send(
                        session_id,
                        events=[
                            {
                                "type": "user.custom_tool_result",
                                "custom_tool_use_id": eid,
                                "content": [
                                    {"type": "text", "text": json.dumps(result_for_agent, default=str)}
                                ],
                            }
                        ],
                    )

            elif etype == "session.status_terminated":
                await ws.send_json({"type": "session_terminated"})
                return

            elif etype == "session.error":
                err = getattr(event, "error", None)
                msg = getattr(err, "message", None) if err is not None else None
                await ws.send_json({"type": "error", "text": msg or "session error"})
```

- [ ] **Step 3: Smoke-test the managed runtime imports**

Run: `.venv/bin/python -c "from api.agent.runtime_managed import run_diagnostic_session_managed; print('OK')"`
Expected: output `OK`.

- [ ] **Step 4: Run the full agent test suite again**

Run: `.venv/bin/pytest tests/agent/ -v`
Expected: all tests still PASS (no test touches `runtime_managed` directly yet).

---

## Task A7b: Update MA bootstrap + re-bootstrap agents (**optional / ask Alexis first**)

**Files:**
- Modify: `scripts/bootstrap_managed_agent.py`

**Context:** The bootstrap script declares the tools that are baked into each MA agent (fast / mid / deep) at creation time. It currently registers only a subset of `mb_*` tools. To let the MA runtime use `bv_*`, the script must register all 16 tools (from `api/agent/manifest.MB_TOOLS + BV_TOOLS`) and then be re-run to produce new agent versions. Re-running writes to `managed_ids.json` and creates new agents on Anthropic's side — that's why this is guarded by Alexis's approval.

**If Alexis defers this task:** the managed runtime works as before (agent sees only `mb_*`), and `bv_*` is reachable only via `DIAGNOSTIC_MODE=direct`. The frontend + backend plumbing from Tasks A1-A7 all still lands. The commit message for A9 should mention "managed-mode bv_* exposure pending A7b" to make the state clear.

- [ ] **Step 1: Ask Alexis if A7b goes in this chantier or is deferred**

If deferred: skip to Task A8. If approved: continue.

- [ ] **Step 2: Read the current bootstrap tool list**

Run: `sed -n '70,200p' scripts/bootstrap_managed_agent.py`
Identify the `TOOLS = [...]` list and the per-tier agent creation call.

- [ ] **Step 3: Refactor the bootstrap to consume `MB_TOOLS + BV_TOOLS` from `api/agent/manifest`**

Edit `scripts/bootstrap_managed_agent.py`. Replace the hard-coded `TOOLS = [...]` with:

```python
from api.agent.manifest import BV_TOOLS, MB_TOOLS

TOOLS = MB_TOOLS + BV_TOOLS
```

Make sure this import works from the script's context (the script is in `scripts/`, the module is importable as `api.agent.manifest` — the repo root must be on `sys.path`, usually handled by running `.venv/bin/python scripts/bootstrap_managed_agent.py`).

- [ ] **Step 4: Re-run the bootstrap**

Run: `.venv/bin/python scripts/bootstrap_managed_agent.py`
Expected: the script creates new agent versions and updates `managed_ids.json`.

Caution: this is a **remote action on Anthropic's API** — not reversible except by rolling back `managed_ids.json` to the previous commit. Alexis should be watching the console output when this runs.

- [ ] **Step 5: Verify the managed runtime now sees bv_* in session-created agents**

Run: `make run`, then open the app on a device that has a board in `board_assets/`, use the agent panel, ask « highlight U1 » (or whatever exists). Confirm the canvas reacts. If the agent logs « I don't have access to a bv_highlight tool », the bootstrap didn't take — re-check the bootstrap output and `managed_ids.json`.

- [ ] **Step 6: Stage `managed_ids.json` if it changed**

If `managed_ids.json` was updated by the re-bootstrap, it can be committed as part of commit 1 OR as its own commit (« chore(agent): re-bootstrap MA with bv_* tools »). Keep it in the backend commit if the change is small; split if it's >20 lines of ID changes.

---

## Task A8: WS integration test

**Files:**
- Create: `tests/agent/test_ws_flow.py`

**Context:** The most important end-to-end test: mock `AsyncAnthropic.messages.create` to return scripted responses (one tool_use, then a final text block), walk the direct runtime, and assert on the exact WS message sequence + `tool_result` purity (no `event` key).

- [ ] **Step 1: Write the integration test**

Create `tests/agent/test_ws_flow.py`:

```python
"""End-to-end tests for the direct diagnostic runtime over a fake WebSocket."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.board.model import Board, Layer, Part, Pin, Point
from api.session.state import SessionState


class FakeWS:
    """Minimal WebSocket double that captures send_json calls."""

    def __init__(self, user_messages: list[str]) -> None:
        self.sent: list[dict] = []
        self._inbox = asyncio.Queue[str]()
        for m in user_messages:
            self._inbox.put_nowait(json.dumps({"type": "message", "text": m}))
        # Sentinel to close the loop after the inbox drains.
        self._closed = False

    async def accept(self) -> None:
        return

    async def close(self) -> None:
        self._closed = True

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive_text(self) -> str:
        if self._closed or self._inbox.empty():
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect
        return await self._inbox.get()


def _stub_session(monkeypatch: pytest.MonkeyPatch, board: Board | None) -> None:
    """Force SessionState.from_device to return a pre-built session."""
    def _from_device(_slug: str) -> SessionState:
        s = SessionState()
        if board is not None:
            s.set_board(board)
        return s
    monkeypatch.setattr(
        "api.agent.runtime_direct.SessionState.from_device",
        staticmethod(_from_device),
    )


def _board_with_u7() -> Board:
    return Board(
        board_id="t", file_hash="sha256:x", source_format="t",
        outline=[],
        parts=[Part(refdes="U7", layer=Layer.TOP, is_smd=True,
                    bbox=(Point(x=0, y=0), Point(x=10, y=10)), pin_refs=[0, 1])],
        pins=[
            Pin(part_refdes="U7", index=1, pos=Point(x=2, y=2), layer=Layer.TOP),
            Pin(part_refdes="U7", index=2, pos=Point(x=8, y=8), layer=Layer.TOP),
        ],
        nets=[], nails=[],
    )


def _mock_anthropic(responses: list[MagicMock]) -> MagicMock:
    """Build an AsyncAnthropic whose messages.create cycles through `responses`."""
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=responses)
    return client


def _text_response(text: str) -> MagicMock:
    block = MagicMock(type="text", text=text)
    return MagicMock(content=[block], stop_reason="end_turn")


def _tool_use_response(name: str, tool_input: dict, tool_id: str = "toolu_1") -> MagicMock:
    block = MagicMock(type="tool_use", name=name, input=tool_input, id=tool_id)
    return MagicMock(content=[block], stop_reason="tool_use")


@pytest.mark.asyncio
async def test_bv_highlight_emits_tool_use_then_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent calls bv_highlight(U7) → WS sees tool_use, then boardview.highlight, then final message."""
    _stub_session(monkeypatch, _board_with_u7())

    # Patch settings so the runtime doesn't refuse on missing API key.
    import api.agent.runtime_direct as rt

    fake_client = _mock_anthropic([
        _tool_use_response("bv_highlight", {"refdes": "U7"}),
        _text_response("Done."),
    ])
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda api_key: fake_client)
    monkeypatch.setattr(rt, "get_settings", lambda: MagicMock(
        anthropic_api_key="sk-fake",
        memory_root=Path("/tmp/nope"),
        anthropic_model_main="claude-opus-4-7",
    ))

    ws = FakeWS(["show U7"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    types = [m.get("type") for m in ws.sent]
    assert "session_ready" in types
    # Order inside the tool_use turn must be: tool_use → boardview.highlight
    tu_idx = types.index("tool_use")
    bv_idx = next(i for i, t in enumerate(types) if t == "boardview.highlight")
    assert tu_idx < bv_idx
    # Final assistant message is emitted.
    assert any(m.get("type") == "message" and m.get("role") == "assistant" for m in ws.sent)


@pytest.mark.asyncio
async def test_bv_highlight_unknown_emits_no_boardview_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """bv_highlight(U999) → tool_use, NO boardview.* event, final message present."""
    _stub_session(monkeypatch, _board_with_u7())
    import api.agent.runtime_direct as rt

    fake_client = _mock_anthropic([
        _tool_use_response("bv_highlight", {"refdes": "U999"}),
        _text_response("Couldn't find that one."),
    ])
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda api_key: fake_client)
    monkeypatch.setattr(rt, "get_settings", lambda: MagicMock(
        anthropic_api_key="sk-fake",
        memory_root=Path("/tmp/nope"),
        anthropic_model_main="claude-opus-4-7",
    ))

    ws = FakeWS(["show U999"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    types = [m.get("type", "") for m in ws.sent]
    assert "tool_use" in types
    assert not any(t.startswith("boardview.") for t in types)


@pytest.mark.asyncio
async def test_tool_result_never_contains_event_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core design invariant: the tool_result sent back to the agent has no 'event' key."""
    _stub_session(monkeypatch, _board_with_u7())
    import api.agent.runtime_direct as rt

    captured_messages: list[list[dict]] = []

    async def recording_create(**kwargs):
        # Capture the messages list as passed to the SDK.
        captured_messages.append(list(kwargs["messages"]))
        # First call: tool_use; second: final text.
        if len(captured_messages) == 1:
            return _tool_use_response("bv_highlight", {"refdes": "U7"})
        return _text_response("ok")

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(side_effect=recording_create)
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda api_key: fake_client)
    monkeypatch.setattr(rt, "get_settings", lambda: MagicMock(
        anthropic_api_key="sk-fake",
        memory_root=Path("/tmp/nope"),
        anthropic_model_main="claude-opus-4-7",
    ))

    ws = FakeWS(["show U7"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    # The second call's messages include the tool_result block — decode it.
    second_call_messages = captured_messages[1]
    # Find the tool_result (role=user, content list with tool_result block).
    tool_result_blocks = [
        b for m in second_call_messages
        for b in (m.get("content") if isinstance(m.get("content"), list) else [])
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert tool_result_blocks, "expected at least one tool_result block"
    decoded = json.loads(tool_result_blocks[0]["content"])
    assert "event" not in decoded
    assert decoded.get("ok") is True


@pytest.mark.asyncio
async def test_sanitizer_wraps_unknown_refdes_in_final_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent text 'U999 is suspect' gets wrapped to '⟨?U999⟩ is suspect' before WS send."""
    _stub_session(monkeypatch, _board_with_u7())
    import api.agent.runtime_direct as rt

    fake_client = _mock_anthropic([
        _text_response("U999 is suspect"),
    ])
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda api_key: fake_client)
    monkeypatch.setattr(rt, "get_settings", lambda: MagicMock(
        anthropic_api_key="sk-fake",
        memory_root=Path("/tmp/nope"),
        anthropic_model_main="claude-opus-4-7",
    ))

    ws = FakeWS(["what's wrong?"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    agent_msgs = [m for m in ws.sent if m.get("type") == "message" and m.get("role") == "assistant"]
    assert agent_msgs
    assert "⟨?U999⟩" in agent_msgs[0]["text"]
    assert "U999 is suspect" not in agent_msgs[0]["text"]  # original form erased
```

- [ ] **Step 2: Run the integration test**

Run: `.venv/bin/pytest tests/agent/test_ws_flow.py -v`
Expected: PASS (4 tests).

If any fails, inspect the `ws.sent` list to see what actually got sent; common gotchas: `MagicMock` needs explicit `type` on blocks, and `session_ready` payload includes `board_loaded` now.

---

## Task A9: Full backend lint + test run + commit 1

- [ ] **Step 1: Ruff format + lint the touched files**

Run: `make format && make lint`
Expected: no lint errors. Fix any raised.

- [ ] **Step 2: Run the whole test suite**

Run: `make test`
Expected: all tests PASS.

- [ ] **Step 3: Verify no stray changes in web/ or docs/**

Run: `git status --short`
Expected: only `api/`, `tests/` paths modified/added (no `web/`, no `docs/`).

- [ ] **Step 4: Stage the backend files explicitly and commit**

```bash
git add \
  api/agent/manifest.py \
  api/agent/dispatch_bv.py \
  api/agent/sanitize.py \
  api/agent/runtime_direct.py \
  api/agent/runtime_managed.py \
  api/agent/tools.py \
  api/session/state.py \
  tests/agent/test_sanitize.py \
  tests/agent/test_manifest_dynamic.py \
  tests/agent/test_dispatch_bv.py \
  tests/agent/test_mb_aggregation.py \
  tests/agent/test_session_from_device.py \
  tests/agent/test_tools.py \
  tests/agent/test_ws_flow.py

git commit -m "$(cat <<'EOF'
feat(agent): bv_* tools + dynamic manifest + mb_* aggregation + sanitizer

Wires the diagnostic agent to the existing PCB renderer handlers:
- Dynamic per-session tool manifest (mb_* always, bv_* when a board is
  loaded), built from a single-source-of-truth module (api/agent/manifest).
- bv_* dispatch router with exception trapping; 12 public names mapped to
  the pre-existing api/tools/boardview.py handlers.
- mb_get_component restructured (breaking) into {memory_bank, board}
  sections; existing tests migrated. Closest matches now merge memory
  bank + board candidates.
- Post-hoc refdes sanitizer wraps unknown ⟨?U999⟩ tokens in outbound
  agent text — second layer of defense behind tool discipline.
- SessionState.from_device(slug) helper auto-loads a board from
  board_assets/ (.kicad_pcb > .brd priority), returns empty session if
  anything fails so the manifest just omits bv_*.
- Both runtimes (direct + managed) rewired: manifest dynamique,
  dispatch routing, event WS emission, sanitizer. The direct runtime
  also uses render_system_prompt; the managed runtime's prompt is
  carried server-side.

Spec: docs/superpowers/specs/2026-04-23-agent-boardview-control-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- \
  api/agent/manifest.py \
  api/agent/dispatch_bv.py \
  api/agent/sanitize.py \
  api/agent/runtime_direct.py \
  api/agent/runtime_managed.py \
  api/agent/tools.py \
  api/session/state.py \
  tests/agent/test_sanitize.py \
  tests/agent/test_manifest_dynamic.py \
  tests/agent/test_dispatch_bv.py \
  tests/agent/test_mb_aggregation.py \
  tests/agent/test_session_from_device.py \
  tests/agent/test_tools.py \
  tests/agent/test_ws_flow.py
```

- [ ] **Step 5: Verify clean commit**

Run: `git log -1 --stat`
Expected: only the files listed above are in the commit; summary line matches.

---

# Group B — Frontend (ends in commit 2)

## Task B1: Early `window.Boardview` stub + pending buffer

**Files:**
- Modify: `web/js/main.js`

**Context:** Without this, events sent before `initBoardview` has run are silently dropped. Install the stub at the top of `main.js` so it's in place before any WS connection.

- [ ] **Step 1: Read the current top of `web/js/main.js`**

Run: `head -20 web/js/main.js`

- [ ] **Step 2: Insert the stub at the top of the file**

Edit `web/js/main.js` — add these lines immediately after any top-level imports and before any other executable code:

```javascript
// Early stub: collect boardview.* events in __pending until brd_viewer
// mounts and replaces this with the real implementation. Without this,
// events sent before the tech navigates to #pcb are silently lost.
if (!window.Boardview) {
  window.Boardview = {
    __pending: [],
    apply(ev) { this.__pending.push(ev); },
  };
}
```

The location is important — it must execute **before** any WS connection opens. `main.js` is the entry point loaded from `index.html`, so top-of-file is right.

- [ ] **Step 3: Quick browser check (manual)**

Run: `make run` then open `http://localhost:8000` in the browser.
Open DevTools console. Expected:
```js
window.Boardview
// → {__pending: [], apply: ƒ}
```

- [ ] **Step 4: No test — this is a 6-line DOM bootstrap, verified by subsequent tasks' browser tests**

---

## Task B2: WS listener for `boardview.*`

**Files:**
- Modify: `web/js/llm.js`

**Context:** `llm.js` currently handles `message`, `tool_use`, `thinking`, `error`, `session_ready`, `session_terminated`. Add a pre-switch branch that routes `boardview.*` events to `window.Boardview.apply(payload)` and returns early (so the chat log doesn't render them).

- [ ] **Step 1: Locate the WS message handler**

The relevant block is in `connect()`, around line 112 in `web/js/llm.js`:

```javascript
ws.addEventListener("message", ev => {
  let payload;
  try { payload = JSON.parse(ev.data); }
  catch { payload = { type: "message", role: "assistant", text: ev.data }; }

  switch (payload.type) {
    case "session_ready": { … }
    …
  }
});
```

- [ ] **Step 2: Insert the boardview routing before the switch**

Edit `web/js/llm.js`, modify the `ws.addEventListener("message", ev => {...})` callback:

```javascript
ws.addEventListener("message", ev => {
  let payload;
  try { payload = JSON.parse(ev.data); }
  catch { payload = { type: "message", role: "assistant", text: ev.data }; }

  // Boardview events are visual mutations — not chat content. Route them
  // to the renderer (or its pending buffer if the renderer hasn't mounted).
  if (typeof payload.type === "string" && payload.type.startsWith("boardview.")) {
    window.Boardview.apply(payload);
    return;
  }

  switch (payload.type) {
    case "session_ready": {
      const model = payload.model || "claude";
      const mode = payload.mode || "managed";
      el("llmModel").textContent = `${model} · ${mode}`;
      logSys(`session prête — ${mode} · ${model}`);
      break;
    }
    // ... (existing cases unchanged)
  }
});
```

- [ ] **Step 3: Manual browser sanity check**

Open the app, open DevTools, paste in the console:

```js
window.Boardview.apply({type: "boardview.highlight", refdes: ["U7"], color: "accent", additive: false});
console.log(window.Boardview.__pending);
// should show one buffered event (Boardview is still the stub)
```

Expected: `__pending` has length 1, the event we just pushed. This verifies the routing works; the stub buffers, and Task B4 will replace the stub with a real renderer that drains the buffer.

---

## Task B3: Plan the `brd_viewer.js` state split (READ + INVENTORY)

**Files:**
- Read: `web/brd_viewer.js` (no modifications in this task — pure inventory)

**Context:** Before splitting `state.selectedPart` → `state.user.selectedPart`, list every call site. The spec flags ~15 sites; losing one breaks interaction. This is a read-only task that produces the migration list to execute in Task B4.

- [ ] **Step 1: Find all references to the state fields we're renaming**

Run:
```bash
grep -n "state\.selectedPart\|state\.selectedPinIdx\|state\.highlights\|state\.annotations\|state\.arrows\|state\.net_highlight\|state\.dim_unrelated\|state\.filter_prefix" web/brd_viewer.js
```

- [ ] **Step 2: Record the inventory inline in this plan**

Write the grep output to a scratch file (you can `rm` it after the commit):

```bash
grep -n "state\.\(selectedPart\|selectedPinIdx\|highlights\|annotations\|arrows\|net_highlight\|dim_unrelated\|filter_prefix\)" web/brd_viewer.js > /tmp/brd_viewer_state_sites.txt
cat /tmp/brd_viewer_state_sites.txt
```

Expected output: ~15-25 lines. Each line is a reference to migrate in Task B4. If the count is zero, something is wrong — `brd_viewer.js` must use these fields.

- [ ] **Step 3: Keep `/tmp/brd_viewer_state_sites.txt` around for Task B4**

Don't commit it. It'll be deleted at the end of Group B.

---

## Task B4: Split user/agent state + expose `window.Boardview` + drain pending

**Files:**
- Modify: `web/brd_viewer.js`

**Context:** The big frontend piece. Three changes bundled because they all touch the same file and are mutually dependent:
1. Rename user-origin state fields to live under `state.user`.
2. Add `state.agent` with its own highlights / focused / annotations / etc.
3. Update `draw()` so both states render: stroke cyan for agent, stroke violet for user; user wins the color when a refdes is in both.
4. Expose `window.Boardview` public API at the end of the file, drain any events queued in the stub's `__pending`.

This task is long but each step is mechanical.

- [ ] **Step 1: Locate the `state` declaration**

Run: `grep -n "^const state\s*=\|^let state\s*=\|^var state\s*=\|^state\s*=" web/brd_viewer.js`

- [ ] **Step 2: Rename user-origin fields**

Edit `web/brd_viewer.js`: the `state` literal must have a `user` sub-object. Rewrite the state initializer so that all **previously-flat** user-origin fields are under `state.user`, and a new `state.agent` is introduced:

```javascript
const state = {
  board: null,
  partsSorted: null,
  partBodyBboxes: null,
  pinsByNet: null,
  netCategory: null,
  partByRefdes: null,
  // User-origin interactive state (mouse/keyboard) — previously flat.
  user: {
    selectedPart: null,
    selectedPinIdx: null,
  },
  // Agent-origin state (WS events from dispatch_bv). Independent from user.
  agent: {
    highlights: new Set(),   // set of refdes strings
    focused: null,            // refdes string or null
    dimmed: false,
    annotations: new Map(),   // id → {refdes, label}
    arrows: new Map(),        // id → {from: [x,y], to: [x,y]}
    net: null,                // highlighted net name or null
    filter: null,             // refdes prefix or null
  },
  // Global canvas state
  side: "top",
  zoom: 1,
  panX: 0,
  panY: 0,
  layerVisibility: { top: true, bottom: true },
};
```

Remove the previous inline field names (`state.selectedPart`, etc.) wherever they were declared. Adjust any pre-existing `partsSorted` / `zoom` / `panX` / `panY` declarations to match your actual file — keep them but make sure they coexist with `user`/`agent`.

- [ ] **Step 3: Rewrite every access site for the renamed fields**

Using the inventory from Task B3, do a find-and-replace across the whole file. The transformations (apply in this order, globally):

- `state.selectedPart` → `state.user.selectedPart`
- `state.selectedPinIdx` → `state.user.selectedPinIdx`

Do NOT rename:
- `state.board`, `state.partsSorted`, `state.pinsByNet`, `state.netCategory`, `state.partByRefdes`, `state.partBodyBboxes` — these are parsed board data, not user state.
- `state.zoom`, `state.panX`, `state.panY` — viewport state, touched by both user scroll and agent focus.
- `state.side`, `state.layerVisibility` — global.

Keep a working commit point at this stage by running:

```bash
.venv/bin/python -c "import http.server"  # (no-op sanity)
```

Load `http://localhost:8000/#pcb` in a browser with `make run` running. Click on a component. **Selection must still work.** If a reference got missed, you'll get `TypeError: state.selectedPart is undefined`.

- [ ] **Step 4: Update `draw()` to render the agent layer on top of user**

Find the `draw()` function (around line 397 per earlier exploration). For each part, compute a stroke color:

```javascript
function _partStroke(refdes) {
  // Precedence: user selection wins over agent highlight (tech stays in control).
  if (state.user.selectedPart && state.user.selectedPart.refdes === refdes) {
    return { color: cssVar("--violet"), width: 2.2 };
  }
  if (state.agent.focused === refdes) {
    return { color: cssVar("--cyan"), width: 2.4 };
  }
  if (state.agent.highlights.has(refdes)) {
    return { color: cssVar("--cyan"), width: 1.6 };
  }
  return null;  // no special stroke
}
```

In the draw loop for each part, replace whatever stroke logic you had for the selected part with a call to `_partStroke(part.refdes)`, and apply the stroke only if non-null. If `state.agent.dimmed === true` and the part is NOT in `state.agent.highlights` AND not `state.user.selectedPart`, reduce its `globalAlpha` to ~0.18 during its draw.

Exact diff depends on the current shape of `draw()` — read the function first, then insert the `_partStroke` helper above it and change the per-part draw block to consult it. Keep all other rendering (body fill, pin dots, etc.) unchanged.

- [ ] **Step 5: Render agent annotations and arrows**

After the parts loop in `draw()`, add a pass for annotations and arrows. Pseudocode (the exact implementation depends on your renderer's canvas API — use the same `ctx.font`, `ctx.fillText`, `ctx.beginPath()` style already in the file):

```javascript
// Agent annotations: small label above the part's bbox.
for (const [, ann] of state.agent.annotations) {
  const part = state.partByRefdes.get(ann.refdes);
  if (!part) continue;
  const [topLeft] = part.bbox;
  const [sx, sy] = milsToScreen(topLeft.x, topLeft.y, /* boardW */ null);
  ctx.fillStyle = cssVar("--cyan");
  ctx.font = "10px 'JetBrains Mono', monospace";
  ctx.fillText(ann.label, sx, sy - 6);
}

// Agent arrows: straight line from center to center with a small arrowhead.
for (const [, arr] of state.agent.arrows) {
  const [fx, fy] = milsToScreen(arr.from[0], arr.from[1], null);
  const [tx, ty] = milsToScreen(arr.to[0], arr.to[1], null);
  ctx.strokeStyle = cssVar("--violet");
  ctx.lineWidth = 1.4;
  ctx.beginPath();
  ctx.moveTo(fx, fy); ctx.lineTo(tx, ty); ctx.stroke();
  // simple arrowhead
  const ang = Math.atan2(ty - fy, tx - fx);
  ctx.beginPath();
  ctx.moveTo(tx, ty);
  ctx.lineTo(tx - 8 * Math.cos(ang - 0.4), ty - 8 * Math.sin(ang - 0.4));
  ctx.moveTo(tx, ty);
  ctx.lineTo(tx - 8 * Math.cos(ang + 0.4), ty - 8 * Math.sin(ang + 0.4));
  ctx.stroke();
}
```

- [ ] **Step 6: Expose `window.Boardview` at the end of the file + drain pending**

At the very end of `web/brd_viewer.js`, below the `window.initBoardview = initBoardview;` line, add:

```javascript
// Public API for the agent. The early stub (in web/js/main.js) may have
// buffered events in __pending — drain them after we install the real
// methods. From this point on, Boardview.apply() mutates state + redraws.
{
  const pending = (window.Boardview && window.Boardview.__pending) || [];

  const _applyHighlight = ({refdes, color = "accent", additive = false}) => {
    const list = Array.isArray(refdes) ? refdes : [refdes];
    if (!additive) state.agent.highlights.clear();
    for (const r of list) state.agent.highlights.add(r);
    requestRedraw();
  };

  const _applyFocus = ({refdes, bbox, zoom, auto_flipped}) => {
    state.agent.focused = refdes;
    state.agent.highlights = new Set([refdes]);
    if (auto_flipped) state.side = state.side === "top" ? "bottom" : "top";
    // Zoom/pan to bbox. Use the same math as fitToBoard, but scoped to the bbox.
    const [[x1, y1], [x2, y2]] = bbox;
    const cx = (x1 + x2) / 2;
    const cy = (y1 + y2) / 2;
    state.zoom = zoom || 2.5;
    // Reset pan to center on (cx, cy) — mirror whatever fitToBoard does.
    state.panX = -cx * state.zoom + (document.getElementById("brd-canvas")?.width || 800) / 2;
    state.panY = -cy * state.zoom + (document.getElementById("brd-canvas")?.height || 600) / 2;
    requestRedraw();
  };

  const _applyReset = () => {
    state.agent.highlights.clear();
    state.agent.focused = null;
    state.agent.dimmed = false;
    state.agent.annotations.clear();
    state.agent.arrows.clear();
    state.agent.net = null;
    state.agent.filter = null;
    // Preserve state.user.* and viewport.
    requestRedraw();
  };

  const _applyFlip = () => {
    state.side = state.side === "top" ? "bottom" : "top";
    requestRedraw();
  };

  const _applyAnnotate = ({refdes, label, id}) => {
    state.agent.annotations.set(id, {refdes, label});
    requestRedraw();
  };

  const _applyDimUnrelated = () => {
    state.agent.dimmed = true;
    requestRedraw();
  };

  const _applyHighlightNet = ({net}) => {
    state.agent.net = net;
    requestRedraw();
  };

  const _applyShowPin = ({refdes}) => {
    // Pulse effect: temporarily add refdes to highlights.
    state.agent.highlights.add(refdes);
    requestRedraw();
  };

  const _applyDrawArrow = ({from, to, id}) => {
    state.agent.arrows.set(id, {from, to});
    requestRedraw();
  };

  const _applyFilter = ({prefix}) => {
    state.agent.filter = prefix || null;
    requestRedraw();
  };

  const _applyMeasure = () => {
    // No persistent visual state — the tech sees the agent's text answer.
    // Extending this with a temporary overlay is a future enhancement.
  };

  const _applyLayerVisibility = ({layer, visible}) => {
    state.layerVisibility[layer] = visible;
    requestRedraw();
  };

  const _dispatch = {
    "boardview.highlight":        _applyHighlight,
    "boardview.focus":            _applyFocus,
    "boardview.reset_view":       _applyReset,
    "boardview.flip":             _applyFlip,
    "boardview.annotate":         _applyAnnotate,
    "boardview.dim_unrelated":    _applyDimUnrelated,
    "boardview.highlight_net":    _applyHighlightNet,
    "boardview.show_pin":         _applyShowPin,
    "boardview.draw_arrow":       _applyDrawArrow,
    "boardview.filter":           _applyFilter,
    "boardview.measure":          _applyMeasure,
    "boardview.layer_visibility": _applyLayerVisibility,
  };

  window.Boardview = {
    apply(ev) {
      const fn = _dispatch[ev?.type];
      if (!fn) { console.warn("[Boardview] unknown event type:", ev?.type); return; }
      try { fn(ev); }
      catch (err) { console.warn("[Boardview] apply failed:", err, ev); }
    },
    // Convenience methods (tests, debugging).
    highlight: _applyHighlight,
    focus: _applyFocus,
    reset: _applyReset,
    flip: _applyFlip,
    annotate: _applyAnnotate,
    dim_unrelated: _applyDimUnrelated,
    highlight_net: _applyHighlightNet,
    show_pin: _applyShowPin,
    draw_arrow: _applyDrawArrow,
    filter: _applyFilter,
    measure: _applyMeasure,
    layer_visibility: _applyLayerVisibility,
  };

  // Drain anything queued before we were ready.
  for (const ev of pending) {
    try { window.Boardview.apply(ev); } catch { /* ignore */ }
  }
}
```

- [ ] **Step 7: Smoke-test in a browser**

Run: `make run` in one terminal.
Open `http://localhost:8000/#pcb?device=demo-pi` (or `mnt-reform-motherboard` if `demo-pi` has no board file).

Open DevTools console and run:

```javascript
window.Boardview.highlight({refdes: "U1", color: "accent", additive: false});
```

Expected: a component gets a cyan stroke. Replace `U1` with a refdes that actually exists in your current board (check `state.partByRefdes.keys()` first).

Click on a different component — it gets a violet stroke. The cyan one stays highlighted. Both visible at once.

Run:

```javascript
window.Boardview.reset();
```

Expected: cyan highlights gone, violet selection persists.

---

## Task B5: Manual end-to-end browser validation

**Files:** none modified — this is the browser acceptance gate.

**Context:** Per memory `feedback_visual_changes_require_user_verify`, any visual change needs Alexis's eyes before commit. Automated tests don't cover rendering. Do all of the following in a browser session and get Alexis's explicit OK.

- [ ] **Step 1: Start the dev server**

Run: `make run`
Open: `http://localhost:8000/#pcb?device=mnt-reform-motherboard`

- [ ] **Step 2: Test agent → canvas pilot (happy path)**

Open the agent panel with `⌘+J` (or `Ctrl+J`). Type:

> « highlight U1 »

Expected:
- Chat log shows a `→ bv_highlight U1` line (tool_use rendered).
- The PCB canvas U1 (if it exists on this board; otherwise try a refdes you see) is surrounded by a cyan stroke.

Type: « focus U1 »

Expected: canvas pans and zooms to U1. Flip happens automatically if U1 is on the hidden side.

- [ ] **Step 3: Test user + agent state coexistence**

With U1 still highlighted by the agent, click manually on a different component (e.g. `R5`).

Expected: R5 has a violet stroke. U1 still has its cyan stroke. **Both visible simultaneously.**

- [ ] **Step 4: Test color precedence**

Type: « highlight R5 » (while R5 is still the user-selected component).

Expected: R5 shows VIOLET (user) stroke, not cyan (agent). The user color wins — spec §7.12.

- [ ] **Step 5: Test reset preserves user state**

Type: « reset view »

Expected: agent highlights (U1, R5's cyan layer) cleared. R5 stays violet (user selection preserved).

- [ ] **Step 6: Test pending buffer (the trickier one)**

Reload the app on `/#home` (NOT `#pcb`). Open the agent panel (`⌘+J`). Type:

> « highlight U1 »

The agent fires `bv_highlight` — but the PCB canvas isn't mounted. The event lands in `window.Boardview.__pending`.

Navigate to `#pcb`.

Expected: when the canvas mounts, U1 is already highlighted (buffered event drained).

- [ ] **Step 7: Test the sanitizer**

Ask the agent a question that will make it mention a refdes it hasn't (and can't) validate — e.g. ask about a made-up PMIC. The exact phrasing depends on the device's memory bank, but any refdes the model writes in free text without a tool call should show up wrapped in the chat log.

Example: « What's the role of U9999 on this board? »

Expected: the agent's response text contains `⟨?U9999⟩` (wrapped by the sanitizer), not `U9999` raw. Backend log contains `sanitizer wrapped unknown refdes: ['U9999']`.

- [ ] **Step 8: Get Alexis's visual sign-off**

Ask Alexis to confirm each of B5 steps 2-7 visually. No commit proceeds until he says OK.

---

## Task B6: Frontend commit

- [ ] **Step 1: Verify no backend / docs changes drifted in**

Run: `git status --short`
Expected: only `web/js/main.js`, `web/js/llm.js`, `web/brd_viewer.js` modified.

- [ ] **Step 2: Clean up the scratch inventory file**

```bash
rm -f /tmp/brd_viewer_state_sites.txt
```

- [ ] **Step 3: Commit with explicit paths**

```bash
git add web/js/main.js web/js/llm.js web/brd_viewer.js
git commit -m "$(cat <<'EOF'
feat(web): window.Boardview public API + agent state split

Closes the agent→canvas loop on the frontend side.

- web/js/main.js: early stub window.Boardview with __pending buffer so
  events sent before the PCB canvas mounts aren't lost (e.g. tech opens
  agent panel before navigating to #pcb).
- web/js/llm.js: pre-switch routing — any WS payload with a
  "boardview.*" type goes to window.Boardview.apply() and is NOT
  rendered in the chat log (the matching tool_use is already there).
- web/brd_viewer.js: split selectedPart/selectedPinIdx under state.user,
  add state.agent (highlights Set, focused, dimmed, annotations Map,
  arrows Map, net, filter). draw() superposes agent stroke (cyan) on
  user stroke (violet); user wins for the same refdes so the tech stays
  in control. Public window.Boardview exposes apply() + one method per
  event type; drains pending buffer on install.

Spec: docs/superpowers/specs/2026-04-23-agent-boardview-control-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- web/js/main.js web/js/llm.js web/brd_viewer.js
```

- [ ] **Step 4: Verify clean commit**

Run: `git log -1 --stat`
Expected: only the 3 web files; summary line matches.

---

# Group C — Docs (ends in commit 3)

## Task C1: Rewrite CLAUDE.md Hard Rule #5

**Files:**
- Modify: `CLAUDE.md`

**Context:** The final commit of the chantier. Only one change: replace Rule #5 with the new two-layer formulation (tool discipline + post-hoc sanitizer). Per the spec §10 diff.

- [ ] **Step 1: Locate Hard Rule #5**

Run: `grep -n "^5\." /home/alex/Documents/hackathon-microsolder/CLAUDE.md | head -3`

The rule is in the "Hard rules — NEVER violate" section, numbered 5.

- [ ] **Step 2: Replace the rule**

Edit `CLAUDE.md`. Find the current text:

```markdown
5. **No hallucinated component IDs.** Every refdes (e.g. `U7`, `C29`) the
   agent mentions must be validated against parsed board data *before* being
   shown to the user. Tools that cannot answer return structured
   null/unknown — never fake data.
```

Replace with:

```markdown
5. **No hallucinated component IDs.** Defense in depth, two layers.
   (1) Tool discipline: every refdes the agent surfaces must originate from
   a tool lookup (`mb_get_component` for memory bank + board aggregation, or
   a `bv_*` tool that cross-checks the parsed board). These tools never
   fabricate — they return `{found: false, closest_matches: [...]}` for
   unknown refdes, and the system prompt instructs the agent to pick from
   `closest_matches` or ask the user. (2) Post-hoc sanitizer: every outbound
   agent `message` text is scanned for refdes-shaped tokens (regex
   `\b[A-Z]{1,3}\d{1,4}\b`) and, when a board is loaded, validated against
   `session.board.part_by_refdes`. Unknown matches are wrapped as
   `⟨?U999⟩` in the delivered text and logged server-side.
```

No other line of `CLAUDE.md` changes.

- [ ] **Step 3: Verify the diff is minimal**

Run: `git diff -- CLAUDE.md`
Expected: the diff touches only Rule #5 (8 lines removed, ~12 added).

---

## Task C2: Docs commit

- [ ] **Step 1: Verify no code / test changes drifted**

Run: `git status --short`
Expected: only `CLAUDE.md` modified.

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: rewrite Hard Rule #5 (tool-boundary verification + post-hoc sanitizer)

The previous formulation ("validated *before* being shown") implied a
post-hoc gate that was never actually implemented. The new wording makes
the mechanism explicit: (1) tools are the single source of refdes and
return {found: false} for the unknown; (2) a real regex-based sanitizer
runs on outbound agent text and wraps unknown tokens as ⟨?U999⟩.

The rule is not weakened — its mechanics become effective for the first
time. See api/agent/sanitize.py for the implementation and
docs/superpowers/specs/2026-04-23-agent-boardview-control-design.md §2
for the full rationale.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- CLAUDE.md
```

- [ ] **Step 3: Final verification**

Run: `git log --oneline -5`
Expected: 3 new commits on top of the earlier spec commit, in the order:
1. `feat(agent): bv_* tools + dynamic manifest + mb_* aggregation + sanitizer`
2. `feat(web): window.Boardview public API + agent state split`
3. `docs: rewrite Hard Rule #5 (tool-boundary verification + post-hoc sanitizer)`

---

# Post-implementation

- [ ] **Full regression run**

Run: `make test && make lint`
Expected: all green.

- [ ] **End-to-end manual demo**

Run: `make run`. In a browser, demo the complete flow described in spec §6 (« J'ai pas d'image HDMI » → agent calls rules_for_symptoms → get_component → focus + highlight U7 → explains the probe points). This is the demo story; if any step fails, open a follow-up ticket, don't unwind the commits.

- [ ] **Ask Alexis about pushing**

Per CLAUDE.md: never `git push` without explicit authorization. Ask « tu veux que je push ? » and wait for the answer. Do NOT push on your own.
