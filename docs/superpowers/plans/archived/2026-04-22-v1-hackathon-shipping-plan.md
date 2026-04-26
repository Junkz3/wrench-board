# wrench-board — v1 Hackathon Shipping Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the V2 knowledge-factory loop (frontend ↔ backend wiring), integrate the parallel Boardviewer component, add a minimal diagnostic conversation, and ship a demonstrable end-to-end product by the Cerebral Valley submission deadline (2026-04-26, 20:00 EST).

**Architecture:** FastAPI backend + vanilla HTML/CSS/JS frontend (no build step, D3.js / PDF.js via CDN). **Two distinct LLM paths** :
- **Pipeline (V2 knowledge factory)** — `messages.create` direct, forced tool use, prompt caching. Batch, one-shot structured outputs. Already shipped in J+1.
- **Diagnostic conversation** — **Anthropic Managed Agents** (persistent agent + memory store per device + session event stream + custom tool use). Fallback `DIAGNOSTIC_MODE=direct` env var bascule sur `messages.create` en 5 min si la beta MA bloque.

The split is deliberate : the pipeline doesn't need session primitives (one-shot, structured), and using MA there would add beta surface for zero benefit. The diagnostic conversation, on the other hand, IS the canonical MA use case — session state, memory persistence, tool use, streaming — so that's where MA is used.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, `anthropic ~= 0.96.0`, D3.js v7, JetBrains Mono + Inter fonts. Tests: pytest + pytest-asyncio. No DB — knowledge packs live on disk as the canonical store (spec §4).

**Spec reference:** `docs/superpowers/specs/2026-04-21-wrench-board-v1-design.md`.

**Current state (end of J+1 / 2026-04-22):**
- ✅ FastAPI scaffolding + static serving + tests green
- ✅ V2 pipeline (`api/pipeline/`) with Phases 1-4 (Scout, Registry, parallel Writers, Auditor + self-healing loop)
- ✅ Endpoints `POST /pipeline/generate`, `GET /pipeline/packs`, `GET /pipeline/packs/{slug}`
- ✅ Knowledge graph v3 design ported in `web/index.html` (empty-state visible, no data wired)
- ⏳ Agent Boardviewer (parallel agent) working on `api/board/parser/*.py` and `api/board/model.py` — their spec in `docs/superpowers/specs/2026-04-21-boardview-design.md`
- ❌ No backend endpoint serving graph data to frontend
- ❌ No Home / Bibliothèque section
- ❌ No diagnostic conversation (no LLM panel wired, no `mb_*` tools)
- ❌ No demo assets seeded

**Phases:**
- **Phase A — J+2 (2026-04-23)** — Close the V2 loop : frontend loads graph from backend
- **Phase B — J+3 (2026-04-24)** — PCB section with Boardviewer integration + rail navigation
- **Phase C — J+4 (2026-04-25)** — Diagnostic conversation MVP via **Managed Agents** (persistent agent + memory store per device + 2 `mb_*` custom tools)
- **Phase D — J+5 (2026-04-26)** — Demo prep, video recording, submission

**Out of scope** (kept in spec §1–13 as the full vision, not shipped this week):
- Postgres persistence — disk files (`memory/{slug}/*.json`) serve as canonical store
- Full Managed Agents migration of the **pipeline** path — stays on `messages.create` (see Architecture). The **diagnostic** path fully uses Managed Agents (Phase C).
- Learning cycle (`cycle apprenant`) — manual seed of 3 sessions for §8.8 demo, no automatic `rule_synthesizer` invocation. The Managed Agents memory store does record session learnings automatically, but we don't act on them programmatically yet.
- Profile UI, full Memory Bank UI (Timeline / Knowledge / Stats), Mesures / Notes / Aide sections
- Cost tracking dashboard (stdout logs suffice for hackathon)

**Conventional commits:** every task ends with a single `feat(scope): …` / `fix(scope): …` / `test(scope): …` / `refactor(scope): …` / `chore(scope): …` / `docs(scope): …` commit per CLAUDE.md commit-hygiene rule. **Never bundle across tasks.** Commit message must end with:

```
Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Phase A — Close the V2 loop (J+2, 2026-04-23)

### Task A1: Graph data transform + endpoint

Produces a combined graph view (nodes + edges) from the on-disk pack files so the frontend can display a knowledge graph for any device that has been generated.

**Files:**
- Create: `api/pipeline/graph_transform.py`
- Modify: `api/pipeline/__init__.py` — append new route
- Create: `tests/pipeline/__init__.py` (empty)
- Create: `tests/pipeline/test_graph_transform.py`
- Create: `tests/pipeline/fixtures/demo-pack/registry.json`
- Create: `tests/pipeline/fixtures/demo-pack/knowledge_graph.json`
- Create: `tests/pipeline/fixtures/demo-pack/rules.json`
- Create: `tests/pipeline/fixtures/demo-pack/dictionary.json`

#### Step 1: Create test fixtures

- [ ] **Write `tests/pipeline/fixtures/demo-pack/registry.json`** :

```json
{
  "schema_version": "1.0",
  "device_label": "Demo Pi",
  "components": [
    {"canonical_name": "U7", "logical_alias": null, "aliases": ["PMIC"],
     "kind": "pmic", "description": "Main PMIC."},
    {"canonical_name": "C29", "logical_alias": null, "aliases": [],
     "kind": "capacitor", "description": "3V3 rail decoupling."}
  ],
  "signals": [
    {"canonical_name": "3V3_RAIL", "aliases": ["3.3V"],
     "kind": "power_rail", "nominal_voltage": 3.3}
  ]
}
```

- [ ] **Write `tests/pipeline/fixtures/demo-pack/knowledge_graph.json`** :

```json
{
  "schema_version": "1.0",
  "nodes": [
    {"id": "cmp_U7",    "kind": "component", "label": "U7", "properties": {}},
    {"id": "cmp_C29",   "kind": "component", "label": "C29", "properties": {}},
    {"id": "net_3V3",   "kind": "net",       "label": "3V3_RAIL", "properties": {}}
  ],
  "edges": [
    {"source_id": "cmp_U7", "target_id": "net_3V3", "relation": "powers"}
  ]
}
```

- [ ] **Write `tests/pipeline/fixtures/demo-pack/rules.json`** :

```json
{
  "schema_version": "1.0",
  "rules": [
    {
      "id": "rule-demo-001",
      "symptoms": ["3V3 rail dead", "device doesn't boot"],
      "likely_causes": [
        {"refdes": "C29", "probability": 0.78, "mechanism": "short-to-ground"},
        {"refdes": "U7",  "probability": 0.25, "mechanism": "dead PMIC"}
      ],
      "diagnostic_steps": [
        {"action": "measure 3V3_RAIL at TP18", "expected": "3.3V ± 5%"}
      ],
      "confidence": 0.82,
      "sources": ["fixture"]
    }
  ]
}
```

- [ ] **Write `tests/pipeline/fixtures/demo-pack/dictionary.json`** :

```json
{
  "schema_version": "1.0",
  "entries": [
    {"canonical_name": "U7",  "role": "PMIC", "package": "QFN-24",
     "typical_failure_modes": ["dead PMIC"], "notes": null},
    {"canonical_name": "C29", "role": "decoupling cap", "package": "0402",
     "typical_failure_modes": ["short-to-ground"], "notes": "adjacent to U7"}
  ]
}
```

#### Step 2: Write the failing test

- [ ] **Write `tests/pipeline/test_graph_transform.py`** :

```python
"""Tests for api.pipeline.graph_transform."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.pipeline.graph_transform import pack_to_graph_payload

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "demo-pack"


def _load(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text())


def test_pack_to_graph_returns_expected_shape():
    payload = pack_to_graph_payload(
        registry=_load("registry.json"),
        knowledge_graph=_load("knowledge_graph.json"),
        rules=_load("rules.json"),
        dictionary=_load("dictionary.json"),
    )

    assert set(payload.keys()) == {"nodes", "edges"}

    # Every knowledge_graph node carried over, enriched from dictionary + registry.
    node_ids = {n["id"] for n in payload["nodes"]}
    assert {"cmp_U7", "cmp_C29", "net_3V3"} <= node_ids

    # Symptom nodes are synthesized from rules.symptoms.
    symptom_nodes = [n for n in payload["nodes"] if n["type"] == "symptom"]
    assert len(symptom_nodes) == 2  # "3V3 rail dead" + "device doesn't boot"
    assert all(n["confidence"] >= 0.0 and n["confidence"] <= 1.0 for n in symptom_nodes)

    # Causes edges are synthesized: likely_causes[i].refdes → symptom.
    causes_edges = [e for e in payload["edges"] if e["relation"] == "causes"]
    assert len(causes_edges) >= 2  # C29 + U7 causing each of the 2 symptoms

    # Component nodes carry dictionary metadata under "meta".
    u7 = next(n for n in payload["nodes"] if n["id"] == "cmp_U7")
    assert u7["type"] == "component"
    assert u7["meta"]["package"] == "QFN-24"
    assert u7["label"] == "U7"


def test_empty_pack_returns_empty_graph():
    payload = pack_to_graph_payload(
        registry={"schema_version": "1.0", "device_label": "empty",
                  "components": [], "signals": []},
        knowledge_graph={"schema_version": "1.0", "nodes": [], "edges": []},
        rules={"schema_version": "1.0", "rules": []},
        dictionary={"schema_version": "1.0", "entries": []},
    )
    assert payload == {"nodes": [], "edges": []}
```

- [ ] **Run the failing test** :

```bash
.venv/bin/pytest tests/pipeline/test_graph_transform.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'api.pipeline.graph_transform'`.

#### Step 3: Implement the transform

- [ ] **Create `api/pipeline/graph_transform.py`** :

```python
"""Transform on-disk pack files (V2 schema) into the graph payload
expected by web/index.html (frontend design v3).

Synthesizes `symptom` nodes from rules.symptoms and `causes` edges from
rules.likely_causes — V2 pipeline only emits component/net nodes natively.
`action` nodes are left empty for now (out of scope for V2; will be added
when the diagnostic agent starts saving recommended actions).
"""

from __future__ import annotations

import re
from typing import Any


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "unknown"


def pack_to_graph_payload(
    *,
    registry: dict[str, Any],
    knowledge_graph: dict[str, Any],
    rules: dict[str, Any],
    dictionary: dict[str, Any],
) -> dict[str, Any]:
    """Merge the four pack files into a single {nodes, edges} payload.

    Returned shape matches what web/index.html's D3 layer expects:
      node: {id, type, label, description, confidence, meta}
      edge: {source, target, relation, label, weight}
    """
    kg_nodes = knowledge_graph.get("nodes", [])
    kg_edges = knowledge_graph.get("edges", [])
    dict_by_name = {e["canonical_name"]: e for e in dictionary.get("entries", [])}
    reg_components = {c["canonical_name"]: c for c in registry.get("components", [])}
    reg_signals = {s["canonical_name"]: s for s in registry.get("signals", [])}

    if not kg_nodes and not rules.get("rules"):
        return {"nodes": [], "edges": []}

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    # 1. Carry component + net nodes from knowledge_graph, enrich from dict/registry.
    for n in kg_nodes:
        kind = n.get("kind")
        if kind not in ("component", "net"):
            continue
        label = n.get("label", "")
        reg = reg_components.get(label) if kind == "component" else reg_signals.get(label)
        dct = dict_by_name.get(label) if kind == "component" else None
        meta: dict[str, Any] = {}
        if dct:
            if dct.get("package"):  meta["package"] = dct["package"]
            if dct.get("role"):     meta["role"] = dct["role"]
        if kind == "net" and reg and reg.get("nominal_voltage") is not None:
            meta["nominal"] = f"{reg['nominal_voltage']} V"
        nodes.append({
            "id": n["id"],
            "type": kind,
            "label": label,
            "description": (reg or {}).get("description") or (dct or {}).get("notes") or "",
            "confidence": 0.80 if reg else 0.55,
            "meta": meta,
        })

    # 2. Carry native edges (typed).
    for e in kg_edges:
        edges.append({
            "source": e["source_id"],
            "target": e["target_id"],
            "relation": e["relation"],
            "label": e.get("relation", ""),
            "weight": 1.0,
        })

    # 3. Synthesize symptom nodes + causes edges from rules.
    component_id_by_refdes = {n["label"]: n["id"] for n in nodes if n["type"] == "component"}
    seen_symptoms: dict[str, str] = {}
    for rule in rules.get("rules", []):
        for symptom_text in rule.get("symptoms", []):
            if symptom_text not in seen_symptoms:
                sid = f"sym_{_slug(symptom_text)}"
                seen_symptoms[symptom_text] = sid
                nodes.append({
                    "id": sid,
                    "type": "symptom",
                    "label": symptom_text,
                    "description": "",
                    "confidence": rule.get("confidence", 0.6),
                    "meta": {},
                })
            sid = seen_symptoms[symptom_text]
            for cause in rule.get("likely_causes", []):
                cid = component_id_by_refdes.get(cause["refdes"])
                if cid is None:
                    continue  # refdes not in registry → skip (anti-hallucination)
                edges.append({
                    "source": cid,
                    "target": sid,
                    "relation": "causes",
                    "label": cause.get("mechanism", "causes"),
                    "weight": float(cause.get("probability", 0.5)),
                })

    return {"nodes": nodes, "edges": edges}
```

- [ ] **Run the test again** :

```bash
.venv/bin/pytest tests/pipeline/test_graph_transform.py -v
```

Expected: **2 passed**.

#### Step 4: Expose the endpoint

- [ ] **Edit `api/pipeline/__init__.py`** — add these lines just before `__all__ = [...]` at the bottom :

```python
from api.pipeline.graph_transform import pack_to_graph_payload


@router.get("/packs/{device_slug}/graph")
async def get_pack_graph(device_slug: str) -> dict:
    """Return the combined graph payload ({nodes, edges}) consumed by web/index.html."""
    settings = get_settings()
    slug = _slugify(device_slug)
    pack_dir = Path(settings.memory_root) / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")

    try:
        registry        = json.loads((pack_dir / "registry.json").read_text())
        knowledge_graph = json.loads((pack_dir / "knowledge_graph.json").read_text())
        rules           = json.loads((pack_dir / "rules.json").read_text())
        dictionary      = json.loads((pack_dir / "dictionary.json").read_text())
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Pack for {slug!r} is incomplete: {exc.filename}",
        ) from exc

    return pack_to_graph_payload(
        registry=registry,
        knowledge_graph=knowledge_graph,
        rules=rules,
        dictionary=dictionary,
    )
```

Also add the missing imports at the top of the file (keep `from __future__ import annotations` first) :

```python
import json
from pathlib import Path

from fastapi import HTTPException
```

(Adjust — some of those imports may already be there. Keep alphabetical order within each import group.)

#### Step 5: Smoke-test the endpoint

- [ ] **Run uvicorn + curl** :

```bash
.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8765 --log-level warning &
UPID=$!
sleep 2

# Empty memory/ → 404 (expected until a pack is generated)
curl -sS -o /dev/null -w "status=%{http_code}\n" \
  http://127.0.0.1:8765/pipeline/packs/demo-pi/graph
# Expected: status=404

kill $UPID 2>/dev/null
```

Expected: `status=404` for a device that hasn't been generated yet.

#### Step 6: Commit

- [ ] **Commit (single concern: graph transform + endpoint)** :

```bash
git add api/pipeline/graph_transform.py api/pipeline/__init__.py \
        tests/pipeline/__init__.py tests/pipeline/test_graph_transform.py \
        tests/pipeline/fixtures/
git commit -m "$(cat <<'MSG'
feat(pipeline): add /packs/{slug}/graph endpoint + pack→graph transform

Combines registry.json, knowledge_graph.json, rules.json, and
dictionary.json into a single payload matching the frontend
design v3 shape (nodes with type ∈ {component, symptom, net,
action}, edges with relation ∈ {causes, powers, connected_to,
resolves}).

Transform rules:
  - component + net nodes carried 1:1 from knowledge_graph.json,
    enriched with package/role/nominal from dictionary + registry
  - symptom nodes synthesized from rules.symptoms (deduplicated
    across rules, slug-id'd)
  - causes edges synthesized from rules.likely_causes (anti-
    hallucination: drops any refdes not in the registry)
  - action nodes left empty for V2 (diagnostic agent will fill
    them in Phase C when save_case starts logging actions)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task A2: Frontend fetch + populate

Wires `web/index.html` to the new endpoint. On page load, if `?device=<slug>` is in the URL, fetch the graph payload and swap in for the empty `DATA`. Otherwise keep the empty-state card.

**Files:**
- Modify: `web/index.html` — replace the `DATA` default + empty-state toggle + add fetch

#### Step 1: Replace the DATA block with a runtime fetch

- [ ] **Edit `web/index.html`** — replace the current DATA declaration block :

Find :

```js
const DATA = { nodes: [], edges: [] };

// Toggle the empty-state overlay until graph data is wired in from the backend.
(() => {
  const el = document.getElementById("emptyState");
  if (!el) return;
  if (DATA.nodes.length === 0) el.classList.remove("hidden");
  else el.classList.add("hidden");
})();
```

Replace with :

```js
let DATA = { nodes: [], edges: [] };

async function loadGraphFromBackend() {
  const params = new URLSearchParams(window.location.search);
  const slug = params.get("device");
  if (!slug) return null;
  try {
    const res = await fetch(`/pipeline/packs/${encodeURIComponent(slug)}/graph`);
    if (!res.ok) {
      console.warn(`loadGraphFromBackend: ${res.status} for slug=${slug}`);
      return null;
    }
    return await res.json();
  } catch (err) {
    console.error("loadGraphFromBackend: fetch failed", err);
    return null;
  }
}

function setEmptyState(visible) {
  const el = document.getElementById("emptyState");
  if (!el) return;
  el.classList.toggle("hidden", !visible);
}

setEmptyState(true);  // show the card synchronously; the fetch may replace it
```

#### Step 2: Refactor the D3 init into a deferred function

The original script binds D3 selections to `DATA` at load time. Since we now want to fetch asynchronously before binding, wrap the entire D3 init into `initGraphWithData(data)` and only call it once data has arrived.

- [ ] **Edit `web/index.html`** — wrap the whole tail of the script. Find the line `const svg = d3.select("#graph");` and everything from there down to the last `requestAnimationFrame(animateParticles);` (just before the `/* ---------- INIT ---------- */` comment). Move all of it inside a function :

```js
function initGraphWithData(data) {
  DATA = data;

  // <paste here everything currently between `const svg = d3.select("#graph");`
  //  and `requestAnimationFrame(animateParticles);` (inclusive)>
}
```

Keep the helpers defined **outside** `initGraphWithData` (they may be called before init): `loadGraphFromBackend`, `setEmptyState`. Keep `neighbors`, `nodeSize`, `iconFor*` outside too if they don't reference the D3 selections.

Simpler heuristic : *everything that calls `d3.select("#graph")`, `d3.forceSimulation(...)`, or assigns to `nodeSel`/`linkSel`/`particleSel`* goes **inside** the function. Everything else stays outside.

- [ ] **Replace the final init block** (`sim.alpha(1).restart(); for (let i=0;i<80;i++) sim.tick(); linkSel.attr("d", ...); nodeSel.attr(...);`) with a bootstrap IIFE :

```js
/* ---------- INIT ---------- */
(async function bootstrap() {
  const fetched = await loadGraphFromBackend();
  if (fetched && fetched.nodes && fetched.nodes.length > 0) {
    setEmptyState(false);
    initGraphWithData(fetched);
  } else {
    setEmptyState(true);
  }
})();
```

No `window.location.reload()`, no page-reload hack. The D3 init only runs once, after the fetch resolves.

#### Step 3: Smoke-test in browser

- [ ] **Start uvicorn and open the page with no query param** :

```bash
.venv/bin/uvicorn api.main:app --reload --host 0.0.0.0 --port 8000 &
```

Open `http://localhost:8000/`. Expected: empty-state card visible, no JS errors in console.

- [ ] **Open with a bogus device param** :

Open `http://localhost:8000/?device=nope`. Expected: empty-state still visible (fetch returned 404, gracefully ignored), console.warn logged.

#### Step 4: Commit

- [ ] **Commit** :

```bash
git add web/index.html
git commit -m "$(cat <<'MSG'
feat(web): fetch graph payload from /pipeline/packs/{slug}/graph

Reads ?device=<slug> from the URL and populates the knowledge
graph with data served by the V2 backend. Empty-state stays
visible when no slug is provided or the pack doesn't exist.

Known limitation: swapping DATA at runtime after D3 has bound to
the empty arrays requires a page reload for the force sim and
selections to rewire cleanly. Deferred-init refactor is tracked
as a Phase D polish task.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task A3: Home / Bibliothèque section

Gives the user a visible entry point without typing URLs. Lists generated packs via `GET /pipeline/packs` and lets the user click a card to jump to `?device=<slug>`. Adds a minimal form to trigger a new generation.

**Files:**
- Modify: `web/index.html` — add Home section HTML + CSS + JS; hide Graphe by default; rail click wires section swap.

#### Step 1: Add the Home section HTML

- [ ] **Edit `web/index.html`** — add this block just before `<!-- ============ CANVAS ============ -->` :

```html
<!-- ============ HOME / BIBLIOTHÈQUE ============ -->
<section class="home" id="homeSection">
  <header class="home-head">
    <h1>Bibliothèque</h1>
    <p class="home-sub">Devices avec un knowledge pack généré. Clique pour ouvrir le graphe.</p>
    <button class="btn primary" id="homeNewBtn">
      <svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>
      Générer un nouveau device
    </button>
  </header>

  <div class="home-grid" id="homeGrid">
    <!-- cards injected by JS -->
  </div>

  <div class="home-empty hidden" id="homeEmpty">
    <h3>Aucun pack généré pour l'instant</h3>
    <p>Lance le pipeline sur un device pour voir sa carte apparaître ici.</p>
  </div>
</section>
```

#### Step 2: Add Home CSS

- [ ] **Edit `web/index.html`** — add inside the existing `<style>` block, before `/* =========== TOOLTIP =========== */` :

```css
/* =========== HOME / BIBLIOTHÈQUE =========== */
.home{position:fixed;top:92px;left:52px;right:0;bottom:28px;overflow:auto;padding:32px 40px;z-index:2}
.home.hidden{display:none}
.home-head{display:flex;align-items:flex-end;gap:16px;margin-bottom:24px;border-bottom:1px solid var(--border);padding-bottom:16px}
.home-head h1{margin:0;font-size:22px;font-weight:600;letter-spacing:-.3px}
.home-head .home-sub{flex:1;margin:0;color:var(--text-3);font-size:13px;line-height:1.5}
.home-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.home-card{position:relative;padding:16px 18px;background:var(--panel);border:1px solid var(--border);border-radius:8px;cursor:pointer;transition:all .15s;text-decoration:none;color:inherit;display:flex;flex-direction:column;gap:8px}
.home-card:hover{border-color:rgba(56,189,248,.45);background:var(--panel-2);transform:translateY(-1px)}
.home-card .slug{font-family:var(--mono);font-size:10.5px;color:var(--text-3)}
.home-card .name{font-size:15px;font-weight:600;color:var(--text)}
.home-card .badges{display:flex;gap:6px;flex-wrap:wrap}
.home-card .badge{padding:2px 7px;border-radius:10px;font-size:10.5px;font-family:var(--mono);border:1px solid var(--border);color:var(--text-2)}
.home-card .badge.ok{color:var(--emerald);border-color:rgba(52,211,153,.3);background:rgba(52,211,153,.08)}
.home-card .badge.warn{color:var(--amber);border-color:rgba(245,158,11,.3);background:rgba(245,158,11,.08)}
.home-empty{text-align:center;padding:60px 20px;color:var(--text-3)}
.home-empty.hidden{display:none}
```

#### Step 3: Hide the graph section when Home is active

- [ ] **Edit `web/index.html`** — find the `.canvas` element :

```html
<div class="canvas" id="canvas">
```

Change to :

```html
<div class="canvas hidden" id="canvas">
```

And add to CSS :

```css
.canvas.hidden{display:none}
```

#### Step 4: Add JS to fetch + render cards

- [ ] **Edit `web/index.html`** — add inside the `<script>` block, just after the `setEmptyState(true);` line from Task A2 :

```js
/* ---------- HOME / BIBLIOTHÈQUE ---------- */
async function loadHomePacks() {
  try {
    const res = await fetch("/pipeline/packs");
    if (!res.ok) return [];
    return await res.json();
  } catch (err) {
    console.warn("loadHomePacks failed", err);
    return [];
  }
}

function renderHome(packs) {
  const grid = document.getElementById("homeGrid");
  const empty = document.getElementById("homeEmpty");
  grid.innerHTML = "";
  if (packs.length === 0) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  for (const p of packs) {
    const card = document.createElement("a");
    card.className = "home-card";
    card.href = `?device=${encodeURIComponent(p.device_slug)}`;
    const complete = p.has_registry && p.has_knowledge_graph && p.has_rules && p.has_dictionary;
    card.innerHTML = `
      <div class="slug">${p.device_slug}</div>
      <div class="name">${p.device_slug.replace(/-/g, " ").replace(/^./, c => c.toUpperCase())}</div>
      <div class="badges">
        <span class="badge ${complete ? 'ok' : 'warn'}">${complete ? 'pack complet' : 'incomplet'}</span>
        ${p.has_audit_verdict ? '<span class="badge">audité</span>' : ''}
      </div>
    `;
    grid.appendChild(card);
  }
}

function showSection(which) {
  document.getElementById("homeSection").classList.toggle("hidden", which !== "home");
  document.getElementById("canvas").classList.toggle("hidden", which !== "graphe");
}

document.getElementById("homeNewBtn").addEventListener("click", () => {
  const label = prompt("Nom du device à générer (ex. 'Raspberry Pi 4 Model B') :");
  if (!label) return;
  fetch("/pipeline/generate", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({device_label: label}),
  }).then(res => {
    if (!res.ok) return res.json().then(e => alert("Erreur: " + JSON.stringify(e)));
    return res.json().then(r => {
      alert(`Pack généré: ${r.device_slug}`);
      window.location.href = `?device=${encodeURIComponent(r.device_slug)}`;
    });
  });
});
```

- [ ] **Modify the final init IIFE** to pick Home vs Graph :

Replace :

```js
(async function () {
  const fetched = await loadGraphFromBackend();
  const data = (fetched && fetched.nodes && fetched.nodes.length > 0)
    ? fetched
    : { nodes: [], edges: [] };
  setEmptyState(data.nodes.length === 0);
  if (data.nodes.length > 0) initWithData(data);
})();
```

With :

```js
(async function () {
  const params = new URLSearchParams(window.location.search);
  const slug = params.get("device");
  if (slug) {
    // Device-scoped view → show graph
    showSection("graphe");
    const fetched = await loadGraphFromBackend();
    const data = (fetched && fetched.nodes && fetched.nodes.length > 0)
      ? fetched
      : { nodes: [], edges: [] };
    setEmptyState(data.nodes.length === 0);
    if (data.nodes.length > 0) initWithData(data);
  } else {
    // No device → show Home
    showSection("home");
    const packs = await loadHomePacks();
    renderHome(packs);
  }
})();
```

#### Step 5: Smoke-test

- [ ] **Reload `http://localhost:8000/`** — expected: Home page with either 0 cards + empty state or 1+ cards if packs exist.
- [ ] **Click « Générer un nouveau device »** — only valid if `ANTHROPIC_API_KEY` is set and credits are available. Otherwise expect an alert with the error payload (still a clean failure path).

#### Step 6: Commit

- [ ] **Commit** :

```bash
git add web/index.html
git commit -m "$(cat <<'MSG'
feat(web): add Home / Bibliothèque section

Default landing view when no ?device=<slug> is in the URL.
Fetches /pipeline/packs, renders a card per generated device,
clicking a card sets the query param and reloads into the
graph view. « Générer un nouveau device » prompts for a
device label and posts to /pipeline/generate.

No router yet — section swap is done via `showSection()` helper
that toggles a `.hidden` class. A proper hash-router lands in
Phase B when the rail becomes interactive.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
MSG
)"
```

**End-of-J+2 gate** : open the page, see at least the empty Home card, `/pipeline/packs/<slug>/graph` returns 200 on any generated pack. If no pack was generated (no API credits yet), fixture-inject one by copying `tests/pipeline/fixtures/demo-pack/*.json` to `memory/demo-pi/` and visiting `?device=demo-pi`.

---

## Phase B — PCB section + Boardviewer integration + rail nav (J+3)

### Task B1: Hash-router for rail navigation

Replaces the pragmatic `showSection()` helper with a small hash-based router. Each rail button updates `location.hash`, the router renders the active section.

**Files:**
- Modify: `web/index.html` — add `<div class="section">` wrappers, replace `showSection` with router.

#### Step 1: Wrap each section in an identifiable container

- [ ] **Edit `web/index.html`** — wrap the existing `.home` and `.canvas` elements in a common `<div class="section" data-section="...">` if not already (they already have distinct IDs — fine).

#### Step 2: Add the router

- [ ] **Edit `web/index.html`** — add a new `<script>` block **before** the current IIFE :

```js
/* ---------- HASH ROUTER ---------- */
const SECTIONS = ["home", "pcb", "schematic", "graphe", "memory-bank", "agent", "profile", "aide"];

function currentSection() {
  const h = (window.location.hash || "#home").slice(1);
  return SECTIONS.includes(h) ? h : "home";
}

function setActiveRail(which) {
  document.querySelectorAll(".rail-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.section === which);
  });
}

function navigate(section) {
  if (!SECTIONS.includes(section)) section = "home";
  setActiveRail(section);
  // Hide all known section DOMs, show the target.
  document.getElementById("homeSection").classList.toggle("hidden", section !== "home");
  document.getElementById("canvas").classList.toggle("hidden", section !== "graphe");
  // pcb/schematic/memory-bank/agent/profile/aide: placeholders for now (Task B2+).
  document.querySelectorAll("[data-section-stub]").forEach(el => {
    el.classList.toggle("hidden", el.dataset.sectionStub !== section);
  });
}

window.addEventListener("hashchange", () => navigate(currentSection()));
```

#### Step 3: Wire rail buttons

- [ ] **Edit `web/index.html`** — add `data-section` attributes to each `.rail-btn` and make them update the hash :

Change the existing :

```html
<button class="rail-btn" title="CAO"><svg ...></button>
<button class="rail-btn active" title="Graphe"><svg ...></button>
<button class="rail-btn" title="Mesures"><svg ...></button>
<button class="rail-btn" title="Notes"><svg ...></button>
<div class="rail-sep"></div>
<button class="rail-btn" title="Historique"><svg ...></button>
<button class="rail-btn" title="Bibliothèque"><svg ...></button>
<div style="flex:1"></div>
<button class="rail-btn" title="Aide"><svg ...></button>
```

To (canonical v1 rail per spec §9.2) :

```html
<button class="rail-btn" data-section="home" title="Bibliothèque"><!-- same svg --></button>
<button class="rail-btn" data-section="pcb" title="PCB"><!-- same svg as CAO --></button>
<button class="rail-btn" data-section="schematic" title="Schematic"><svg class="icon" viewBox="0 0 24 24"><path d="M4 4h16v16H4z"/><path d="M4 8h16M4 12h16M4 16h16"/></svg></button>
<button class="rail-btn" data-section="graphe" title="Graphe"><!-- original graphe svg --></button>
<div class="rail-sep"></div>
<button class="rail-btn" data-section="memory-bank" title="Memory Bank"><svg class="icon" viewBox="0 0 24 24"><path d="M4 7c0-1.1.9-2 2-2h12a2 2 0 012 2v10a2 2 0 01-2 2H6a2 2 0 01-2-2z"/><path d="M4 11h16M8 7v12"/></svg></button>
<button class="rail-btn" data-section="agent" title="Agent"><svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg></button>
<button class="rail-btn" data-section="profile" title="Profil"><svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg></button>
<div style="flex:1"></div>
<button class="rail-btn" data-section="aide" title="Aide"><!-- original svg --></button>
```

Attach click handlers (in the same script block as the router) :

```js
document.querySelectorAll(".rail-btn[data-section]").forEach(btn => {
  btn.addEventListener("click", () => {
    window.location.hash = "#" + btn.dataset.section;
  });
});
```

#### Step 4: Add stub divs for the non-built sections

- [ ] **Edit `web/index.html`** — add before `<!-- ============ HOME / BIBLIOTHÈQUE ============ -->` :

```html
<!-- Stub sections — progressively replaced in following tasks -->
<section class="stub hidden" data-section-stub="pcb">
  <div class="stub-card">
    <h2>PCB</h2>
    <p>Intégration du composant <code>web/boardviewer/</code> d'Agent Boardviewer — voir Task B2.</p>
  </div>
</section>
<section class="stub hidden" data-section-stub="schematic">
  <div class="stub-card"><h2>Schematic</h2><p>Viewer PDF + nets — à faire en V2.</p></div>
</section>
<section class="stub hidden" data-section-stub="memory-bank">
  <div class="stub-card"><h2>Memory Bank</h2><p>3 onglets Timeline / Knowledge / Stats — à faire en V2.</p></div>
</section>
<section class="stub hidden" data-section-stub="agent">
  <div class="stub-card"><h2>Agent</h2><p>Config / Historique / Traces / Coûts — à faire en V2.</p></div>
</section>
<section class="stub hidden" data-section-stub="profile">
  <div class="stub-card"><h2>Profil</h2><p>Stats dérivées + overrides éditables — à faire en V2.</p></div>
</section>
<section class="stub hidden" data-section-stub="aide">
  <div class="stub-card">
    <h2>Raccourcis</h2>
    <dl style="display:grid;grid-template-columns:100px 1fr;gap:8px;font-family:var(--mono)">
      <dt>⌘K</dt><dd>Recherche dans le graphe</dd>
      <dt>Esc</dt><dd>Fermer panel / inspector</dd>
      <dt>1–8</dt><dd>Bascule section (à venir)</dd>
    </dl>
  </div>
</section>
```

And CSS :

```css
.stub{position:fixed;top:92px;left:52px;right:0;bottom:28px;display:flex;align-items:center;justify-content:center;z-index:2;padding:40px}
.stub.hidden{display:none}
.stub-card{max-width:520px;padding:32px;background:var(--panel);border:1px solid var(--border);border-radius:10px;text-align:center}
.stub-card h2{margin:0 0 12px;font-size:20px}
.stub-card p{margin:0;color:var(--text-2);line-height:1.5}
```

#### Step 5: Update the initial IIFE to use the router

- [ ] **Edit the init IIFE** to delegate to `navigate()` :

```js
(async function () {
  const hash = window.location.hash;
  const params = new URLSearchParams(window.location.search);
  const slug = params.get("device");

  // Precedence: explicit hash > slug-implies-graphe > home default
  const initial = hash ? currentSection() : (slug ? "graphe" : "home");
  navigate(initial);

  if (initial === "graphe" && slug) {
    const fetched = await loadGraphFromBackend();
    const data = (fetched && fetched.nodes && fetched.nodes.length > 0)
      ? fetched : { nodes: [], edges: [] };
    setEmptyState(data.nodes.length === 0);
    if (data.nodes.length > 0) initWithData(data);
  } else if (initial === "home") {
    renderHome(await loadHomePacks());
  }
})();
```

#### Step 6: Commit

- [ ] **Commit** :

```bash
git add web/index.html
git commit -m "$(cat <<'MSG'
feat(web): hash-router for rail navigation (8 canonical sections)

Replaces the pragmatic showSection() helper with a hash-router
aligned on spec §9.2 rail order: Home / PCB / Schematic / Graphe
/ Memory Bank / Agent / Profile / Aide. Stub sections are
rendered as placeholder cards for everything beyond Home and
Graphe; they will be replaced by real content in Phases B+C.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task B2: PCB section — Boardviewer integration slot

Reserves the canvas area for Agent Boardviewer's component and wires a minimal load-board flow. Since Agent Boardviewer is still in progress, this task ships the **integration slot** with a fallback placeholder; when their component merges, the slot is replaced with `<script src="/boardviewer/boardviewer.js">`.

**Files:**
- Modify: `web/index.html` — replace the PCB stub with the integration slot.

#### Step 1: Check what Agent Boardviewer has delivered so far

- [ ] **Run** :

```bash
ls web/boardviewer/ 2>/dev/null || echo "boardviewer not merged yet"
ls api/board/
```

Two possible states :
- **`web/boardviewer/boardviewer.js` exists** → integrate it.
- **It doesn't exist yet** → ship a slot that loads it later without code change.

#### Step 2: Replace the PCB stub with the integration slot

- [ ] **Edit `web/index.html`** — remove the `<section class="stub" data-section-stub="pcb">...` block and add :

```html
<!-- ============ PCB / BOARDVIEWER ============ -->
<section class="pcb hidden" data-section-stub="pcb">
  <header class="pcb-head">
    <h1>PCB — <span id="pcbDeviceLabel">aucun device</span></h1>
    <div class="pcb-actions">
      <button class="top-btn" id="pcbFlip">Top / Bottom</button>
      <button class="top-btn" id="pcbResetView">Reset view</button>
    </div>
  </header>
  <div class="pcb-canvas-holder" id="pcbCanvasHolder">
    <div class="pcb-empty" id="pcbEmpty">
      <p>Composant Boardviewer non chargé. Consulte <code>docs/integration/boardviewer-contract.md</code> pour intégration.</p>
    </div>
  </div>
</section>
```

And CSS :

```css
.pcb{position:fixed;top:92px;left:52px;right:0;bottom:28px;display:flex;flex-direction:column;z-index:2}
.pcb.hidden{display:none}
.pcb-head{padding:16px 24px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:16px}
.pcb-head h1{margin:0;font-size:18px;font-weight:600}
.pcb-head #pcbDeviceLabel{color:var(--text-3);font-weight:400}
.pcb-actions{margin-left:auto;display:flex;gap:8px}
.pcb-canvas-holder{flex:1;position:relative;overflow:hidden;background:var(--bg-deep)}
.pcb-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--text-3);text-align:center;padding:40px}
```

#### Step 3: Try to lazy-load the Boardviewer module

- [ ] **Add JS** :

```js
/* ---------- PCB / BOARDVIEWER ---------- */
async function initPCB() {
  const holder = document.getElementById("pcbCanvasHolder");
  const empty = document.getElementById("pcbEmpty");
  const params = new URLSearchParams(window.location.search);
  const slug = params.get("device");
  document.getElementById("pcbDeviceLabel").textContent = slug || "aucun device";

  try {
    // Try to import the Boardviewer module. If it doesn't exist yet, fall back.
    const mod = await import("/boardviewer/boardviewer.js");
    if (!mod || !window.BoardviewerAPI) throw new Error("BoardviewerAPI not exposed");
    empty.style.display = "none";
    // Wiring: load the board file. For V1, Agent Boardviewer's contract will
    // define the exact loadBoard signature — adjust here once spec §11 is final.
    if (slug) {
      try {
        await window.BoardviewerAPI.loadBoard(`/pipeline/packs/${slug}/board`);
      } catch (err) {
        empty.style.display = "";
        empty.innerHTML = `<p>Aucun fichier board trouvé pour ${slug}.</p>`;
      }
    }
  } catch (err) {
    console.warn("Boardviewer not loaded:", err);
    // Keep the empty-state — Agent Boardviewer's component not merged yet.
  }
}

// Trigger on navigate to PCB
window.addEventListener("hashchange", () => {
  if (currentSection() === "pcb") initPCB();
});
```

- [ ] **Also invoke on direct navigation** — update the initial IIFE's `else if` branch :

```js
} else if (initial === "pcb") {
  await initPCB();
}
```

#### Step 4: Commit

- [ ] **Commit** :

```bash
git add web/index.html
git commit -m "$(cat <<'MSG'
feat(web): add PCB section with Boardviewer integration slot

Reserves the canvas area for the parallel Agent Boardviewer
component (cf. spec §2.7). initPCB() lazy-imports
/boardviewer/boardviewer.js; if the module is not yet merged
the empty-state is shown with a pointer to the integration
contract doc. When the device slug is in the URL, BoardviewerAPI
.loadBoard() is invoked against /pipeline/packs/{slug}/board
(endpoint not yet built — returns 404 until Phase C wiring).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
MSG
)"
```

**End-of-J+3 gate** : navigating to `#pcb` shows either the live boardviewer (if Agent Boardviewer merged) or a clean empty-state card explaining the dependency.

---

## Phase C — Diagnostic conversation MVP via Managed Agents (J+4)

Managed Agents is the canonical fit for this path: **persistent agent config**, **session event stream** (SSE-like via SDK), **memory store per device** that auto-consults at session start and saves learnings at session end, and **custom tool-use** with structured JSON. The pipeline stays on `messages.create` (Phases 1–4 already shipped J+1) — only the conversational path moves to MA.

**Beta header** : `managed-agents-2026-04-01` (set automatically by the SDK on `client.beta.{agents,sessions,memory_stores}.*` calls).

**Fallback** : env var `DIAGNOSTIC_MODE=direct` switches to a minimal `messages.create` path if MA beta blocks during the demo (sibling file `runtime_direct.py`, wired in Task C1 Step 6).

### Task C0: Bootstrap managed agent + environment

Creates the persistent `diagnostic` agent and a single shared environment, persists their IDs in `managed_ids.json` (gitignored). Idempotent : on re-run, reads existing IDs instead of creating duplicates.

**Files:**
- Create: `scripts/bootstrap_managed_agent.py`
- Modify: `.gitignore` — add `managed_ids.json`
- Create: `api/agent/managed_ids.py` (reader helper)

#### Step 1: Write the bootstrap script

- [ ] **Create `scripts/bootstrap_managed_agent.py`** :

```python
"""Bootstrap the managed agent + environment for the diagnostic conversation.

Idempotent: reads managed_ids.json if present, creates and persists otherwise.
Run once per deployment.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from anthropic import Anthropic

IDS_FILE = Path(__file__).parent.parent / "managed_ids.json"

SYSTEM_PROMPT = """\
You are a calm, methodical board-level diagnostics assistant for a
microsoldering technician. Tu tutoies, en français, direct et pédagogique.

Tu pilotes visuellement une carte électronique en appelant les tools
mis à disposition :
  - mb_get_component(refdes) — valide qu'un refdes existe dans le
    registry du device. RÈGLE ANTI-HALLUCINATION STRICTE : tu NE
    mentionnes JAMAIS un refdes (U7, C29, J3100, etc.) sans l'avoir
    validé d'abord via ce tool. Si le tool retourne
    {found: false, closest_matches: [...]}, tu proposes une de ces
    closest_matches ou tu demandes clarification — JAMAIS d'invention.
  - mb_get_rules_for_symptoms(symptoms) — cherche les règles diagnostiques
    matchant les symptômes du user, triées par overlap + confidence.

Le device en cours est fourni dans le premier message user (slug +
display name). Quand l'utilisateur décrit des symptômes, cherche les
règles matchantes. Quand il demande un composant par refdes, valide-le.
Privilégie les causes à haute probabilité et les étapes de diagnostic
concrètes (mesurer tel voltage sur tel test point).
"""

TOOLS = [
    {
        "type": "custom",
        "name": "mb_get_component",
        "description": "Look up a component by refdes. Returns role/package/failure_modes if found, else closest_matches.",
        "input_schema": {
            "type": "object",
            "properties": {"refdes": {"type": "string", "description": "e.g. U7, C29, J3100"}},
            "required": ["refdes"],
        },
    },
    {
        "type": "custom",
        "name": "mb_get_rules_for_symptoms",
        "description": "Find diagnostic rules matching a list of symptoms, ranked by overlap + confidence.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symptoms": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["symptoms"],
        },
    },
]


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: ANTHROPIC_API_KEY not set. Copy .env.example to .env and fill in.")

    if IDS_FILE.exists():
        ids = json.loads(IDS_FILE.read_text())
        print(f"✅ Existing managed agent: {ids['agent_id']} (v{ids['agent_version']})")
        print(f"   Environment: {ids['environment_id']}")
        return

    client = Anthropic()

    print("Creating environment…")
    env = client.beta.environments.create(
        name="wrench-board-diagnostic-env",
        config={"type": "cloud", "networking": {"type": "unrestricted"}},
    )
    print(f"   → {env.id}")

    print("Creating diagnostic agent…")
    agent = client.beta.agents.create(
        name="wrench-board-diagnostic",
        model="claude-opus-4-7",
        system=SYSTEM_PROMPT,
        tools=TOOLS,
    )
    print(f"   → {agent.id} (version {agent.version})")

    IDS_FILE.write_text(json.dumps({
        "agent_id": agent.id,
        "agent_version": agent.version,
        "environment_id": env.id,
    }, indent=2) + "\n")
    print(f"✅ IDs persisted to {IDS_FILE.name}")


if __name__ == "__main__":
    main()
```

#### Step 2: Gitignore the IDs file

- [ ] **Edit `.gitignore`** — append to the secrets section :

```
# Bootstrap output — contains Anthropic-side resource IDs
managed_ids.json
```

#### Step 3: Create the reader helper

- [ ] **Create `api/agent/managed_ids.py`** :

```python
"""Read the bootstrap IDs produced by scripts/bootstrap_managed_agent.py."""

from __future__ import annotations

import json
from pathlib import Path

IDS_FILE = Path(__file__).resolve().parent.parent.parent / "managed_ids.json"


def load_managed_ids() -> dict:
    if not IDS_FILE.exists():
        raise RuntimeError(
            f"{IDS_FILE.name} not found. Run `python scripts/bootstrap_managed_agent.py` "
            "before starting the diagnostic agent."
        )
    return json.loads(IDS_FILE.read_text())
```

#### Step 4: Run bootstrap + commit

- [ ] **Run** :

```bash
.venv/bin/python scripts/bootstrap_managed_agent.py
```

Expected : output with the 3 resource IDs + `✅ IDs persisted to managed_ids.json`. Re-running prints the existing IDs.

- [ ] **Verify the file is gitignored** :

```bash
git check-ignore managed_ids.json
# Expected output: managed_ids.json
```

- [ ] **Commit** :

```bash
git add scripts/bootstrap_managed_agent.py api/agent/managed_ids.py .gitignore
git commit -m "$(cat <<'MSG'
feat(agent): bootstrap script for Managed Agents diagnostic setup

scripts/bootstrap_managed_agent.py creates the persistent
diagnostic agent (`wrench-board-diagnostic`, claude-opus-4-7,
2 custom tools) and a shared environment. Persists the IDs in
managed_ids.json (gitignored — the IDs are tenant-specific).
Idempotent: re-runs read the existing file.

api/agent/managed_ids.py is the reader helper used by the WS
runtime to load the IDs at session creation time.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task C1: Session runtime + memory store per device

The diagnostic WebSocket endpoint opens a Managed Agents session attached to :
- The shared `diagnostic` agent (from Task C0)
- A **per-device memory store** created lazily on first access, cached in `memory/{slug}/managed.json`

The backend relays between the browser WebSocket and the MA event stream, and dispatches `agent.custom_tool_use` events to the local `mb_*` tool handlers.

**Files:**
- Create: `api/agent/tools.py` (the 2 `mb_*` tools)
- Create: `api/agent/memory_stores.py` (per-device memory store cache)
- Create: `api/agent/runtime_managed.py` (MA session relay)
- Create: `api/agent/runtime_direct.py` (fallback: `messages.create` path)
- Modify: `api/main.py` — register `/ws/diagnostic/{slug}` with MODE dispatch
- Create: `tests/agent/__init__.py`
- Create: `tests/agent/test_tools.py`

#### Step 1: Write failing tests for the `mb_*` tools

- [ ] **Create `tests/agent/__init__.py`** (empty) and **`tests/agent/test_tools.py`** :

```python
"""Tests for api.agent.tools (the 2 mb_* tools exposed in v1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.agent.tools import mb_get_component, mb_get_rules_for_symptoms

FIXTURE_DIR = Path(__file__).parent.parent / "pipeline" / "fixtures" / "demo-pack"


@pytest.fixture
def seeded_memory_root(tmp_path):
    dest = tmp_path / "demo-pi"
    dest.mkdir()
    for name in ("registry.json", "dictionary.json", "knowledge_graph.json", "rules.json"):
        (dest / name).write_text((FIXTURE_DIR / name).read_text())
    return tmp_path


def test_mb_get_component_found(seeded_memory_root):
    result = mb_get_component(device_slug="demo-pi", refdes="U7",
                              memory_root=seeded_memory_root)
    assert result["found"] is True
    assert result["canonical_name"] == "U7"
    assert result["role"] == "PMIC"


def test_mb_get_component_not_found_suggests_closest(seeded_memory_root):
    result = mb_get_component(device_slug="demo-pi", refdes="U99",
                              memory_root=seeded_memory_root)
    assert result["found"] is False
    assert result["error"] == "not_found"
    assert "closest_matches" in result


def test_mb_get_rules_for_symptoms_returns_matches(seeded_memory_root):
    result = mb_get_rules_for_symptoms(
        device_slug="demo-pi", symptoms=["3V3 rail dead"],
        memory_root=seeded_memory_root,
    )
    assert isinstance(result["matches"], list)
    assert len(result["matches"]) >= 1
    assert result["matches"][0]["rule_id"] == "rule-demo-001"
```

- [ ] **Run** : `.venv/bin/pytest tests/agent/ -v` → Expected: FAIL (`ModuleNotFoundError: No module named 'api.agent.tools'`).

#### Step 2: Implement the tools

- [ ] **Create `api/agent/tools.py`** :

```python
"""Two mb_* tools — minimal v1 for the hackathon diagnostic agent.

Deliberately simple: no Levenshtein for closest-matches (prefix letter
only), no caching, reads straight from disk on every call. Good enough
for demo traffic; upgrade path in spec §7.3.
"""

from __future__ import annotations

import json
from pathlib import Path


def _load_pack(slug: str, memory_root: Path) -> dict:
    pack_dir = memory_root / slug
    return {
        "registry":   json.loads((pack_dir / "registry.json").read_text()),
        "dictionary": json.loads((pack_dir / "dictionary.json").read_text()),
        "rules":      json.loads((pack_dir / "rules.json").read_text()),
    }


def mb_get_component(*, device_slug: str, refdes: str, memory_root: Path) -> dict:
    """Return component info or {found: False, closest_matches: [...]}."""
    pack = _load_pack(device_slug, memory_root)
    reg_comp = {c["canonical_name"]: c for c in pack["registry"]["components"]}
    dct_comp = {e["canonical_name"]: e for e in pack["dictionary"]["entries"]}

    if refdes in reg_comp:
        dct = dct_comp.get(refdes, {})
        reg = reg_comp[refdes]
        return {
            "found": True,
            "canonical_name": refdes,
            "aliases": reg.get("aliases", []),
            "kind": reg.get("kind", "unknown"),
            "role": dct.get("role"),
            "package": dct.get("package"),
            "typical_failure_modes": dct.get("typical_failure_modes", []),
            "description": reg.get("description", ""),
        }

    prefix = refdes[0].upper() if refdes else ""
    candidates = sorted(c for c in reg_comp if c.startswith(prefix))
    return {
        "found": False,
        "error": "not_found",
        "queried_refdes": refdes,
        "closest_matches": candidates[:5],
        "hint": f"No refdes {refdes!r} in the registry for {device_slug!r}.",
    }


def mb_get_rules_for_symptoms(
    *, device_slug: str, symptoms: list[str], memory_root: Path, max_results: int = 5
) -> dict:
    """Return rules whose symptoms overlap the query list."""
    pack = _load_pack(device_slug, memory_root)
    qset = {s.lower() for s in symptoms}
    matches = []
    for rule in pack["rules"].get("rules", []):
        rset = {s.lower() for s in rule.get("symptoms", [])}
        overlap = qset & rset
        if not overlap:
            continue
        matches.append({
            "rule_id": rule["id"],
            "overlap_count": len(overlap),
            "symptoms_matched": list(overlap),
            "likely_causes": rule.get("likely_causes", []),
            "diagnostic_steps": rule.get("diagnostic_steps", []),
            "confidence": rule.get("confidence", 0.5),
            "sources": rule.get("sources", []),
        })
    matches.sort(key=lambda m: (m["overlap_count"], m["confidence"]), reverse=True)
    return {
        "device_slug": device_slug,
        "query_symptoms": symptoms,
        "matches": matches[:max_results],
        "total_available_rules": len(pack["rules"].get("rules", [])),
    }
```

- [ ] **Run** : `.venv/bin/pytest tests/agent/ -v` → Expected: **3 passed**.

#### Step 3: Commit the tools

- [ ] **Commit** :

```bash
git add api/agent/tools.py tests/agent/__init__.py tests/agent/test_tools.py
git commit -m "$(cat <<'MSG'
feat(agent): add mb_get_component and mb_get_rules_for_symptoms

First two of the seven mb_* tools promised in spec §7.2. Reads
straight from memory/{slug}/*.json on each call (no caching for
v1). mb_get_component does prefix-match suggestions on not_found,
mb_get_rules_for_symptoms ranks by symptom overlap + confidence.
Unit-tested against tests/pipeline/fixtures/demo-pack.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
MSG
)"
```

#### Step 4: Per-device memory store cache

- [ ] **Create `api/agent/memory_stores.py`** :

```python
"""Per-device memory store cache.

First call for a given device_slug creates an Anthropic Managed Agents
memory store via the API and persists its id in memory/{slug}/managed.json.
Subsequent calls read from that file.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from anthropic import AsyncAnthropic

from api.config import get_settings

logger = logging.getLogger("wrench_board.agent.memory_stores")


async def ensure_memory_store(client: AsyncAnthropic, device_slug: str) -> str:
    """Return the memory_store_id for this device, creating on first access."""
    settings = get_settings()
    pack_dir = Path(settings.memory_root) / device_slug
    pack_dir.mkdir(parents=True, exist_ok=True)
    meta_path = pack_dir / "managed.json"

    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        if meta.get("memory_store_id"):
            return meta["memory_store_id"]

    logger.info("[MemoryStore] Creating for device=%s", device_slug)
    store = await client.beta.memory_stores.create(
        name=f"wrench-board-{device_slug}",
        description=(
            f"Repair history + learned facts for device {device_slug}. "
            "Contains previous diagnostic sessions, confirmed component failures, "
            "and patterns the agent observed across multiple repairs."
        ),
    )
    meta_path.write_text(json.dumps(
        {"memory_store_id": store.id, "device_slug": device_slug}, indent=2
    ) + "\n")
    logger.info("[MemoryStore] Created id=%s for device=%s", store.id, device_slug)
    return store.id
```

#### Step 5: Managed Agents session relay

- [ ] **Create `api/agent/runtime_managed.py`** :

```python
"""Diagnostic session runtime using Anthropic Managed Agents.

Flow:
  browser ⇄ /ws/diagnostic/{slug} ⇄ backend ⇄ MA session events stream.

Two concurrent asyncio tasks:
  - _forward_ws_to_session : read user messages from the WS, send them
    to the MA session as `user.message` events.
  - _forward_session_to_ws  : stream session events, relay agent.message
    text to the WS, and on agent.custom_tool_use dispatch locally
    (mb_get_component / mb_get_rules_for_symptoms) and send back
    `user.custom_tool_result`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from anthropic import AsyncAnthropic
from fastapi import WebSocket, WebSocketDisconnect

from api.agent.managed_ids import load_managed_ids
from api.agent.memory_stores import ensure_memory_store
from api.agent.tools import mb_get_component, mb_get_rules_for_symptoms
from api.config import get_settings

logger = logging.getLogger("wrench_board.agent.managed")


async def run_diagnostic_session_managed(ws: WebSocket, device_slug: str) -> None:
    settings = get_settings()
    if not settings.anthropic_api_key:
        await ws.accept()
        await ws.send_json({"type": "error", "text": "ANTHROPIC_API_KEY not set"})
        await ws.close()
        return

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    ids = load_managed_ids()
    memory_store_id = await ensure_memory_store(client, device_slug)
    memory_root = Path(settings.memory_root)

    # NOTE: `memory_store_ids` is the expected parameter name for attaching a
    # memory store to a session — verify against the current SDK via
    # `inspect.signature(client.beta.sessions.create)` if the call 400s, and
    # fall back to the `resources=[{type:"memory_store", id:...}]` form if the
    # SDK surface has moved. The behaviour (session auto-consults the store at
    # start + saves learnings at end) is stable across SDK versions.
    session = await client.beta.sessions.create(
        agent={"type": "agent", "id": ids["agent_id"], "version": ids["agent_version"]},
        environment_id=ids["environment_id"],
        memory_store_ids=[memory_store_id],
        title=f"diag-{device_slug}",
    )
    logger.info("[Diag-MA] session=%s device=%s", session.id, device_slug)

    await ws.accept()
    await ws.send_json({
        "type": "session_ready",
        "session_id": session.id,
        "memory_store_id": memory_store_id,
    })

    try:
        recv_task = asyncio.create_task(_forward_ws_to_session(ws, client, session.id))
        emit_task = asyncio.create_task(
            _forward_session_to_ws(ws, client, session.id, device_slug, memory_root)
        )
        done, pending = await asyncio.wait(
            {recv_task, emit_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
    except WebSocketDisconnect:
        logger.info("[Diag-MA] WS disconnected for device=%s", device_slug)
    finally:
        # Best-effort session cleanup (non-critical)
        try:
            await client.beta.sessions.archive(session.id)
        except Exception:
            pass


async def _forward_ws_to_session(
    ws: WebSocket, client: AsyncAnthropic, session_id: str
) -> None:
    """Read JSON {text} from the WS, send as `user.message` to the session."""
    while True:
        raw = await ws.receive_text()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"text": raw}
        text = (payload.get("text") or "").strip()
        if not text:
            continue
        await client.beta.sessions.events.send(
            session_id,
            events=[{
                "type": "user.message",
                "content": [{"type": "text", "text": text}],
            }],
        )


async def _forward_session_to_ws(
    ws: WebSocket,
    client: AsyncAnthropic,
    session_id: str,
    device_slug: str,
    memory_root: Path,
) -> None:
    """Stream session events, relay to WS, dispatch custom tool calls locally."""
    async with client.beta.sessions.events.stream(session_id) as stream:
        async for event in stream:
            etype = event.type

            if etype == "agent.message":
                for block in (getattr(event, "content", []) or []):
                    if getattr(block, "type", None) == "text":
                        await ws.send_json({
                            "type": "message",
                            "role": "assistant",
                            "text": block.text,
                        })

            elif etype == "agent.custom_tool_use":
                name = getattr(event, "name", None) or getattr(event, "tool_name", None)
                input_ = getattr(event, "input", {}) or {}
                await ws.send_json({"type": "tool_use", "name": name, "input": input_})
                result = _dispatch_tool(name, input_, device_slug, memory_root)
                await client.beta.sessions.events.send(
                    session_id,
                    events=[{
                        "type": "user.custom_tool_result",
                        "custom_tool_use_id": event.id,
                        "content": [{"type": "text", "text": json.dumps(result)}],
                    }],
                )

            elif etype == "session.status_idle":
                # Only terminal stop_reasons end the loop; "requires_action"
                # fires transiently while we compute a tool result.
                stop = getattr(event, "stop_reason", None)
                if stop is None or getattr(stop, "type", None) != "requires_action":
                    # idle + end_turn / retries_exhausted → wait for next user turn,
                    # which re-enters via the send path. Don't break: the stream
                    # stays alive for the session lifetime.
                    pass

            elif etype == "session.status_terminated":
                break


def _dispatch_tool(name: str, input_: dict, device_slug: str, memory_root: Path) -> dict:
    if name == "mb_get_component":
        return mb_get_component(
            device_slug=device_slug,
            refdes=input_.get("refdes", ""),
            memory_root=memory_root,
        )
    if name == "mb_get_rules_for_symptoms":
        return mb_get_rules_for_symptoms(
            device_slug=device_slug,
            symptoms=input_.get("symptoms", []),
            memory_root=memory_root,
            max_results=input_.get("max_results", 5),
        )
    return {"error": f"unknown tool: {name}"}
```

#### Step 6: Direct-mode fallback (`messages.create`)

- [ ] **Create `api/agent/runtime_direct.py`** — identical behaviour from the user's point of view (WS protocol same), no MA dependency. Used when `DIAGNOSTIC_MODE=direct` :

```python
"""Fallback diagnostic runtime using `messages.create` (no Managed Agents).

Keeps the WS protocol identical to runtime_managed so the frontend
doesn't care which mode is active. Activated via env var
DIAGNOSTIC_MODE=direct when MA beta blocks."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from anthropic import AsyncAnthropic
from fastapi import WebSocket, WebSocketDisconnect

from api.agent.tools import mb_get_component, mb_get_rules_for_symptoms
from api.config import get_settings

logger = logging.getLogger("wrench_board.agent.direct")

SYSTEM_PROMPT_DIRECT = """\
You are a calm, methodical board-level diagnostics assistant for a
microsoldering technician. Tu tutoies, en français, direct et pédagogique.

RÈGLE ANTI-HALLUCINATION : tu NE mentionnes JAMAIS un refdes sans
l'avoir validé via mb_get_component. Device courant : {device_slug}.
"""

TOOLS = [
    {"name": "mb_get_component", "description": "Look up component by refdes.",
     "input_schema": {"type": "object",
                      "properties": {"refdes": {"type": "string"}},
                      "required": ["refdes"]}},
    {"name": "mb_get_rules_for_symptoms", "description": "Find rules for symptoms.",
     "input_schema": {"type": "object",
                      "properties": {"symptoms": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
                      "required": ["symptoms"]}},
]


async def run_diagnostic_session_direct(ws: WebSocket, device_slug: str) -> None:
    settings = get_settings()
    if not settings.anthropic_api_key:
        await ws.accept()
        await ws.send_json({"type": "error", "text": "ANTHROPIC_API_KEY not set"})
        await ws.close()
        return
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    memory_root = Path(settings.memory_root)
    messages: list[dict] = []
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                user_text = json.loads(raw).get("text", "").strip()
            except json.JSONDecodeError:
                user_text = raw.strip()
            if not user_text:
                continue
            messages.append({"role": "user", "content": user_text})
            while True:
                response = await client.messages.create(
                    model=settings.anthropic_model_main,
                    max_tokens=8000,
                    system=SYSTEM_PROMPT_DIRECT.format(device_slug=device_slug),
                    messages=messages,
                    tools=TOOLS,
                )
                for block in response.content:
                    if block.type == "text":
                        await ws.send_json({"type": "message", "role": "assistant", "text": block.text})
                if response.stop_reason != "tool_use":
                    messages.append({"role": "assistant", "content": response.content})
                    break
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    await ws.send_json({"type": "tool_use", "name": block.name, "input": block.input})
                    if block.name == "mb_get_component":
                        result = mb_get_component(
                            device_slug=device_slug,
                            refdes=block.input.get("refdes", ""),
                            memory_root=memory_root,
                        )
                    elif block.name == "mb_get_rules_for_symptoms":
                        result = mb_get_rules_for_symptoms(
                            device_slug=device_slug,
                            symptoms=block.input.get("symptoms", []),
                            memory_root=memory_root,
                        )
                    else:
                        result = {"error": f"unknown tool: {block.name}"}
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })
                messages.append({"role": "user", "content": tool_results})
    except WebSocketDisconnect:
        logger.info("[Diag-Direct] WS closed for device=%s", device_slug)
```

#### Step 7: Wire the WS endpoints + MODE dispatch

- [ ] **Edit `api/main.py`** — add :

```python
import os

from fastapi import WebSocket


@app.websocket("/ws/diagnostic/{device_slug}")
async def diagnostic_session(websocket: WebSocket, device_slug: str) -> None:
    """Diagnostic conversation. MODE env var picks managed (default) or direct."""
    mode = os.environ.get("DIAGNOSTIC_MODE", "managed")
    if mode == "direct":
        from api.agent.runtime_direct import run_diagnostic_session_direct
        await run_diagnostic_session_direct(websocket, device_slug)
    else:
        from api.agent.runtime_managed import run_diagnostic_session_managed
        await run_diagnostic_session_managed(websocket, device_slug)
```

The existing legacy `/ws` handler stays untouched so `tests/test_websocket.py` continues to pass.

#### Step 8: Smoke-test

- [ ] **Verify the bootstrap** :

```bash
test -f managed_ids.json || .venv/bin/python scripts/bootstrap_managed_agent.py
```

- [ ] **Start uvicorn** :

```bash
.venv/bin/uvicorn api.main:app --reload --host 0.0.0.0 --port 8000 &
```

- [ ] **Browser smoke test** :

Open `http://localhost:8000/?device=pi-4-model-b` (after Task D1 seeds the demo pack), press ⌘J, type *« où est U7 ? »*. Expected stream :

```
(tool_use) mb_get_component({"refdes": "U7"})
assistant: U7 est le PMIC principal, package QFN-24…
```

- [ ] **Test the direct fallback** :

```bash
kill $(pgrep -f uvicorn)  # restart
DIAGNOSTIC_MODE=direct .venv/bin/uvicorn api.main:app --reload --host 0.0.0.0 --port 8000 &
```

Same UX, using `messages.create` under the hood. Useful if MA beta blocks.

#### Step 9: Commit the runtime

- [ ] **Commit** :

```bash
git add api/agent/memory_stores.py api/agent/runtime_managed.py \
        api/agent/runtime_direct.py api/main.py
git commit -m "$(cat <<'MSG'
feat(agent): diagnostic runtime — Managed Agents default, direct fallback

New endpoint /ws/diagnostic/{device_slug}. Default path uses
Anthropic Managed Agents:
  - Bootstrap agent + environment (Task C0, persisted IDs).
  - Per-device memory store, auto-consulted by the agent at
    session start and updated at session end (spec §2.3 Flow A).
  - Two concurrent asyncio tasks relay the session event stream
    to/from the browser WebSocket. agent.custom_tool_use events
    are dispatched locally against memory/{slug}/*.json.

Fallback: DIAGNOSTIC_MODE=direct uses messages.create with the
same tool definitions (api/agent/runtime_direct.py). Same WS
protocol, so the frontend is mode-agnostic.

The pipeline (api/pipeline/) keeps using messages.create direct
per the split documented in the hackathon plan.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
MSG
)"
```

**End-of-J+4 gate** : with `managed_ids.json` bootstrapped, `memory/pi-4-model-b/` seeded (Task D1), and `ANTHROPIC_API_KEY` set, opening `/?device=pi-4-model-b` + ⌘J + typing *« où est U7 ? »* produces a `tool_use` line then an `assistant` message citing U7 correctly. If MA blocks, `DIAGNOSTIC_MODE=direct` gives the same UX via `messages.create`.

---

### Task C2: LLM panel frontend

Adds the right-side push-on-demand LLM panel promised in spec §9.3, connects to `/ws/diagnostic/{slug}`, streams messages.

**Files:**
- Modify: `web/index.html` — add `<aside class="llm-panel">`, CSS, JS connector.

#### Step 1: Add the panel HTML

- [ ] **Edit `web/index.html`** — add before `<!-- ============ INSPECTOR ============ -->` :

```html
<!-- ============ LLM PANEL (push-on-demand, §9.3) ============ -->
<aside class="llm-panel" id="llmPanel">
  <header class="llm-head">
    <h3>Agent <span class="llm-model" id="llmModel">claude-opus-4-7</span></h3>
    <button class="close-x" id="llmClose"><svg class="icon" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
  </header>
  <div class="llm-status" id="llmStatus">déconnecté</div>
  <div class="llm-log" id="llmLog"></div>
  <form class="llm-input" id="llmForm">
    <input type="text" id="llmInput" placeholder="Pose ta question au diagnostic…" autocomplete="off" />
    <button type="submit">Envoyer</button>
  </form>
</aside>

<!-- Toggle button in topbar -->
<button class="top-btn" id="llmToggle" style="margin-left:auto" title="Toggle panel agent (⌘J)">
  <svg class="icon" viewBox="0 0 24 24"><circle cx="8" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="16" cy="12" r="1"/><path d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
</button>
```

(Move the toggle button into the topbar next to the existing Tweaks button.)

#### Step 2: CSS (push mode — shrinks the main content area)

- [ ] **Add CSS** :

```css
.llm-panel{position:fixed;top:92px;right:0;bottom:28px;width:420px;background:linear-gradient(180deg,rgba(20,32,48,.98),rgba(15,26,46,.98));border-left:1px solid var(--border);display:none;flex-direction:column;z-index:25}
.llm-panel.open{display:flex}
body.llm-open .canvas,body.llm-open .home,body.llm-open .pcb,body.llm-open .stub{right:420px}
.llm-head{padding:14px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.llm-head h3{margin:0;font-size:14px;font-weight:600;flex:1}
.llm-model{font-family:var(--mono);font-size:10.5px;color:var(--text-3);font-weight:400}
.llm-status{padding:6px 16px;font-family:var(--mono);font-size:10.5px;color:var(--text-3);border-bottom:1px solid var(--border-soft)}
.llm-status.connected{color:var(--emerald)}
.llm-log{flex:1;overflow-y:auto;padding:14px 16px;display:flex;flex-direction:column;gap:10px;font-size:12.5px;line-height:1.5}
.llm-log .msg{padding:8px 10px;border-radius:6px}
.llm-log .msg.user{background:rgba(56,189,248,.08);border-left:2px solid var(--cyan);color:var(--text)}
.llm-log .msg.assistant{background:var(--bg-2);border-left:2px solid var(--border);color:var(--text)}
.llm-log .msg.tool{font-family:var(--mono);font-size:10.5px;color:var(--text-3);padding:4px 10px}
.llm-input{display:flex;gap:6px;padding:10px;border-top:1px solid var(--border)}
.llm-input input{flex:1;background:var(--panel);border:1px solid var(--border);color:var(--text);border-radius:5px;padding:8px 10px;font-family:inherit;font-size:12.5px;outline:none}
.llm-input input:focus{border-color:var(--cyan)}
.llm-input button{background:rgba(56,189,248,.15);color:var(--cyan);border:1px solid rgba(56,189,248,.3);border-radius:5px;padding:8px 14px;cursor:pointer;font-family:inherit;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
```

#### Step 3: JS — WebSocket connector + push toggle

- [ ] **Add JS** :

```js
/* ---------- LLM PANEL (push-on-demand) ---------- */
let llmWS = null;

function llmLog(role, text) {
  const log = document.getElementById("llmLog");
  const msg = document.createElement("div");
  msg.className = "msg " + role;
  msg.textContent = text;
  log.appendChild(msg);
  log.scrollTop = log.scrollHeight;
}

function llmConnect() {
  const params = new URLSearchParams(window.location.search);
  const slug = params.get("device");
  if (!slug) {
    document.getElementById("llmStatus").textContent = "aucun device sélectionné";
    return;
  }
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const url = `${scheme}://${window.location.host}/ws/diagnostic/${encodeURIComponent(slug)}`;
  llmWS = new WebSocket(url);
  llmWS.addEventListener("open", () => {
    document.getElementById("llmStatus").textContent = "connecté · " + slug;
    document.getElementById("llmStatus").classList.add("connected");
  });
  llmWS.addEventListener("close", () => {
    document.getElementById("llmStatus").textContent = "fermé";
    document.getElementById("llmStatus").classList.remove("connected");
  });
  llmWS.addEventListener("message", (ev) => {
    let payload; try { payload = JSON.parse(ev.data); } catch { payload = {type:"message", text: ev.data}; }
    if (payload.type === "message") llmLog(payload.role || "assistant", payload.text);
    else if (payload.type === "tool_use") llmLog("tool", `→ ${payload.name}(${JSON.stringify(payload.input)})`);
    else if (payload.type === "error") llmLog("assistant", `[erreur] ${payload.text}`);
  });
}

document.getElementById("llmToggle").addEventListener("click", () => {
  document.body.classList.toggle("llm-open");
  document.getElementById("llmPanel").classList.toggle("open");
  if (!llmWS || llmWS.readyState > 1) llmConnect();
});

document.getElementById("llmClose").addEventListener("click", () => {
  document.body.classList.remove("llm-open");
  document.getElementById("llmPanel").classList.remove("open");
});

document.getElementById("llmForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = document.getElementById("llmInput");
  const text = input.value.trim();
  if (!text || !llmWS || llmWS.readyState !== 1) return;
  llmLog("user", text);
  llmWS.send(JSON.stringify({type: "message", text}));
  input.value = "";
});

document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "j") {
    e.preventDefault();
    document.getElementById("llmToggle").click();
  }
});
```

#### Step 4: Commit

- [ ] **Commit** :

```bash
git add web/index.html
git commit -m "$(cat <<'MSG'
feat(web): push-on-demand LLM panel on /ws/diagnostic/{slug}

Right-side panel (420px) that shrinks the main content when
open (body.llm-open gets the right margin). Connects to
WebSocket /ws/diagnostic/{device_slug} and streams
user.message → assistant.message + tool_use pastilles.
⌘J toggles. Status bar colorise en vert quand connecté.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
MSG
)"
```

**End-of-J+4 gate** : with `ANTHROPIC_API_KEY` set and a valid pack in `memory/demo-pi/`, open `http://localhost:8000/?device=demo-pi`, press ⌘J, type *« où est U7 ? »* — the agent should respond with info about U7 (after calling `mb_get_component`).

---

## Phase D — Demo prep + submission (J+5)

### Task D1: Seed demo data

Without API credits the demo can still run if we pre-seed a pack in `memory/`. This task creates a realistic Pi 4 demo pack + 3 C29 cases for the §8.8 cycle apprenant narrative.

**Files:**
- Create: `scripts/seed_demo.py`
- Create: `memory/pi-4-model-b/*.json` (generated by the script)

#### Step 1: Write the seed script

- [ ] **Create `scripts/seed_demo.py`** :

```python
"""Seed a demo Pi 4 pack in memory/pi-4-model-b/ for the hackathon video.

Run: python scripts/seed_demo.py

Produces registry.json, knowledge_graph.json, rules.json,
dictionary.json, audit_verdict.json, and 3 case fixtures for the
§8.8 demo narrative (C29 failure cycle).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

DEST = Path(__file__).parent.parent / "memory" / "pi-4-model-b"
DEST.mkdir(parents=True, exist_ok=True)

REGISTRY = {
    "schema_version": "1.0",
    "device_label": "Raspberry Pi 4 Model B",
    "components": [
        {"canonical_name": "U7",  "logical_alias": None,
         "aliases": ["PMIC", "main PMIC"], "kind": "pmic",
         "description": "Main PMIC, generates 3V3 and 1V8 rails."},
        {"canonical_name": "C29", "logical_alias": None, "aliases": [],
         "kind": "capacitor", "description": "3V3 rail decoupling, adjacent to U7."},
        {"canonical_name": "BCM2711", "logical_alias": "SoC",
         "aliases": ["SoC", "processor"], "kind": "ic",
         "description": "Broadcom quad-A72 SoC."},
    ],
    "signals": [
        {"canonical_name": "3V3_RAIL", "aliases": ["3.3V", "VDD_3V3"],
         "kind": "power_rail", "nominal_voltage": 3.3},
        {"canonical_name": "5V_IN", "aliases": ["USB-C 5V"],
         "kind": "power_rail", "nominal_voltage": 5.0},
    ],
}
(DEST / "registry.json").write_text(json.dumps(REGISTRY, indent=2))

KG = {
    "schema_version": "1.0",
    "nodes": [
        {"id": "cmp_U7",       "kind": "component", "label": "U7",       "properties": {"package": "QFN-24"}},
        {"id": "cmp_C29",      "kind": "component", "label": "C29",      "properties": {"package": "0402"}},
        {"id": "cmp_BCM2711",  "kind": "component", "label": "BCM2711",  "properties": {"package": "FCBGA"}},
        {"id": "net_3V3",      "kind": "net",       "label": "3V3_RAIL", "properties": {"nominal": "3.3V"}},
        {"id": "net_5V",       "kind": "net",       "label": "5V_IN",    "properties": {"nominal": "5V"}},
    ],
    "edges": [
        {"source_id": "net_5V",     "target_id": "cmp_U7",    "relation": "powers"},
        {"source_id": "cmp_U7",     "target_id": "net_3V3",   "relation": "powers"},
        {"source_id": "cmp_C29",    "target_id": "net_3V3",   "relation": "decouples"},
        {"source_id": "net_3V3",    "target_id": "cmp_BCM2711","relation": "powers"},
    ],
}
(DEST / "knowledge_graph.json").write_text(json.dumps(KG, indent=2))

RULES = {
    "schema_version": "1.0",
    "rules": [
        {
            "id": "rule-pi4-001",
            "symptoms": ["3V3 rail dead", "device doesn't boot"],
            "likely_causes": [
                {"refdes": "C29", "probability": 0.78, "mechanism": "short-to-ground"},
                {"refdes": "U7",  "probability": 0.22, "mechanism": "dead PMIC"},
            ],
            "diagnostic_steps": [
                {"action": "measure 3V3_RAIL at TP18", "expected": "3.3V ± 5%"},
                {"action": "if 0V, measure resistance TP18 to GND", "expected": "> 100 Ω"},
            ],
            "confidence": 0.82,
            "sources": ["seed"],
        },
    ],
}
(DEST / "rules.json").write_text(json.dumps(RULES, indent=2))

DICTIONARY = {
    "schema_version": "1.0",
    "entries": [
        {"canonical_name": "U7", "role": "PMIC", "package": "QFN-24",
         "typical_failure_modes": ["dead PMIC after surge"], "notes": None},
        {"canonical_name": "C29", "role": "decoupling cap", "package": "0402",
         "typical_failure_modes": ["short-to-ground after liquid damage"],
         "notes": "Common failure point. Located near U7."},
        {"canonical_name": "BCM2711", "role": "SoC", "package": "FCBGA",
         "typical_failure_modes": ["thermal stress cracks"], "notes": None},
    ],
}
(DEST / "dictionary.json").write_text(json.dumps(DICTIONARY, indent=2))

AUDIT = {
    "schema_version": "1.0",
    "overall_status": "APPROVED",
    "consistency_score": 0.95,
    "files_to_rewrite": [],
    "drift_report": [],
    "revision_brief": "",
}
(DEST / "audit_verdict.json").write_text(json.dumps(AUDIT, indent=2))

# The 3 C29 cases for §8.8 cycle apprenant narrative
cases_dir = DEST / "cases"
cases_dir.mkdir(exist_ok=True)
for i in range(1, 4):
    case = {
        "schema_version": "1.0",
        "id": f"case-{i:03d}",
        "device_id": "pi-4-model-b",
        "title": f"3V3 rail dead → C29 replacement ({i}/3)",
        "symptoms": ["3V3 rail dead", "device doesn't boot"],
        "resolution": {
            "cause_refdes": "C29",
            "cause_mechanism": "short-to-ground",
            "action": "replace",
            "outcome": "device boots",
        },
        "status": "resolved",
        "created_at": f"2026-04-2{3+i}T10:00:00Z",
    }
    (cases_dir / f"case-{i:03d}.json").write_text(json.dumps(case, indent=2))

print(f"✅ Seeded demo pack at {DEST}")
print(f"   Open http://localhost:8000/?device=pi-4-model-b")
```

- [ ] **Run** :

```bash
.venv/bin/python scripts/seed_demo.py
```

Expected : `✅ Seeded demo pack at .../memory/pi-4-model-b` and the `memory/pi-4-model-b/` directory now contains 5 JSON files + `cases/case-001.json`, `case-002.json`, `case-003.json`.

- [ ] **Verify via endpoint** :

```bash
.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8765 --log-level warning &
UPID=$!
sleep 2
curl -sS http://127.0.0.1:8765/pipeline/packs/pi-4-model-b/graph | head -c 500
kill $UPID
```

Expected : JSON payload with nodes (U7, C29, BCM2711, 3V3_RAIL, 5V_IN) and synthesized symptom nodes.

#### Step 2: Commit

- [ ] **Commit** :

```bash
git add scripts/seed_demo.py memory/pi-4-model-b/
git commit -m "$(cat <<'MSG'
chore(demo): seed Pi 4 pack + 3 C29 cases for video narrative

scripts/seed_demo.py produces a complete hand-crafted pack at
memory/pi-4-model-b/ (registry + knowledge_graph + rules +
dictionary + audit_verdict) plus 3 case fixtures matching the
spec §8.8 "I've seen this 3 times" narrative. Lets the demo
video run without depending on a live /pipeline/generate call.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task D2: Deferred-init refactor (graph reload hack removal)

Task A2 left a `window.location.reload()` hack when swapping data. Before the video, refactor to a clean deferred-init pattern.

**Files:**
- Modify: `web/index.html`

#### Step 1: Wrap all graph init inside `initGraphWithData(data)`

- [ ] **Edit `web/index.html`** — rename the existing top-level block starting at `const svg = d3.select("#graph");` down to the last `requestAnimationFrame(animateParticles);` by wrapping them in :

```js
function initGraphWithData(data) {
  // Set DATA for the closures that read it.
  DATA = data;

  // <paste the body from `const svg = ...` through `requestAnimationFrame(animateParticles);`>
}
```

Keep the helpers referenced outside (`loadGraphFromBackend`, `setEmptyState`, `loadHomePacks`, router, navigate, etc.) at the top level.

#### Step 2: Update the main IIFE to call `initGraphWithData` directly

- [ ] **Edit** the main IIFE :

```js
(async function () {
  const hash = window.location.hash;
  const params = new URLSearchParams(window.location.search);
  const slug = params.get("device");
  const initial = hash ? currentSection() : (slug ? "graphe" : "home");
  navigate(initial);

  if (initial === "graphe" && slug) {
    const fetched = await loadGraphFromBackend();
    if (fetched && fetched.nodes && fetched.nodes.length > 0) {
      setEmptyState(false);
      initGraphWithData(fetched);
    } else {
      setEmptyState(true);
    }
  } else if (initial === "home") {
    renderHome(await loadHomePacks());
  } else if (initial === "pcb") {
    await initPCB();
  }
})();
```

#### Step 3: Remove the `window.location.reload()` hack

- [ ] Verify no more `window.location.reload()` in the init path.

#### Step 4: Commit

- [ ] **Commit** :

```bash
git add web/index.html
git commit -m "$(cat <<'MSG'
refactor(web): deferred-init for D3 graph, drops reload hack

Wraps the D3 selection + force-sim setup into
initGraphWithData(data) and only invokes it when a non-empty
payload is available. Removes the pragmatic
window.location.reload() introduced in Task A2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task D3: README update for submission

Updates the README with current state, screenshots placeholders, and submission-ready blurb. Also drafts the 100–200-word description for Cerebral Valley.

**Files:**
- Modify: `README.md`
- Create: `docs/submission.md` (the 100–200-word blurb + checklist)

#### Step 1: Update README

- [ ] **Edit `README.md`** — replace the "Project status" section with :

```markdown
## Project status

**Built for the Anthropic × Cerebral Valley "Built with Opus 4.7" Hackathon (April 21–26, 2026).**

Current capabilities (v1) :
- Autonomous 4-phase knowledge generation pipeline (Scout / Registry / Writers / Auditor) driven by Opus 4.7
- Knowledge graph UI (D3.js, 4-column causal flow: Actions → Composants → Nets → Symptômes)
- Minimal diagnostic conversation over WebSocket (2 `mb_*` tools, no Managed Agents in v1)
- Boardviewer integration slot (parallel agent, see `docs/superpowers/specs/2026-04-21-boardview-design.md`)

Known limitations (tracked in spec §1.4, out of scope for v1) :
- No Postgres — disk files are the canonical store
- No cycle apprenant auto-trigger — cases seeded manually for video
- No Managed Agents — see spec §6.8 for migration path
- No Profile / Memory Bank / Agent-config UI sections yet
```

#### Step 2: Draft the submission blurb

- [ ] **Create `docs/submission.md`** :

```markdown
# Cerebral Valley submission — wrench-board

## 100–200-word description (paste into the form)

wrench-board turns an electronics repair technician into
the operator of a diagnostic copilot. Claude Opus 4.7 runs an
autonomous knowledge-factory that researches the community's
collected wisdom for any device, structures it into a canonical
registry + diagnostic rules, and audits its own output with a
self-healing loop. The technician then has a typed knowledge
graph (components, symptoms, nets, repair actions) to navigate,
and a conversational agent that answers questions like « where
is the PMIC? » with refdes validated against the registry —
never hallucinated.

Built from scratch in 6 days with strict rules: Apache 2.0,
permissive deps only, open-hardware-only data (iPhone X covered
via deep-research fallback from iFixit + public datasheets).
Three devices showcased: Raspberry Pi 4 Model B, Framework
Laptop 13, iPhone X.

## Submission checklist

- [ ] Video recorded (3 min, ElevenLabs VO in English)
- [ ] Repo public on GitHub with clean commit history
- [ ] README updated with capabilities + limitations
- [ ] LICENSE present (Apache 2.0)
- [ ] Submission form filled at cerebralvalley.ai (before 2026-04-26 20:00 EST)
```

#### Step 3: Commit

- [ ] **Commit** :

```bash
git add README.md docs/submission.md
git commit -m "$(cat <<'MSG'
docs: submission-ready README + Cerebral Valley blurb

Updates README 'Project status' with v1 capabilities and
explicit limitations (disk-only store, no Managed Agents, no
cycle apprenant trigger). Adds docs/submission.md with the
100–200-word blurb for the form + checklist for the sunday
deadline.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Post-plan: what to do next

1. Review this plan end-to-end, flag any task that should be split or reordered.
2. Execute phases in sequence (A → B → C → D). Within a phase, tasks are mostly ordered by dependency.
3. Before each phase, sync with Agent Boardviewer to check their delivery status (particularly for Phase B/Task B2).
4. After Phase D, record the video and submit.

**If a gate is missed**, fall back to the escape hatches listed in spec §13 and its §12 gate-escalation table — never cascade failures into the next phase.
