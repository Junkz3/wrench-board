# Agent + Pipeline Optimizations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the ten audit-identified optimizations from `docs/superpowers/specs/2026-04-24-agent-pipeline-optimizations-design.md` — runtime cache (R1–R4), mirror durability (D1), memory-store hardening (D2), bimodal mount (M1), auditor cache block (P1+P2), and per-phase token analytics (P3).

**Architecture:** Nine tasks, each a self-contained commit. TDD inside every task. No mixing of `api/agent/` and `api/pipeline/` in a single commit. Caches live on `SessionState` (mtime-invalidated) so they're per-session and die with the socket. Durability adds retry + awaited cleanup on existing fire-and-forget paths. The pipeline gets structured content blocks with explicit `cache_control` on the Auditor user message, and a telemetry sidecar accumulating `usage` metadata from every `call_with_forced_tool` invocation.

**Tech Stack:** Python 3.11+, pytest, pytest-asyncio, anthropic (beta `managed-agents-2026-04-01`), dataclasses, `pathlib`.

---

## Pre-flight

- [ ] **Step 0: Confirm baseline green**

Run: `.venv/bin/make test`
Expected: all tests pass. If not, stop and fix before starting this plan.

- [ ] **Step 0b: Read the spec once**

File: `docs/superpowers/specs/2026-04-24-agent-pipeline-optimizations-design.md`
This plan is the instruction manual; the spec is the source of truth for motivation and YAGNI boundaries.

---

## Task 1 (R1): Pack cache in `SessionState`

**Why:** `api/agent/tools.py::_load_pack()` reads ~2.4MB of JSON per `mb_*` call; a typical 10-call turn burns 24MB of disk I/O.

**Files:**
- Modify: `api/session/state.py` (add cache field + helper)
- Modify: `api/agent/tools.py:26-32` (route `_load_pack` through the cache)
- Modify: `api/agent/tools.py:198-233` (`mb_expand_knowledge` invalidates after success)
- Test: `tests/agent/test_tools.py` (add cache-hit test)

- [ ] **Step 1: Write the failing test**

Append to `tests/agent/test_tools.py`:

```python
def test_pack_cache_hits_on_repeated_calls(tmp_path: Path, monkeypatch):
    """Second mb_get_component call on same slug must not re-read pack files."""
    from api.session.state import SessionState
    from api.agent.tools import mb_get_component

    slug = "demo"
    pack_dir = tmp_path / slug
    pack_dir.mkdir()
    (pack_dir / "registry.json").write_text('{"components": [{"canonical_name": "U1", "kind": "ic"}], "signals": []}')
    (pack_dir / "dictionary.json").write_text('{"entries": [{"canonical_name": "U1", "role": "cpu"}]}')
    (pack_dir / "rules.json").write_text('{"rules": []}')

    session = SessionState()
    reads: list[Path] = []
    orig_read_text = Path.read_text
    def counting_read(self, *args, **kwargs):
        if self.suffix == ".json" and self.parent == pack_dir:
            reads.append(self)
        return orig_read_text(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", counting_read)

    mb_get_component(device_slug=slug, refdes="U1", memory_root=tmp_path, session=session)
    first_call_reads = len(reads)
    assert first_call_reads >= 3  # registry + dictionary + rules

    mb_get_component(device_slug=slug, refdes="U1", memory_root=tmp_path, session=session)
    assert len(reads) == first_call_reads, "second call hit disk — cache did not work"
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `.venv/bin/pytest tests/agent/test_tools.py::test_pack_cache_hits_on_repeated_calls -v`
Expected: FAIL (second call re-reads the JSON files).

- [ ] **Step 3: Add cache state to `SessionState`**

In `api/session/state.py`, extend the dataclass — two new fields plus a helper. Paste inside the `SessionState` class, immediately after `layer_visibility`:

```python
    # R1: pack cache — keyed by device_slug, storing (max_mtime, pack_dict).
    pack_cache: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)

    def invalidate_pack_cache(self, device_slug: str) -> None:
        """Drop the cached pack for `device_slug`. Called after mb_expand_knowledge."""
        self.pack_cache.pop(device_slug, None)
```

- [ ] **Step 4: Route `_load_pack` through the cache**

Replace `_load_pack` in `api/agent/tools.py:26-32` with:

```python
def _load_pack(
    slug: str,
    memory_root: Path,
    session: SessionState | None = None,
) -> dict[str, Any]:
    pack_dir = memory_root / slug
    paths = (
        pack_dir / "registry.json",
        pack_dir / "dictionary.json",
        pack_dir / "rules.json",
    )
    try:
        max_mtime = max(p.stat().st_mtime for p in paths)
    except FileNotFoundError:
        # Propagate the original read error so callers fail the same way.
        return {
            "registry": json.loads(paths[0].read_text()),
            "dictionary": json.loads(paths[1].read_text()),
            "rules": json.loads(paths[2].read_text()),
        }

    if session is not None:
        cached = session.pack_cache.get(slug)
        if cached is not None and cached[0] >= max_mtime:
            return cached[1]

    pack = {
        "registry": json.loads(paths[0].read_text()),
        "dictionary": json.loads(paths[1].read_text()),
        "rules": json.loads(paths[2].read_text()),
    }
    if session is not None:
        session.pack_cache[slug] = (max_mtime, pack)
    return pack
```

- [ ] **Step 5: Thread `session` through the `mb_*` call sites**

In the same file, update each `_load_pack(device_slug, memory_root)` call to pass `session` when the caller has it.

- `mb_get_component` (line ~50) already has `session` — change to `_load_pack(device_slug, memory_root, session=session)`.
- `mb_get_rules_for_symptoms` (line ~117) has no `session` param today. Add `session: SessionState | None = None` to the signature and pass it through: `_load_pack(device_slug, memory_root, session=session)`.
- Update `api/agent/runtime_direct.py` and `api/agent/runtime_managed.py` call sites of `mb_get_rules_for_symptoms` to forward `session=session_state`. Grep for `mb_get_rules_for_symptoms(` to locate both.

- [ ] **Step 6: Invalidate on `mb_expand_knowledge` success**

In `mb_expand_knowledge` (`api/agent/tools.py:198`), add a `session: SessionState | None = None` param. Inside the `try` branch, after `summary["ok"] = True`, add:

```python
        if session is not None:
            session.invalidate_pack_cache(device_slug)
```

Forward `session=session_state` from both runtimes' dispatch sites.

- [ ] **Step 7: Run the test — green**

Run: `.venv/bin/pytest tests/agent/test_tools.py::test_pack_cache_hits_on_repeated_calls -v`
Expected: PASS.

- [ ] **Step 8: Full test pass**

Run: `.venv/bin/make test`
Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add api/session/state.py api/agent/tools.py api/agent/runtime_direct.py api/agent/runtime_managed.py tests/agent/test_tools.py
git commit -m "$(cat <<'EOF'
perf(agent): cache pack JSON in SessionState

mtime-keyed cache on SessionState.pack_cache avoids re-reading the
2.4MB pack on every mb_* call. mb_expand_knowledge invalidates after
a successful mutation.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/session/state.py api/agent/tools.py api/agent/runtime_direct.py api/agent/runtime_managed.py tests/agent/test_tools.py
```

---

## Task 2 (R2): `mb_get_component` LRU

**Why:** same refdes queried 3–5× per session.

**Files:**
- Modify: `api/session/state.py` (add LRU field + reset on board load)
- Modify: `api/agent/tools.py:35-106` (`mb_get_component` short-circuits on hit)
- Test: `tests/agent/test_tools.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/agent/test_tools.py`:

```python
def test_mb_get_component_lru_skips_pack_reload(tmp_path: Path, monkeypatch):
    from api.session.state import SessionState
    from api.agent.tools import mb_get_component

    slug = "demo"
    pack_dir = tmp_path / slug
    pack_dir.mkdir()
    (pack_dir / "registry.json").write_text('{"components": [{"canonical_name": "U5", "kind": "ic"}], "signals": []}')
    (pack_dir / "dictionary.json").write_text('{"entries": [{"canonical_name": "U5", "role": "pmic"}]}')
    (pack_dir / "rules.json").write_text('{"rules": []}')

    session = SessionState()
    calls: list[tuple[str, str]] = []
    from api.agent import tools as tools_mod
    orig_load_pack = tools_mod._load_pack
    def spy(slug_arg, root, session=None):
        calls.append((slug_arg, "pack"))
        return orig_load_pack(slug_arg, root, session=session)
    monkeypatch.setattr(tools_mod, "_load_pack", spy)

    mb_get_component(device_slug=slug, refdes="U5", memory_root=tmp_path, session=session)
    mb_get_component(device_slug=slug, refdes="U5", memory_root=tmp_path, session=session)

    # R1 means the second call hits cached pack; R2 means it never invokes _load_pack at all.
    assert len(calls) == 1, f"expected 1 _load_pack call, got {len(calls)}"
```

- [ ] **Step 2: Run — FAIL**

Run: `.venv/bin/pytest tests/agent/test_tools.py::test_mb_get_component_lru_skips_pack_reload -v`
Expected: FAIL (two `_load_pack` calls).

- [ ] **Step 3: Add LRU to `SessionState`**

Inside `SessionState` (after the R1 fields):

```python
    # R2: per-session LRU for mb_get_component results, keyed by (device_slug, refdes).
    # Size cap kept small — sessions ask about the same ~dozen refdes repeatedly.
    component_cache: "OrderedDict[tuple[str, str], dict[str, Any]]" = field(
        default_factory=OrderedDict
    )

    COMPONENT_CACHE_MAX: ClassVar[int] = 64
```

Add imports at the top of the file:

```python
from collections import OrderedDict
from typing import ClassVar
```

In `set_board`, add at the end:

```python
        self.component_cache.clear()
```

- [ ] **Step 4: Short-circuit `mb_get_component`**

At the top of `mb_get_component` (after the docstring), add:

```python
    cache_key = (device_slug, refdes)
    if session is not None:
        cached = session.component_cache.get(cache_key)
        if cached is not None:
            session.component_cache.move_to_end(cache_key)
            return cached
```

Right before every `return` statement in the function (there are two — the "not found" branch and the "found" branch), add the cache write path. Refactor to a single return at the end if it reads cleaner:

```python
    result = (
        {"found": False, "error": "not_found", ...}
        if memory_section is None and board_section is None
        else {"found": True, "canonical_name": refdes, "memory_bank": memory_section, "board": board_section}
    )
    if session is not None:
        session.component_cache[cache_key] = result
        session.component_cache.move_to_end(cache_key)
        while len(session.component_cache) > SessionState.COMPONENT_CACHE_MAX:
            session.component_cache.popitem(last=False)
    return result
```

- [ ] **Step 5: Run — PASS**

Run: `.venv/bin/pytest tests/agent/test_tools.py::test_mb_get_component_lru_skips_pack_reload -v`
Expected: PASS.

- [ ] **Step 6: Full test pass**

Run: `.venv/bin/make test`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add api/session/state.py api/agent/tools.py tests/agent/test_tools.py
git commit -m "$(cat <<'EOF'
perf(agent): LRU cache for mb_get_component by refdes

Per-session OrderedDict (cap 64) short-circuits repeated refdes lookups
within the same diagnostic session. Cleared on board reload.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/session/state.py api/agent/tools.py tests/agent/test_tools.py
```

---

## Task 3 (R3): Lazy `profile_get`

**Why:** profile file is stable between sessions; re-reading on every open wastes a disk hit.

**Files:**
- Modify: `api/profile/store.py` (expose `profile_path()` if not already; source the mtime there)
- Modify: `api/profile/tools.py:43-61` (accept an optional session cache)
- Modify: `api/session/state.py` (new `profile_cache` field)
- Modify: `api/agent/runtime_managed.py`, `api/agent/runtime_direct.py` (pass session to `profile_get`)
- Test: `tests/profile/test_tools.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/profile/test_tools.py`:

```python
def test_profile_get_caches_within_session(tmp_path: Path, monkeypatch):
    """Second profile_get on the same session must not re-read disk."""
    from api.session.state import SessionState
    from api.profile import tools as profile_tools

    calls: list[str] = []
    orig = profile_tools.load_profile
    def spy():
        calls.append("load")
        return orig()
    monkeypatch.setattr(profile_tools, "load_profile", spy)

    session = SessionState()
    profile_tools.profile_get(session=session)
    profile_tools.profile_get(session=session)

    assert len(calls) == 1, f"expected 1 load, got {len(calls)}"
```

- [ ] **Step 2: Run — FAIL**

Run: `.venv/bin/pytest tests/profile/test_tools.py::test_profile_get_caches_within_session -v`
Expected: FAIL (either TypeError `unexpected keyword argument 'session'` or 2 loads).

- [ ] **Step 3: Add `profile_cache` to `SessionState`**

Inside `SessionState` (after component_cache):

```python
    # R3: profile snapshot cache — mtime-checked on every lookup.
    profile_cache: tuple[float, dict[str, Any]] | None = None
```

- [ ] **Step 4: Expose `profile_path()` publicly**

`api/profile/store.py` has a private `_profile_path()` (line 24). Add a public alias just after it:

```python
def profile_path() -> Path:
    """Public accessor for the profile file path (used by mtime-based caches)."""
    return _profile_path()
```

- [ ] **Step 5: Extend `profile_get` with the cache path**

Replace the full `profile_get()` function in `api/profile/tools.py` (currently lines 43-61) with:

```python
def profile_get(session: "SessionState | None" = None) -> dict[str, Any]:
    from api.profile.store import profile_path
    path = profile_path()
    mtime = path.stat().st_mtime if path.exists() else 0.0

    if session is not None and session.profile_cache is not None:
        cached_mtime, cached_data = session.profile_cache
        if cached_mtime >= mtime:
            return cached_data

    profile = load_profile()
    data = {
        "identity": {
            "name": profile.identity.name,
            "avatar": profile.identity.avatar,
            "years_experience": profile.identity.years_experience,
            "specialties": profile.identity.specialties,
        },
        "level": global_level(profile),
        "verbosity_effective": effective_verbosity(profile),
        "tools_available": [
            t.value for t in ToolId if getattr(profile.tools, t.value)
        ],
        "tools_missing": [
            t.value for t in ToolId if not getattr(profile.tools, t.value)
        ],
        "skills_summary": _skills_summary(profile),
    }
    if session is not None:
        session.profile_cache = (mtime, data)
    return data
```

Add at the top of `api/profile/tools.py` (guarded to avoid import cycle):

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from api.session.state import SessionState
```

- [ ] **Step 6: Wire the runtimes**

In `api/agent/runtime_direct.py:396` change `return profile_get()` → `return profile_get(session=session_state)`.

In `api/agent/runtime_managed.py`, find the profile dispatch (grep `profile_get(` in that file) and add the same `session=session_state` argument.

- [ ] **Step 7: Run — PASS**

Run: `.venv/bin/pytest tests/profile/test_tools.py::test_profile_get_caches_within_session -v`
Expected: PASS.

- [ ] **Step 8: Full tests**

Run: `.venv/bin/make test`
Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add api/session/state.py api/profile/tools.py api/profile/store.py api/agent/runtime_direct.py api/agent/runtime_managed.py tests/profile/test_tools.py
git commit -m "$(cat <<'EOF'
perf(agent): lazy profile_get cached on SessionState

mtime-checked per-session cache on SessionState.profile_cache. Second
profile_get call within the same session reads nothing unless the
profile JSON was modified on disk.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/session/state.py api/profile/tools.py api/profile/store.py api/agent/runtime_direct.py api/agent/runtime_managed.py tests/profile/test_tools.py
```

---

## Task 4 (R4): `electrical_graph.json` cache

**Why:** ~2MB JSON reloaded per `mb_schematic_graph` call.

**Files:**
- Modify: `api/session/state.py` (new cache field)
- Modify: `api/tools/schematic.py:38-75` (thread session through `_load_graph`)
- Modify: `api/tools/schematic.py:637-700` (`mb_schematic_graph` accepts `session`)
- Modify: `api/agent/runtime_direct.py:291`, `api/agent/runtime_managed.py` (forward `session`)
- Test: `tests/pipeline/test_schematic_tools.py` (new) OR extend an existing schematic test file — check `tests/tools/` first; if empty, create `tests/tools/test_schematic_tools.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/tools/test_schematic_tools.py` (make the `tests/tools/` dir + `__init__.py` if missing):

```python
from pathlib import Path
import json
from api.session.state import SessionState
from api.tools.schematic import mb_schematic_graph


def test_schematic_graph_cache_hits(tmp_path: Path, monkeypatch):
    slug = "demo"
    pack = tmp_path / slug
    pack.mkdir()
    graph = {"power_rails": [], "boot_sequence": [], "components": []}
    (pack / "electrical_graph.json").write_text(json.dumps(graph))

    session = SessionState()
    reads: list[Path] = []
    from api.tools import schematic as schem
    orig = Path.read_text
    def counting(self, *args, **kwargs):
        if self.name == "electrical_graph.json":
            reads.append(self)
        return orig(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", counting)

    mb_schematic_graph(device_slug=slug, memory_root=tmp_path, query="list_rails", session=session)
    mb_schematic_graph(device_slug=slug, memory_root=tmp_path, query="list_rails", session=session)

    assert len(reads) == 1, f"expected 1 disk read, got {len(reads)}"
```

- [ ] **Step 2: Run — FAIL**

Run: `.venv/bin/pytest tests/tools/test_schematic_tools.py::test_schematic_graph_cache_hits -v`
Expected: FAIL (TypeError on `session=` kwarg, or 2 reads).

- [ ] **Step 3: Add cache on `SessionState`**

```python
    # R4: electrical_graph.json cache (and analyzer overlay) keyed by device_slug.
    schematic_graph_cache: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)
```

- [ ] **Step 4: Thread session through `_load_graph` and `mb_schematic_graph`**

In `api/tools/schematic.py`, change `_load_graph`:

```python
def _load_graph(
    device_slug: str,
    memory_root: Path,
    session: "SessionState | None" = None,
) -> tuple[dict | None, str | None]:
    path = memory_root / device_slug / "electrical_graph.json"
    analyzed_path = memory_root / device_slug / "boot_sequence_analyzed.json"
    classified_path = memory_root / device_slug / "nets_classified.json"
    if not path.exists():
        return None, "no_schematic_graph"

    tracked = [p for p in (path, analyzed_path, classified_path) if p.exists()]
    max_mtime = max(p.stat().st_mtime for p in tracked)

    if session is not None:
        cached = session.schematic_graph_cache.get(device_slug)
        if cached is not None and cached[0] >= max_mtime:
            return cached[1], None

    try:
        graph = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None, "malformed_graph"

    # Opus-refined boot analysis overlay (stays verbatim from the pre-cache version).
    if analyzed_path.exists():
        try:
            analyzed = json.loads(analyzed_path.read_text())
            graph["boot_sequence_compiler"] = graph.get("boot_sequence", [])
            graph["boot_sequence"] = analyzed.get("phases", graph.get("boot_sequence", []))
            graph["boot_sequence_source"] = "analyzer"
            graph["boot_analyzer_meta"] = {
                "sequencer_refdes": analyzed.get("sequencer_refdes"),
                "global_confidence": analyzed.get("global_confidence"),
                "model_used": analyzed.get("model_used"),
                "ambiguities": analyzed.get("ambiguities", []),
            }
        except (json.JSONDecodeError, OSError):
            graph["boot_sequence_source"] = "compiler"
    else:
        graph["boot_sequence_source"] = "compiler"

    # Net classification overlay.
    if classified_path.exists():
        try:
            classified = json.loads(classified_path.read_text())
            graph["net_domains"] = classified.get("nets", {})
            graph["net_domains_meta"] = {
                "domain_summary": classified.get("domain_summary", {}),
                "model_used": classified.get("model_used", "regex"),
                "ambiguities": classified.get("ambiguities", []),
            }
        except (json.JSONDecodeError, OSError):
            graph["net_domains"] = {}
            graph["net_domains_meta"] = {}
    else:
        graph["net_domains"] = {}
        graph["net_domains_meta"] = {}

    if session is not None:
        session.schematic_graph_cache[device_slug] = (max_mtime, graph)
    return graph, None
```

Add the TYPE_CHECKING import at file top:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from api.session.state import SessionState
```

In `mb_schematic_graph`, add `session: SessionState | None = None` to the signature and change the `_load_graph` call:

```python
    graph, err = _load_graph(device_slug, memory_root, session=session)
```

- [ ] **Step 5: Forward from runtimes**

In both runtimes, locate every `mb_schematic_graph(` dispatch and add `session=session_state`.

- [ ] **Step 6: Run — PASS**

Run: `.venv/bin/pytest tests/tools/test_schematic_tools.py::test_schematic_graph_cache_hits -v`
Expected: PASS.

- [ ] **Step 7: Full tests**

Run: `.venv/bin/make test`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add api/session/state.py api/tools/schematic.py api/agent/runtime_direct.py api/agent/runtime_managed.py tests/tools/
git commit -m "$(cat <<'EOF'
perf(agent): cache electrical_graph per session

mtime-keyed cache on SessionState.schematic_graph_cache spares the
~2MB JSON reload on every mb_schematic_graph call. Also tracks the
boot_sequence_analyzed.json overlay for combined invalidation.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/session/state.py api/tools/schematic.py api/agent/runtime_direct.py api/agent/runtime_managed.py tests/tools/
```

---

## Task 5 (D1): Durable mirror outcomes

**Why:** `runtime_managed.py:351` fires `mirror_outcome_to_memory` via `asyncio.create_task()` and forgets it. On a fast WS close the coroutine is cancelled mid-flight and the MA memory store never receives the outcome.

**Files:**
- Modify: `api/agent/runtime_managed.py:320-370` (track pending tasks + await on close)
- Modify: `api/tools/validation.py:121-180` (retry loop around upsert)
- Test: `tests/agent/test_runtime_managed_mirror.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/agent/test_runtime_managed_mirror.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path
import pytest

@pytest.mark.asyncio
async def test_mirror_outcome_retries_then_succeeds(monkeypatch, tmp_path: Path):
    """upsert_memory fails twice then succeeds — mirror_outcome_to_memory must retry."""
    from api.tools import validation as val_mod
    from api.tools.validation import mirror_outcome_to_memory
    from api.agent.validation import Outcome

    # Force the flag ON and stub out load_outcome + ensure_memory_store.
    class FakeSettings:
        ma_memory_store_enabled = True
    monkeypatch.setattr(val_mod, "get_settings", lambda: FakeSettings())

    outcome = MagicMock()
    outcome.model_dump_json = lambda indent=2: '{"ok": true}'
    monkeypatch.setattr(val_mod, "load_outcome", lambda **kw: outcome)

    async def fake_ensure(client, slug):
        return "memstore_123"
    monkeypatch.setattr(
        "api.agent.memory_stores.ensure_memory_store",
        fake_ensure,
    )

    calls = {"n": 0}
    async def flaky_upsert(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return {"id": "mem_abc"}
    monkeypatch.setattr(
        "api.agent.memory_stores.upsert_memory",
        flaky_upsert,
    )

    status = await mirror_outcome_to_memory(
        client=MagicMock(), device_slug="demo",
        repair_id="r1", memory_root=tmp_path,
    )
    assert status == "mirrored"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_mirror_task_awaited_on_session_close():
    """Runtime must await pending mirrors in its finally block."""
    from api.agent.runtime_managed import _SessionMirrors

    mirrors = _SessionMirrors()

    slow_calls = {"done": False}
    async def slow_mirror():
        await asyncio.sleep(0.1)
        slow_calls["done"] = True
        return "mirrored"

    mirrors.spawn(slow_mirror())
    await mirrors.wait_drain(timeout=2.0)
    assert slow_calls["done"] is True
```

- [ ] **Step 2: Run — FAIL**

Run: `.venv/bin/pytest tests/agent/test_runtime_managed_mirror.py -v`
Expected: FAIL (no `_SessionMirrors` class; mirror_outcome_to_memory gives up after one attempt).

- [ ] **Step 3: Add retry to `mirror_outcome_to_memory`**

In `api/tools/validation.py`, replace the single `upsert_memory` call (lines ~160-172) with:

```python
    last_exc: Exception | None = None
    delays = (0.5, 1.0, 2.0)
    for attempt in range(len(delays)):
        try:
            result = await upsert_memory(
                client,
                store_id=store_id,
                path=f"/outcomes/{repair_id}.json",
                content=outcome.model_dump_json(indent=2),
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning(
                "[ValidationMirror] upsert attempt %d/%d failed for %s/%s: %s",
                attempt + 1, len(delays), device_slug, repair_id, exc,
            )
            await asyncio.sleep(delays[attempt])
            continue
        if result is not None:
            logger.info(
                "[ValidationMirror] mirrored %s/%s on attempt %d",
                device_slug, repair_id, attempt + 1,
            )
            return "mirrored"
        await asyncio.sleep(delays[attempt])
    logger.warning(
        "[ValidationMirror] giving up after %d attempts for %s/%s: %s",
        len(delays), device_slug, repair_id, last_exc,
    )
    return "error:upsert_failed"
```

Add `import asyncio` at the top of `api/tools/validation.py` if not already.

- [ ] **Step 4: Add `_SessionMirrors` helper**

Near the top of `api/agent/runtime_managed.py` (after the imports), add:

```python
class _SessionMirrors:
    """Tracks fire-and-forget mirror tasks and awaits them on session close."""

    def __init__(self) -> None:
        self._pending: set[asyncio.Task] = set()

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return task

    async def wait_drain(self, timeout: float = 5.0) -> None:
        if not self._pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._pending, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[Diag-MA] %d mirror tasks still pending after %.1fs — cancelling",
                len(self._pending), timeout,
            )
            for task in list(self._pending):
                task.cancel()
```

- [ ] **Step 5: Replace the `create_task` call**

In `api/agent/runtime_managed.py` around line 351, find the `asyncio.create_task(mirror_outcome_to_memory(...))` block and replace with `session_mirrors.spawn(mirror_outcome_to_memory(...))`.

Create the `session_mirrors` instance near the start of `run_diagnostic_session_managed` (right after `client = AsyncAnthropic(...)`):

```python
    session_mirrors = _SessionMirrors()
```

Thread `session_mirrors` into `_dispatch_tool` or whatever helper invokes the mirror (grep for where `asyncio.create_task` appears around line 351). Easiest: pass `session_mirrors` as a closure-captured local.

- [ ] **Step 6: Drain on close**

Find the `finally:` block at the end of `run_diagnostic_session_managed` (or the session cleanup point — grep `finally`). Add:

```python
    finally:
        await session_mirrors.wait_drain(timeout=5.0)
        # ...existing cleanup (ws close, etc.)
```

If no `finally` exists at the right layer, wrap the main body in `try: ... finally: await session_mirrors.wait_drain(...)`.

- [ ] **Step 7: Run — PASS**

Run: `.venv/bin/pytest tests/agent/test_runtime_managed_mirror.py -v`
Expected: PASS.

- [ ] **Step 8: Full tests**

Run: `.venv/bin/make test`
Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add api/agent/runtime_managed.py api/tools/validation.py tests/agent/test_runtime_managed_mirror.py
git commit -m "$(cat <<'EOF'
fix(agent): durable mirror outcomes on WS close

Retry 3x with exp backoff (0.5s, 1s, 2s) inside mirror_outcome_to_memory,
and track pending mirror tasks on a _SessionMirrors helper awaited in
the session finally block (5s timeout). Disk writes already durable;
this hardens the MA store mirror path for long-running repair flows.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/agent/runtime_managed.py api/tools/validation.py tests/agent/test_runtime_managed_mirror.py
```

---

## Task 6 (D2): Memory store attached read-only

**Why:** the MA memory store is currently attached `read_write`. The agent's builtin `write`/`edit` tools could corrupt field-report or knowledge files. All writes already go through the direct HTTP API (`upsert_memory` in `api/agent/memory_stores.py` + mirror paths); code needs no write-mount.

**Files:**
- Modify: `api/agent/runtime_managed.py:446` (flip `access` to `read_only`)
- Test: `tests/agent/test_runtime_managed_access.py` (new — assert the session-create payload shape)

- [ ] **Step 1: Write the failing test**

Create `tests/agent/test_runtime_managed_access.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from pathlib import Path

@pytest.mark.asyncio
async def test_memory_store_attached_read_only(monkeypatch, tmp_path):
    """Session-create payload must attach the memory store with access='read_only'."""
    from api.agent import runtime_managed as rm

    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    ws.receive_text = AsyncMock(side_effect=Exception("stop early"))

    class FakeSettings:
        anthropic_api_key = "sk-test"
        memory_root = str(tmp_path)
        ma_memory_store_enabled = True
    monkeypatch.setattr(rm, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(rm, "load_managed_ids", lambda: {"environment_id": "env_x"})
    monkeypatch.setattr(rm, "get_agent", lambda ids, tier: {"id": "agent_x", "version": 1, "model": "claude-haiku-4-5"})

    async def fake_ensure(client, slug):
        return "memstore_999"
    monkeypatch.setattr(rm, "ensure_memory_store", fake_ensure)

    captured = {}
    class FakeSessions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            raise Exception("stop here")  # bail before streaming
        async def retrieve(self, sid):  # noqa: D401
            raise Exception("none")
    class FakeBeta:
        sessions = FakeSessions()
    class FakeClient:
        beta = FakeBeta()
    monkeypatch.setattr(rm, "AsyncAnthropic", lambda api_key: FakeClient())

    # Run until session create raises; we only care about the payload.
    try:
        await rm.run_diagnostic_session_managed(ws, "demo", tier="fast")
    except Exception:
        pass

    assert "resources" in captured, "memory store must be attached"
    resource = captured["resources"][0]
    assert resource["type"] == "memory_store"
    assert resource["access"] == "read_only", f"expected read_only, got {resource['access']!r}"
```

- [ ] **Step 2: Run — FAIL**

Run: `.venv/bin/pytest tests/agent/test_runtime_managed_access.py -v`
Expected: FAIL (`access == 'read_write'`).

- [ ] **Step 3: Flip the flag**

In `api/agent/runtime_managed.py:446`, change `"access": "read_write"` → `"access": "read_only"`.

Update the `prompt:` line on the same resource to reflect reality (strip the "write durable learnings at the end" phrase, which is now misleading):

```python
                "prompt": (
                    "Repair history for this specific device. Read it at "
                    "the start of diagnosis. New findings are written by "
                    "the server — you do not write through this mount."
                ),
```

- [ ] **Step 4: Run — PASS**

Run: `.venv/bin/pytest tests/agent/test_runtime_managed_access.py -v`
Expected: PASS.

- [ ] **Step 5: Full tests**

Run: `.venv/bin/make test`
Expected: all green. If a test was asserting on `"access": "read_write"`, update it to match — grep for `read_write` in `tests/` and fix occurrences tied to the memory store resource (not unrelated ones).

- [ ] **Step 6: Commit**

```bash
git add api/agent/runtime_managed.py tests/agent/test_runtime_managed_access.py
git commit -m "$(cat <<'EOF'
fix(agent): attach MA memory store read-only

Agent's builtin write/edit tools could corrupt field-report or
knowledge files on the mount; all code writes route through the
direct HTTP API (upsert_memory). read_only is the safer default.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/agent/runtime_managed.py tests/agent/test_runtime_managed_access.py
```

---

## Task 7 (M1): Bimodal mount prompt — sharpen + test

**Why:** `scripts/bootstrap_managed_agent.py:115-141` already contains bimodal language, but nothing asserts the wording is still there after future edits, and the prompt has no concrete `grep` example for the agent to anchor on.

**Files:**
- Modify: `scripts/bootstrap_managed_agent.py:115-141` (add one concrete grep example)
- Test: `tests/agent/test_bootstrap_prompt.py` (new — asserts the bimodal block + example survive)

- [ ] **Step 1: Write the failing test**

Create `tests/agent/test_bootstrap_prompt.py`:

```python
import importlib.util
from pathlib import Path


def _load_bootstrap_module():
    spec = importlib.util.spec_from_file_location(
        "bootstrap_managed_agent",
        Path(__file__).resolve().parents[2] / "scripts" / "bootstrap_managed_agent.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_system_prompt_has_bimodal_block():
    mod = _load_bootstrap_module()
    prompt = mod.SYSTEM_PROMPT
    assert "Mode mount" in prompt
    assert "Mode disk-only" in prompt
    assert "/mnt/memory/" in prompt
    assert "mb_list_findings" in prompt


def test_system_prompt_has_grep_example():
    mod = _load_bootstrap_module()
    prompt = mod.SYSTEM_PROMPT
    assert "grep -r" in prompt or "grep \"" in prompt, (
        "prompt should include a concrete grep example so the agent has "
        "a pattern to imitate in Mode mount"
    )
```

- [ ] **Step 2: Run — FAIL**

Run: `.venv/bin/pytest tests/agent/test_bootstrap_prompt.py -v`
Expected: FAIL on the grep example test (bimodal block likely passes, grep example probably missing).

- [ ] **Step 3: Add a concrete grep example**

Find the `SYSTEM_PROMPT` variable in `scripts/bootstrap_managed_agent.py`. Locate the Mode mount block (around line 123-133) and add a concrete example after the "N'appelle JAMAIS `mb_list_findings`" sentence:

```
   Exemple de lookup en mode mount (remplace `{store}` par le nom réel
   du répertoire affiché dans la note d'attachement) :

       grep -r "U1501" /mnt/memory/{store}/field_reports/

   ou, pour lister les findings d'un symptôme :

       grep -l "no-power" /mnt/memory/{store}/field_reports/
```

- [ ] **Step 4: Run — PASS**

Run: `.venv/bin/pytest tests/agent/test_bootstrap_prompt.py -v`
Expected: PASS on both tests.

- [ ] **Step 5: Full tests**

Run: `.venv/bin/make test`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add scripts/bootstrap_managed_agent.py tests/agent/test_bootstrap_prompt.py
git commit -m "$(cat <<'EOF'
chore(agent): concrete grep example in bimodal system prompt

Adds a worked grep pattern under Mode mount so the agent has something
to imitate instead of inferring the call shape. Locks the bimodal
block structure behind a test that asserts both modes + the example
survive future edits.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- scripts/bootstrap_managed_agent.py tests/agent/test_bootstrap_prompt.py
```

---

## Task 8 (P3): Per-phase token analytics

**Why:** no structured visibility into token / cache usage per pipeline phase. Required to measure Task 9 (auditor cache) before/after.

**Files:**
- Create: `api/pipeline/telemetry/__init__.py` (empty)
- Create: `api/pipeline/telemetry/token_stats.py` (dataclass + accumulator + CLI)
- Modify: `api/pipeline/tool_call.py:23-140` (accept optional `stats: PhaseTokenStats | None`)
- Modify: `api/pipeline/orchestrator.py:68-330` (instantiate stats per phase + write to disk at end)
- Create: `tests/pipeline/telemetry/__init__.py` (empty)
- Create: `tests/pipeline/telemetry/test_token_stats.py` (accumulator unit test + CLI smoke test)

- [ ] **Step 1: Write the failing unit test**

Create `tests/pipeline/telemetry/test_token_stats.py`:

```python
from pathlib import Path
import json

def test_phase_token_stats_accumulates():
    from api.pipeline.telemetry.token_stats import PhaseTokenStats
    stats = PhaseTokenStats(phase="writers")
    stats.record(input_tokens=1000, output_tokens=500, cache_read=0, cache_write=800, duration_s=1.2)
    stats.record(input_tokens=900, output_tokens=300, cache_read=800, cache_write=0, duration_s=0.8)
    assert stats.input_tokens == 1900
    assert stats.output_tokens == 800
    assert stats.cache_read_input_tokens == 800
    assert stats.cache_creation_input_tokens == 800
    assert round(stats.duration_s, 2) == 2.0

def test_write_and_read_token_stats(tmp_path: Path):
    from api.pipeline.telemetry.token_stats import (
        PhaseTokenStats, write_token_stats, read_token_stats,
    )
    stats = [
        PhaseTokenStats(phase="scout", input_tokens=500, output_tokens=4000),
        PhaseTokenStats(phase="auditor", input_tokens=12000, output_tokens=1500, cache_read_input_tokens=10000),
    ]
    path = tmp_path / "token_stats.json"
    write_token_stats(path, stats)
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["phases"][1]["cache_read_input_tokens"] == 10000

    loaded = read_token_stats(path)
    assert len(loaded) == 2
    assert loaded[0].phase == "scout"
```

- [ ] **Step 2: Run — FAIL**

Run: `.venv/bin/pytest tests/pipeline/telemetry/test_token_stats.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Create the telemetry module**

Create `api/pipeline/telemetry/__init__.py` — empty.

Create `api/pipeline/telemetry/token_stats.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Per-phase token + cache accounting for the knowledge-factory pipeline.

Each phase (scout, registry, writer_*, auditor, auditor_rev_N) gets a
PhaseTokenStats instance. The tool_call helper records into it on every
Anthropic call; the orchestrator writes the full list to
memory/{slug}/token_stats.json at pipeline end.

A tiny CLI renders a readable table: python -m api.pipeline.telemetry.token_stats --slug=<slug>
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class PhaseTokenStats:
    phase: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    duration_s: float = 0.0
    call_count: int = 0

    def record(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read: int = 0,
        cache_write: int = 0,
        duration_s: float = 0.0,
    ) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_input_tokens += cache_read
        self.cache_creation_input_tokens += cache_write
        self.duration_s += duration_s
        self.call_count += 1


def write_token_stats(path: Path, stats: list[PhaseTokenStats]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"phases": [asdict(s) for s in stats]}
    path.write_text(json.dumps(payload, indent=2))


def read_token_stats(path: Path) -> list[PhaseTokenStats]:
    data = json.loads(path.read_text())
    return [PhaseTokenStats(**entry) for entry in data["phases"]]


def render_table(stats: list[PhaseTokenStats]) -> str:
    headers = ("phase", "calls", "in", "out", "cache_r", "cache_w", "hit%", "sec")
    rows = [headers]
    for s in stats:
        total_in = s.input_tokens + s.cache_read_input_tokens
        hit_pct = (s.cache_read_input_tokens / total_in * 100) if total_in else 0.0
        rows.append((
            s.phase,
            str(s.call_count),
            str(s.input_tokens),
            str(s.output_tokens),
            str(s.cache_read_input_tokens),
            str(s.cache_creation_input_tokens),
            f"{hit_pct:.0f}",
            f"{s.duration_s:.1f}",
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(headers))]
    out = []
    for i, row in enumerate(rows):
        out.append("  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row)))
        if i == 0:
            out.append("  ".join("-" * widths[j] for j in range(len(headers))))
    return "\n".join(out)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render per-phase token stats for a knowledge-pack run.",
    )
    parser.add_argument("--slug", required=True)
    parser.add_argument("--memory-root", default="memory")
    args = parser.parse_args(argv)
    path = Path(args.memory_root) / args.slug / "token_stats.json"
    if not path.exists():
        print(f"no token_stats.json found at {path}", file=sys.stderr)
        return 1
    stats = read_token_stats(path)
    print(render_table(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
```

Create `tests/pipeline/telemetry/__init__.py` — empty.

- [ ] **Step 4: Run unit test — PASS**

Run: `.venv/bin/pytest tests/pipeline/telemetry/test_token_stats.py -v`
Expected: PASS.

- [ ] **Step 5: Integrate with `call_with_forced_tool`**

In `api/pipeline/tool_call.py`, extend the signature:

```python
async def call_with_forced_tool(
    *,
    client: AsyncAnthropic,
    model: str,
    system: str | list[dict],
    messages: list[dict],
    tools: list[dict],
    forced_tool_name: str,
    output_schema: type[T],
    max_attempts: int = 2,
    max_tokens: int = 16000,
    log_label: str = "tool_call",
    stats: "PhaseTokenStats | None" = None,
) -> T:
```

Add at top of file:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from api.pipeline.telemetry.token_stats import PhaseTokenStats
```

Inside the loop, after the existing `logger.info("[%s] attempt=%d usage ...")` block, record to stats:

```python
        if stats is not None:
            stats.record(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_read=cache_read,
                cache_write=cache_write,
            )
```

- [ ] **Step 6: Instrument the orchestrator**

In `api/pipeline/orchestrator.py`:

- Add import at top: `from api.pipeline.telemetry.token_stats import PhaseTokenStats, write_token_stats`.
- At the start of `generate_knowledge_pack`, create `phase_stats: list[PhaseTokenStats] = []`.
- Before each phase block, create a `PhaseTokenStats(phase="scout")`, `…(phase="registry")`, `…(phase="writer_cartographe")`, `…(phase="writer_clinicien")`, `…(phase="writer_lexicographe")`, `…(phase="auditor")`, `…(phase=f"auditor_rev_{rounds_used}")` — append each to `phase_stats`.
- Thread each into its `call_with_forced_tool(...)` / `run_auditor(...)` / `run_scout(...)` / `run_registry_builder(...)` / `run_writers_parallel(...)` call via a new `stats=` kwarg. Propagate down the call chain (e.g. `run_auditor` gets a `stats: PhaseTokenStats | None = None` param which it forwards to `call_with_forced_tool`). For `run_writers_parallel`, accept three separate stats objects (one per writer) via a `{"cartographe": ..., "clinicien": ..., "lexicographe": ...}` dict.
- After the pipeline finishes (after the `seed_memory_store_from_pack` call on line 316), write the file:

```python
        write_token_stats(pack_dir / "token_stats.json", phase_stats)
```

For `run_scout`: it likely calls `messages.create` directly with web_search (not `call_with_forced_tool`). Record into scout stats manually around the `client.messages.create`/`stream` call — use the same pattern (pull `response.usage` fields after the call returns).

- [ ] **Step 7: Integration smoke test**

Append to `tests/pipeline/telemetry/test_token_stats.py`:

```python
def test_render_table_has_header_row():
    from api.pipeline.telemetry.token_stats import PhaseTokenStats, render_table
    out = render_table([PhaseTokenStats(phase="scout", input_tokens=100, output_tokens=50)])
    lines = out.splitlines()
    assert lines[0].startswith("phase")
    assert "scout" in lines[2]
```

Run: `.venv/bin/pytest tests/pipeline/telemetry/ -v`
Expected: PASS.

- [ ] **Step 8: Full tests**

Run: `.venv/bin/make test`
Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add api/pipeline/telemetry/ api/pipeline/tool_call.py api/pipeline/orchestrator.py api/pipeline/scout.py api/pipeline/writers.py api/pipeline/registry.py api/pipeline/auditor.py tests/pipeline/telemetry/
git commit -m "$(cat <<'EOF'
feat(pipeline): per-phase token analytics

New api/pipeline/telemetry module aggregates usage (input, output,
cache_read, cache_creation) per phase of generate_knowledge_pack.
Results written to memory/{slug}/token_stats.json at pipeline end.
CLI: python -m api.pipeline.telemetry.token_stats --slug=<slug>.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/telemetry/ api/pipeline/tool_call.py api/pipeline/orchestrator.py api/pipeline/scout.py api/pipeline/writers.py api/pipeline/registry.py api/pipeline/auditor.py tests/pipeline/telemetry/
```

Paths in the `git add` and `git commit -- …` lists include only files touched by this task. If you did not modify e.g. `registry.py` (no `stats=` wiring needed there), drop it from the list.

---

## Task 9 (P1+P2): Auditor cache block + drift

**Why:** the Auditor currently renders the full pack JSON inline in its user message every revision round. Restructuring into two content blocks (A cached, B delta) makes round ≥2 within 5 min a cache-read hit.

**Files:**
- Modify: `api/pipeline/prompts.py:431-462` (split `AUDITOR_USER_TEMPLATE` into two parts)
- Modify: `api/pipeline/auditor.py:41-94` (build `messages` with `[{"type":"text", "text":..., "cache_control":{"type":"ephemeral"}}, {"type":"text", "text":...}]`)
- Modify: `api/pipeline/tool_call.py:27` (the function already accepts arbitrary `messages`; verify it forwards structured content correctly — it should, no change needed)
- Modify: `api/pipeline/orchestrator.py` (pass a `revision_brief` for round ≥2, default empty string on round 1)
- Test: `tests/pipeline/test_auditor_cache.py` (new — integration test with mocked client asserting cache_control shape)

- [ ] **Step 1: Write the failing test**

Create `tests/pipeline/test_auditor_cache.py`:

```python
from unittest.mock import AsyncMock, MagicMock
import pytest

@pytest.mark.asyncio
async def test_auditor_user_message_has_cached_context_block():
    """User message must be a list of two content blocks: [A cached, B delta]."""
    from api.pipeline.auditor import run_auditor
    from api.pipeline.schemas import (
        Registry, KnowledgeGraph, RulesSet, Dictionary, AuditVerdict,
    )

    captured = {}
    async def fake_call(*, client, model, system, messages, tools, forced_tool_name, output_schema, **kw):
        captured["messages"] = messages
        return AuditVerdict(
            overall_status="APPROVED",
            consistency_score=1.0,
            drift_report=[],
            files_to_rewrite=[],
            revision_brief="",
        )

    from api.pipeline import auditor as auditor_mod
    import types
    monkeypatch_target = auditor_mod
    # replace the module-local name
    auditor_mod.call_with_forced_tool = fake_call  # type: ignore[attr-defined]

    await run_auditor(
        client=MagicMock(), model="claude-opus-4-7",
        device_label="Demo",
        registry=Registry(components=[], signals=[], taxonomy={"brand": "x", "model": "y", "version": "z"}),
        knowledge_graph=KnowledgeGraph(nodes=[], edges=[]),
        rules=RulesSet(rules=[]),
        dictionary=Dictionary(entries=[]),
        precomputed_drift=[],
    )

    msg = captured["messages"][0]
    assert isinstance(msg["content"], list), "content must be structured blocks for cache_control to work"
    assert len(msg["content"]) >= 2
    block_a = msg["content"][0]
    assert block_a["type"] == "text"
    assert block_a.get("cache_control", {}).get("type") == "ephemeral", "block A must be ephemeral-cached"
    # Block A contains the heavy JSON; block B is the revision_brief delta.
    assert "Registry" in block_a["text"] or "registry" in block_a["text"]
```

- [ ] **Step 2: Run — FAIL**

Run: `.venv/bin/pytest tests/pipeline/test_auditor_cache.py -v`
Expected: FAIL (current `user_prompt` is a plain string).

- [ ] **Step 3: Split the prompt template**

In `api/pipeline/prompts.py`, replace `AUDITOR_USER_TEMPLATE` with two templates. Remove the old single-string template and add:

```python
AUDITOR_USER_CONTEXT_TEMPLATE = """\
Audit the following knowledge pack for device: {device_label}

# Pre-computed vocabulary drift (code-level set diff — GROUND TRUTH)
```json
{precomputed_drift_json}
```

# Registry
```json
{registry_json}
```

# Knowledge graph
```json
{knowledge_graph_json}
```

# Rules
```json
{rules_json}
```

# Dictionary
```json
{dictionary_json}
```
"""

AUDITOR_USER_DIRECTIVE_TEMPLATE = """\
{revision_brief_block}Include every pre-computed drift entry verbatim in your `drift_report`, add your
own cross-file coherence and plausibility findings, and submit your verdict via
`submit_audit_verdict`. No other output.
"""
```

- [ ] **Step 4: Build structured messages in `run_auditor`**

In `api/pipeline/auditor.py`, replace the `user_prompt = AUDITOR_USER_TEMPLATE.format(...)` block and the downstream `messages=[{"role": "user", "content": user_prompt}]` with:

```python
    from api.pipeline.prompts import (
        AUDITOR_USER_CONTEXT_TEMPLATE,
        AUDITOR_USER_DIRECTIVE_TEMPLATE,
    )

    context_text = AUDITOR_USER_CONTEXT_TEMPLATE.format(
        device_label=device_label,
        precomputed_drift_json=precomputed_drift_json,
        registry_json=registry.model_dump_json(indent=2),
        knowledge_graph_json=knowledge_graph.model_dump_json(indent=2),
        rules_json=rules.model_dump_json(indent=2),
        dictionary_json=dictionary.model_dump_json(indent=2),
    )
    revision_block = ""
    if revision_brief:
        revision_block = f"# Revision brief\n{revision_brief}\n\n"
    directive_text = AUDITOR_USER_DIRECTIVE_TEMPLATE.format(
        revision_brief_block=revision_block,
    )

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": context_text,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": directive_text,
                },
            ],
        }
    ]
```

Add `revision_brief: str = ""` to the `run_auditor` signature (optional; default empty keeps round-1 compatibility).

Update the `call_with_forced_tool(messages=messages, ...)` call to use the new `messages` list.

- [ ] **Step 5: Thread `revision_brief` from orchestrator**

In `api/pipeline/orchestrator.py`, inside the `while True:` audit loop, pass the previous verdict's `revision_brief` (if any) on round ≥2:

```python
            previous_brief = verdict.revision_brief if rounds_used > 0 else ""
            verdict = await run_auditor(
                client=client,
                model=models_by_role["auditor"],
                device_label=device_label,
                registry=registry,
                knowledge_graph=kg,
                rules=rules,
                dictionary=dictionary,
                precomputed_drift=code_drift,
                revision_brief=previous_brief,
            )
```

(Position the `previous_brief` read *before* any reassignment of `verdict` inside the loop — the read is only meaningful on round ≥1 where `verdict` was set by the previous iteration.)

- [ ] **Step 6: Run — PASS**

Run: `.venv/bin/pytest tests/pipeline/test_auditor_cache.py -v`
Expected: PASS.

- [ ] **Step 7: Verify drift tests still pass**

Run: `.venv/bin/pytest tests/pipeline/test_drift.py -v`
Expected: PASS (pure-Python drift logic is unchanged; P2's "drift in Block A" is purely where we place the serialized JSON — still computed by `compute_drift`).

- [ ] **Step 8: Full tests**

Run: `.venv/bin/make test`
Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add api/pipeline/prompts.py api/pipeline/auditor.py api/pipeline/orchestrator.py tests/pipeline/test_auditor_cache.py
git commit -m "$(cat <<'EOF'
perf(pipeline): explicit cache_control on auditor context

Splits the auditor user message into Block A (registry + kg + rules +
dictionary + drift JSON, ephemeral cache_control) and Block B (revision
brief delta). Round >=2 within the 5-min cache TTL becomes a cache-read
hit instead of a fresh billed ~12k-token resend. Drift JSON rides with
Block A since it is stable across revisions of the same writer set.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/prompts.py api/pipeline/auditor.py api/pipeline/orchestrator.py tests/pipeline/test_auditor_cache.py
```

---

## Final verification

- [ ] **Step F1: Full test suite**

Run: `.venv/bin/make test`
Expected: all green, no flakes.

- [ ] **Step F2: Lint**

Run: `.venv/bin/make lint`
Expected: no warnings.

- [ ] **Step F3: Inspect the commit history**

Run: `git log --oneline -12`
Expected: 9 new commits on top of `03c469b` (the spec commit), one per task, messages matching the patterns above. No mixed `api/agent/` + `api/pipeline/` in any single commit.

- [ ] **Step F4: Token-stats smoke (optional, requires API key)**

If `.env` has `ANTHROPIC_API_KEY`, run a small pack to confirm the telemetry artefact lands:

```bash
.venv/bin/python -c "import asyncio; from api.pipeline.orchestrator import generate_knowledge_pack; asyncio.run(generate_knowledge_pack('Test Device'))"
cat memory/test-device/token_stats.json | head -30
.venv/bin/python -m api.pipeline.telemetry.token_stats --slug=test-device
```

Optional because it burns real tokens; skip on the first pass and confirm only the unit-level coverage.

---

## YAGNI restatement

No async atomic writes for caches. No external metric collector. M1 does not rewrite the whole prompt — only adds one example + a test lock. P3 does not unify with the diagnostic runtime's accounting (separate feature). No changes to the core 4-phase pipeline structure. No pipeline migration to Managed Agents.
