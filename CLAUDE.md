# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

`wrench-board` is an agent-native diagnostics workbench for board-level
electronics repair. Claude drives a multi-panel UI (boardview, knowledge
graph, memory bank, diagnostic chat) through tool calls, in response to a
microsoldering technician's natural-language questions. Two LLM paths run the
product: a stateless **knowledge factory** builds per-device packs offline,
and a stateful **diagnostic conversation** runs the live repair session.

## Hard rules — NEVER violate

1. **Proprietary source-available license** for all code in this repo — see
   the `LICENSE` file at the root for the canonical terms. The repo is
   public for evaluation, education, and personal non-commercial use only;
   redistribution, hosted deployment, and any commercial use require prior
   written permission. Do **not** add per-file copyright or SPDX headers
   to new source files; the root `LICENSE` is the single source of truth.
2. **Permissive dependencies only** (MIT, Apache 2.0, BSD). Never pull in
   GPL, AGPL, or LGPL packages — the proprietary license on our own code
   does not relax this; copyleft deps would still contaminate the bundle.
3. **No hallucinated component IDs.** Defense in depth, two layers.
   (1) Tool discipline: every refdes the agent surfaces comes from a tool
   lookup (`mb_get_component`, or a `bv_*` tool that cross-checks the parsed
   board). These never fabricate — they return `{found: false,
   closest_matches: [...]}` for unknown refdes, and the prompt tells the agent
   to pick from `closest_matches` or ask. (2) Post-hoc sanitizer: outbound
   agent `message` text is scanned for refdes-shaped tokens
   (`\b[A-Z]{1,3}\d{1,4}\b`) and, when a board is loaded, validated against
   `session.board.part_by_refdes`; unknown matches are wrapped `⟨?U999⟩` and
   logged server-side. Impl: `api/agent/sanitize.py`.

## Stack

- **Backend:** Python 3.11+, FastAPI (~0.136), uvicorn, Pydantic v2,
  WebSocket (native), pdfplumber, pytest + pytest-asyncio
- **Agent:** `anthropic ~= 0.97.0` — tier-selectable at WS-open time:
  `deep` = Opus (`claude-opus-4-8`), `normal` = Sonnet, `fast` = Haiku
  (`claude-haiku-4-5`). The pipeline distributes Sonnet/Opus per sub-agent.
- **Frontend:** Vanilla HTML + CSS + JS (no build step, no bundler). All
  external assets come from permissively-licensed CDNs (all MIT): D3.js v7,
  Three.js r128 (WebGL boardview), Tailwind CDN (PCB-section utilities),
  marked + DOMPurify (safe chat Markdown), Pickr 1.9.1 (Tweaks pin-color
  pickers — popover auto-flips at viewport edges where native
  `<input type="color">` clips off a fixed panel), Inter + JetBrains Mono
  fonts. Any new CDN dep must be permissively licensed and land in
  `web/index.html` with no transitive package-manager step.

## Commands

All tasks go through `make` (see `Makefile`):

```bash
make install   # create .venv and install deps (incl. [dev])
make run       # uvicorn api.main:app --reload on :8000
make test      # pytest tests/ -v -m "not slow" — fast subset (~1 min)
make test-all  # pytest tests/ -v — full suite incl. slow accuracy gates (7+ min)
make lint      # ruff check api/ tests/
make check-web # validate web/ ESM imports (no-build guard — see below)
make format    # ruff format api/ tests/
make clean     # drop __pycache__, .pytest_cache, .ruff_cache, egg-info
```

**No-build frontend guard (`make check-web`).** The `web/` UI ships as raw ES
modules served byte-for-byte (no bundler), so a broken import path or renamed
export only surfaces at runtime — `ruff`/`pytest` stay green.
`scripts/check_web_imports.py` (zero-dep Python) resolves every relative import
(stripping the `?v=…` cache-bust) and validates each named/default import
against the target's exports, plus `node --check` per module when node is
present. It catches import-path-depth and renamed-export breaks; it does
**not** catch a bare undefined reference (identifier used but never imported) —
that needs scope analysis (ESLint / `tsc --checkJs`), a JS toolchain this repo
avoids, so it stays a browser-verification responsibility. Run after any
`web/js/` move or import edit; **not** a prerequisite of `make lint` (ruff is
Python-only).

Single test / subset:

```bash
.venv/bin/pytest tests/board/test_test_link_parser.py -v
.venv/bin/pytest tests/agent/test_sanitize.py::test_wraps_unknown_refdes -v
.venv/bin/pytest -k "validator and not slow"
```

The API key is loaded from `.env` (copy `.env.example`). Tests do not require
`ANTHROPIC_API_KEY` — `api/config.py` defaults it to empty and only the
runtime code paths raise if it's missing.

**Test markers.** `make test` runs `-m "not slow"`. Any test that hits the
Anthropic API, ingests a real schematic, or acts as an accuracy gate must be
tagged `@pytest.mark.slow` so it only runs in `make test-all`. New tests
default to fast (no marker) and should stay under a second.

Bootstrapping Managed Agents (one-off, before the first `/ws/diagnostic`
session in `managed` mode):

```bash
.venv/bin/python scripts/bootstrap_managed_agent.py
# Creates the environment + 3 tier-scoped agents, writes managed_ids.json
# (gitignored). Re-runnable / idempotent.
```

## Layout

```
api/
  main.py            FastAPI app: /health, /ws/diagnostic/{slug}, mounts
                     web/ static, includes pipeline + board + profile routers
  config.py          Pydantic-settings Settings loaded from .env (cached)
  logging_setup.py   Single stdout handler, idempotent
  pipeline/          Knowledge factory — Scout → Registry → Writers(×3) → Auditor
    schematic/       PDF schematic → page vision → merge → ElectricalGraph,
                     plus simulator + hypothesize (deterministic engines)
    bench_generator/ Auto-generates simulator scenarios from a knowledge pack,
                     writes memory/{slug}/simulator_reliability.json
  board/             Boardview domain: model, parser registry (13 formats),
                     validator, /api/board/parse router, WS event envelopes
  agent/             Diagnostic runtime — managed (default) + direct fallback,
                     tool manifest (MB + BV), sanitizer, chat history, memory,
                     reliability-line injector (from simulator_reliability.json)
  profile/           Technician profile — catalog/derive/model/prompt/router/
                     store/tools; backs memory/_profile/technician.json
  stock/             Stock & donor salvage — schemas / safety table /
                     parts_index builder / atomic store / search engine /
                     HTTP router / agent tools. Searches across donor
                     boards for replacement parts under a hard safety
                     filter; backs the `stock_*` agent tools and
                     /api/stock/* HTTP endpoints
  session/           Per-session state (board, highlights, annotations)
  tools/             boardview.py — bv_* side-effect functions; ws_events.py
  vision/            Stub — reserved for image helpers
  telemetry/         Stub — reserved for structured logs / metrics
web/                 Static frontend served by FastAPI
  index.html         Shell (topbar/rail/metabar/workspace/statusbar)
  brd_viewer.js      Legacy D3 fallback renderer (kept for non-XZZ formats)
  js/pcb_viewer.js   Three.js WebGL boardview (InstancedMesh renderer)
                     — consumed via /api/board/render
  js/pcb_viewer_bridge.js  Bridges window.Boardview / window.initBoardview
                           to the WebGL viewer; falls back to brd_viewer.js
                           when the render endpoint is unavailable
  js/                main, router, graph, memory_bank, pipeline_progress, llm,
                     schematic, stock, profile, protocol, camera, mascot, i18n
  js/features/       global/landing (home/landing), repair/ (workspace, diagnostic)
  styles/            tokens, layout, graph, home, memory_bank, pipeline_progress,
                     llm, brd, modal, stub (semantic OKLCH palette in tokens.css)
  boards/            Demo BRD/KiCad artefacts
tests/               pytest suite mirroring api/ layout (agent/, board/, pipeline/,
                     pipeline/schematic/, session/, tools/)
memory/              Generated knowledge packs + repair sessions. One directory
                     per device_slug (canonical store). See §memory-layout below.
board_assets/        Input boards (.brd / .kicad_pcb / schematic .pdf) + ATTRIBUTIONS.md
scripts/             bootstrap_managed_agent.py — one-off MA environment setup
managed_ids.json     (gitignored) Environment + tier→agent IDs written by bootstrap
docs/superpowers/    specs/ and plans/ — read these before structural changes
```

### memory-layout — on-disk canonical store

```
memory/{device_slug}/
  raw_research_dump.md     # Scout output (free markdown)
  registry.json            # canonical vocabulary (refdes, signals, taxonomy)
  knowledge_graph.json     # Cartographe output (nodes + edges)
  rules.json               # Clinicien output (symptom → rule → action)
  dictionary.json          # Lexicographe output (glossary)
  audit_verdict.json       # Auditor verdict (APPROVED / NEEDS_REVISION / REJECTED)
  pack_quality.json        # optional: pre-persist lint findings ({"lint_findings": [{code,severity,detail},...]}), written after APPROVED
  schematic_pages/         # optional: page_NNN.json from schematic sub-pipeline
  schematic_graph.json     # optional: post-merge, pre-compile
  nets_classified.json     # optional: net classifier output (power/logic/connector)
  passive_classification_llm.json  # optional: passive role classifier (R/C/L/Q)
  electrical_graph.json    # optional: compiled ElectricalGraph
  boot_sequence_analyzed.json      # optional: Opus-refined boot sequence
  simulator_reliability.json       # optional: bench-generator reliability score
  parts_index.json         # optional: searchable per-device projection
                           # (refdes → type/value/role/safety) for stock_*
  repairs/{repair_id}/
    messages.jsonl         # chat history, one JSON-line per turn
    findings.json          # cross-session field reports for this repair
memory/_stock/
  inventory.json           # singleton: physical donor inventory (every
                           # declared donor + per-refdes consumed log)
```

`memory/{slug}/` is the source of truth. HTTP endpoints read it; agent tools
(`mb_*`) read it; the UI Memory Bank section reads it. Nothing else duplicates
these shapes.

## Architecture — the two paths

For the full reference (event flows, tool dispatch, on-disk artefact map,
known architectural debt), see `docs/ARCHITECTURE.md`.

There are **two distinct LLM paths**, by design:

1. **Pipeline (knowledge factory)** — `api/pipeline/`. Direct `messages.create`
   with forced tool use (`tool_choice={"type":"tool"}`) + Pydantic validation
   via `tool_call.py::call_with_forced_tool`. Batch / one-shot / structured, no
   session state. Builds per-device packs and (separately) compiles schematic
   PDFs to electrical graphs.

2. **Diagnostic conversation** — `api/agent/`, at
   `WS /ws/diagnostic/{device_slug}?tier=…&repair=…`. **Anthropic Managed
   Agents** by default: persistent agent + per-device memory store + session
   event stream + custom `mb_*` / `bv_*` tools. Fallback: `DIAGNOSTIC_MODE=direct`
   routes through `runtime_direct.py` (plain `messages.create` loop, no MA deps).

The split is deliberate — the pipeline doesn't benefit from session
primitives. Do not migrate pipeline to Managed Agents.

### The 4-phase pipeline (`api/pipeline/`)

`orchestrator.generate_knowledge_pack(device_label)` runs these sequentially
and writes each artefact to `memory/{device_slug}/`:

| Phase | Module        | Input           | Output (on disk)                   |
|-------|---------------|-----------------|------------------------------------|
| 1 Scout        | `scout.py`    | device_label | `raw_research_dump.md` (free Markdown via native `web_search` tool, handles `pause_turn` resumptions, broadened whitelist + thin-dump reject) |
| 2 Registry     | `registry.py` | raw dump     | `registry.json` (canonical vocabulary + inline device taxonomy — brand/model/version) |
| 3 Writers ×3   | `writers.py`  | raw + registry | `knowledge_graph.json`, `rules.json`, `dictionary.json` — Cartographe / Clinicien / Lexicographe run in parallel, share a **cache-controlled prefix**: writer 1 launches first, then `asyncio.sleep(cache_warmup_seconds)` lets Anthropic materialize the cache entry before writers 2+3 arrive. Models distributed per sub-agent (Sonnet/Opus split). |
| 4 Auditor      | `auditor.py`  | all 4 above  | `audit_verdict.json` — APPROVED / NEEDS_REVISION / REJECTED. On NEEDS_REVISION the orchestrator loops back to the flagged writers (`_apply_revisions`) up to `pipeline_max_revise_rounds` times. REJECTED raises. Deterministic drift check (`drift.py`) rejects on max rounds. |

Post-pipeline, `graph_transform.pack_to_graph_payload()` synthesizes action
nodes and emits the graph payload for the frontend (Actions → Components →
Nets → Symptoms column order).

**Source of truth for data shapes:** `api/pipeline/schemas.py`. These Pydantic
classes do double duty as runtime validators *and* JSON Schema sources for
the forced-tool `input_schema`. Never duplicate a shape — import from there.

### Schematic sub-pipeline (`api/pipeline/schematic/`)

PDF schematic → `ElectricalGraph`, independent of the knowledge factory.
`orchestrator.ingest_schematic(pdf_path, device_slug, client)`:

1. `renderer.render_pages()` — pdfplumber splits the PDF into per-page PNGs.
2. `grounding.extract_grounding()` — optional text/layout markers to stabilize
   the vision pass.
3. `page_vision.extract_page()` — one forced-tool vision call per page against
   `SchematicPageGraph`. Page 1 runs first to warm cache, then `asyncio.gather`
   fans out the rest.
4. `merger.merge_pages()` — deduplicates nets cross-page, produces
   `SchematicGraph`.
5. `compiler.compile_electrical_graph()` — classifies edges (power / logic /
   connector), infers boot sequence, emits quality report → `ElectricalGraph`.

Artefacts: `schematic_pages/page_NNN.json` → `schematic_graph.json` →
`electrical_graph.json`. Full ingestion runs from `ingest_schematic()`
(typically an upload on `POST /pipeline/packs/{slug}/documents`); the module
CLI (`python -m api.pipeline.schematic.cli PDF PAGE`) is a single-page vision
debug tool — or passive re-classifier with `--classify-passives SLUG` — not a
full entry point. Shapes live in `api/pipeline/schematic/schemas.py`.

### Deterministic engines — simulator + hypothesize

Two pure-sync modules sit alongside the schematic sub-pipeline. Neither calls
an LLM at runtime; both operate on the compiled `ElectricalGraph`.

- **`simulator.py`** (`SimulationEngine`) — event-driven behavioral simulator
  advancing phase-by-phase over the analyzed boot sequence (or compiler
  fallback); takes failures (refdes + mode) + optional rail overrides, emits a
  `SimulationTimeline` (dead rails/components, signal states, per-phase blocking
  cause). Exposed via `mb_schematic_graph(query="simulate", …)` and
  `POST /schematic/simulate`.
- **`hypothesize.py`** — reverse-diagnostic: from a partial observation
  (dead/alive components + rails), enumerates refdes-kill candidates that
  explain it — single-fault exhaustive + 2-fault pruned (seeded from top-K
  single survivors, paired only where cascades intersect residual unexplained
  observations). F1 soft-penalty scoring; returns top-N with structured diff +
  deterministic French narrative. Depends on `SimulationEngine`; no IO, no LLM.

These two are the distinctive engines of the product. Keep them pure and
sync — the `microsolder-evolve` skill (below) relies on fast, deterministic
re-runs to score variants.

### Bench auto-generator (`api/pipeline/bench_generator/`)

Reads a knowledge pack and generates simulator scenarios (cause → expected
cascade) via one Sonnet extractor pass + Opus rescue for span/topology rejects.
Five validators gate output: V1 sanity, V2 grounding (span must be a literal
pack substring), V3 topology (refdes + rails exist in the `ElectricalGraph`),
V4 pertinence (mirrors `evaluator._is_pertinent`), V5 dedup. Writes
`memory/{slug}/simulator_reliability.json` (aggregate + per-scenario) plus
per-run artefacts under `benchmark/auto_proposals/`.

`agent/reliability.py` reads `simulator_reliability.json` and injects a
one-liner into both runtimes' prompts so the agent can signal when its causal
engine is weak on the loaded device. Don't skip writing it — the prompt path
degrades gracefully but the agent loses self-awareness of its accuracy.

CLI: `scripts/generate_bench_from_pack.py --slug=…`. Frozen human oracle
lives separately at `benchmark/scenarios.jsonl` (17 scenarios, validated
by hand, provenance contract per `benchmark/README.md`). Never merge
auto-generated scenarios into the frozen oracle.

### Self-modifying subsystems — `microsolder-evolve`

The repo ships a local skill (`microsolder-evolve`) running an autonomous
nightly loop that modifies `simulator.py` or `hypothesize.py`, scores via
`scripts/eval_simulator.py`, and keeps (commit prefixed `evolve:`) or reverts
via `git` — authorised to commit unattended inside those two files.

Implications:
- **Don't refactor `simulator.py` / `hypothesize.py` for style alone** — the
  loop treats them as an optimization surface; cosmetic churn breaks its
  before/after measurement. Functional changes fine, drive-by cleanup not.
- `evolve:` commits intermixed with `feat:`/`fix:` are normal, as are
  `Revert "evolve: …"` (the loop reverts its own regressions) — not a bug.
- The oracle (`benchmark/scenarios.jsonl`) and `evaluator.py` are **read-only
  to the loop** (human-curated; closes the score-gaming backdoor, `4d0c9ba`).

### HTTP + WebSocket surface

Pipeline (`api/pipeline/__init__.py`):
- `POST /pipeline/generate` — run the full factory synchronously (~30–120 s)
- `POST /pipeline/repairs` — create a repair session + fire-and-forget pack
  generation (when the device is new). A repair is a persistent client
  session; packs are shared device knowledge reused across repairs.
- `WS   /pipeline/progress/{slug}` — live progress events for an in-flight
  pipeline (phase started / progress / completed / finished)
- `GET  /pipeline/packs` — list packs on disk with a presence bitmask
- `GET  /pipeline/packs/{slug}` — pack metadata
- `GET  /pipeline/packs/{slug}/full` — all JSON artefacts bundled (Memory Bank)
- `GET  /pipeline/taxonomy` — packs grouped `brand > model > version` (home view)

Board:
- `POST /api/board/parse` — upload + parse via `parser_for(path)` → `Board` JSON

Schematic:
- `POST /schematic/simulate` — drives `SimulationEngine` with `failures` +
  `rail_overrides`; same payload shape as `mb_schematic_graph(query="simulate")`

Diagnostic:
- `WS   /ws/diagnostic/{device_slug}?tier={fast|normal|deep}&repair={id}`
  — tier-selectable, optional repair scoping (replays prior messages).
  `DIAGNOSTIC_MODE` env var picks `managed` (default) vs `direct`.

### Diagnostic runtime (`api/agent/`)

Two siblings, same WS protocol:

- `runtime_managed.py` — Anthropic Managed Agents path. Opens the MA event
  stream **before** the first user message, relays `agent.message` tokens to
  the WS, caches `agent.custom_tool_use` events, dispatches on
  `requires_action`, writes `user.custom_tool_result` back. Seeds device
  context on fresh repairs (pack + findings) via `memory_seed.py`. Attaches a
  **layered 4-store MA memory**: `global-patterns` (RO archetypes),
  `global-playbooks` (RO protocol templates for `bv_propose_protocol`),
  `device-{slug}` (RO pack mirror), `repair-{repair_id}` (RW scribe notebook —
  `state.md` / `decisions/` / `measurements/` / `open_questions.md`). The agent
  self-orients on resume via `read state.md`, not a pre-cooked summary.
- `runtime_direct.py` — `messages.create` fallback with a Python tool loop.
  Same WS protocol; feature-equivalent fallback when the MA beta is
  unavailable.

Custom tools (`manifest.py`):

- **MB** — memory bank + board aggregation (14 tools): `mb_get_component`
  (Levenshtein-validated refdes anti-hallucination), `mb_get_rules_for_symptoms`,
  `mb_record_finding` (canonical archival API; mirrors to the device mount),
  `mb_record_session_log`, `mb_record_measurement`, `mb_list_measurements`,
  `mb_compare_measurements`, `mb_observations_from_measurements`,
  `mb_set_observation`, `mb_clear_observations`, `mb_validate_finding`,
  `mb_schematic_graph` (drives the simulator + boot sequence views),
  `mb_hypothesize` (reverse-diagnostic enumeration), `mb_expand_knowledge`
  (self-extends the pack when rules return empty — focused Scout + Clinicien
  pass, `pipeline/expansion.py`). Impl in `agent/tools.py`. Field-report
  listing is no longer a tool — the agent greps
  `/mnt/memory/wrench-board-{slug}/field_reports/` via `agent_toolset_20260401`.
- **BV** — boardview control (17 tools): `bv_highlight`, `bv_highlight_net`,
  `bv_focus`, `bv_reset_view`, `bv_flip`, `bv_annotate`, `bv_filter_by_type`,
  `bv_draw_arrow`, `bv_measure`, `bv_show_pin`, `bv_dim_unrelated`,
  `bv_layer_visibility`, `bv_scene`, `bv_propose_protocol`, `bv_get_protocol`,
  `bv_update_protocol`, `bv_record_step_result`. Conditional —
  `build_tools_manifest(session)` strips BV when no board is loaded.
  Dispatched by `dispatch_bv.py` to `api/tools/boardview.py`; each call
  mutates `session` and emits a WS event consumed by `brd_viewer.js`.
- **Profile** (3 tools): `profile_get`, `profile_check_skills`,
  `profile_track_skill` — read/update the technician profile under
  `memory/_profile/technician.json`.
- **Stock** (5 tools): `stock_search` (strict-then-tolerant match across donor
  boards under a hard safety filter — refuses tuned / feedback / RF / SMPS / IC
  roles), `stock_consume`, `stock_mark_donor`, `stock_unmark_donor`,
  `stock_list_donors`. Backed by per-device `memory/{slug}/parts_index.json`
  and the singleton `memory/_stock/inventory.json` (`api/stock/tools.py`). The
  "Stock awareness" prompt block tells the agent to call `stock_search` after
  confirming a root cause needing replacement.
- **Camera** (1 tool, conditional): `cam_capture` — request a webcam frame from
  the frontend for visual inspection during diagnosis.
- **Consult** (1 tool): `consult_specialist` — escalate to a deeper-tier
  sub-agent for a focused second opinion.

Chat persistence: `chat_history.py` appends every turn to
`memory/{slug}/repairs/{repair_id}/messages.jsonl`. Cross-session findings
(`field_reports.py`) are JSON-first and mirrored to the MA memory store when
available.

### Board parsing (`api/board/`)

- `model.py` — Pydantic v2 `Board` with private refdes/net indexes built in
  `model_post_init`. Access via `board.part_by_refdes()` /
  `board.net_by_name()`.
- `parser/base.py` — abstract `BoardParser`, **extension-based registry**:
  concrete parsers use `@register` + declare `extensions = (...)`; dispatch via
  `parser_for(path)`. New format = one new file in `parser/`, no base changes.
- Implemented parsers (all independent, Apache 2.0): `test_link.py`
  (OpenBoardView `.brd` v3; refuses obfuscated files via `ObfuscatedFileError`),
  `brd2.py` (KiCad BRD2, content-sniffed from `.brd`), `kicad.py`
  (`.kicad_pcb`), plus `asc.py`, `bdv.py`, `bv.py`, `cad.py`, `cst.py`,
  `f2b.py`, `fz.py`, `gr.py`, `tvw.py` (legacy boardview formats). Shared
  helpers in underscore siblings: `_kicad_extract.py` (kicad token reader),
  `_ascii_boardview.py` (Test_Link ASCII dialect, reused by several text
  formats), `_fz_zlib.py` (zlib + pipe-delimited scanner for `.fz`),
  `_gencad.py` (GenCAD 1.4 section parser for `.cad`).
- `validator.py` — anti-hallucination guardrail (pure functions, no I/O).
  `is_valid_refdes`, `resolve_part`, `resolve_net`, `resolve_pin`,
  `suggest_similar` (Levenshtein neighbours for "did you mean").
- `router.py` — `POST /api/board/parse`; WS event envelopes
  (`BoardLoaded`, `Highlight`, `Focus`, `Flip`, `Annotate`, …) live in
  `api/tools/ws_events.py` and are shared between backend and frontend.

### Session state (`api/session/state.py`)

`SessionState` is a per-WS-connection container:
`board: Board | None`, `layer: Side`, `highlights: set[str]`,
`net_highlight`, `annotations`, `arrows`, `dim_unrelated`, `filter_prefix`.
`SessionState.from_device(slug)` probes `board_assets/{slug}.kicad_pcb` then
`.brd` and populates `board` when found — so opening a diagnostic WS for a
known device loads the board automatically.

## Frontend design language (`web/`)

The web shell is a **pro-tool diagnostics workbench** — Figma / KiCad / Zed.
Dense, dark, purposeful. Match this aesthetic when editing `web/`; don't
drift toward a generic SaaS-card, Bootstrap, or "rounded-cartoon + emoji"
look.

### Frontend modules

Entrypoint: `web/index.html` loads `web/js/main.js` which wires:

| Module                 | Role                                                           |
|------------------------|----------------------------------------------------------------|
| `js/main.js`           | Boot, hash navigation, section dispatch                        |
| `js/router.js`         | `SECTIONS`, `navigate()`, rail button handlers                 |
| `js/features/global/landing/` | Home/landing list of **repairs** (persistent sessions) grouped by brand > model; "new repair" modal calls `POST /pipeline/repairs` |
| `js/memory_bank.js`    | Pack explorer reading `/pipeline/packs/{slug}/full`            |
| `js/graph.js`          | D3 force-layout knowledge graph (Actions→Components→Nets→Symptoms) |
| `js/pipeline_progress.js` | WS consumer of `/pipeline/progress/{slug}` — drawer UI      |
| `js/llm.js`            | Diagnostic chat panel; opens WS `/ws/diagnostic/{slug}?…`, auto-opens on `?repair=` URL |
| `brd_viewer.js`        | D3 boardview renderer; consumes WS boardview events; exposes public `window.Boardview` API for the agent-state split (see commit 7a44108) |

Shared layer (no view of their own): `shared/dom.js` (escapeHtml /
prettifySlug / relativeTime), `shared/api.js` (`apiGet`/`apiSend` — the single
HTTP wrapper), `services/*` (typed read-side data services + `*Socket.js` WS
transports), `store.js` (pub/sub). All new frontend network code goes through
these — never a raw `fetch`/`new WebSocket` in a view (see **Hosted-deployment
compatibility** for why this is load-bearing, not just tidy).

### Design tokens (CSS variables in `:root`)

- **Surfaces**, darkest → highest: `--bg-deep`, `--bg`, `--bg-2`, `--panel`, `--panel-2`
- **Text**, primary → tertiary: `--text`, `--text-2`, `--text-3`
- **Borders**: `--border` (hard line), `--border-soft` (inner divider),
  `--border-hover` (hover / focus edge)
- **Semantic accents** (OKLCH — **locked to meaning, never repurpose**):
  - `--amber`   → **symptom** — what the client observes
  - `--cyan`    → **component** — refdes, chip, connector
  - `--emerald` → **net / rail** — power and signal
  - `--violet` → **action** — reflow, replace, clean

  A new domain concept maps to one of these four families or introduces its
  own token — never reuse a semantic color for an unrelated affordance, never
  hard-code a hex when a token exists. Hex outside `tokens.css` is tolerated
  only for single-use decorative one-offs (gradient stops, muted/neutral edge
  strokes) with an inline why-comment; any color reused twice becomes a token.

### Layout shell (all `position: fixed`)

Pro-tool chrome — do not break this skeleton:

| Band       | Size    | Role                                                   |
|------------|---------|--------------------------------------------------------|
| Top bar    | 48 px   | brand · breadcrumbs · mode pill · global actions       |
| Left rail  | 52 px   | canonical section switcher (5 entries, hash-routed)    |
| Metabar    | 44 px   | device context · filter chips · search                 |
| Workspace  | flex    | the view for the current section                       |
| Status bar | 28 px   | agent state · counts · zoom readout (mono)             |

Sections are URL-hash routed via `SECTIONS` / `navigate()`: `#home`, `#pcb`,
`#schematic`, `#graphe`, `#profile`. The legacy `#memory-bank` redirects to
`#graphe?view=md` (raw memory-bank view inside the graph section). Adding a
section = append to `SECTIONS`, add a rail button with `data-section="…"`, and
ship a real DOM block or a `<section class="stub">` placeholder.

### Typography

- **Inter** — all UI prose, labels, buttons, headings
- **JetBrains Mono** — refdes, IDs, slugs, keyboard hints, column chips,
  metadata, status bar, confidence values, any fixed-format machine payload
- Body 13 px · chrome 11–12 px · mono chips 10–10.5 px
  (`text-transform: uppercase` + `letter-spacing: .4px` for the "workshop
  label" feel)

### Interaction vocabulary

- All hover/state transitions `.15s`; semantic motion gets weight
  (inspector slide-in `.28s cubic-bezier(.2,.8,.2,1)`, mode-pill pulse 2.4 s
  infinite).
- Hover = elevate: brighten text, deepen border, swap `--panel` → `--panel-2`.
- **Graph focus pattern**: the `.has-focus` modifier on the graph root fades
  non-neighbor nodes to `opacity: .15` and active links to `.06` — reuse
  this for any graph-like view, don't invent a new dimming scheme.
- Floating overlays (legend, zoom controls, inspector, tweaks, tooltip,
  empty state) are **glass**: `rgba(panel, .85–.96)` +
  `backdrop-filter: blur(8–14px)` + 1 px `--border`. No opaque floating
  panels.

### Graph visual grammar (do not dilute)

- **Shape = type**: circle = symptom · rounded square = component · hexagon
  = net · diamond = action. A new node type needs a new shape.
- **Stroke style = relation**, with matching SVG markers in `<defs>`:
  `causes` dashed amber · `powers` solid emerald · `connected_to` thin grey
  · `resolves` dotted violet. Reuse `arrow-causes` / `arrow-powers` /
  `arrow-connected` / `arrow-resolves` — never invent an edge color or style
  locally.
- **Reading flow is strictly left-to-right**: Actions → Components → Nets →
  Symptoms. The `.col-band` strip enforces it visually; the force simulation
  uses `forceX(d._tx).strength(0.8)` to keep columns stable. Don't weaken it
  or reorder the narrative.

### Icons

All UI icons are **inline SVG**, 16×16 (or 12×12 via `.icon-sm`), with
`stroke="currentColor"`, `stroke-width="1.6"`, `stroke-linecap="round"`,
`stroke-linejoin="round"`, `fill="none"`. No icon font, no external icon
library. Chrome interactions (close buttons, rail buttons, action buttons)
ship SVG.

Inline Unicode indicators are accepted in dense diagnostic views where
per-instance SVG would balloon the code without added clarity — `⚠` SPOF
markers, `✅`/`❌`/`·`/`●` passive-state/criticality glyphs in the boot
timeline, `✓` ticks in repair rows. New indicators piggy-back on that set
rather than add new emoji; chrome icons still get SVG.

### Copy

UI strings are authored in **English** with `data-i18n` keys; a parallel
translation layer (`web/js/i18n.js`) emits the French the user sees
(« Bibliothèque », « Graphe de connaissances », « Démarrer diagnostic »…).
Tag every new UI string, button label and helper text with a `data-i18n`
key — never hard-code a single language. Code identifiers, console logs,
and comments stay in English; so do all model-facing prompts.

### Don'ts

- No Tailwind, utility-class framework, or component library (Radix,
  shadcn…). Vanilla HTML/CSS/JS — see Stack.
- Linear gradients are reserved for five primitives: glass panel backgrounds
  (modal, inspector, railbar), chrome head fades (topbar, rail, mb/pp/llm
  heads), 2-color data-viz fill bars (conf/prob/crit-fill), grid background
  patterns (grid-bg, sch-grid), and the brand-mark icon. Don't add a sixth
  without flagging it in a spec/plan first — flat surfaces with single accent
  borders carry the rest of the mood.
- No scrollbars on `<body>` — the shell is `overflow: hidden` and each zone
  scrolls internally (thin 6 px `::-webkit-scrollbar` when needed).
- Never hard-code the semantic four colors when the CSS variable exists;
  never repurpose them for an unrelated UI state (loading, "info", etc.).

## Hosted-deployment compatibility — keep the engine proxy-safe

The engine runs **standalone** (`make run`) **and** behind a gated reverse-proxy
front-door (the hosted deployment) that serves `web/`, `/pipeline`, `/api`
verbatim, injects an auth + billing shim into HTML, and relays the WS paths — so
the *same* frontend code works either way. The rules below cost nothing
standalone and are what stop a hosted deploy from silently breaking; check a new
endpoint/fetch/socket against them **before** writing it.

- **Same-origin, relative URLs only.** Use root-relative paths
  (`/pipeline/...`, `/api/...`, `ws(s)://${location.host}/...`). Never hard-code
  an origin or port.
- **Call the global `fetch` at call time.** The front-door wraps `window.fetch`
  for billing; a reference captured at module load (`const f = fetch`, import
  alias, bound fn) escapes the wrap. Route HTTP through `web/js/shared/api.js`
  (`apiGet`/`apiSend`), and WS through a `web/js/services/*Socket.js` transport
  — never an inline `new WebSocket`.
- **Don't touch `</body>` in `web/index.html`.** The shim string-replaces the
  single `</body>`; keep exactly one. New ESM modules are *imported* by an
  existing entrypoint, not added as `<script>` tags.
- **`POST /pipeline/repairs` stays `application/x-www-form-urlencoded`** with
  its field names (`device_label`, `device_slug`, `symptom`, `device_kind`,
  `force_rebuild`, `schematic_pending`) — the front-door rebuilds the body
  field-for-field. Adding a field is safe; switching to JSON or renaming breaks
  the hosted path. Same for the document-upload multipart POST — keep it
  multipart.
- **Stay auth-header-agnostic for UI calls.** The UI sends no `Authorization`
  header; the front-door injects the service token + an `X-Owner-Ref` tenant
  scope. Don't depend on a browser-set auth/tenant header; honor `X-Owner-Ref`
  for owner-scoped data rather than assuming one global owner.
- **A new WS endpoint is a front-door change.** `/pipeline/progress/:slug` and
  `/ws/diagnostic/:slug` are relayed by exact path; a brand-new WS route won't
  reach a hosted client until the relay learns it — call it out explicitly.
- **Socle under `web/js/shared/`, never `web/js/lib/`.** `.gitignore` matches
  `lib/` — a `web/js/lib/` module would be untracked and 404 on a clean deploy.

## Development principles

- **Clean separation.** Top-level boundaries are `api/`, `web/`, `tests/`.
  Do not cross them without reason.
- **No God class.** Keep modules focused on one responsibility. If a file
  creeps past ~300 lines, ask whether it should split.
- **Tools return structured null/unknown, never fake data.** If a lookup
  fails, return `{"found": false, "reason": "..."}`. The agent will choose
  how to recover.
- **Anti-hallucination guardrail.** Before the agent's reply renders in the
  UI, `api/agent/sanitize.py` validates every refdes-shaped token against
  the parsed board and wraps or flags any that don't resolve.
- **Streaming over polling.** Agent output flows to the client through the
  WebSocket, token by token / event by event. Never batch a full response
  before sending. Same contract for pipeline progress events.
- **Repairs vs packs.** A **repair** is a persistent client session listed on
  the home view (one per ticket, identified by `repair_id`, stored under
  `memory/{slug}/repairs/{repair_id}/`). A **pack** is shared device
  knowledge (`memory/{slug}/*.json`) reused across repairs. Don't conflate
  them at the UI, endpoint, or storage layer.
- **Commit hygiene.** Descriptive English messages, conventional-commits
  style (`feat(scope):`, `fix(scope):`, `refactor(scope):`, `chore(scope):`,
  `docs(scope):`, `test(scope):`). Each commit passes tests and is
  independently reviewable by an outside reader walking the history cold.
  - Never bundle two domains (e.g. `web/` + `api/`) in one commit, even from
    one session. Stage narrowly across domain boundaries, commit cohesively
    within a domain.
  - **With parallel agents on this repo, always pass paths explicitly:**
    `git commit -m "msg" -- path/to/file1 path/to/file2`. The `-- path…`
    form commits strictly those files; a bare `git add X && git commit`
    sweeps up whatever another agent had staged for its own commit,
    bundling its work under your message. Prefer the path form whenever a
    parallel agent might be active.
  - Never rewrite history (`reset --soft`, `rebase -i`, `commit --amend`)
    once another agent has committed on top of yours — leave the
    sub-optimal commit and split better next time.
  - **Never `git push` without explicit authorization from the maintainer** —
    committing locally is fine, pushing to `origin` is not. Always ask first
    (« tu veux que je push ? ») even if commits look clean — applies to
    `push`/`--force`/`--set-upstream`, no exceptions (even a trivial `docs:`).
- **Verify before declaring done.** Run `make test` before saying a change
  is complete. UI changes require a manual check in the browser.
- **Long-running smoke scripts must stream output live.** Anything over
  ~30s (curator spawn, `expand_pack`, schematic ingest, MA session smoke)
  must show live progress, not a blank shell. Three rules:
  - **In the script:** `sys.stdout.reconfigure(line_buffering=True)` at
    module top + `logging.basicConfig(level=INFO, stream=sys.stderr)` so
    the pipeline's `[Curator]`/`[Expand]`/`[CacheRate]` logs surface —
    otherwise Python block-buffers stdout off-TTY and you see nothing
    until exit.
  - **When invoking:** never pipe to `tail` (silently swallows the run);
    redirect to a file — `python -u script.py > /tmp/smoke_X.log 2>&1`
    with `run_in_background: true`.
  - **Watch live:** harness `Monitor` + `tail -F` on the log, filtered to
    signal lines (`[Curator]`, `[Expand]`, `web_search`, `Error`,
    `Traceback`, `PASS`, `FAIL`), breaking on a terminal pattern. Don't
    chain short `sleep` polls — the harness blocks them.

## Models

Loaded from `.env` via `api/config.py`:

- `ANTHROPIC_MODEL_MAIN` → `claude-opus-4-8` (agent reasoning at `deep`
  tier, heavy pipeline sub-agents)
- `ANTHROPIC_MODEL_FAST` → `claude-haiku-4-5` (agent reasoning at `fast`
  tier, validation, formatting, cheap classification)

The pipeline distributes models per sub-agent (Sonnet/Opus split). The
diagnostic runtime picks the model from the `tier` query param at WS open
(`fast`/`normal`/`deep`); changing tier in the frontend reconnects the WS
(explicit new conversation).

## Editorial rule — keep this file permanent

`CLAUDE.md` and `README.md` are reference docs, not changelogs. Strip any
temporal or urgency framing when editing them — these files should still
read as accurate six months from now. Dated context (incidents,
empirical observations, in-flight plans) belongs in the auto-memory
under `~/.claude/projects/.../memory/` or in local-only design notes.
