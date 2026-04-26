# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

`microsolder-agent` is an agent-native diagnostics workbench for board-level
electronics repair. Claude drives a multi-panel UI (boardview, knowledge
graph, memory bank, diagnostic chat) through tool calls, in response to a
microsoldering technician's natural-language questions. Two LLM paths run the
product: a stateless **knowledge factory** builds per-device packs offline,
and a stateful **diagnostic conversation** runs the live repair session.

## Hard rules — NEVER violate

1. **All code written from scratch.** Never copy from any external codebase.
2. **Apache 2.0** is the license for all code in this repo.
3. **Permissive dependencies only** (MIT, Apache 2.0, BSD). Never pull in
   GPL, AGPL, or LGPL packages.
4. **Open hardware only *in the repo*.** No proprietary schematics or
   boardviews committed to this repository — no Apple, Samsung, ZXW or
   WUXINJI content in `board_assets/`, `web/boards/`, test fixtures, or
   any other tracked path. **Runtime is a separate matter.** The product
   is a pro microsoldering workbench, so the technician must be able to
   run a diagnostic on *any* device at runtime. Knowledge packs built by
   the pipeline (LLM-driven research under `memory/{slug}/*.json`) are
   unrestricted by device brand — iPhone, Galaxy, ThinkPad, Framework,
   whatever hits the bench. The technician may also upload their own
   schematic PDF or boardview and those land under `memory/{slug}/` so
   subsequent repairs on the same device reuse the pack + schematic +
   boardview automatically. What stays out of this repo is proprietary
   *source* material — not the technician's runtime workflow. The entire
   `memory/` tree (except `.gitkeep`) is gitignored for this reason.
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
   `⟨?U999⟩` in the delivered text and logged server-side. Implementation:
   `api/agent/sanitize.py`.

## Stack

- **Backend:** Python 3.11+, FastAPI (~0.136), uvicorn, Pydantic v2,
  WebSocket (native), pdfplumber, pytest + pytest-asyncio
- **Agent:** `anthropic ~= 0.97.0` — tier-selectable at WS-open time:
  `deep` = Opus (`claude-opus-4-7`), `normal` = Sonnet, `fast` = Haiku
  (`claude-haiku-4-5`). The pipeline distributes Sonnet/Opus per sub-agent.
- **Frontend:** Vanilla HTML + CSS + JS (no build step, no bundler). All
  external assets come from permissively-licensed CDNs: D3.js v7
  (`d3js.org`), marked and DOMPurify (`cdn.jsdelivr.net`, both MIT) for
  safe Markdown rendering in the chat panel, and Inter + JetBrains Mono
  fonts (`fonts.googleapis.com`). No Tailwind, no Alpine, no component
  library. Any new CDN dependency must be permissively licensed and land
  in `web/index.html` with no transitive package-manager step.

## Commands

All tasks go through `make` (see `Makefile`):

```bash
make install   # create .venv and install deps (incl. [dev])
make run       # uvicorn api.main:app --reload on :8000
make test      # pytest tests/ -v -m "not slow" — fast subset (~1 min)
make test-all  # pytest tests/ -v — full suite incl. slow accuracy gates (7+ min)
make lint      # ruff check api/ tests/
make format    # ruff format api/ tests/
make clean     # drop __pycache__, .pytest_cache, .ruff_cache, egg-info
```

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
  main.py            FastAPI app: /health, legacy /ws, /ws/diagnostic/{slug},
                     mounts web/ static, includes pipeline + board routers
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
  session/           Per-session state (board, highlights, annotations)
  tools/             boardview.py — bv_* side-effect functions; ws_events.py
  vision/            Stub — reserved for image helpers
  telemetry/         Stub — reserved for structured logs / metrics
web/                 Static frontend served by FastAPI
  index.html         Shell (topbar/rail/metabar/workspace/statusbar)
  brd_viewer.js      D3 board renderer + WS event consumer + window.Boardview
  js/                main, router, home, graph, memory_bank, pipeline_progress, llm
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
docs/HACKATHON.md    submission context, outside CLAUDE.md scope
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
  schematic_pages/         # optional: page_NNN.json from schematic sub-pipeline
  schematic_graph.json     # optional: post-merge, pre-compile
  nets_classified.json     # optional: net classifier output (power/logic/connector)
  passive_classification_llm.json  # optional: passive role classifier (R/C/L/Q)
  electrical_graph.json    # optional: compiled ElectricalGraph
  boot_sequence_analyzed.json      # optional: Opus-refined boot sequence
  simulator_reliability.json       # optional: bench-generator reliability score
  repairs/{repair_id}/
    messages.jsonl         # chat history, one JSON-line per turn
    findings.json          # cross-session field reports for this repair
```

`memory/{slug}/` is the source of truth. HTTP endpoints read it; agent tools
(`mb_*`) read it; the UI Memory Bank section reads it. Nothing else duplicates
these shapes.

## Architecture — the two paths

There are **two distinct LLM paths**, by design:

1. **Pipeline (knowledge factory)** — `api/pipeline/`. Direct
   `messages.create` calls with forced tool use (`tool_choice={"type":"tool"}`)
   and Pydantic validation via `api/pipeline/tool_call.py::call_with_forced_tool`.
   Batch / one-shot / structured output. No session state. Builds per-device
   knowledge packs and (separately) compiles schematic PDFs to electrical
   graphs.

2. **Diagnostic conversation** — `api/agent/`, served at
   `WS /ws/diagnostic/{device_slug}?tier=…&repair=…`. **Anthropic Managed
   Agents** by default: persistent agent + memory store per device + session
   event stream + custom `mb_*` / `bv_*` tools. Fallback: set
   `DIAGNOSTIC_MODE=direct` to route through `runtime_direct.py`
   (plain `messages.create` tool-use loop, no MA dependencies).

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

Artefacts: `memory/{slug}/schematic_pages/page_NNN.json`, then
`schematic_graph.json`, then `electrical_graph.json`. Full ingestion
runs from `ingest_schematic()` (typically via an upload on
`POST /pipeline/packs/{slug}/documents`) — the module's CLI
(`python -m api.pipeline.schematic.cli PDF PAGE`) is a single-page
vision debug tool, or a passive re-classifier when invoked with
`--classify-passives SLUG`, not a full ingestion entry point. All data
shapes live in `api/pipeline/schematic/schemas.py`.

### Deterministic engines — simulator + hypothesize

Two pure-sync modules sit alongside the schematic sub-pipeline. Neither calls
an LLM at runtime; both operate on the compiled `ElectricalGraph`.

- **`simulator.py`** (`SimulationEngine`) — event-driven behavioral simulator
  that advances phase-by-phase over the analyzed boot sequence (or the
  compiler fallback), takes a list of failures (refdes + mode) plus optional
  rail overrides, and emits a `SimulationTimeline` with dead rails, dead
  components, signal states, and the cause of blocking per phase. Exposed to
  the agent via `mb_schematic_graph(query="simulate", failures=…,
  rail_overrides=…)` and to the UI via `POST /schematic/simulate`.
- **`hypothesize.py`** — reverse-diagnostic: takes a partial observation
  (dead/alive components and rails) and enumerates refdes-kill candidates
  that explain it, single-fault exhaustive + 2-fault pruned (seeded from
  top-K single survivors, paired only with components whose cascade
  intersects the residual unexplained observations). F1 soft-penalty
  scoring. Returns top-N with structured diff + deterministic French
  narrative. Depends on `SimulationEngine`; no IO, no LLM.

These two are the distinctive engines of the product. Keep them pure and
sync — the `microsolder-evolve` skill (below) relies on fast, deterministic
re-runs to score variants.

### Bench auto-generator (`api/pipeline/bench_generator/`)

Reads a knowledge pack and generates simulator scenarios (cause →
expected cascade) via one Sonnet extractor pass with an Opus rescue for
span + topology rejects. Five validator passes gate the output: V1 sanity,
V2 grounding (evidence span must be literal pack substring), V3 topology
(refdes + rails must exist in the `ElectricalGraph`), V4 pertinence
(mirrors `evaluator._is_pertinent`), V5 dedup. Writes
`memory/{slug}/simulator_reliability.json` (aggregate score + per-scenario
breakdown) plus per-run artefacts under `benchmark/auto_proposals/`.

The `simulator_reliability.json` file is consumed by `agent/reliability.py`,
which injects a one-liner into both diagnostic runtimes' system prompts so
the agent can explicitly signal when its causal engine is weak on the loaded
device. Do not skip writing this file — the prompt path gracefully degrades
but the agent loses self-awareness of its own accuracy.

CLI: `scripts/generate_bench_from_pack.py --slug=…`. Frozen human oracle
lives separately at `benchmark/scenarios.jsonl` (17 scenarios, validated
by hand, provenance contract per `benchmark/README.md`). Never merge
auto-generated scenarios into the frozen oracle.

### Self-modifying subsystems — `microsolder-evolve`

The repo ships a local Claude Code skill (`microsolder-evolve`) that runs
an autonomous nightly loop modifying `api/pipeline/schematic/simulator.py`
or `api/pipeline/schematic/hypothesize.py`, scoring via
`scripts/eval_simulator.py`, and either keeping the change (commit prefixed
`evolve:`) or reverting via `git`. The skill is authorised to commit
without user intervention inside these two files; its commits are visible
in `git log` under the `evolve:` prefix and often include a parenthesised
score delta.

Implications for any agent working here:
- **Do not refactor `simulator.py` or `hypothesize.py` for style alone.**
  The evolve loop treats both files as an optimization surface; cosmetic
  churn there conflicts with its ability to measure before/after deltas.
  Functional changes are fine; drive-by cleanup is not.
- `evolve:` commits intermixed with `feat:` / `fix:` commits in the log
  are expected and normal. Reverts of `evolve:` commits are also normal
  (anti-pattern detection). Do not assume a `Revert "evolve: …"` is a
  bug — the loop itself reverts regressions.
- The scoring oracle (`benchmark/scenarios.jsonl`) is **read-only for the
  evolve agent**; a human curates it. The evaluator (`evaluator.py`) is
  likewise off-limits to the loop to close the score-gaming backdoor
  (see commit `4d0c9ba`).

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
- `WS   /ws` — legacy echo endpoint, kept for smoke tests

### Diagnostic runtime (`api/agent/`)

Two siblings, same WS protocol:

- `runtime_managed.py` — Anthropic Managed Agents path. Loads the tier-scoped
  agent + device memory store, opens the MA event stream **before** the first
  user message, relays `agent.message` tokens onto the WS, caches
  `agent.custom_tool_use` events, dispatches them on `requires_action`, and
  writes `user.custom_tool_result` back. Auto-injects device context on fresh
  repair sessions (pack + findings) via `memory_seed.py`. Attaches a
  **layered 4-store MA memory** to every session: `global-patterns` (RO,
  cross-device archetypes), `global-playbooks` (RO, protocol templates for
  `bv_propose_protocol`), `device-{slug}` (RO, knowledge pack mirror),
  `repair-{repair_id}` (RW, agent's scribe notebook — `state.md` /
  `decisions/` / `measurements/` / `open_questions.md`). The agent
  self-orients on resume via `read state.md` instead of a pre-cuisined
  LLM summary. Spec:
  `docs/superpowers/plans/2026-04-26-ma-memory-layered-architecture.md`.
- `runtime_direct.py` — `messages.create` fallback with a Python tool loop.
  Same WS protocol; feature-equivalent for demos when MA beta is
  unavailable.

Custom tools (`manifest.py`):

- **MB** — memory bank + board aggregation (4 tools): `mb_get_component`
  (Levenshtein-validated refdes anti-hallucination), `mb_get_rules_for_symptoms`,
  `mb_record_finding` (canonical archival API; mirrors to the device mount),
  `mb_expand_knowledge` (the agent self-extends the pack when rules return
  empty, running a focused Scout + Clinicien pass — see `pipeline/expansion.py`).
  Implementations in `agent/tools.py`. Field-report listing is no longer a
  tool — the agent greps `/mnt/memory/microsolder-{slug}/field_reports/`
  directly via `agent_toolset_20260401`.
- **BV** — boardview control (12 tools): `bv_highlight_component`,
  `bv_focus_component`, `bv_reset_view`, `bv_highlight_net`, `bv_flip_board`,
  `bv_annotate`, `bv_filter_by_type`, `bv_draw_arrow`, `bv_measure_distance`,
  `bv_show_pin`, `bv_dim_unrelated`, `bv_layer_visibility`. Conditional —
  `build_tools_manifest(session)` strips BV when no board is loaded.
  Dispatched by `dispatch_bv.py` to `api/tools/boardview.py`; each call
  mutates `session` and emits a WS event consumed by `brd_viewer.js`.

Chat persistence: `chat_history.py` appends every turn to
`memory/{slug}/repairs/{repair_id}/messages.jsonl`. Cross-session findings
(`field_reports.py`) are JSON-first and mirrored to the MA memory store when
available.

### Board parsing (`api/board/`)

- `model.py` — Pydantic v2 `Board` with private refdes/net indexes built in
  `model_post_init`. Access via `board.part_by_refdes()` /
  `board.net_by_name()`.
- `parser/base.py` — abstract `BoardParser` with **extension-based registry**.
  Concrete parsers use the `@register` decorator and declare
  `extensions = (...)`. Dispatch via `parser_for(path)`. Adding a new format
  = one new file in `parser/`, no changes to base.
- Implemented parsers: `test_link.py` (OpenBoardView `.brd` v3, clean-room;
  refuses obfuscated files with `ObfuscatedFileError`), `brd2.py` (KiCad-
  boardview BRD2 output), `kicad.py` (`.kicad_pcb`, helpers in
  `_kicad_extract.py`).
- Stubs pending real parsers: `bv.py`, `cad.py`, `gr.py`, `cst.py`, `tvw.py`,
  `asc.py`, `fz.py`, `f2b.py`, `bdv.py` (each declares its extensions +
  raises `NotImplementedError`). Generic shape in `_stub.py`.
- `validator.py` — anti-hallucination guardrail (pure functions, no I/O).
  `is_valid_refdes`, `resolve_part`, `resolve_net`, `resolve_pin`,
  `suggest_similar` (Levenshtein neighbours for "did you mean").
- `router.py` — `POST /api/board/parse`; `events.py` — WS event envelopes
  (`BoardLoaded`, `Highlight`, `Focus`, `Flip`, `Annotate`, …) shared between
  backend and frontend.

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
| `js/home.js`           | Home list of **repairs** (persistent sessions) grouped by brand > model; "new repair" modal calls `POST /pipeline/repairs` |
| `js/memory_bank.js`    | Pack explorer reading `/pipeline/packs/{slug}/full`            |
| `js/graph.js`          | D3 force-layout knowledge graph (Actions→Components→Nets→Symptoms) |
| `js/pipeline_progress.js` | WS consumer of `/pipeline/progress/{slug}` — drawer UI      |
| `js/llm.js`            | Diagnostic chat panel; opens WS `/ws/diagnostic/{slug}?…`, auto-opens on `?repair=` URL |
| `brd_viewer.js`        | D3 boardview renderer; consumes WS boardview events; exposes public `window.Boardview` API for the agent-state split (see commit 7a44108) |

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

  A new domain concept must map to one of these four families or introduce
  its own token — never reuse a semantic color for an unrelated affordance,
  and never hard-code a hex color when a token exists. Hex values outside
  `tokens.css` are tolerated only for single-use decorative one-offs (brand
  mark gradient stops, muted signal-link stroke, neutral edge stroke) with
  a short inline comment explaining why; any color reused in two or more
  places becomes a token first.

### Layout shell (all `position: fixed`)

Pro-tool chrome — do not break this skeleton:

| Band       | Size    | Role                                                   |
|------------|---------|--------------------------------------------------------|
| Top bar    | 48 px   | brand · breadcrumbs · mode pill · global actions       |
| Left rail  | 52 px   | canonical section switcher (8 entries, hash-routed)    |
| Metabar    | 44 px   | device context · filter chips · search                 |
| Workspace  | flex    | the view for the current section                       |
| Status bar | 28 px   | agent state · counts · zoom readout (mono)             |

Sections are URL-hash routed via `SECTIONS` and `navigate()`: `#home`, `#pcb`,
`#schematic`, `#graphe`, `#memory-bank`, `#agent`, `#profile`, `#aide`.
Adding a section = append to `SECTIONS`, add a rail button with
`data-section="…"`, and ship either a real DOM block or a
`<section class="stub">` placeholder.

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

Inline Unicode indicators are accepted in dense diagnostic views where per
instance SVG would balloon the render code without added clarity — SPOF
`⚠` markers in the schematic canvas, `✅`/`❌`/`·`/`●` passive-state and
criticality glyphs in the boot timeline, `✓` tick in a repair row or
"Marquer fix" button. New indicators of this kind should piggy-back on
that set rather than introduce new emoji; regular chrome icons still get
SVG.

### Copy

UI ships in **French** (« Bibliothèque », « Graphe de connaissances »,
« Démarrer diagnostic »). Keep new UI strings, button labels and helper
text in French. Code identifiers, console logs, and comments stay in
English.

### Don'ts

- No Tailwind, utility-class framework, or component library (Radix,
  shadcn…). Vanilla HTML/CSS/JS — see Stack.
- Linear gradients are reserved for five recurring design primitives:
  glass panel backgrounds (modal, inspector, railbar, sch-inspector),
  chrome head fades (topbar, rail, mb-head, pp-head, llm head), 2-color
  data-viz fill bars (conf-fill, prob-fill, crit-fill), grid background
  patterns (grid-bg, sch-grid), and the brand-mark icon. Don't introduce
  a sixth category without flagging it in a spec or plan first — flat
  surfaces with single accent borders carry the rest of the mood.
- No scrollbars on `<body>` — the shell is `overflow: hidden` and each zone
  scrolls internally (thin 6 px `::-webkit-scrollbar` when needed).
- Never hard-code the semantic four colors when the CSS variable exists;
  never repurpose them for an unrelated UI state (loading, "info", etc.).

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
- **Commit hygiene — one commit = one user-visible change.** Descriptive
  English messages, conventional-commits style (`feat(scope):`,
  `fix(scope):`, `refactor(scope):`, `chore(scope):`, `docs(scope):`,
  `test(scope):`). Each commit passes tests and is independently reviewable
  by an outside reader walking the history cold. A cohesive feature lands
  as **one** commit — a rename + CSS + HTML + JS wiring that all serves the
  same user-visible change stay together. Split only when concerns are
  genuinely separable (docs vs code, backend vs frontend, or when one
  sub-change is risky enough to want isolated revert).
  - Never bundle changes from two different domains (e.g. `web/` + `api/`
    pivots) into the same commit, even if they land in the same working
    session. Stage narrowly across domain boundaries, commit cohesively
    within a domain.
  - **When multiple agents are working in parallel on this repo, always
    pass paths explicitly to `git commit`:**
    ```bash
    git commit -m "msg" -- path/to/file1 path/to/file2
    ```
    The `-- path...` form tells Git to commit strictly those files and
    ignore the rest of the staging area. Without it, `git add X && git
    commit` will also sweep up anything another agent had already staged
    in preparation for its own commit — bundling its unrelated work under
    your misleading commit message (real incident: commit e053002, later
    corrected in 71dd23a). The staged-but-not-yours files remain staged
    after your commit, ready for the other agent to commit with its own
    message. Always prefer this form over `git add ... && git commit`
    whenever a parallel agent might be active.
  - Never rewrite history (`reset --soft`, `rebase -i`, `commit --amend`)
    once another agent has committed on top of yours — leave the
    sub-optimal commit and split better next time.
  - **Never `git push` without explicit authorization from Alexis** —
    committing locally is encouraged, pushing to `origin` is not. Always
    ask first (« tu veux que je push ? ») even if the commits look clean.
    This applies to `push`, `push --force`, `push --set-upstream`, and any
    equivalent. No exceptions, even for a trivial `docs:` commit.
- **Verify before declaring done.** Run `make test` before saying a change
  is complete. UI changes require a manual check in the browser.

## Models

Loaded from `.env` via `api/config.py`:

- `ANTHROPIC_MODEL_MAIN` → `claude-opus-4-7` (agent reasoning at `deep`
  tier, heavy pipeline sub-agents)
- `ANTHROPIC_MODEL_FAST` → `claude-haiku-4-5` (agent reasoning at `fast`
  tier, validation, formatting, cheap classification)

The pipeline distributes models per sub-agent (Sonnet/Opus split — see
commit 21de00b). The diagnostic runtime picks the model from the `tier`
query param at WS open: `fast` / `normal` / `deep`. Changing tier in the
frontend reconnects the WS (explicit new conversation).

**Schematic vision pipeline = Opus 4.7 only — do not migrate to Sonnet.**
`page_vision.extract_page` defaults to `settings.anthropic_model_main`
(Opus 4.7) on purpose. Sonnet 4.6 was tested empirically on iPhone X
schematic page 4 (2026-04-25): identical capacitor/IC/net coverage but
**3 OCR hallucinations on rail names** (`PP9V8_SOC_FIXED_S1` instead of
`PP0V8_SOC_FIXED_S1`, etc.) — silent corruption of the power tree, not
detectable by the simulator invariants. Opus 4.7 had zero hallucinations
on the same page. Full methodology + reproduction script:
`docs/notes/2026-04-25-vision-model-opus-vs-sonnet.md`. Re-test only when
a new vision-capable model lands (Sonnet 4.7+ or improved Haiku).

## Specs and plans — read before structural work

Current:
- `docs/superpowers/specs/2026-04-22-backend-v2-knowledge-factory.md` — the
  authoritative knowledge-factory spec; supersedes the 2026-04-21 v1 design.
- `docs/superpowers/specs/2026-04-22-boardview-formats-roadmap.md` — parser
  roadmap for the stub formats in `api/board/parser/`.
- `docs/superpowers/specs/2026-04-23-agent-boardview-control-design.md` —
  bv_* tools + dynamic manifest + mb_* aggregation design.
- `docs/superpowers/plans/2026-04-23-agent-boardview-control.md` — current
  implementation plan (source of truth for in-progress scope).

Archived (kept for historical context but marked archive):
- `docs/superpowers/specs/2026-04-21-microsolder-agent-v1-design.md`
- `docs/superpowers/specs/2026-04-21-boardview-design.md`

`docs/HACKATHON.md` holds submission context (only relevant until the
original build window closes) — never mix that framing into this file.

## Editorial rule — keep this file permanent

Temporal pressure framing ("this week", "ship by X", "demo", "hackathon",
"prize track") never appears in `CLAUDE.md` or `README.md`. That content
lives in `docs/HACKATHON.md` or a dated plan file under
`docs/superpowers/plans/` only. When editing either file, strip any phrasing
that would read as outdated six months from now.
