# Agent + Pipeline Optimizations — Design

**Date:** 2026-04-24
**Status:** Draft — pending implementation plan
**Scope:** Diagnostic runtime (`api/agent/`) + knowledge factory pipeline (`api/pipeline/`)

## Motivation

An audit of the two agent-heavy code paths surfaced ten concrete inefficiencies.
None are bugs — the system works correctly today — but they waste disk I/O,
tokens, and risk data loss at WebSocket close. This document proposes a
coordinated fix for all ten, grouped by theme and sequenced by dependency.

The audit preserved the CLAUDE.md architectural invariant: the pipeline stays
on `messages.create` + forced tool choice (no Managed Agents migration). All
changes are micro-optimizations within the existing seams.

## Goals

- Cut per-turn disk I/O in the diagnostic runtime by caching pack JSON,
  component lookups, profile, and electrical graph data in `SessionState`.
- Make field-report mirroring durable under WebSocket disconnection.
- Harden MA memory store access (read-only for agent, direct HTTP for code).
- Exploit the `/mnt/memory/` mount bimodally so field-report lookups skip a
  tool call when the mount is live.
- Make the Auditor's revision loop quasi-free when rounds land within 5 min
  by restructuring its user message into explicit cache blocks.
- Instrument token usage per pipeline phase to measure future optimizations.

## Non-Goals

- No pipeline migration to Managed Agents (explicit CLAUDE.md rule,
  audit-validated in 2026-04).
- No new parser or board format work.
- No UI changes.
- No new skill, tool, or event surfaces.
- No change to the 4-phase pipeline structure (Scout/Registry/Writers/Auditor).

## Design

### Theme A — Runtime cache (4 items)

#### R1 — Pack cache in `SessionState`

**Problem.** `api/agent/tools.py::_load_pack()` reads ~2.4MB of JSON from
disk on every `mb_*` invocation. A typical turn makes ~10 such calls, so
~24MB of disk I/O per turn.

**Solution.** Add `SessionState.pack_cache: dict[str, tuple[float, dict]]`
keyed by `device_slug`, storing `(max_mtime_across_pack_files, pack_dict)`.
On cache hit, `stat()` the four pack files (`registry.json`,
`knowledge_graph.json`, `rules.json`, `dictionary.json`); if the max mtime
moved, invalidate and reload. `mb_expand_knowledge` explicitly invalidates
after a successful pack mutation (it already writes files back to disk).

**Files.** `api/agent/tools.py`, `api/session/state.py`,
`tests/agent/test_tools.py`.

#### R2 — `mb_get_component` LRU

**Problem.** The same refdes is queried 3–5× per session as the agent
re-confirms lookups.

**Solution.** Per-session `OrderedDict[str, dict]` of size 64.
`mb_get_component(refdes)` checks the cache on exact refdes match before
running the usual resolution. Cache is cleared when the board reloads.
Fuzzy misses (`closest_matches` payloads) are **not** cached — their shape
depends on small input variance and the cache-hit rate wouldn't justify it.

**Files.** `api/agent/tools.py`, `api/session/state.py`,
`tests/agent/test_tools.py`.

#### R3 — Lazy `profile_get`

**Problem.** `api/profile/tools.py::profile_get()` reads disk on every
session open, even if the file hasn't changed.

**Solution.** Cache the profile in `SessionState.profile_cache` with an
mtime check on the single profile file. Reload only if mtime moved.

**Files.** `api/profile/tools.py`, `api/session/state.py`,
`tests/profile/test_tools.py`.

#### R4 — `electrical_graph` cache

**Problem.** `electrical_graph.json` (~2MB) is reloaded on every
`mb_schematic_graph` call.

**Solution.** `SessionState.schematic_graph_cache: dict[str, tuple[float, dict]]`
keyed by `device_slug`, mtime-checked on the single file.

**Files.** `api/agent/tools.py`, `api/session/state.py`,
`tests/agent/test_tools.py`.

### Theme B — Durability (2 items)

#### D1 — Mirror outcome durability

**Problem.** `runtime_managed.py:351` launches
`mirror_outcome_to_memory(...)` via `asyncio.create_task()` — fire-and-forget.
If the WebSocket closes before the task completes, the MA memory store
never receives the field report (the disk write still happens, so this
is a mirror-availability issue, not a data-loss issue — the disk remains
the durable source of truth).

**Solution.**
1. Track pending mirror tasks on the per-session context:
   `_pending_mirrors: set[asyncio.Task]`. Each task adds itself on
   creation and removes itself on completion (via done-callback).
2. In the session's `finally` block (WS close or exception),
   `await asyncio.wait(pending, timeout=5.0)`. Cancel stragglers after
   the timeout; log warning on cancel.
3. Inside `mirror_outcome_to_memory`, wrap the HTTP call in a retry
   loop: 3 attempts, exponential backoff (0.5s → 1s → 2s). Final failure
   logs an error; disk write stays intact.

**Files.** `api/agent/runtime_managed.py`, `api/agent/field_reports.py`,
`tests/agent/test_runtime_managed_mirror.py` (new).

#### D2 — Memory store attached `read_only`

**Problem.** `runtime_managed.py:446` attaches the MA memory store with
`access="read_write"`. The agent has write access via the `agent_toolset_20260401`
builtins (`write`, `edit`) — a foot-gun if the agent hallucinates an edit
on a field-report or knowledge file. No versioning or rollback exists.

**Solution.**
- Attach the memory store with `access="read_only"`.
- All writes already go through the direct HTTP API in
  `ensure_memory_store` and `mirror_outcome_to_memory`; no code change
  needed for the write path beyond the access flip.
- The agent's `read` / `grep` builtins continue to work over the read-only
  mount.

**Files.** `api/agent/runtime_managed.py`,
`tests/agent/test_runtime_managed_access.py` (new, asserts attach shape).

### Theme C — Mount bimodal (1 item)

#### M1 — Bimodal system prompt for field-reports

**Problem.** The bootstrap enables `read` / `grep` on `/mnt/memory/` but
the agent keeps calling `mb_list_findings` unconditionally. Mount = wasted
RAM.

**Solution.** Prompt-only change in
`scripts/bootstrap_managed_agent.py`:
- Tighten the bimodal language so that when the `/mnt/memory/` mount is
  reachable, the agent **prefers** `grep /mnt/memory/{store}/field_reports/`
  over the `mb_list_findings` tool.
- Detection remains a harmless first-turn `read /mnt/memory/{store}/`
  probe. If it succeeds, the agent stays on the mount path for the rest
  of the session.
- Add one worked example to the prompt showing the grep pattern.

No runtime code changes (no new tool, no new dispatch logic). The agent's
choice is guided by prompt + existing builtins.

**Files.** `scripts/bootstrap_managed_agent.py`,
`tests/agent/test_bootstrap_prompt.py` (new — asserts the prompt contains
the bimodal block + example).

### Theme D — Pipeline optimizations (3 items)

#### P1 + P2 — Auditor cached context + drift

**Problem.** `api/pipeline/auditor.py` sends the full
`registry + knowledge_graph + rules + dictionary + drift` payload inline
on every revision round. When a round lands more than 5 minutes after the
previous one, the ephemeral cache has expired and all ~12k tokens are
billed fresh. P1 and P2 share a single structural fix — treated as one
item with two effects.

**Solution.** Restructure the Auditor's user message into two content
blocks:

- **Block A (cached)** — `registry.json` + `knowledge_graph.json` +
  `rules.json` + `dictionary.json` + drift JSON, all concatenated and
  wrapped with `cache_control: {"type": "ephemeral"}`. This block is
  stable across revision rounds of the **same** writer set.
- **Block B (non-cached)** — the revision brief delta (what writers
  changed since the last round), empty on round 1.

On round ≥2 within the 5-min window, `cache_read_input_tokens` covers
Block A; only Block B is new input. Drift JSON is pre-computed once per
writer-set and lives inside Block A — it doesn't change between rounds
of the same writer outputs.

**Files.** `api/pipeline/auditor.py`, `api/pipeline/drift.py` (minor
refactor to return a cache-stable serialization),
`tests/pipeline/test_auditor_cache.py` (new integ test: run 2 rounds
within 5min, assert `cache_read_input_tokens > 0` on round 2).

#### P3 — Per-phase token analytics

**Problem.** No structured visibility into cache hit rate per pack phase.
Existing logs in `tool_call.py:89-99` emit counts per call but don't
aggregate.

**Solution.** New module `api/pipeline/telemetry/token_stats.py` with a
single dataclass:

```python
@dataclass
class PhaseTokenStats:
    phase: str                          # "scout" | "registry" | "writer_cartographe" | "auditor" | "auditor_rev_1" | ...
    input_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    output_tokens: int
    duration_s: float
```

Accumulated by the orchestrator (one entry per phase, one entry per
revision round). Written to `memory/{slug}/token_stats.json` at pipeline
end (overwritten on re-run). CLI:

```bash
python -m api.pipeline.telemetry.token_stats --slug=<slug>
```

Prints a table (phase, tokens in/out, cache hit %, duration). Existing
logging unchanged — this is purely an additional artefact.

**Files.** `api/pipeline/telemetry/__init__.py`,
`api/pipeline/telemetry/token_stats.py` (new),
`api/pipeline/orchestrator.py` (hook points at each phase boundary +
end-of-run writeout), `tests/pipeline/telemetry/test_token_stats.py` (new).

## Execution Order

Four self-contained sprints:

1. **Sprint 1 — Runtime cache** (R1 → R2 → R3 → R4). Independent; any
   order works. Each item = one commit. ~1 day total.
2. **Sprint 2 — Durability** (D1 → D2). D1 first so the retry path is
   proven before we flip the access flag. Each item = one commit. ~0.5 day.
3. **Sprint 3 — Mount bimodal** (M1). Prompt-only; one commit. ~0.25 day.
4. **Sprint 4 — Pipeline** (P3 → P1+P2). P3 first so we can measure P1+P2
   before/after. P3 = one commit; P1+P2 = one commit (shared fix). ~1 day.

Total: 10 commits, ~3 days of focused work.

## Test Discipline

- TDD per item: red test → implementation → green.
- `make test` passes after every item (not every sprint).
- Integration tests where tool-call behaviour or WS flow matters
  (D1, D2, M1, P1+P2).
- Unit tests for pure logic (R1–R4 cache behaviour, P3 accumulator).
- Perf items (R1, R2, R4, P1+P2) assert on a measurable quantity:
  number of disk reads, cache hit bytes, `cache_read_input_tokens`.

## Commit Discipline

- One commit per item (10 commits total — not 1, not 4).
- Conventional-commits style: `perf(agent):`, `fix(agent):`,
  `perf(pipeline):`, `feat(pipeline):`, `chore(agent):` as appropriate.
- **Never** mix `api/agent/` and `api/pipeline/` in one commit
  (CLAUDE.md rule).
- Always pass explicit paths to `git commit -- path...` (parallel-agent
  safety, CLAUDE.md rule).
- No `git push` without explicit authorization from Alexis.

## Files Touched (summary)

| File | Items |
|---|---|
| `api/agent/tools.py` | R1, R2, R4 |
| `api/agent/runtime_managed.py` | D1, D2 |
| `api/agent/field_reports.py` | D1 |
| `api/profile/tools.py` | R3 |
| `api/session/state.py` | R1, R2, R3, R4 (cache fields) |
| `api/pipeline/auditor.py` | P1+P2 |
| `api/pipeline/drift.py` | P2 (minor) |
| `api/pipeline/orchestrator.py` | P3 (hook points) |
| `api/pipeline/telemetry/__init__.py` | P3 (new package) |
| `api/pipeline/telemetry/token_stats.py` | P3 (new) |
| `scripts/bootstrap_managed_agent.py` | M1 |
| `tests/agent/test_tools.py` | R1, R2, R4 |
| `tests/profile/test_tools.py` | R3 |
| `tests/agent/test_runtime_managed_mirror.py` (new) | D1 |
| `tests/agent/test_runtime_managed_access.py` (new) | D2 |
| `tests/agent/test_bootstrap_prompt.py` (new) | M1 |
| `tests/pipeline/test_auditor_cache.py` (new) | P1+P2 |
| `tests/pipeline/telemetry/test_token_stats.py` (new) | P3 |

## YAGNI

- No sophisticated cache eviction (mtime + LRU-64 is enough).
- No external metrics collector (P3 writes a local JSON file, existing
  logs stay).
- No generalization of M1 to all `mb_*` tools (only field-reports — the
  highest-gain target; others stay on tool-call path).
- No atomic-write machinery for `pack_cache` (`open()` + `json.load` is
  safe at current write frequency).
- No new schema or Pydantic model beyond `PhaseTokenStats`.

## Out of Scope (future work)

- Full MA migration of the pipeline (audit verdict: no).
- Memory store quota monitoring (no immediate trigger).
- Cost-reduction exploration via Haiku-backed auditor (separate study).
- Pipeline-level thinking mode on the auditor (separate study).
- Rust port of any hot path (separate plan).

## Verification Checklist

- [x] Placeholder scan — no TBD / TODO.
- [x] Internal consistency — sprint list matches the file-touched table.
- [x] Scope — 10 items, 4 sprints, 10 commits: plausible for one plan.
- [x] Ambiguity — every item has a measurable test criterion.
