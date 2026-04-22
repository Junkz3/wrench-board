# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

`microsolder-agent` is an agent-native diagnostics workbench for board-level
electronics repair. Claude Opus 4.7 drives a three-panel UI (boardview,
schematic, chat+journal) through tool calls, in response to a microsoldering
technician's natural-language questions. The target demo board is the
MNT Reform motherboard (CERN-OHL-S-2.0, fully open-hardware KiCad sources).
Built for the Anthropic × Cerebral Valley "Built with Opus 4.7" hackathon,
April 21–26 2026.

## Hard rules — NEVER violate

1. **All code written from scratch during the hackathon week.** Never copy
   from any external codebase.
2. **Apache 2.0** is the license for all code in this repo.
3. **Permissive dependencies only** (MIT, Apache 2.0, BSD). Never pull in
   GPL, AGPL, or LGPL packages.
4. **Open hardware only.** No proprietary schematics or boardviews — no
   Apple, Samsung, ZXW, WUXINJI content. Target is the MNT Reform motherboard.
5. **No hallucinated component IDs.** Every refdes (e.g. `U7`, `C29`) the
   agent mentions must be validated against parsed board data *before* being
   shown to the user. Tools that cannot answer return structured
   null/unknown — never fake data.

## Stack

- **Backend:** Python 3.11+, FastAPI (~0.136), uvicorn, Pydantic v2,
  WebSocket (native), pdfplumber, pytest + pytest-asyncio
- **Agent:** `anthropic ~= 0.96.0` — `claude-opus-4-7` for reasoning,
  `claude-haiku-4-5` for fast validation/formatting
- **Frontend:** Vanilla HTML + CSS + JS (no build step), D3.js v7 via CDN,
  Inter + JetBrains Mono fonts. No Tailwind, no Alpine, no bundler.

## Commands

All tasks go through `make` (see `Makefile`):

```bash
make install   # create .venv and install deps (incl. [dev])
make run       # uvicorn api.main:app --reload on :8000
make test      # pytest tests/ -v
make lint      # ruff check api/ tests/
make format    # ruff format api/ tests/
make clean     # drop __pycache__, .pytest_cache, .ruff_cache, egg-info
```

Single test / subset:

```bash
.venv/bin/pytest tests/board/test_brd_parser.py -v
.venv/bin/pytest tests/board/test_brd_parser.py::test_parse_minimal_board -v
.venv/bin/pytest -k "validator and not slow"
```

The API key is loaded from `.env` (copy `.env.example`). Tests do not require
`ANTHROPIC_API_KEY` — `api/config.py` defaults it to empty and only the
pipeline code paths raise if it's missing at runtime.

## Layout

```
api/
  main.py          FastAPI app: /health, /ws placeholder, mounts web/, includes pipeline router
  config.py        Pydantic-settings Settings loaded from .env (get_settings() is process-cached)
  logging_setup.py Single stdout handler, idempotent
  pipeline/        V2 knowledge factory — Scout → Registry → Writers(×3 parallel) → Auditor
  board/           Boardview domain: model (Board/Part/Pin/Net), parser registry, validator
  agent/           Stub — diagnostic conversation (Managed Agents) lands here, Phase C
  session/         Stub — per-session state / journal
  vision/          Stub — image / PDF rendering helpers
  tools/           Stub — mb_* custom tools exposed to the diagnostic agent
  telemetry/       Stub — structured logs / metrics
web/               Static frontend served by FastAPI (index.html is the whole app)
tests/             pytest suite; mirror api/ layout (tests/board/, tests/pipeline/, …)
memory/            Generated knowledge packs, one directory per device_slug (canonical store)
docs/superpowers/  specs/ and plans/ — read these before structural changes
```

## Architecture — the two paths

There are **two distinct LLM paths**, by design:

1. **Pipeline (V2 knowledge factory)** — `api/pipeline/`. Direct
   `messages.create` calls with forced tool use (`tool_choice={"type":"tool"}`)
   and Pydantic validation via `api/pipeline/tool_call.py::call_with_forced_tool`.
   Batch / one-shot / structured output. No session state. Used to build
   per-device knowledge packs.

2. **Diagnostic conversation** — `api/agent/` (Phase C, not yet landed).
   **Anthropic Managed Agents**: persistent agent + memory store per device +
   session event stream + custom `mb_*` tools. Fallback `DIAGNOSTIC_MODE=direct`
   env var pivots to `messages.create` if the MA beta blocks us.

The split is deliberate — the pipeline doesn't benefit from session primitives
(see `docs/superpowers/plans/2026-04-22-v1-hackathon-shipping-plan.md` for the
rationale). Do not migrate pipeline to Managed Agents.

### The 4-phase pipeline (`api/pipeline/`)

`orchestrator.generate_knowledge_pack(device_label)` runs these sequentially
and writes each artefact to `memory/{device_slug}/` as the canonical store:

| Phase | Module        | Input           | Output (on disk)                   |
|-------|---------------|-----------------|------------------------------------|
| 1 Scout        | `scout.py`    | device_label | `raw_research_dump.md` (free Markdown via native `web_search` tool, handles `pause_turn` resumptions) |
| 2 Registry     | `registry.py` | raw dump     | `registry.json` (canonical vocabulary — refdes, signals) |
| 3 Writers ×3   | `writers.py`  | raw + registry | `knowledge_graph.json`, `rules.json`, `dictionary.json` — Cartographe / Clinicien / Lexicographe run in parallel, share a **cache-controlled prefix**: writer 1 launches first, then `asyncio.sleep(cache_warmup_seconds)` lets Anthropic materialize the cache entry before writers 2+3 arrive |
| 4 Auditor      | `auditor.py`  | all 4 above  | `audit_verdict.json` — APPROVED / NEEDS_REVISION / REJECTED. On NEEDS_REVISION the orchestrator loops back to the flagged writers (`_apply_revisions`) up to `pipeline_max_revise_rounds` times. REJECTED raises. |

**Source of truth for data shapes:** `api/pipeline/schemas.py`. These Pydantic
classes do double duty as runtime validators *and* JSON Schema sources for the
forced-tool `input_schema`. Never duplicate a shape — import from there.

**HTTP surface** (`api/pipeline/__init__.py`):
- `POST /pipeline/generate` — run the full pipeline, blocks ~30–120s
- `GET  /pipeline/packs` — list generated packs
- `GET  /pipeline/packs/{device_slug}` — pack metadata

### Board parsing (`api/board/`)

- `model.py` — Pydantic v2 `Board` with private refdes/net indexes built in
  `model_post_init`. Access via `board.part_by_refdes()` / `board.net_by_name()`.
- `parser/base.py` — abstract `BoardParser` with **extension-based registry**.
  Concrete parsers use the `@register` decorator and declare `extensions = (...)`.
  Dispatch via `parser_for(path)`. Adding a new format = one new file in
  `parser/`, no changes to base.
- `parser/test_link.py` — clean-room OpenBoardView `.brd` (Test_Link) parser.
  Refuses OBV-signature obfuscated files (`ObfuscatedFileError`). A sibling
  `parser/brd2.py` for the BRD2 format (kicad-boardview output) is coming in
  parallel — the fixture `board_assets/mnt-reform-motherboard.brd` is BRD2.
- `validator.py` — anti-hallucination guardrail (pure functions, no I/O). Every
  refdes the agent plans to surface passes `is_valid_refdes` / `resolve_part`
  / `resolve_net` / `resolve_pin` first. `suggest_similar` gives Levenshtein
  neighbours for the "did you mean" recovery path.

## Frontend design language (`web/index.html`)

The web shell is a **pro-tool diagnostics workbench** — Figma / KiCad / Zed.
Dense, dark, purposeful. Match this aesthetic when editing `web/`; don't drift
toward a generic SaaS-card, Bootstrap, or "rounded-cartoon + emoji" look.

### Design tokens (CSS variables in `:root`)

- **Surfaces**, darkest → highest: `--bg-deep`, `--bg`, `--bg-2`, `--panel`, `--panel-2`
- **Text**, primary → tertiary: `--text`, `--text-2`, `--text-3`
- **Borders**: `--border` (hard line), `--border-soft` (inner divider)
- **Semantic accents** (OKLCH — **locked to meaning, never repurpose**):
  - `--amber`   → **symptom** — what the client observes
  - `--cyan`    → **component** — refdes, chip, connector
  - `--emerald` → **net / rail** — power and signal
  - `--violet`  → **action** — reflow, replace, clean

  A new domain concept must map to one of these four families or introduce its
  own token — never reuse a semantic color for an unrelated affordance, and
  never hard-code a hex color when a token exists.

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
`#schematic`, `#graphe`, `#memory-bank`, `#agent`, `#profile`, `#aide`. Adding a
section = append to `SECTIONS`, add a rail button with `data-section="…"`, and
ship either a real DOM block or a `<section class="stub">` placeholder.

### Typography

- **Inter** — all UI prose, labels, buttons, headings
- **JetBrains Mono** — refdes, IDs, slugs, keyboard hints, column chips,
  metadata, status bar, confidence values, any fixed-format machine payload
- Body 13 px · chrome 11–12 px · mono chips 10–10.5 px (`text-transform: uppercase`
  + `letter-spacing: .4px` for the "workshop label" feel)

### Interaction vocabulary

- All hover/state transitions `.15s`; semantic motion gets weight
  (inspector slide-in `.28s cubic-bezier(.2,.8,.2,1)`, mode-pill pulse 2.4 s
  infinite).
- Hover = elevate: brighten text, deepen border, swap `--panel` → `--panel-2`.
- **Graph focus pattern**: the `.has-focus` modifier on the graph root fades
  non-neighbor nodes to `opacity: .15` and active links to `.06` — reuse this
  for any graph-like view, don't invent a new dimming scheme.
- Floating overlays (legend, zoom controls, inspector, tweaks, tooltip, empty
  state) are **glass**: `rgba(panel, .85–.96)` + `backdrop-filter: blur(8–14px)`
  + 1 px `--border`. No opaque floating panels.

### Graph visual grammar (do not dilute)

- **Shape = type**: circle = symptom · rounded square = component · hexagon =
  net · diamond = action. A new node type needs a new shape.
- **Stroke style = relation**, with matching SVG markers in `<defs>`:
  `causes` dashed amber · `powers` solid emerald · `connected_to` thin grey ·
  `resolves` dotted violet. Reuse `arrow-causes` / `arrow-powers` /
  `arrow-connected` / `arrow-resolves` — never invent an edge color or style
  locally.
- **Reading flow is strictly left-to-right**: Actions → Components → Nets →
  Symptoms. The `.col-band` strip enforces it visually; the force simulation
  uses `forceX(d._tx).strength(0.8)` to keep columns stable. Don't weaken it or
  reorder the narrative.

### Icons

All UI icons are **inline SVG**, 16×16 (or 12×12 via `.icon-sm`), with
`stroke="currentColor"`, `stroke-width="1.6"`, `stroke-linecap="round"`,
`stroke-linejoin="round"`, `fill="none"`. No emoji, no icon font, no external
icon library.

### Copy

UI ships in **French** (« Bibliothèque », « Graphe de connaissances »,
« Démarrer diagnostic »). Keep new UI strings, button labels and helper text
in French. Code identifiers, console logs, and comments stay in English.

### Don'ts

- No Tailwind, utility-class framework, or component library (Radix, shadcn…).
  Vanilla HTML/CSS/JS — see Stack.
- No `linear-gradient` beyond the two already wired (topbar, inspector head) —
  flat surfaces + single accent borders carry the mood.
- No scrollbars on `<body>` — the shell is `overflow: hidden` and each zone
  scrolls internally (thin 6 px `::-webkit-scrollbar` when needed).
- Never hard-code the semantic four colors when the CSS variable exists; never
  repurpose them for an unrelated UI state (loading, "info", etc.).

## Development principles

- **Clean separation.** Top-level boundaries are `api/`, `web/`, `tests/`.
  Do not cross them without reason.
- **No God class.** Keep modules focused on one responsibility. If a file
  creeps past ~300 lines, ask whether it should split.
- **Tools return structured null/unknown, never fake data.** If a lookup
  fails, return `{"found": false, "reason": "..."}`. The agent will choose
  how to recover.
- **Anti-hallucination guardrail.** Before the agent's reply renders in the
  UI, validate every refdes against the parsed board. Drop or flag any that
  don't resolve.
- **Streaming over polling.** Agent output flows to the client through the
  WebSocket, token by token / event by event. Never batch a full response
  before sending.
- **Commit hygiene — one commit = one user-visible change.** Descriptive
  English messages, conventional-commits style (`feat(scope):`, `fix(scope):`,
  `refactor(scope):`, `chore(scope):`, `docs(scope):`, `test(scope):`). Each
  commit passes tests and is independently reviewable by a hackathon judge
  reading the history. A cohesive feature lands as **one** commit — a rename
  + CSS + HTML + JS wiring that all serves the same user-visible change stay
  together. Split only when concerns are genuinely separable (docs vs code,
  backend vs frontend, or when one sub-change is risky enough to want
  isolated revert).
  - Never bundle changes from two different domains (e.g. `web/` + `api/`
    pivots) into the same commit, even if they land in the same working
    session. Stage narrowly across domain boundaries, commit cohesively
    within a domain.
  - Never rewrite history (`reset --soft`, `rebase -i`, `commit --amend`)
    once another agent has committed on top of yours — leave the sub-optimal
    commit and split better next time.
  - **Never `git push` without explicit authorization from Alexis** —
    committing locally is encouraged, pushing to `origin` is not. Always ask
    first (« tu veux que je push ? ») even if the commits look clean. This
    applies to `push`, `push --force`, `push --set-upstream`, and any
    equivalent. No exceptions, even for a trivial `docs:` commit.
- **Verify before declaring done.** Run `make test` before saying a change
  is complete. UI changes require a manual check in the browser.

## Models

- `ANTHROPIC_MODEL_MAIN` → `claude-opus-4-7` (agent reasoning, tool planning,
  every sub-agent of the pipeline)
- `ANTHROPIC_MODEL_FAST` → `claude-haiku-4-5` (validation, formatting,
  cheap classification — reserved, not wired everywhere yet)

Both are loaded from `.env` via `api/config.py`.

## Specs and plans — read before structural work

- `docs/superpowers/specs/2026-04-21-microsolder-agent-v1-design.md` — full v1
  design. §2.3 Flow A documents the diagnostic-conversation path; §4 documents
  the on-disk knowledge-pack store.
- `docs/superpowers/specs/2026-04-21-boardview-design.md` — boardview / parser
  spec. §7 has the `.brd` Test_Link field layout the parser is built against.
- `docs/superpowers/plans/2026-04-22-v1-hackathon-shipping-plan.md` — current
  shipping plan (Phases A→D through 2026-04-26). This is the source of truth
  for what ships this week and what is explicitly out of scope.

## Hackathon prize-track context (background, not a rule)

Cerebral Valley announced a **$5 000 "best use of Managed Agents"** track on
top of the main hackathon prize. This is CONTEXT, not scope: do not warp
architectural choices to chase it. We use Managed Agents where they genuinely
fit — the **diagnostic conversation** path (persistent agent + memory store
per device + session event stream + custom tool use, cf. spec §2.3 Flow A). The
**pipeline** path stays on `messages.create` direct because it's batch and
doesn't benefit from session primitives. Never mention prizes in commit
messages, plans, or code — keep the work technically-motivated.
