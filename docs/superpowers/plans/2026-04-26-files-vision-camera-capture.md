<!-- SPDX-License-Identifier: Apache-2.0 -->
# Files+Vision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Donner la vision à l'agent diagnostic via deux flows complémentaires :
upload manuel d'une photo macro par le tech (Flow A) et capture caméra
agent-initiated via tool `cam_capture` (Flow B). Les deux flows convergent
vers la session MA active du `runtime_managed.py`.

**Architecture:** Frontend ↔ backend en WS exclusif (pas de nouveau registry
de sessions actives). Files API d'Anthropic pour ingérer les images, blocks
`image` natifs (vision Opus 4.7 haute-res 2576px) dans `user.message` ou
`user.custom_tool_result`. Persistance locale des bytes sous
`memory/{slug}/repairs/{repair_id}/macros/` pour replay. Nouveau préfixe
de tool `cam_*`, conditionnel sur `session.has_camera` (signalée par une
capabilities frame frontend à l'open WS).

**Tech Stack:** Python 3.11 + FastAPI + Anthropic SDK 0.97 (`client.beta.files.upload`,
`client.beta.sessions.events.send`). Frontend vanilla JS + `getUserMedia` +
`<canvas>.toBlob('image/jpeg', 0.92)`. Persistance disque sous
`memory/{slug}/repairs/{repair_id}/macros/{ts}_{source}.{ext}`.

**Spec source:** `docs/superpowers/specs/2026-04-26-files-vision-camera-capture-design.md`

---

## File structure

**Backend new files:**
- `api/agent/macros.py` (~80 LOC) — persist + path helpers
- `tests/agent/test_macros_persistence.py`
- `tests/agent/test_runtime_macro_upload.py`
- `tests/agent/test_runtime_camera_capture.py`
- `tests/agent/test_runtime_camera_timeout.py`
- `tests/agent/test_manifest_cam_conditional.py`
- `tests/agent/test_capabilities_frame.py`
- `tests/agent/test_runtime_conv_id_dispatch.py` (Phase 1 hardening)
- `tests/api/test_macros_route.py`
- `scripts/smoke_files_vision.py`

**Backend modified files:**
- `api/session/state.py` — add `has_camera`, `pending_captures` fields
- `api/agent/manifest.py` — add `CAM_TOOLS`, gate by `session.has_camera`
- `api/agent/runtime_managed.py` — handlers (capabilities, upload_macro,
  capture_response) + dispatch `cam_capture`
- `api/main.py` — add `GET /api/macros/{slug}/{repair_id}/{filename}`
- `scripts/bootstrap_managed_agent.py` — extend SYSTEM_PROMPT with VISION block
- `scripts/smoke_layered_memory.py` — extend with multi-session check

**Frontend new files:**
- `web/js/camera.js` — picker init + getUserMedia helpers

**Frontend modified files:**
- `web/index.html` — metabar picker + drag-drop overlay markup
- `web/js/main.js` — boot `initCameraPicker()`
- `web/js/llm.js` — upload button, drag-drop, capabilities frame, capture
  handler, image bubble render, replay
- `web/styles/llm.css` — upload btn + drag-drop overlay + image bubble styles

**Test asset (manual creation):**
- `tests/fixtures/macro_devboard_test.png` — clean-room photo of an
  Arduino/RPi/dev board (NOT proprietary hardware — see Hard rule #4).
  Listed as TODO in Task V1; creation is manual.

---

## Phase 1 — Hardening (precondition to Files+Vision)

### Task H1: Smoke E2E multi-session for scribe pattern

**Files:**
- Modify: `scripts/smoke_layered_memory.py`

**Goal:** Validate that the scribe pattern (`state.md` + `decisions/` written
to the repair mount) actually survives across sessions on the same `repair_id`.

- [ ] **Step 1: Read the current smoke script to understand its shape**

Run: `wc -l scripts/smoke_layered_memory.py && head -30 scripts/smoke_layered_memory.py`
Expected: ~172 LOC, opens one session, sends one kickoff. We extend it.

- [ ] **Step 2: Refactor the existing single-session flow into a helper**

Wrap the current logic into `async def run_session(client, ids, repair_id, kickoff: str) -> dict`
that returns a dict `{full_text, tool_uses, hit_playbooks, hit_fs_tool}`. Move
the existing assertions out of the helper — the helper just streams + collects.

- [ ] **Step 3: Add a Session 1 kickoff that explicitly asks the agent to write state.md**

```python
SESSION_1_KICKOFF = (
    "Salut. iphone-x sur le banc, plainte: ne s'allume pas. "
    "Avant de partir, écris ton état actuel dans /mnt/memory/microsolder-repair-smoke-R1/state.md "
    "(symptôme initial, hypothèse en cours, prochaine action) et un fichier decisions/initial.md "
    "résumant ta première décision. Réponds-moi juste 'OK noté' à la fin."
)
```

- [ ] **Step 4: Add a Session 2 on the SAME repair_id**

After Session 1 completes, close the stream, then open a new session with the
same `repair_id="smoke-R1"` and a fresh `agent.id` reference (same agent, new session):

```python
SESSION_2_KICKOFF = (
    "Re-bonjour. On reprend la repair en cours. "
    "Lis /mnt/memory/microsolder-repair-smoke-R1/state.md et raconte-moi ce que tu trouves "
    "(quelle hypothèse, quelle prochaine action). Cite explicitement le contenu du fichier."
)
```

- [ ] **Step 5: Assert that Session 2 references content from Session 1**

```python
assert "iphone" in result_2["full_text"].lower() or "ne s'allume pas" in result_2["full_text"].lower(), (
    f"Session 2 did not surface content from state.md. Got: {result_2['full_text'][:500]}"
)
assert any(t in {"read", "grep", "ls", "cat"} for t in result_2["tool_uses"]), (
    f"Session 2 did not use any filesystem tool. Tool uses: {result_2['tool_uses']}"
)
```

- [ ] **Step 6: Run the smoke locally**

Run: `.venv/bin/python scripts/smoke_layered_memory.py`
Expected: Session 1 ends with the agent confirming write + tool_use of `write` or `edit` ;
Session 2 streams content matching the assertions ; final `✅ PASS` line.

If Session 2 doesn't actually grep the mount, the scribe pattern is broken —
fix prompt or runtime, don't lower assertions.

- [ ] **Step 7: Commit**

```bash
git add scripts/smoke_layered_memory.py
git commit -m "$(cat <<'EOF'
test(scribe): smoke E2E multi-session validates state.md + grep on resume

Extends smoke_layered_memory with a 2-session flow on the same repair_id.
Session 1 writes state.md + decisions/initial.md ; Session 2 must grep the
mount and surface content from Session 1. Closes the validation gap on the
4-layer MA memory pattern landed in the previous session.

Costs ~3-5¢ per run (one Haiku-tier session × 2).
EOF
)" -- scripts/smoke_layered_memory.py
```

---

### Task H2: Anti-regression test for `conv_id` in `_forward_session_to_ws`

**Files:**
- Create: `tests/agent/test_runtime_conv_id_dispatch.py`

**Goal:** Lock the fix from `6bd6628` (NameError on `resolved_conv_id`
referenced in `_forward_session_to_ws` when dispatching a `bv_*` tool).

- [ ] **Step 1: Locate the closure that was broken**

Run: `grep -n "save_board_state\|_forward_session_to_ws" api/agent/runtime_managed.py | head -10`
Note the line where `save_board_state(..., conv_id=conv_id, ...)` is called inside `_forward_session_to_ws`.

- [ ] **Step 2: Write the failing test**

```python
# SPDX-License-Identifier: Apache-2.0
"""Anti-regression: conv_id in _forward_session_to_ws board_state save.

Original bug (6bd6628): the inner closure referenced `resolved_conv_id`,
a name from the parent _forward_ws_to_session scope that wasn't visible
inside _forward_session_to_ws. Any bv_* tool fire raised NameError.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.agent import runtime_managed
from api.session.state import SessionState


@pytest.mark.asyncio
async def test_forward_session_to_ws_passes_conv_id_to_save_board_state(monkeypatch, tmp_path):
    """When a bv_* tool fires inside _forward_session_to_ws, save_board_state
    must receive conv_id as a keyword arg with the right value."""
    save_calls: list[dict] = []

    def fake_save_board_state(memory_root, slug, repair_id, view, *, conv_id):
        save_calls.append({
            "memory_root": memory_root, "slug": slug, "repair_id": repair_id,
            "conv_id": conv_id,
        })

    monkeypatch.setattr(runtime_managed, "save_board_state", fake_save_board_state)

    # Build minimal session + ws stubs
    session = SessionState()
    ws = AsyncMock()
    ws.send_json = AsyncMock()

    # Synthesize a bv_* tool dispatch path. The exact entrypoint depends on
    # how _forward_session_to_ws calls into save_board_state ; the assertion
    # is positional/structural, not whitebox-deep.
    # Drive the function through one tick that should trigger save_board_state.
    # NOTE: this test asserts the contract, not the entire MA loop. If the
    # bv dispatch path moves, update the entrypoint but keep the assertion.
    from api.agent.dispatch_bv import dispatch_bv

    result = await dispatch_bv(
        session=session,
        ws=ws,
        memory_root=tmp_path,
        device_slug="test-device",
        repair_id="r1",
        conv_id="conv-xyz",
        name="bv_reset_view",
        input_={},
    )
    assert result is not None
    assert any(call["conv_id"] == "conv-xyz" for call in save_calls), (
        f"save_board_state never received conv_id='conv-xyz'. Calls: {save_calls}"
    )
```

- [ ] **Step 3: Run it to verify scope (it might pass already since the fix is in)**

Run: `.venv/bin/pytest tests/agent/test_runtime_conv_id_dispatch.py -v`
Expected outcomes :
- PASS → the fix from `6bd6628` is verified ; we now have a regression lock.
- FAIL with NameError → the fix didn't take ; investigate before proceeding.
- FAIL with import error / signature mismatch → adjust the entrypoint
  (`dispatch_bv` is in `api/agent/dispatch_bv.py` ; check actual signature).

- [ ] **Step 4: If signature mismatch, adapt the test to the real dispatch path**

Run: `grep -n "^async def dispatch_bv\|^def dispatch_bv" api/agent/dispatch_bv.py`
Adjust call site in the test to match the actual signature. The KEY assertion
remains: `save_board_state` must be called with `conv_id="conv-xyz"`.

- [ ] **Step 5: Commit**

```bash
git add tests/agent/test_runtime_conv_id_dispatch.py
git commit -m "$(cat <<'EOF'
test(agent): lock conv_id in _forward_session_to_ws board_state save

Anti-regression for the NameError fixed in 6bd6628 (resolved_conv_id was
referenced inside a nested closure that didn't have it in scope, crashing
on every bv_* tool fire). Asserts save_board_state is called with the
correct conv_id keyword.
EOF
)" -- tests/agent/test_runtime_conv_id_dispatch.py
```

---

### Task H3: Audit `resolved_conv_id` closure scopes

**Files:** (audit only, may produce no edit)

- [ ] **Step 1: Grep all references**

Run: `grep -n "resolved_conv_id" api/`
Capture the output. Each hit is a candidate for inspection.

- [ ] **Step 2: For each hit, classify by scope**

For each match, determine :
- (A) The variable is **defined** in this scope (`resolved_conv_id = ...` or fn arg)
- (B) The variable is **read** in this scope, AND the enclosing scope defines it (closure)
- (C) The variable is **read** but not defined anywhere reachable → bug

Document classification inline in a scratch note. Targets that fall in (C)
are the bugs.

- [ ] **Step 3: Fix any (C) match**

If none → audit clean, move on. If at least one → fix by either (a) passing
the value as a function argument, or (b) renaming to the locally-defined name.
Pattern matches the `6bd6628` fix.

- [ ] **Step 4: Run the full agent test suite to make sure nothing regresses**

Run: `.venv/bin/pytest tests/agent/ -v -m "not slow"`
Expected: all green.

- [ ] **Step 5: Commit (only if a real bug was found and fixed)**

If audit clean, no commit. If fix applied :

```bash
git add api/agent/<modified-file>.py tests/agent/<test-if-added>.py
git commit -m "fix(agent): resolved_conv_id NameError in <function> closure"
```

If no fix, document the audit result in the next phase's commit message
("audit clean"), no separate commit.

---

## Phase 2 — Backend Files+Vision

### Task B1: Macros persistence module + tests

**Files:**
- Create: `api/agent/macros.py`
- Create: `tests/agent/test_macros_persistence.py`

**Goal:** Encapsulate the disk-persistence helpers used by both Flow A and
Flow B handlers. Pure functions, no I/O outside the macros directory.

- [ ] **Step 1: Write the failing tests**

```python
# SPDX-License-Identifier: Apache-2.0
"""Persistence helpers for macro images (Flow A + Flow B)."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from api.agent.macros import (
    persist_macro,
    macro_path_for,
    build_image_ref,
)


def test_persist_macro_writes_jpeg(tmp_path: Path):
    bytes_data = b"\xff\xd8\xff\xe0fake_jpeg_payload"
    path = persist_macro(
        memory_root=tmp_path,
        slug="iphone-x",
        repair_id="R1",
        source="manual",
        bytes_=bytes_data,
        mime="image/jpeg",
    )
    assert path.exists()
    assert path.suffix == ".jpg"
    assert path.read_bytes() == bytes_data
    assert path.parent.name == "macros"
    assert "_manual." in path.name


def test_persist_macro_png_extension_from_mime(tmp_path: Path):
    path = persist_macro(
        memory_root=tmp_path, slug="iphone-x", repair_id="R1",
        source="capture", bytes_=b"\x89PNG\r\n\x1a\n", mime="image/png",
    )
    assert path.suffix == ".png"
    assert "_capture." in path.name


def test_persist_macro_rejects_unknown_mime(tmp_path: Path):
    with pytest.raises(ValueError, match="unsupported mime"):
        persist_macro(
            memory_root=tmp_path, slug="iphone-x", repair_id="R1",
            source="manual", bytes_=b"x", mime="application/pdf",
        )


def test_persist_macro_rejects_invalid_source(tmp_path: Path):
    with pytest.raises(ValueError, match="source"):
        persist_macro(
            memory_root=tmp_path, slug="iphone-x", repair_id="R1",
            source="foo", bytes_=b"x", mime="image/png",
        )


def test_macro_path_for_resolves_under_macros_dir(tmp_path: Path):
    path = macro_path_for(
        memory_root=tmp_path, slug="iphone-x", repair_id="R1",
        filename="1745704812_manual.png",
    )
    assert path == tmp_path / "iphone-x" / "repairs" / "R1" / "macros" / "1745704812_manual.png"


def test_macro_path_for_blocks_path_traversal(tmp_path: Path):
    with pytest.raises(ValueError, match="invalid filename"):
        macro_path_for(
            memory_root=tmp_path, slug="iphone-x", repair_id="R1",
            filename="../../etc/passwd",
        )
    with pytest.raises(ValueError, match="invalid filename"):
        macro_path_for(
            memory_root=tmp_path, slug="iphone-x", repair_id="R1",
            filename="/etc/passwd",
        )


def test_build_image_ref_shape():
    ref = build_image_ref(
        path=Path("/tmp/memory/iphone-x/repairs/R1/macros/1745704812_manual.png"),
        memory_root=Path("/tmp/memory"),
        slug="iphone-x",
        repair_id="R1",
        source="manual",
    )
    assert ref == {
        "type": "image_ref",
        "path": "macros/1745704812_manual.png",
        "source": "manual",
    }
```

- [ ] **Step 2: Run to verify they fail (module not found)**

Run: `.venv/bin/pytest tests/agent/test_macros_persistence.py -v`
Expected: ImportError on `api.agent.macros`.

- [ ] **Step 3: Implement `api/agent/macros.py`**

```python
# SPDX-License-Identifier: Apache-2.0
"""Persistence helpers for macro images.

Macros land under memory/{slug}/repairs/{repair_id}/macros/{ts}_{source}.{ext}.
Two sources :
  - 'manual' : tech drag-dropped or uploaded via the chat panel (Flow A)
  - 'capture' : agent called cam_capture, frontend snapped via getUserMedia (Flow B)

The path layout is mirrored on the frontend's replay route
(GET /api/macros/{slug}/{repair_id}/{filename}).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal

Source = Literal["manual", "capture"]

_EXT_FROM_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def persist_macro(
    *,
    memory_root: Path,
    slug: str,
    repair_id: str,
    source: str,
    bytes_: bytes,
    mime: str,
) -> Path:
    """Write `bytes_` under macros/{ts}_{source}.{ext}, return the absolute path.

    Creates the macros directory if missing. Raises ValueError on unknown
    mime or invalid source.
    """
    if source not in ("manual", "capture"):
        raise ValueError(f"source must be 'manual' or 'capture', got {source!r}")
    ext = _EXT_FROM_MIME.get(mime.lower())
    if ext is None:
        raise ValueError(f"unsupported mime: {mime!r}")
    macros_dir = memory_root / slug / "repairs" / repair_id / "macros"
    macros_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    filename = f"{ts}_{source}{ext}"
    path = macros_dir / filename
    # If two captures arrive in the same second, suffix-disambiguate.
    counter = 1
    while path.exists():
        path = macros_dir / f"{ts}_{source}_{counter}{ext}"
        counter += 1
    path.write_bytes(bytes_)
    return path


def macro_path_for(
    *,
    memory_root: Path,
    slug: str,
    repair_id: str,
    filename: str,
) -> Path:
    """Resolve a stored macro path safely. Blocks path traversal."""
    if "/" in filename or "\\" in filename or filename.startswith(".") or ".." in filename:
        raise ValueError(f"invalid filename: {filename!r}")
    macros_dir = memory_root / slug / "repairs" / repair_id / "macros"
    candidate = macros_dir / filename
    # Defense in depth: resolve and assert containment.
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"invalid filename: {filename!r}") from exc
    macros_resolved = macros_dir.resolve(strict=False)
    if not str(resolved).startswith(str(macros_resolved)):
        raise ValueError(f"invalid filename: {filename!r}")
    return candidate


def build_image_ref(
    *,
    path: Path,
    memory_root: Path,
    slug: str,
    repair_id: str,
    source: Source,
) -> dict:
    """Build the image_ref dict that lands in messages.jsonl chat history.

    The frontend resolves `path` (relative to memory/{slug}/repairs/{repair_id}/)
    via GET /api/macros/{slug}/{repair_id}/{filename} on replay.
    """
    repair_root = memory_root / slug / "repairs" / repair_id
    relative = path.relative_to(repair_root)
    return {
        "type": "image_ref",
        "path": str(relative),
        "source": source,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/agent/test_macros_persistence.py -v`
Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add api/agent/macros.py tests/agent/test_macros_persistence.py
git commit -m "$(cat <<'EOF'
feat(agent): macros persistence module for Files+Vision

Encapsulates the disk-persistence helpers shared by Flow A (tech upload)
and Flow B (agent cam_capture). Writes under memory/{slug}/repairs/
{repair_id}/macros/{ts}_{source}.{ext}, blocks path traversal in the
replay-side resolver, and emits image_ref dicts for messages.jsonl.

Pure functions, no I/O outside the macros dir, no Anthropic call.
EOF
)" -- api/agent/macros.py tests/agent/test_macros_persistence.py
```

---

### Task B2: SessionState extensions

**Files:**
- Modify: `api/session/state.py` (add fields)

**Goal:** Two new fields on `SessionState` :
- `has_camera: bool = False` — set from `client.capabilities` frame
- `pending_captures: dict[str, asyncio.Future]` — Flow B Future tracker

- [ ] **Step 1: Add the import for asyncio.Future at the top of state.py**

Find the existing imports block. Add :
```python
import asyncio  # already may be present — check first
```

If `import asyncio` is missing, add it next to the other stdlib imports.

- [ ] **Step 2: Add the two fields inside the `@dataclass` body**

Locate the `class SessionState:` block (around line 35). After the
`schematic_graph_cache` field (around line 71), add :

```python
    # Files+Vision: capability flag from the frontend's client.capabilities
    # frame at WS open. Default False — `cam_capture` is gated off until set.
    has_camera: bool = False
    # Files+Vision Flow B: per-request capture Futures, keyed by request_id.
    # Resolved when the frontend posts back client.capture_response.
    pending_captures: dict[str, asyncio.Future] = field(default_factory=dict)
```

- [ ] **Step 3: Verify no existing test breaks**

Run: `.venv/bin/pytest tests/session/ -v`
Expected: all green (the new fields default to neutral values).

- [ ] **Step 4: Commit**

```bash
git add api/session/state.py
git commit -m "$(cat <<'EOF'
feat(session): add has_camera + pending_captures for Files+Vision

has_camera is set from the frontend's client.capabilities frame at WS open
and gates the cam_capture tool in the manifest. pending_captures tracks
asyncio Futures per request_id for the Flow B round-trip (server pushes
capture_request, awaits client.capture_response).
EOF
)" -- api/session/state.py
```

---

### Task B3: `cam_capture` tool in manifest, conditional gating

**Files:**
- Modify: `api/agent/manifest.py`
- Create: `tests/agent/test_manifest_cam_conditional.py`

- [ ] **Step 1: Write the failing test**

```python
# SPDX-License-Identifier: Apache-2.0
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/agent/test_manifest_cam_conditional.py -v`
Expected: 2 FAIL (the assertions on `cam_capture` present), 1 PASS (the absent case
since the tool doesn't exist yet).

- [ ] **Step 3: Add the CAM_TOOLS list and gate in manifest.py**

In `api/agent/manifest.py`, after `BV_TOOLS = [...]` and before
`PROFILE_TOOLS`, add :

```python
CAM_TOOLS: list[dict] = [
    {
        "type": "custom",
        "name": "cam_capture",
        "description": (
            "Acquire a still frame from the technician's selected camera "
            "(microscope, webcam, etc.). Use when you need a fresh visual "
            "on a specific component or anomaly. The tech has already "
            "framed and focused — no parameters needed beyond an optional "
            "reason for traceability. Returns the captured image as a "
            "tool_result the model can read directly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Brief reason for the capture (logged, not shown to the tech).",
                }
            },
            "additionalProperties": False,
        },
    },
]
```

Then modify `build_tools_manifest` :

```python
def build_tools_manifest(session: SessionState) -> list[dict]:
    """Return the tools list for `session`. `profile_*` and `protocol_*` always
    present; `bv_*` only when a board is loaded; `cam_*` only when the
    frontend reported a camera available."""
    manifest: list[dict] = list(MB_TOOLS) + list(PROFILE_TOOLS) + list(PROTOCOL_TOOLS)
    if session.board is not None:
        manifest.extend(BV_TOOLS)
    if session.has_camera:
        manifest.extend(CAM_TOOLS)
    return manifest
```

- [ ] **Step 4: Run tests to verify pass**

Run: `.venv/bin/pytest tests/agent/test_manifest_cam_conditional.py tests/agent/ -v -m "not slow"`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add api/agent/manifest.py tests/agent/test_manifest_cam_conditional.py
git commit -m "$(cat <<'EOF'
feat(agent): cam_capture tool, conditional on session.has_camera

New cam_* tool family. cam_capture is exposed in the manifest only when
the frontend has signalled camera_available via client.capabilities.
Independent of board presence (mirrors the bv_* gating pattern but on a
different signal). Triggers Flow B (server-pushed capture_request →
client.capture_response → user.custom_tool_result with image block).
EOF
)" -- api/agent/manifest.py tests/agent/test_manifest_cam_conditional.py
```

---

### Task B4: WS handler — `client.capabilities` frame

**Files:**
- Modify: `api/agent/runtime_managed.py` (extend `_forward_ws_to_session` or sibling)
- Create: `tests/agent/test_capabilities_frame.py`

**Goal:** When the frontend sends `{type: "client.capabilities",
camera_available: bool}`, set `session.has_camera`. Same handler shape on
both runtime_managed AND runtime_direct (do both, lest we get a mismatch
between modes).

- [ ] **Step 1: Locate the WS receive loop**

Run: `grep -n "_forward_ws_to_session\|websocket.receive_json\|ws.receive_json\|receive_text" api/agent/runtime_managed.py | head`
This reveals where incoming WS frames are dispatched. The handler is in
`_forward_ws_to_session` around line 1572 (read 1572-1670 to see the
existing dispatch shape).

- [ ] **Step 2: Write the failing test**

```python
# SPDX-License-Identifier: Apache-2.0
"""client.capabilities frame must update session.has_camera."""

from __future__ import annotations

import pytest

from api.agent.runtime_managed import _handle_client_capabilities
from api.session.state import SessionState


def test_capabilities_sets_has_camera_true():
    session = SessionState()
    assert session.has_camera is False
    _handle_client_capabilities(session, {"type": "client.capabilities", "camera_available": True})
    assert session.has_camera is True


def test_capabilities_sets_has_camera_false():
    session = SessionState()
    session.has_camera = True
    _handle_client_capabilities(session, {"type": "client.capabilities", "camera_available": False})
    assert session.has_camera is False


def test_capabilities_missing_camera_field_defaults_false():
    session = SessionState()
    session.has_camera = True
    _handle_client_capabilities(session, {"type": "client.capabilities"})
    assert session.has_camera is False


def test_capabilities_non_bool_camera_field_coerces_safely():
    session = SessionState()
    _handle_client_capabilities(session, {"type": "client.capabilities", "camera_available": "yes"})
    # truthy non-bool → True (we coerce via bool())
    assert session.has_camera is True
    _handle_client_capabilities(session, {"type": "client.capabilities", "camera_available": None})
    assert session.has_camera is False
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/pytest tests/agent/test_capabilities_frame.py -v`
Expected: ImportError on `_handle_client_capabilities`.

- [ ] **Step 4: Add the handler in `api/agent/runtime_managed.py`**

Near the other top-level helpers (above `_dispatch_tool` is fine), add :

```python
def _handle_client_capabilities(session: SessionState, frame: dict) -> None:
    """Update session capability flags from a client.capabilities frame.

    Idempotent ; can be sent multiple times during the WS session if the
    frontend's device list changes.
    """
    session.has_camera = bool(frame.get("camera_available"))
```

Then in `_forward_ws_to_session`, find the dispatch chain (the `if/elif`
branches on incoming frame type — they're keyed on a `"type"` field
typically). Add a branch :

```python
elif frame_type == "client.capabilities":
    _handle_client_capabilities(session, frame)
    continue  # capability frame consumed, no MA forwarding
```

If the existing dispatch uses a different style (dict lookup, match
statement), adapt to that style — keep the new branch in sync with the
prevailing pattern.

- [ ] **Step 5: Run all tests to verify no regression**

Run: `.venv/bin/pytest tests/agent/test_capabilities_frame.py tests/agent/ -v -m "not slow"`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add api/agent/runtime_managed.py tests/agent/test_capabilities_frame.py
git commit -m "$(cat <<'EOF'
feat(agent): handle client.capabilities frame to set session.has_camera

Frontend signals camera availability at WS open (and on device changes)
via a client.capabilities frame. The handler updates session.has_camera,
which gates cam_capture exposure in the per-tour tool manifest.
EOF
)" -- api/agent/runtime_managed.py tests/agent/test_capabilities_frame.py
```

---

### Task B5: WS handler — `client.upload_macro` (Flow A)

**Files:**
- Modify: `api/agent/runtime_managed.py` (new handler `_handle_client_upload_macro`)
- Create: `tests/agent/test_runtime_macro_upload.py`

**Goal:** When the frontend sends `{type: "client.upload_macro",
base64, mime, filename}`, decode → persist → upload to Files API → inject
`user.message` into the live MA session.

- [ ] **Step 1: Write the failing test**

```python
# SPDX-License-Identifier: Apache-2.0
"""Flow A handler: client.upload_macro injects user.message into MA session."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.agent.runtime_managed import _handle_client_upload_macro
from api.session.state import SessionState


@pytest.mark.asyncio
async def test_upload_macro_persists_and_injects_user_message(tmp_path: Path):
    session = SessionState()
    bytes_data = b"\xff\xd8\xff\xe0fake_jpeg"
    frame = {
        "type": "client.upload_macro",
        "base64": base64.b64encode(bytes_data).decode("ascii"),
        "mime": "image/jpeg",
        "filename": "macro_001.jpg",
    }

    fake_file = MagicMock(id="file_abc123")
    client = MagicMock()
    client.beta.files.upload = AsyncMock(return_value=fake_file)
    client.beta.sessions.events.send = AsyncMock()

    await _handle_client_upload_macro(
        client=client,
        session=session,
        memory_root=tmp_path,
        slug="iphone-x",
        repair_id="R1",
        ma_session_id="sesn_xyz",
        frame=frame,
    )

    # Persisted to disk
    macros_dir = tmp_path / "iphone-x" / "repairs" / "R1" / "macros"
    files = list(macros_dir.glob("*_manual.jpg"))
    assert len(files) == 1
    assert files[0].read_bytes() == bytes_data

    # Files API was called with the right payload
    client.beta.files.upload.assert_awaited_once()
    upload_kwargs = client.beta.files.upload.call_args.kwargs
    assert upload_kwargs.get("purpose") == "agent"

    # MA session received the user.message with image block
    client.beta.sessions.events.send.assert_awaited_once()
    send_call = client.beta.sessions.events.send.call_args
    events = send_call.kwargs.get("events") or send_call.args[1].get("events")
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "user.message"
    image_blocks = [b for b in event["content"] if b.get("type") == "image"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["source"] == {"type": "file", "file_id": "file_abc123"}


@pytest.mark.asyncio
async def test_upload_macro_rejects_oversized_payload(tmp_path: Path):
    session = SessionState()
    # 10 MB of bytes → exceeds the 5 MB cap
    big_bytes = b"\x00" * (10 * 1024 * 1024)
    frame = {
        "type": "client.upload_macro",
        "base64": base64.b64encode(big_bytes).decode("ascii"),
        "mime": "image/png",
        "filename": "huge.png",
    }
    client = MagicMock()
    client.beta.files.upload = AsyncMock()
    client.beta.sessions.events.send = AsyncMock()

    with pytest.raises(ValueError, match="too large"):
        await _handle_client_upload_macro(
            client=client, session=session, memory_root=tmp_path,
            slug="iphone-x", repair_id="R1", ma_session_id="sesn_xyz", frame=frame,
        )
    client.beta.files.upload.assert_not_awaited()
    client.beta.sessions.events.send.assert_not_awaited()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/agent/test_runtime_macro_upload.py -v`
Expected: ImportError on `_handle_client_upload_macro`.

- [ ] **Step 3: Implement the handler in `api/agent/runtime_managed.py`**

Near `_handle_client_capabilities`, add :

```python
import base64 as _b64

from api.agent.macros import persist_macro

_MAX_MACRO_BYTES = 5 * 1024 * 1024  # 5 MB raw, post-decode


async def _handle_client_upload_macro(
    *,
    client,
    session: SessionState,
    memory_root: Path,
    slug: str,
    repair_id: str,
    ma_session_id: str,
    frame: dict,
) -> None:
    """Flow A : tech-uploaded photo → persist → Files API → inject user.message.

    Raises ValueError on payload too large or invalid base64. The caller
    should catch and surface to the frontend, not crash the whole loop.
    """
    b64 = frame.get("base64") or ""
    mime = (frame.get("mime") or "").lower()
    filename = frame.get("filename") or "macro.png"

    try:
        bytes_ = _b64.b64decode(b64, validate=True)
    except Exception as exc:
        raise ValueError(f"invalid base64 payload: {exc}") from exc

    if len(bytes_) > _MAX_MACRO_BYTES:
        raise ValueError(
            f"macro upload too large: {len(bytes_)} bytes > {_MAX_MACRO_BYTES} cap"
        )

    path = persist_macro(
        memory_root=memory_root, slug=slug, repair_id=repair_id,
        source="manual", bytes_=bytes_, mime=mime,
    )

    uploaded = await client.beta.files.upload(
        file=(filename, bytes_, mime),
        purpose="agent",
    )

    await client.beta.sessions.events.send(
        session_id=ma_session_id,
        events=[{
            "type": "user.message",
            "content": [
                {"type": "image", "source": {"type": "file", "file_id": uploaded.id}},
                {"type": "text", "text": "Photo macro envoyée par le tech."},
            ],
        }],
    )
```

Then wire the dispatch in `_forward_ws_to_session` :

```python
elif frame_type == "client.upload_macro":
    try:
        await _handle_client_upload_macro(
            client=client,
            session=session,
            memory_root=memory_root,
            slug=device_slug,
            repair_id=repair_id or "default",
            ma_session_id=ma_session_id,
            frame=frame,
        )
    except ValueError as exc:
        logger.warning("upload_macro rejected: %s", exc)
        await ws.send_json({
            "type": "server.upload_macro_error",
            "reason": str(exc),
        })
    continue
```

The exact variable names (`memory_root`, `device_slug`, `repair_id`,
`ma_session_id`) depend on what's in scope inside `_forward_ws_to_session`.
Read the function around line 1572-1700 to confirm the right variable
references — adapt names if needed.

Note about `client.beta.files.upload(purpose="agent")` : the Anthropic
SDK 0.97 accepts `purpose="agent"` per the Files API docs. If the call
returns 400, fall back to `purpose="agent_resource"` (alternate form
some versions accept). We'll discover this at smoke-test time (Task V1) ;
keep `"agent"` for now.

- [ ] **Step 4: Run tests to verify pass**

Run: `.venv/bin/pytest tests/agent/test_runtime_macro_upload.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Run the wider agent test suite to ensure no regression**

Run: `.venv/bin/pytest tests/agent/ -v -m "not slow"`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add api/agent/runtime_managed.py tests/agent/test_runtime_macro_upload.py
git commit -m "$(cat <<'EOF'
feat(agent): client.upload_macro handler — Flow A (tech upload)

Decodes base64 frame, enforces 5MB cap, persists locally via
api.agent.macros.persist_macro, uploads to Anthropic Files API
(purpose='agent'), then injects a user.message with an image block
referencing the file_id into the live MA session. Rejects oversized
payloads with a server.upload_macro_error frame back to the frontend.
EOF
)" -- api/agent/runtime_managed.py tests/agent/test_runtime_macro_upload.py
```

---

### Task B6: WS handler — `cam_capture` dispatch + `client.capture_response` (Flow B)

**Files:**
- Modify: `api/agent/runtime_managed.py` (dispatch + handler + timeout)
- Create: `tests/agent/test_runtime_camera_capture.py`
- Create: `tests/agent/test_runtime_camera_timeout.py`

**Goal:**
1. When the agent fires `cam_capture`, push `server.capture_request` to the
   frontend and await a Future stored in `session.pending_captures[request_id]`.
2. When the frontend posts `client.capture_response`, resolve the Future.
3. After resolution, persist + Files API upload + send `user.custom_tool_result`.
4. Timeout (default 30s) → resolve with error → send `is_error: true` tool_result.

- [ ] **Step 1: Write the failing happy-path test**

```python
# SPDX-License-Identifier: Apache-2.0
"""Flow B happy path : cam_capture → server.capture_request → client.capture_response."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.agent.runtime_managed import (
    _dispatch_cam_capture,
    _handle_client_capture_response,
)
from api.session.state import SessionState


@pytest.mark.asyncio
async def test_cam_capture_full_round_trip(tmp_path: Path):
    session = SessionState()
    session.has_camera = True
    bytes_data = b"\xff\xd8\xff\xe0captured_frame"

    fake_file = MagicMock(id="file_capture123")
    client = MagicMock()
    client.beta.files.upload = AsyncMock(return_value=fake_file)
    client.beta.sessions.events.send = AsyncMock()
    ws = AsyncMock()

    # Schedule the frontend "response" to arrive after dispatch starts.
    async def simulate_frontend():
        await asyncio.sleep(0.05)
        # Find the request_id that dispatch generated.
        assert len(session.pending_captures) == 1
        request_id = next(iter(session.pending_captures))
        await _handle_client_capture_response(
            session=session,
            frame={
                "type": "client.capture_response",
                "request_id": request_id,
                "base64": base64.b64encode(bytes_data).decode("ascii"),
                "mime": "image/jpeg",
                "device_label": "HD USB Camera",
            },
        )

    asyncio.create_task(simulate_frontend())

    await _dispatch_cam_capture(
        client=client,
        session=session,
        ws=ws,
        memory_root=tmp_path,
        slug="iphone-x",
        repair_id="R1",
        ma_session_id="sesn_xyz",
        tool_use_id="sevt_tool123",
        tool_input={"reason": "looking at U2"},
        timeout_s=2.0,
    )

    # WS push happened
    ws.send_json.assert_awaited()
    pushed = ws.send_json.call_args.args[0]
    assert pushed["type"] == "server.capture_request"
    assert "request_id" in pushed
    assert pushed["tool_use_id"] == "sevt_tool123"

    # Persisted
    macros = list((tmp_path / "iphone-x" / "repairs" / "R1" / "macros").glob("*_capture.jpg"))
    assert len(macros) == 1

    # Files API
    client.beta.files.upload.assert_awaited_once()

    # Tool result sent
    client.beta.sessions.events.send.assert_awaited_once()
    sent = client.beta.sessions.events.send.call_args.kwargs.get("events") \
           or client.beta.sessions.events.send.call_args.args[1]["events"]
    event = sent[0]
    assert event["type"] == "user.custom_tool_result"
    assert event["custom_tool_use_id"] == "sevt_tool123"
    img = [c for c in event["content"] if c.get("type") == "image"]
    assert img and img[0]["source"]["file_id"] == "file_capture123"
    text = [c for c in event["content"] if c.get("type") == "text"]
    assert text and "HD USB Camera" in text[0]["text"]

    # Future cleaned up
    assert len(session.pending_captures) == 0
```

- [ ] **Step 2: Write the failing timeout test**

```python
# SPDX-License-Identifier: Apache-2.0
"""Flow B timeout : no client.capture_response → is_error tool_result."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.agent.runtime_managed import _dispatch_cam_capture
from api.session.state import SessionState


@pytest.mark.asyncio
async def test_cam_capture_timeout_returns_is_error(tmp_path: Path):
    session = SessionState()
    session.has_camera = True

    client = MagicMock()
    client.beta.files.upload = AsyncMock()
    client.beta.sessions.events.send = AsyncMock()
    ws = AsyncMock()

    # Don't simulate any client.capture_response — let it time out.
    await _dispatch_cam_capture(
        client=client, session=session, ws=ws,
        memory_root=tmp_path, slug="iphone-x", repair_id="R1",
        ma_session_id="sesn_xyz", tool_use_id="sevt_tool123",
        tool_input={"reason": "test timeout"},
        timeout_s=0.2,  # short for fast test
    )

    # Files API never called
    client.beta.files.upload.assert_not_awaited()

    # Tool result sent with is_error
    client.beta.sessions.events.send.assert_awaited_once()
    sent = client.beta.sessions.events.send.call_args.kwargs.get("events") \
           or client.beta.sessions.events.send.call_args.args[1]["events"]
    event = sent[0]
    assert event["type"] == "user.custom_tool_result"
    assert event["custom_tool_use_id"] == "sevt_tool123"
    assert event.get("is_error") is True
    text = [c for c in event["content"] if c.get("type") == "text"]
    assert text and "timeout" in text[0]["text"].lower()

    # Future cleaned up even on timeout
    assert len(session.pending_captures) == 0
```

- [ ] **Step 3: Run both to verify failure**

Run: `.venv/bin/pytest tests/agent/test_runtime_camera_capture.py tests/agent/test_runtime_camera_timeout.py -v`
Expected: ImportError on `_dispatch_cam_capture` and `_handle_client_capture_response`.

- [ ] **Step 4: Implement the dispatcher and the response handler**

In `api/agent/runtime_managed.py`, add :

```python
import secrets

_CAPTURE_TIMEOUT_S = 30.0


async def _dispatch_cam_capture(
    *,
    client,
    session: SessionState,
    ws,
    memory_root: Path,
    slug: str,
    repair_id: str,
    ma_session_id: str,
    tool_use_id: str,
    tool_input: dict,
    timeout_s: float = _CAPTURE_TIMEOUT_S,
) -> None:
    """Flow B dispatcher : push capture_request, await response, send tool_result.

    Always sends back exactly one user.custom_tool_result for the given
    tool_use_id — either with the captured image (success) or is_error
    (timeout / decode failure / Files API failure).
    """
    request_id = secrets.token_urlsafe(8)
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    session.pending_captures[request_id] = fut

    try:
        await ws.send_json({
            "type": "server.capture_request",
            "request_id": request_id,
            "tool_use_id": tool_use_id,
            "reason": tool_input.get("reason") or "",
        })

        try:
            response = await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            await client.beta.sessions.events.send(
                session_id=ma_session_id,
                events=[{
                    "type": "user.custom_tool_result",
                    "custom_tool_use_id": tool_use_id,
                    "is_error": True,
                    "content": [{
                        "type": "text",
                        "text": f"Capture timeout after {timeout_s:.0f}s — le frontend n'a pas répondu.",
                    }],
                }],
            )
            return

        # Process the captured frame
        try:
            bytes_ = _b64.b64decode(response.get("base64") or "", validate=True)
            mime = (response.get("mime") or "image/jpeg").lower()
            device_label = response.get("device_label") or "caméra"

            path = persist_macro(
                memory_root=memory_root, slug=slug, repair_id=repair_id,
                source="capture", bytes_=bytes_, mime=mime,
            )

            uploaded = await client.beta.files.upload(
                file=(path.name, bytes_, mime),
                purpose="agent",
            )

            await client.beta.sessions.events.send(
                session_id=ma_session_id,
                events=[{
                    "type": "user.custom_tool_result",
                    "custom_tool_use_id": tool_use_id,
                    "content": [
                        {"type": "image", "source": {"type": "file", "file_id": uploaded.id}},
                        {"type": "text", "text": f"Capture acquise depuis {device_label}."},
                    ],
                }],
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("cam_capture processing failed: %s", exc)
            await client.beta.sessions.events.send(
                session_id=ma_session_id,
                events=[{
                    "type": "user.custom_tool_result",
                    "custom_tool_use_id": tool_use_id,
                    "is_error": True,
                    "content": [{
                        "type": "text",
                        "text": f"Capture processing failed: {exc}",
                    }],
                }],
            )
    finally:
        session.pending_captures.pop(request_id, None)


async def _handle_client_capture_response(
    *,
    session: SessionState,
    frame: dict,
) -> None:
    """Resolve the pending Future for the matching request_id."""
    request_id = frame.get("request_id")
    if not request_id or request_id not in session.pending_captures:
        logger.warning("capture_response with unknown request_id: %r", request_id)
        return
    fut = session.pending_captures[request_id]
    if not fut.done():
        fut.set_result(frame)
```

Then wire :

(a) In `_forward_ws_to_session`, add :
```python
elif frame_type == "client.capture_response":
    await _handle_client_capture_response(session=session, frame=frame)
    continue
```

(b) In the place where custom tool dispatches happen (search :
`grep -n "agent.custom_tool_use\|custom_tool_use_id" api/agent/runtime_managed.py | head`).
Add a branch for `cam_capture` :
```python
if tool_name == "cam_capture":
    asyncio.create_task(_dispatch_cam_capture(
        client=client, session=session, ws=ws,
        memory_root=memory_root, slug=device_slug, repair_id=repair_id or "default",
        ma_session_id=ma_session_id,
        tool_use_id=tool_use_id, tool_input=tool_input,
    ))
    continue  # don't fall through to the generic dispatcher
```

The generic dispatch (`_dispatch_tool` etc.) doesn't know about `cam_capture` —
this branch must come BEFORE the generic dispatcher, and use `create_task`
so the dispatch loop continues processing other events while we await the
frontend.

- [ ] **Step 5: Run both tests to verify pass**

Run: `.venv/bin/pytest tests/agent/test_runtime_camera_capture.py tests/agent/test_runtime_camera_timeout.py -v`
Expected: 2 PASS (one per file).

- [ ] **Step 6: Run the broader agent suite**

Run: `.venv/bin/pytest tests/agent/ -v -m "not slow"`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add api/agent/runtime_managed.py tests/agent/test_runtime_camera_capture.py tests/agent/test_runtime_camera_timeout.py
git commit -m "$(cat <<'EOF'
feat(agent): cam_capture dispatch + client.capture_response — Flow B

Agent calls cam_capture → backend pushes server.capture_request to the
frontend with a fresh request_id, awaits an asyncio.Future stored in
session.pending_captures, then on resolution persists + Files API uploads
+ sends user.custom_tool_result with an image block to the MA session.

Timeout (default 30s) → resolves with is_error tool_result so the agent
can recover gracefully. Future is always cleaned up on exit.
EOF
)" -- api/agent/runtime_managed.py tests/agent/test_runtime_camera_capture.py tests/agent/test_runtime_camera_timeout.py
```

---

### Task B7: `GET /api/macros/{slug}/{repair_id}/{filename}` route

**Files:**
- Modify: `api/main.py` (add route)
- Create: `tests/api/test_macros_route.py`

**Goal:** Frontend resolves `image_ref.path` from `messages.jsonl` via this
route at chat replay time.

- [ ] **Step 1: Write the failing test**

```python
# SPDX-License-Identifier: Apache-2.0
"""GET /api/macros/{slug}/{repair_id}/{filename} serves macro images."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app


def _seed_macro(memory_root: Path, slug: str, repair_id: str, filename: str, content: bytes) -> Path:
    macros_dir = memory_root / slug / "repairs" / repair_id / "macros"
    macros_dir.mkdir(parents=True, exist_ok=True)
    path = macros_dir / filename
    path.write_bytes(content)
    return path


def test_macros_route_serves_jpeg(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MICROSOLDER_MEMORY_ROOT", str(tmp_path))
    bytes_ = b"\xff\xd8\xff\xe0fake_jpeg"
    _seed_macro(tmp_path, "iphone-x", "R1", "1745704812_manual.jpg", bytes_)

    client = TestClient(app)
    res = client.get("/api/macros/iphone-x/R1/1745704812_manual.jpg")
    assert res.status_code == 200
    assert res.content == bytes_
    assert res.headers["content-type"] == "image/jpeg"


def test_macros_route_404_on_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MICROSOLDER_MEMORY_ROOT", str(tmp_path))
    client = TestClient(app)
    res = client.get("/api/macros/iphone-x/R1/does_not_exist.png")
    assert res.status_code == 404


def test_macros_route_blocks_path_traversal(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MICROSOLDER_MEMORY_ROOT", str(tmp_path))
    client = TestClient(app)
    # Note FastAPI may URL-decode dots ; the route handler validates.
    res = client.get("/api/macros/iphone-x/R1/..%2F..%2Fetc%2Fpasswd")
    assert res.status_code in (400, 404)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/api/test_macros_route.py -v`
Expected: 404 across the board (route doesn't exist).

- [ ] **Step 3: Add the route in `api/main.py`**

Read `api/main.py` to find where other routes / routers are mounted. Add :

```python
from fastapi.responses import FileResponse
from api.agent.macros import macro_path_for


@app.get("/api/macros/{slug}/{repair_id}/{filename}")
async def get_macro(slug: str, repair_id: str, filename: str):
    """Serve a stored macro image for chat replay rendering."""
    settings = get_settings()
    try:
        path = macro_path_for(
            memory_root=Path(settings.memory_root),
            slug=slug, repair_id=repair_id, filename=filename,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="macro not found")
    media_type = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type)
```

Imports `Path`, `get_settings`, `HTTPException`, `FileResponse` may already
exist or need adding. Check the top of `api/main.py` and add only the missing
ones.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/api/test_macros_route.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/api/test_macros_route.py
git commit -m "$(cat <<'EOF'
feat(api): GET /api/macros/{slug}/{repair_id}/{filename} for replay

Serves persisted macro images (Flow A or Flow B) so the frontend can
re-render image bubbles when the chat history reloads. Path validation
delegates to api.agent.macros.macro_path_for which blocks traversal.
EOF
)" -- api/main.py tests/api/test_macros_route.py
```

---

## Phase 3 — Frontend Files+Vision

### Task F1: Camera picker in metabar + `web/js/camera.js`

**Files:**
- Create: `web/js/camera.js`
- Modify: `web/index.html` (metabar markup)
- Modify: `web/styles/llm.css` (or create `web/styles/camera.css` if cleaner)
- Modify: `web/js/main.js` (boot)

**Goal:** Permanent camera picker in the metabar. Persists choice in
`localStorage`. Populates from `enumerateDevices()`.

- [ ] **Step 1: Add the metabar markup**

Open `web/index.html`, locate the existing metabar (`class="metabar"` or similar).
Add a new `.meta-camera` block among the existing `.meta-*` widgets :

```html
<div class="meta-camera" title="Caméra pour cam_capture">
  <svg class="icon" width="14" height="14" viewBox="0 0 24 24" fill="none"
       stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
    <path d="M3 7h3l2-2h8l2 2h3v12H3z"/>
    <circle cx="12" cy="13" r="4"/>
  </svg>
  <select id="camera-picker" class="meta-select">
    <option value="">— aucune —</option>
  </select>
</div>
```

- [ ] **Step 2: Create `web/js/camera.js`**

```javascript
// SPDX-License-Identifier: Apache-2.0
// Camera picker + capture helpers for the metabar.

const LS_KEY = 'microsolder.cameraDeviceId';

let _cachedDevices = [];
let _onChangeCb = null;

export async function initCameraPicker(onChange) {
  _onChangeCb = onChange || null;
  const select = document.getElementById('camera-picker');
  if (!select) return;

  // Trigger a perm prompt to unlock device labels (best-effort).
  try {
    const probe = await navigator.mediaDevices.getUserMedia({ video: true });
    probe.getTracks().forEach((t) => t.stop());
  } catch (_) {
    // Permission denied or no camera — labels will be empty but enumeration still works.
  }

  await refreshDevices();
  navigator.mediaDevices.addEventListener('devicechange', refreshDevices);

  select.addEventListener('change', () => {
    localStorage.setItem(LS_KEY, select.value);
    if (_onChangeCb) _onChangeCb(select.value);
  });
}

async function refreshDevices() {
  const select = document.getElementById('camera-picker');
  if (!select) return;
  const all = await navigator.mediaDevices.enumerateDevices();
  _cachedDevices = all.filter((d) => d.kind === 'videoinput');
  const saved = localStorage.getItem(LS_KEY) || '';
  // Preserve "aucune" entry, replace the rest.
  while (select.options.length > 1) select.remove(1);
  _cachedDevices.forEach((d) => {
    const opt = document.createElement('option');
    opt.value = d.deviceId;
    opt.textContent = d.label || `Caméra ${d.deviceId.slice(0, 6)}…`;
    select.appendChild(opt);
  });
  // Restore previous selection if still present.
  if (saved && _cachedDevices.some((d) => d.deviceId === saved)) {
    select.value = saved;
  }
}

export function selectedCameraDeviceId() {
  const select = document.getElementById('camera-picker');
  return select ? select.value : '';
}

export function selectedCameraLabel() {
  const id = selectedCameraDeviceId();
  if (!id) return '';
  const d = _cachedDevices.find((x) => x.deviceId === id);
  return d ? (d.label || 'caméra') : '';
}

export function isCameraAvailable() {
  return Boolean(selectedCameraDeviceId());
}

export async function captureFrame({ deviceId, mime = 'image/jpeg', quality = 0.92 }) {
  const stream = await navigator.mediaDevices.getUserMedia({
    video: { deviceId: { exact: deviceId } },
  });
  try {
    const video = document.createElement('video');
    video.srcObject = stream;
    video.muted = true;
    await video.play();
    // Wait one frame to ensure the video has a usable size.
    await new Promise((r) => requestAnimationFrame(r));
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(video, 0, 0);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, mime, quality));
    return blob;
  } finally {
    stream.getTracks().forEach((t) => t.stop());
  }
}

export async function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => {
      // reader.result is "data:image/jpeg;base64,XXXX" — strip the prefix.
      const dataUrl = reader.result;
      const idx = dataUrl.indexOf(',');
      resolve(idx >= 0 ? dataUrl.slice(idx + 1) : '');
    };
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}
```

- [ ] **Step 3: Wire init in `web/js/main.js`**

Find the boot sequence in `main.js`. Add :
```javascript
import { initCameraPicker, isCameraAvailable } from './camera.js';

// ... in the boot function ...
await initCameraPicker((deviceId) => {
  // Notify llm.js if the WS is open — sends a fresh client.capabilities.
  if (window.LLM && typeof window.LLM.sendCapabilities === 'function') {
    window.LLM.sendCapabilities();
  }
});
```

- [ ] **Step 4: Style the picker in `web/styles/llm.css`**

Append (or in `layout.css` near other `.meta-*` rules — pick the file
that already contains the metabar styles) :

```css
.meta-camera {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 10px;
  border-left: 1px solid var(--border-soft);
  color: var(--text-2);
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
}
.meta-camera .icon {
  flex-shrink: 0;
  color: var(--text-3);
}
.meta-camera .meta-select {
  background: transparent;
  color: var(--text-2);
  border: none;
  font-family: inherit;
  font-size: inherit;
  cursor: pointer;
  outline: none;
  padding: 2px 4px;
  border-radius: 3px;
  transition: background-color 0.15s;
}
.meta-camera .meta-select:hover {
  background: var(--panel-2);
  color: var(--text);
}
```

(Adapt to existing `.meta-*` patterns if `layout.css` defines a more
specific look — match the prevailing style.)

- [ ] **Step 5: Open the app, verify the picker shows up and persists**

Run: `make run` (in another terminal)
Open `http://localhost:8000/` → metabar should show 📷 + a select dropdown.
Pick a camera, refresh — the selection persists.

- [ ] **Step 6: Commit**

```bash
git add web/js/camera.js web/index.html web/js/main.js web/styles/llm.css
git commit -m "$(cat <<'EOF'
feat(web): camera picker in metabar + getUserMedia helpers

New camera.js module exposing initCameraPicker (enumerateDevices +
localStorage persist), captureFrame (getUserMedia → canvas → blob), and
blobToBase64. Picker rendered in the metabar next to the existing
.meta-* widgets, JetBrains Mono labels, glass styling. Auto-prompts for
permission on first run to unlock device labels.

Triggered when llm.js needs to acquire a frame for cam_capture.
EOF
)" -- web/js/camera.js web/index.html web/js/main.js web/styles/llm.css
```

---

### Task F2: `client.capabilities` frame at WS open

**Files:**
- Modify: `web/js/llm.js`

**Goal:** When the diagnostic WS opens (and on subsequent camera selection
changes), send a `client.capabilities` frame announcing whether a camera
is available.

- [ ] **Step 1: Locate where the diagnostic WS opens**

Run: `grep -n "new WebSocket\|/ws/diagnostic\|onopen" web/js/llm.js | head`
Note the WS open hook ; the existing pattern is likely a `ws.addEventListener('open', ...)`.

- [ ] **Step 2: Add the capabilities-send logic**

Near the WS open handler, import `isCameraAvailable` and `selectedCameraDeviceId` :

```javascript
import { isCameraAvailable, selectedCameraDeviceId } from './camera.js';

function sendCapabilities() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({
    type: 'client.capabilities',
    camera_available: isCameraAvailable(),
    selected_device_id: selectedCameraDeviceId(),
  }));
}

// Inside the existing onopen handler — send capabilities right after
// the connection establishes.
ws.addEventListener('open', () => {
  // ...existing open logic...
  sendCapabilities();
});

// Expose for camera.js to call when the user changes their selection.
window.LLM = window.LLM || {};
window.LLM.sendCapabilities = sendCapabilities;
```

The exact `ws` variable name and module structure depend on the existing
`llm.js` shape — match the prevailing style. If `llm.js` uses a class,
attach `sendCapabilities` to the class instance and expose via `window.LLM`.

- [ ] **Step 3: Manual verification**

Run: `make run`
Open the app, start a diag, open browser DevTools → Network tab → WS
frames. The first frame after the WS handshake should be
`{"type": "client.capabilities", "camera_available": true|false, ...}`.

- [ ] **Step 4: Commit**

```bash
git add web/js/llm.js
git commit -m "$(cat <<'EOF'
feat(web): send client.capabilities at WS open + on camera change

Frontend announces camera availability to the backend so it can gate
cam_capture in the manifest. Re-sent when the user changes their camera
selection in the metabar picker. Exposed as window.LLM.sendCapabilities
so camera.js can trigger it after a picker change.
EOF
)" -- web/js/llm.js
```

---

### Task F3: Upload button + drag-drop in `web/js/llm.js` (Flow A)

**Files:**
- Modify: `web/index.html` (upload button + drag-drop overlay markup)
- Modify: `web/js/llm.js` (upload + drag-drop wiring)
- Modify: `web/styles/llm.css` (button + overlay styles)

**Goal:** Tech can upload a photo via a button next to the chat input, OR
drag-drop one onto the chat panel. Both paths emit a
`client.upload_macro` frame.

- [ ] **Step 1: Add the upload button next to the chat input**

In `web/index.html`, find the LLM input row (likely a `<form>` or `<div>`
near the chat textarea / send button). Add immediately before the send
button :

```html
<button type="button" class="llm-upload-btn" id="llm-upload-btn"
        title="Upload macro photo (PNG/JPEG, max 5MB)" aria-label="Upload macro">
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
       stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
    <polyline points="17 8 12 3 7 8"/>
    <line x1="12" y1="3" x2="12" y2="15"/>
  </svg>
</button>
<input type="file" id="llm-upload-input" accept="image/png,image/jpeg" hidden>
```

Add a drag-drop overlay near the `<main>` chat panel container :
```html
<div class="llm-dropzone" id="llm-dropzone" hidden>
  Lâche la photo ici
</div>
```

- [ ] **Step 2: Wire the upload button + drag-drop in `llm.js`**

```javascript
const MAX_UPLOAD_BYTES = 5 * 1024 * 1024;  // 5 MB

async function handleMacroUpload(file) {
  if (!file) return;
  if (file.size > MAX_UPLOAD_BYTES) {
    appendChatBubble('error', `Photo trop grosse (${(file.size/1024/1024).toFixed(1)}MB > 5MB max).`);
    return;
  }
  if (!['image/png', 'image/jpeg'].includes(file.type)) {
    appendChatBubble('error', `Format non supporté : ${file.type}. PNG ou JPEG seulement.`);
    return;
  }
  const base64 = await blobToBase64(file);
  // Optimistic: render the bubble immediately.
  const url = URL.createObjectURL(file);
  appendImageBubble('user', url, 'Photo macro envoyée par le tech.');
  // Send to backend.
  ws.send(JSON.stringify({
    type: 'client.upload_macro',
    base64,
    mime: file.type,
    filename: file.name || 'macro.jpg',
  }));
}

document.getElementById('llm-upload-btn').addEventListener('click', () => {
  document.getElementById('llm-upload-input').click();
});
document.getElementById('llm-upload-input').addEventListener('change', (e) => {
  const file = e.target.files && e.target.files[0];
  e.target.value = '';  // reset so re-upload of the same file fires
  handleMacroUpload(file);
});

// Drag-drop on the chat panel
const chatPanel = document.querySelector('.llm-panel') || document.body;
const dropzone = document.getElementById('llm-dropzone');
let dragDepth = 0;

chatPanel.addEventListener('dragenter', (e) => {
  if (!e.dataTransfer.types.includes('Files')) return;
  dragDepth += 1;
  dropzone.hidden = false;
});
chatPanel.addEventListener('dragleave', () => {
  dragDepth -= 1;
  if (dragDepth <= 0) {
    dragDepth = 0;
    dropzone.hidden = true;
  }
});
chatPanel.addEventListener('dragover', (e) => e.preventDefault());
chatPanel.addEventListener('drop', (e) => {
  e.preventDefault();
  dragDepth = 0;
  dropzone.hidden = true;
  const file = e.dataTransfer.files && e.dataTransfer.files[0];
  handleMacroUpload(file);
});
```

`appendImageBubble` is added in Task F5. For now, stub it as :
```javascript
function appendImageBubble(role, url, text) {
  // Implemented in Task F5.
  console.log('image bubble', role, url, text);
}
```

`blobToBase64` import comes from `camera.js` :
```javascript
import { blobToBase64 } from './camera.js';
```

- [ ] **Step 3: Style the button + dropzone in `llm.css`**

```css
.llm-upload-btn {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-2);
  border-radius: 4px;
  padding: 6px 8px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s;
}
.llm-upload-btn:hover {
  background: var(--panel-2);
  border-color: var(--border-hover);
  color: var(--text);
}

.llm-dropzone {
  position: absolute;
  inset: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(20, 20, 20, 0.85);
  backdrop-filter: blur(10px);
  border: 2px dashed var(--cyan);
  color: var(--text);
  font-family: 'JetBrains Mono', monospace;
  font-size: 14px;
  letter-spacing: 0.4px;
  text-transform: uppercase;
  pointer-events: none;
}
```

(The chat panel container needs `position: relative` for the dropzone overlay
to work — verify in existing `llm.css` and add if missing.)

- [ ] **Step 4: Manual verification**

Run: `make run`
Drag any PNG / JPEG file onto the chat panel → dropzone overlay appears →
release → bubble appears (text only — `appendImageBubble` is a stub) +
WS frame `client.upload_macro` visible in DevTools.

Click the upload button → file picker opens → same flow.

- [ ] **Step 5: Commit**

```bash
git add web/index.html web/js/llm.js web/styles/llm.css
git commit -m "$(cat <<'EOF'
feat(web): upload button + drag-drop for macro photos (Flow A)

Tech can drop a PNG/JPEG onto the chat panel or click the upload button
next to the input. 5MB cap enforced client-side, format validation, then
emits client.upload_macro frame with base64 payload to the backend.
Image bubble rendering is stubbed here ; F5 implements it for real.
EOF
)" -- web/index.html web/js/llm.js web/styles/llm.css
```

---

### Task F4: Capture handler in `llm.js` (Flow B)

**Files:**
- Modify: `web/js/llm.js`

**Goal:** When the backend sends `server.capture_request`, snap a frame
from the selected camera and post back `client.capture_response`.

- [ ] **Step 1: Add the handler in the WS message dispatcher**

Locate the WS `onmessage` handler in `llm.js`. Inside the `switch`/`if`
chain on `data.type`, add :

```javascript
import { captureFrame, blobToBase64, selectedCameraDeviceId, selectedCameraLabel } from './camera.js';

case 'server.capture_request': {
  await handleCaptureRequest(data);
  break;
}

case 'server.upload_macro_error': {
  appendChatBubble('error', `Upload rejeté : ${data.reason}`);
  break;
}
```

And the handler :

```javascript
async function handleCaptureRequest(data) {
  const { request_id, tool_use_id, reason } = data;
  const deviceId = selectedCameraDeviceId();
  if (!deviceId) {
    // Frontend somehow has the tool exposed but no camera picked. Rare.
    ws.send(JSON.stringify({
      type: 'client.capture_response',
      request_id,
      base64: '',
      mime: '',
      device_label: '',
      error: 'no camera selected',
    }));
    return;
  }
  try {
    const blob = await captureFrame({ deviceId, mime: 'image/jpeg', quality: 0.92 });
    const base64 = await blobToBase64(blob);
    ws.send(JSON.stringify({
      type: 'client.capture_response',
      request_id,
      base64,
      mime: 'image/jpeg',
      device_label: selectedCameraLabel(),
    }));
  } catch (err) {
    console.error('capture failed', err);
    ws.send(JSON.stringify({
      type: 'client.capture_response',
      request_id,
      base64: '',
      mime: '',
      device_label: '',
      error: String(err),
    }));
  }
}
```

The backend's `_dispatch_cam_capture` will treat an empty `base64` field as
a decode failure and send `is_error: true` tool_result back to the agent.

- [ ] **Step 2: Manual verification**

Hard to test without the agent driving — defer to V1 smoke. For now :

Run: `make run`
Open the app, pick the laptop's webcam in the picker, open DevTools console.
Manually paste a fake `server.capture_request` event into the WS handler if
exposed, OR wait until V1 smoke runs end-to-end.

- [ ] **Step 3: Commit**

```bash
git add web/js/llm.js
git commit -m "$(cat <<'EOF'
feat(web): handle server.capture_request — Flow B agent-initiated snap

When the backend pushes server.capture_request (triggered by an agent
cam_capture call), the frontend uses the metabar-selected camera to grab
a single frame via getUserMedia + canvas, then posts client.capture_response
with the JPEG payload back to the backend. Errors (no camera selected,
getUserMedia rejection) are reported as empty-base64 responses so the
backend can surface is_error to the agent without hanging.

Also handles server.upload_macro_error to surface Flow A rejections.
EOF
)" -- web/js/llm.js
```

---

### Task F5: Image bubble render + replay

**Files:**
- Modify: `web/js/llm.js` (image bubble + replay support)
- Modify: `web/styles/llm.css` (image bubble styles)

**Goal:** Render image bubbles in the chat (200px thumb, click → fullscreen
modal). On replay (chat history reload), resolve `image_ref.path` via
`/api/macros/...` and render the same.

- [ ] **Step 1: Implement `appendImageBubble` (replace the stub from F3)**

```javascript
function appendImageBubble(role, srcUrl, captionText) {
  const list = document.querySelector('.llm-messages') || document.querySelector('.chat-list');
  if (!list) return;
  const bubble = document.createElement('div');
  bubble.className = `chat-bubble chat-bubble--${role} chat-bubble--has-image`;
  const img = document.createElement('img');
  img.src = srcUrl;
  img.alt = captionText || 'macro';
  img.className = 'chat-bubble-img';
  img.addEventListener('click', () => openImageModal(srcUrl, captionText));
  bubble.appendChild(img);
  if (captionText) {
    const cap = document.createElement('div');
    cap.className = 'chat-bubble-caption';
    cap.textContent = captionText;
    bubble.appendChild(cap);
  }
  list.appendChild(bubble);
  list.scrollTop = list.scrollHeight;
}

function openImageModal(srcUrl, captionText) {
  let modal = document.getElementById('llm-image-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'llm-image-modal';
    modal.className = 'llm-image-modal';
    modal.addEventListener('click', () => modal.remove());
    document.body.appendChild(modal);
  } else {
    modal.innerHTML = '';
  }
  const img = document.createElement('img');
  img.src = srcUrl;
  img.alt = captionText || '';
  modal.appendChild(img);
  modal.style.display = 'flex';
}
```

- [ ] **Step 2: Handle `image_ref` blocks during chat replay**

Locate the chat-history replay logic in `llm.js` (where `messages.jsonl`
content is rendered). Add a branch :

```javascript
function renderMessageContent(role, content) {
  for (const block of content) {
    if (block.type === 'text') {
      appendChatBubble(role, block.text);
    } else if (block.type === 'image_ref') {
      const slug = currentDeviceSlug();
      const repairId = currentRepairId();
      const filename = block.path.replace(/^macros\//, '');
      const url = `/api/macros/${encodeURIComponent(slug)}/${encodeURIComponent(repairId)}/${encodeURIComponent(filename)}`;
      const caption = role === 'user' ? 'Photo macro envoyée par le tech.' : 'Capture acquise.';
      appendImageBubble(role, url, caption);
    }
  }
}
```

(`currentDeviceSlug()` and `currentRepairId()` are existing helpers — adapt
if they have different names. Search the file first.)

- [ ] **Step 3: Style the image bubble + modal in `llm.css`**

```css
.chat-bubble--has-image {
  padding: 0;
  overflow: hidden;
  max-width: 240px;
}
.chat-bubble-img {
  display: block;
  width: 200px;
  height: auto;
  cursor: zoom-in;
  border-radius: 4px;
  background: var(--bg-2);
}
.chat-bubble-caption {
  padding: 6px 10px;
  font-size: 11px;
  color: var(--text-3);
  font-family: 'JetBrains Mono', monospace;
}

.llm-image-modal {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.85);
  backdrop-filter: blur(6px);
  cursor: zoom-out;
}
.llm-image-modal img {
  max-width: 90vw;
  max-height: 90vh;
  border-radius: 6px;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.6);
}
```

- [ ] **Step 4: Manual verification**

Run: `make run`
Drag a photo onto the chat panel → bubble shows with the photo as a
200px thumbnail. Click → fullscreen modal opens. Click backdrop → closes.

Restart a session that has already received an upload — replay should
re-show the image (calls `/api/macros/...`).

- [ ] **Step 5: Commit**

```bash
git add web/js/llm.js web/styles/llm.css
git commit -m "$(cat <<'EOF'
feat(web): image bubble render + replay via /api/macros

User and tool_result bubbles can now contain image blocks. Rendered as a
200px thumbnail with click → fullscreen modal. On chat history replay,
image_ref blocks resolve via GET /api/macros/{slug}/{repair_id}/{filename}
so the frontend re-renders without re-uploading. Glass modal styling
cohérent avec les overlays existants.
EOF
)" -- web/js/llm.js web/styles/llm.css
```

---

## Phase 4 — System Prompt + Bootstrap

### Task SP1: VISION block in `SYSTEM_PROMPT` + re-bootstrap

**Files:**
- Modify: `scripts/bootstrap_managed_agent.py`

**Goal:** Tell the agents about Flow A, Flow B, and the discipline
expectations. Re-run bootstrap to push to all 3 tier-scoped agents.

- [ ] **Step 1: Locate `SYSTEM_PROMPT` in bootstrap_managed_agent.py**

Run: `grep -n "SYSTEM_PROMPT\|VISION" scripts/bootstrap_managed_agent.py | head`

- [ ] **Step 2: Add the VISION block**

Insert (in a sensible position — near the other capability sections, or at
the end of the prompt) :

```python
VISION_BLOCK = """
**VISION** — Le tech a (parfois) une caméra branchée et sélectionnée dans la metabar.

1. Si le tech upload une photo (block `image` dans son `user.message`) : identifie
   composants par boîtier (SOT-23, SO-8, QFN, BGA, MELF, etc.), signale anomalies
   visibles (décoloration, soudure cassée, condo gonflé, brûlure), propose mapping
   role probable → composant ("le BGA central c'est probablement le SoC ; le SO-8
   près du connecteur USB-C, un load switch ou une protection"). Demande au tech
   ce qu'il a vu de son côté avant de proposer un plan.

2. Si tu as besoin de voir un détail spécifique et que `cam_capture` est exposé
   dans tes tools : appelle-le. Le tech a déjà cadré côté physique (zoom optique
   manuel). Pas de paramètres requis — `reason` est juste pour les logs.

3. Pas de capture spéculative : appelle `cam_capture` quand ça apporte une info
   diagnostique précise, pas par réflexe ou pour "voir si c'est intéressant".

4. Discipline anti-hallucination maintenue : la vision te donne des boîtiers et
   positions, jamais des refdes. Si tu mentionnes un refdes, il doit venir d'un
   `mb_get_component` ou `bv_*` lookup, pas d'une lecture visuelle.
"""
```

Then merge `VISION_BLOCK` into the existing `SYSTEM_PROMPT` (concatenate
with appropriate separation).

- [ ] **Step 3: Re-run bootstrap to push to MA**

Run: `.venv/bin/python scripts/bootstrap_managed_agent.py`
Expected: idempotent — updates the 3 existing agents (or creates them if
absent), writes `managed_ids.json`. Should print `version` bumps for each
agent.

- [ ] **Step 4: Commit**

```bash
git add scripts/bootstrap_managed_agent.py
git commit -m "$(cat <<'EOF'
feat(agent): add VISION block to MA SYSTEM_PROMPT

Tells the diagnostic agent how to handle two flows :
  1. Tech-uploaded photo (user.message with image block) → analyse
     visuelle structurée, mapping boîtier → role, demande au tech
     son contexte avant plan.
  2. Agent-initiated cam_capture → snap frame from the metabar-selected
     camera (no parameters needed beyond optional reason).

Re-bootstrap pushes to all 3 tier-scoped agents (fast/normal/deep). The
anti-hallucination discipline is reinforced : vision gives packages +
positions, never refdes — refdes still go through mb_get_component.
EOF
)" -- scripts/bootstrap_managed_agent.py
```

---

## Phase 5 — Smoke + Validation

### Task V1: Smoke E2E live + manual browser test

**Files:**
- Create: `scripts/smoke_files_vision.py`
- Manual: capture/source `tests/fixtures/macro_devboard_test.png` (clean-room
  dev board photo — Arduino, RPi, custom bench board, NOT proprietary)

**Goal:** Two validation paths :
1. Scripted smoke that simulates Flow A end-to-end with a real Anthropic call.
2. Manual browser test that triggers Flow B (agent calls `cam_capture` after
   user request).

- [ ] **Step 1: Source a clean-room test fixture image**

Photograph a dev board you own (Arduino, RPi, custom). Ensure no Apple /
Samsung / proprietary content visible in frame. Save as
`tests/fixtures/macro_devboard_test.png`. Add to git tracking.

If you don't have a board handy : take a clean-room photo of any
electronic component lying around (a USB cable's PCB, a powerbank
internals, etc.). The test only validates that the agent says something
visual, not that it correctly identifies any specific board.

- [ ] **Step 2: Write the smoke script**

```python
# SPDX-License-Identifier: Apache-2.0
"""Live smoke test for Files+Vision Flow A.

Opens an MA session, sends a capabilities frame, uploads a fixture image
via the existing _handle_client_upload_macro path (driven directly to skip
the WS hop), asserts the agent streams an analysis containing visual
keywords.

Costs ~5-10¢ per run.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: ANTHROPIC_API_KEY not set")

    from anthropic import AsyncAnthropic

    from api.agent.managed_ids import load_managed_ids
    from api.agent.runtime_managed import _handle_client_upload_macro
    from api.session.state import SessionState

    fixture = REPO_ROOT / "tests" / "fixtures" / "macro_devboard_test.png"
    if not fixture.exists():
        sys.exit(f"ERROR: fixture missing : {fixture}")

    client = AsyncAnthropic()
    ids = load_managed_ids()
    if not ids or "fast" not in ids.get("agents", {}):
        sys.exit("ERROR: managed_ids.json missing — run bootstrap")

    agent = ids["agents"]["fast"]
    env_id = ids["environment_id"]

    print("Creating session…")
    session = await client.beta.sessions.create(
        agent={"type": "agent", "id": agent["id"], "version": agent["version"]},
        environment_id=env_id,
        title="smoke files+vision",
    )
    print(f"  session id: {session.id}")

    state = SessionState()
    state.has_camera = True  # not strictly needed for Flow A

    import base64
    img_bytes = fixture.read_bytes()
    frame = {
        "type": "client.upload_macro",
        "base64": base64.b64encode(img_bytes).decode("ascii"),
        "mime": "image/png",
        "filename": fixture.name,
    }

    memory_root = REPO_ROOT / "memory"
    print("Uploading fixture + injecting user.message…")
    stream = await client.beta.sessions.events.stream(session_id=session.id)

    await _handle_client_upload_macro(
        client=client,
        session=state,
        memory_root=memory_root,
        slug="iphone-x",  # any seeded slug
        repair_id="smoke-vision-R1",
        ma_session_id=session.id,
        frame=frame,
    )

    print("Streaming agent response…\n" + "-" * 60)
    text_seen = []
    async for event in stream:
        etype = getattr(event, "type", "?")
        if etype == "agent.message":
            for blk in getattr(event, "content", []):
                if getattr(blk, "type", "") == "text":
                    chunk = getattr(blk, "text", "")
                    text_seen.append(chunk)
                    print(chunk, end="", flush=True)
        elif etype == "session.status_idle":
            stop = getattr(event, "stop_reason", None)
            stop_type = getattr(stop, "type", None) if stop else None
            if stop_type != "requires_action":
                break
        elif etype == "session.status_terminated":
            break

    print("\n" + "=" * 60)
    full = "".join(text_seen).lower()
    visual_keywords = ["composant", "boîtier", "soudure", "ic", "résist", "cap",
                       "connecteur", "smd", "qfn", "bga", "sot", "so-8", "puce"]
    hits = [k for k in visual_keywords if k in full]
    print(f"Visual keywords matched: {hits}")
    if hits:
        print("✅ PASS")
    else:
        print("❌ FAIL : agent did not produce a visual analysis")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: Run the smoke**

Run: `.venv/bin/python scripts/smoke_files_vision.py`
Expected: agent streams a visual description of the fixture image, at
least one visual keyword matches, ✅ PASS.

If no keywords match, read the agent's response — it might be commenting
on something the keyword list doesn't cover. Update the keyword list if
appropriate, or investigate the prompt / image.

- [ ] **Step 4: Manual browser test for Flow B**

Run: `make run` (in another terminal)
Open `http://localhost:8000/` :
1. Pick the laptop's webcam in the metabar camera picker.
2. Start a diagnostic on iphone-x (or any seeded device).
3. Send : "Regarde ma board avec ta caméra et dis-moi ce que tu vois."
4. Expected : agent calls `cam_capture` (visible in dev tools / server logs),
   frontend snaps a webcam frame, agent analyzes the result and replies.
5. Validate : look at `memory/iphone-x/repairs/{repair_id}/macros/` —
   should contain a `*_capture.jpg` file written during the test.

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke_files_vision.py tests/fixtures/macro_devboard_test.png
git commit -m "$(cat <<'EOF'
test(smoke): live Files+Vision smoke + dev board fixture

scripts/smoke_files_vision.py drives Flow A end-to-end: open MA session,
upload a fixture PNG via _handle_client_upload_macro, stream the agent's
response, assert at least one visual keyword fires. Costs ~5-10¢ per run.

tests/fixtures/macro_devboard_test.png is a clean-room dev board photo
(NOT proprietary hardware — see CLAUDE.md hard rule #4).

Flow B is validated manually in the browser per the V1 task instructions.
EOF
)" -- scripts/smoke_files_vision.py tests/fixtures/macro_devboard_test.png
```

---

## Final validation

- [ ] **Step 1: Run full fast test suite**

Run: `make test`
Expected: all green. ~1216 prior tests + new tests (~15 new) = ~1230 PASS.

- [ ] **Step 2: Run lint**

Run: `make lint`
Expected: clean (or only the 7 pre-existing intentional errors flagged
in the previous session's chore commits).

- [ ] **Step 3: Run the multi-session smoke from Phase 1 to verify nothing regressed in the scribe path**

Run: `.venv/bin/python scripts/smoke_layered_memory.py`
Expected: ✅ PASS.

- [ ] **Step 4: Run the Files+Vision smoke**

Run: `.venv/bin/python scripts/smoke_files_vision.py`
Expected: ✅ PASS.

- [ ] **Step 5: Manual browser smoke**

Per Task V1 step 4 above. Confirm `cam_capture` fires and the analysis is
coherent.

- [ ] **Step 6: Confirm with user**

Surface a status summary : tests, smokes, manual checks. Wait for explicit
approval before any `git push`.

---

## Self-review checklist

- [x] **Spec coverage** : every Goal in the spec maps to a task. Hardening
  (3 tasks H1-H3) ; Flow A (B5 + F3) ; Flow B (B6 + F4) ; persistence (B1) ;
  state (B2) ; manifest gating (B3) ; capabilities (B4 + F2) ; replay route
  (B7) ; replay rendering (F5) ; system prompt (SP1) ; smoke (V1).
- [x] **Placeholders** : no TBDs ; all code blocks complete ; commands
  precise ; expected outputs given.
- [x] **Type consistency** : `_handle_client_capabilities`, `_handle_client_upload_macro`,
  `_handle_client_capture_response`, `_dispatch_cam_capture` named consistently
  across tasks. `persist_macro` / `macro_path_for` / `build_image_ref` signatures
  match between B1 (definition) and B5/B6/B7 (use). `Source` type
  (`'manual' | 'capture'`) consistent.
- [x] **Hard rules** : SPDX header on every new file ; Apache 2.0 ; no GPL deps ;
  vanilla JS frontend ; SVG icons inline ; OKLCH tokens ; French UI strings ;
  `git commit -- path...` form everywhere (parallel-agent safety) ; clean-room
  fixture image only.

---

## Execution

After this plan is approved, executing-plans (inline) will :
1. Walk H1 → H2 → H3 (hardening, ~1h cumulative).
2. Walk B1 → B7 (backend Files+Vision, ~2-3h cumulative).
3. Walk F1 → F5 (frontend Files+Vision, ~2h cumulative).
4. SP1 (system prompt + bootstrap, ~15min).
5. V1 (smoke + manual, ~30min).

Total wall-clock estimate : 5-7h depending on real bugs hit.

Commits per task, paths explicit. No `git push` without explicit
authorization from Alexis.
