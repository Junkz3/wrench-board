# Journal — Repair Dashboard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the `#home` Journal section into a two-state surface driven by URL query params. State **list** stays as today (grid of repair cards). New state **dashboard** activates when both `?device=<slug>&repair=<rid>` are present — a focused hub showing symptom, device, tool shortcuts, conversations, cross-session findings, an activity timeline, and pack status. Add a persistent **session pill** in the topbar (visible across all sections when a session is active). Single **Quitter la session** handler cleans the URL and returns to the list.

**Architecture:** Pure frontend refactor + one new backend read-only route. The WebSocket protocol, agent runtimes, and chat panel are untouched. The dashboard is a new inline DOM block under `<section id="homeSection">`, sibling to the existing list. Dashboard rendering is a new function in `web/js/home.js` that fetches from 4 existing endpoints + 1 new one. Session detection is URL-derived via a new `currentSession()` helper exported from `web/js/router.js`. The session pill is markup added to the topbar, wired from `updateChrome()`.

**Tech Stack:** Vanilla HTML/CSS/JS (no build step), FastAPI + Pydantic v2 for the new route, pytest + FastAPI TestClient for the route test. Zero new dependencies.

**Spec:** `docs/superpowers/specs/2026-04-23-journal-repair-dashboard-design.md` (commit `7bc18fc`).

**Design system contract:** All new CSS uses tokens from `web/styles/tokens.css` — `--bg`, `--bg-2`, `--panel`, `--panel-2`, `--text`, `--text-2`, `--text-3`, `--border`, `--border-soft`, `--cyan`, `--emerald`, `--amber`, `--violet`, `--mono`. No hard-coded hex for semantic meaning. Session pill reuses the `.mode-pill` grammar (cyan family). Dashboard blocks reuse the `.mb-block` grammar from Memory Bank CSS (see `web/styles/memory_bank.css`).

**Browser verification:** this project has no JS test framework — per `feedback_visual_changes_require_user_verify`, frontend tasks include a "Browser verification checklist" step that must be acknowledged by the user before commit.

---

## Task 1: Backend — `GET /pipeline/packs/{slug}/findings` route

One read-only HTTP route that wraps the existing `list_field_reports()` helper. TDD.

**Files:**
- Create: `tests/pipeline/test_findings_endpoint.py`
- Modify: `api/pipeline/__init__.py` (add route + import)

- [ ] **Step 1: Write the failing endpoint test**

Create `tests/pipeline/test_findings_endpoint.py`:

```python
"""Tests for GET /pipeline/packs/{device_slug}/findings."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import config as config_mod
from api.main import app


@pytest.fixture
def memory_root(tmp_path, monkeypatch):
    """Isolate settings.memory_root per test."""
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    yield tmp_path
    monkeypatch.setattr(config_mod, "_settings", None)


@pytest.fixture
def client():
    return TestClient(app)


def _write_report(
    memory_root: Path,
    *,
    slug: str,
    report_id: str,
    refdes: str,
    symptom: str,
    confirmed_cause: str,
    created_at: str,
    session_id: str | None = None,
) -> None:
    reports_dir = memory_root / slug / "field_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"report_id: {report_id}",
        f"device_slug: {slug}",
        f"refdes: {refdes}",
        f'symptom: "{symptom}"',
        f'confirmed_cause: "{confirmed_cause}"',
    ]
    if session_id:
        lines.append(f"session_id: {session_id}")
    lines.append(f"created_at: {created_at}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {refdes} — {confirmed_cause}")
    lines.append("")
    lines.append(f"**Symptom observed:** {symptom}")
    lines.append("")
    (reports_dir / f"{report_id}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_findings_returns_empty_list_for_unknown_device(memory_root, client):
    resp = client.get("/pipeline/packs/does-not-exist/findings")
    assert resp.status_code == 200
    assert resp.json() == []


def test_findings_returns_empty_list_when_no_reports(memory_root, client):
    (memory_root / "demo-device").mkdir(parents=True)
    resp = client.get("/pipeline/packs/demo-device/findings")
    assert resp.status_code == 200
    assert resp.json() == []


def test_findings_returns_reports_newest_first(memory_root, client):
    slug = "demo-device"
    _write_report(
        memory_root,
        slug=slug,
        report_id="2026-03-01-u12",
        refdes="U12",
        symptom="no-boot",
        confirmed_cause="cold joint",
        created_at="2026-03-01T10:00:00+00:00",
    )
    _write_report(
        memory_root,
        slug=slug,
        report_id="2026-03-02-q7",
        refdes="Q7",
        symptom="brownout",
        confirmed_cause="gate short",
        created_at="2026-03-02T10:00:00+00:00",
        session_id="abc12345",
    )

    resp = client.get(f"/pipeline/packs/{slug}/findings")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    # Newest first — Q7 was created 2026-03-02, U12 on 2026-03-01.
    assert body[0]["refdes"] == "Q7"
    assert body[0]["session_id"] == "abc12345"
    assert body[0]["confirmed_cause"] == "gate short"
    assert body[1]["refdes"] == "U12"
    assert body[1]["session_id"] is None


def test_findings_rejects_bad_slug(memory_root, client):
    resp = client.get("/pipeline/packs/bad..slug/findings")
    assert resp.status_code in (400, 422)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/pipeline/test_findings_endpoint.py -v
```

Expected: all 4 tests fail with `404 Not Found` (route doesn't exist yet).

- [ ] **Step 3: Add the route in `api/pipeline/__init__.py`**

Open `api/pipeline/__init__.py`. Find the existing `@router.get("/packs/{device_slug}/graph")` definition (~line 679). Right before it, check that `list_field_reports` is imported at the top of the file. If not, add the import near the other `api.agent.*` imports:

```python
from api.agent.field_reports import list_field_reports
```

Then add the route definition right after the `@router.get("/packs/{device_slug}/full")` block (around line 404):

```python
@router.get("/packs/{device_slug}/findings")
async def list_device_findings(device_slug: str, limit: int = 50) -> list[dict]:
    """Return every field report recorded for this device, newest first.

    Mirrors what `mb_list_findings` sees at agent-tool scope, exposed to the
    web UI so the Journal dashboard can render the cross-session memory
    without a WS round-trip. Strictly JSON-on-disk — no MA memory-store.
    """
    return list_field_reports(device_slug=_validate_slug(device_slug), limit=limit)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/pipeline/test_findings_endpoint.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Run lint and full test suite**

```bash
make lint && make test
```

Expected: both pass. Fix any issues inline.

- [ ] **Step 6: Commit**

```bash
git add api/pipeline/__init__.py tests/pipeline/test_findings_endpoint.py
git commit -m "$(cat <<'EOF'
feat(api): GET /pipeline/packs/{slug}/findings for dashboard

Thin wrapper over list_field_reports() so the Journal dashboard can
render cross-session findings without a WS round-trip. JSON-on-disk
only — no MA memory-store mirror. Returns [] for unknown devices or
empty field_reports/ dirs; rejects malformed slugs via _validate_slug.
EOF
)" -- api/pipeline/__init__.py tests/pipeline/test_findings_endpoint.py
```

---

## Task 2: Frontend — Dashboard static shell (DOM + CSS, hidden)

Add the `#repairDashboard` block inside `#homeSection` and a new CSS file. Shell is `.hidden` by default — no behavioral change until Task 3. This commit is a safe intermediate: the existing list still renders; the new shell is invisible.

**Files:**
- Create: `web/styles/repair_dashboard.css`
- Modify: `web/index.html` (add `<link>` + `#repairDashboard` DOM block)

- [ ] **Step 1: Create `web/styles/repair_dashboard.css`**

Create the file with all styles. Values follow the Memory Bank grammar (see `web/styles/memory_bank.css`) for block framing, and the mode-pill grammar for chip-like elements:

```css
/* ─────────────────────────────────────────────────────────────
 * Repair dashboard — the two-state Journal's "session hub" view.
 * Activated when both ?device=<slug>&repair=<rid> are in the URL.
 * Sibling of #homeSections (the list) under <section id="homeSection">.
 * ───────────────────────────────────────────────────────────── */

#repairDashboard {
  display: flex;
  flex-direction: column;
  gap: 20px;
  padding: 24px 32px 48px;
  max-width: 1280px;
  margin: 0 auto;
  width: 100%;
  color: var(--text);
}

#repairDashboard.hidden { display: none; }

/* Header ────────────────────────────────────────────────────── */
.rd-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  padding: 20px 22px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-left: 3px solid var(--cyan);
  border-radius: 8px;
}
.rd-head-left { display: flex; flex-direction: column; gap: 8px; min-width: 0; flex: 1; }
.rd-head-right { display: flex; align-items: center; gap: 8px; }
.rd-slug {
  font-family: var(--mono);
  font-size: 10.5px;
  letter-spacing: .4px;
  text-transform: uppercase;
  color: var(--text-3);
}
.rd-device {
  font-size: 22px;
  line-height: 1.2;
  margin: 0;
  color: var(--text);
  font-weight: 600;
}
.rd-symptom {
  font-size: 14px;
  color: var(--text-2);
  margin: 0;
  max-width: 72ch;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.rd-badges {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 2px;
}
.rd-created {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-3);
}
.rd-leave-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 12px;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-2);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
  transition: background .15s, color .15s, border-color .15s;
}
.rd-leave-btn:hover {
  background: var(--panel-2);
  border-color: var(--amber);
  color: var(--amber);
}

/* Tool tiles ───────────────────────────────────────────────── */
.rd-tiles {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}
body.llm-open .rd-tiles { grid-template-columns: repeat(2, 1fr); }
@media (max-width: 960px) { .rd-tiles { grid-template-columns: repeat(2, 1fr); } }

.rd-tile {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 14px 16px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text);
  text-decoration: none;
  transition: background .15s, border-color .15s, transform .15s;
  cursor: pointer;
}
.rd-tile:hover {
  background: var(--panel-2);
  border-color: var(--cyan);
  transform: translateY(-1px);
}
.rd-tile-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.rd-tile-icon {
  width: 18px;
  height: 18px;
  stroke: var(--text-2);
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
  fill: none;
}
.rd-tile:hover .rd-tile-icon { stroke: var(--cyan); }
.rd-tile-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}
.rd-tile-meta {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-3);
  margin: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* Body 2 columns ───────────────────────────────────────────── */
.rd-body {
  display: grid;
  grid-template-columns: 1.4fr 1fr;
  gap: 16px;
}
body.llm-open .rd-body { grid-template-columns: 1fr; }
@media (max-width: 960px) {
  .rd-body { grid-template-columns: 1fr; }
}
.rd-col { display: flex; flex-direction: column; gap: 16px; min-width: 0; }

.rd-block {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.rd-block-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--border-soft);
}
.rd-block-head h2 {
  font-size: 14px;
  font-weight: 600;
  margin: 0;
  color: var(--text);
}
.rd-block-tag {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: .4px;
  text-transform: uppercase;
  color: var(--text-3);
}
.rd-block-count {
  margin-left: auto;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-3);
}

.rd-block-body { display: flex; flex-direction: column; gap: 8px; min-height: 24px; }
.rd-block-empty {
  font-size: 13px;
  color: var(--text-3);
  font-style: italic;
  padding: 4px 0;
}

/* Conversations rows ───────────────────────────────────────── */
.rd-conv-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 10px;
  background: var(--bg-2);
  border: 1px solid var(--border-soft);
  border-radius: 6px;
  text-align: left;
  cursor: pointer;
  color: var(--text);
  font-family: inherit;
  font-size: 13px;
  transition: background .15s, border-color .15s;
}
.rd-conv-row:hover { background: var(--panel-2); border-color: var(--cyan); }
.rd-conv-row.active { border-color: var(--cyan); box-shadow: inset 2px 0 0 var(--cyan); }
.rd-conv-tier {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: .4px;
  padding: 2px 6px;
  border-radius: 4px;
  background: var(--panel-2);
  color: var(--text-2);
}
.rd-conv-tier.t-fast    { color: var(--amber); }
.rd-conv-tier.t-normal  { color: var(--cyan); }
.rd-conv-tier.t-deep    { color: var(--violet); }
.rd-conv-title {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.rd-conv-meta {
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--text-3);
  white-space: nowrap;
}
.rd-conv-new {
  margin-top: 4px;
  padding: 7px 10px;
  background: transparent;
  border: 1px dashed var(--border);
  border-radius: 6px;
  color: var(--text-2);
  font-size: 12px;
  cursor: pointer;
  transition: border-color .15s, color .15s;
}
.rd-conv-new:hover { border-color: var(--cyan); color: var(--cyan); }

/* Findings rows ────────────────────────────────────────────── */
.rd-finding-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  background: var(--bg-2);
  border: 1px solid var(--border-soft);
  border-radius: 6px;
}
.rd-finding-top {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.rd-finding-refdes {
  font-family: var(--mono);
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--panel-2);
  color: var(--cyan);
  border: 1px solid var(--border-soft);
}
.rd-finding-symptom {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--amber);
}
.rd-finding-session {
  margin-left: auto;
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--text-3);
}
.rd-finding-session.current { color: var(--violet); }
.rd-finding-cause { font-size: 13px; color: var(--text); margin: 0; }
.rd-finding-notes {
  font-size: 12px;
  color: var(--text-2);
  margin: 4px 0 0 0;
  white-space: pre-wrap;
}

/* Timeline ─────────────────────────────────────────────────── */
.rd-timeline {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.rd-timeline-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  font-size: 13px;
  color: var(--text);
}
.rd-timeline-node {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--cyan);
  margin-top: 6px;
  flex-shrink: 0;
}
.rd-timeline-node.emerald { background: var(--emerald); }
.rd-timeline-node.violet  { background: var(--violet); }
.rd-timeline-node.amber   { background: var(--amber); }
.rd-timeline-when {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-3);
  min-width: 8ch;
}
.rd-timeline-label { color: var(--text-2); }

/* Pack status ──────────────────────────────────────────────── */
.rd-pack-status {
  display: flex;
  align-items: center;
  gap: 10px;
}
.rd-pack-status-label {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: .4px;
  text-transform: uppercase;
}
.rd-pack-status-label.ok    { color: var(--emerald); }
.rd-pack-status-label.warn  { color: var(--amber); }
.rd-pack-artefacts {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.rd-pack-artefact {
  font-family: var(--mono);
  font-size: 10.5px;
  padding: 2px 8px;
  border-radius: 4px;
  background: var(--bg-2);
  border: 1px solid var(--border-soft);
  color: var(--text-3);
}
.rd-pack-artefact.present { color: var(--emerald); border-color: var(--emerald); }
```

- [ ] **Step 2: Link the new CSS in `web/index.html`**

Open `web/index.html`. Find the existing `<link rel="stylesheet" href="styles/home.css">` (around line 14) and add **immediately after** it:

```html
<link rel="stylesheet" href="styles/repair_dashboard.css">
```

- [ ] **Step 3: Add the `#repairDashboard` DOM block**

In `web/index.html`, find `<section class="home hidden" id="homeSection">` (around line 312). Right before its closing `</section>`, add the new block as a sibling to `#homeSections` + `#homeEmpty`:

```html
  <!-- Repair dashboard — activated when ?device=&repair= both present. -->
  <div class="hidden" id="repairDashboard">
    <header class="rd-head">
      <div class="rd-head-left">
        <span class="rd-slug mono" id="rdSlug">—</span>
        <h1 class="rd-device" id="rdDevice">—</h1>
        <p class="rd-symptom" id="rdSymptom">—</p>
        <div class="rd-badges" id="rdBadges"></div>
      </div>
      <div class="rd-head-right">
        <button type="button" class="rd-leave-btn" id="rdLeaveBtn" title="Quitter la session">
          <svg class="icon icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
            <path d="M15 7l-5 5 5 5"/><path d="M20 4v16"/><path d="M4 12h11"/>
          </svg>
          <span>Quitter la session</span>
        </button>
      </div>
    </header>

    <section class="rd-tiles" id="rdTiles">
      <a class="rd-tile" data-tool="pcb" id="rdTilePcb">
        <div class="rd-tile-head">
          <svg class="rd-tile-icon" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 3v18M15 9v12M3 15h12"/></svg>
          <span class="rd-tile-title">Boardview</span>
        </div>
        <p class="rd-tile-meta" id="rdTilePcbMeta">—</p>
      </a>
      <a class="rd-tile" data-tool="graphe" id="rdTileGraphe">
        <div class="rd-tile-head">
          <svg class="rd-tile-icon" viewBox="0 0 24 24"><circle cx="6" cy="6" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="12" cy="18" r="2"/><path d="M7.5 7.5L11 16.5M16.5 7.5L13 16.5M8 6h8"/></svg>
          <span class="rd-tile-title">Graphe</span>
        </div>
        <p class="rd-tile-meta" id="rdTileGrapheMeta">—</p>
      </a>
      <a class="rd-tile" data-tool="schematic" id="rdTileSchematic">
        <div class="rd-tile-head">
          <svg class="rd-tile-icon" viewBox="0 0 24 24"><path d="M4 12h3"/><path d="M7 10v4"/><path d="M9 10v4"/><path d="M9 12h3"/><path d="M15 10l2 2-2 2"/><path d="M17 12h3"/></svg>
          <span class="rd-tile-title">Schematic</span>
        </div>
        <p class="rd-tile-meta" id="rdTileSchematicMeta">—</p>
      </a>
      <a class="rd-tile" data-tool="memory-bank" id="rdTileMemoryBank">
        <div class="rd-tile-head">
          <svg class="rd-tile-icon" viewBox="0 0 24 24"><path d="M4 5a2 2 0 012-2h12a2 2 0 012 2v14l-4-2-4 2-4-2-4 2z"/><path d="M12 3v16"/></svg>
          <span class="rd-tile-title">Memory Bank</span>
        </div>
        <p class="rd-tile-meta" id="rdTileMemoryBankMeta">—</p>
      </a>
    </section>

    <div class="rd-body">
      <aside class="rd-col rd-col-primary">
        <section class="rd-block" id="rdBlockConvs">
          <header class="rd-block-head">
            <span class="rd-block-tag">conversations</span>
            <h2>Fils de diagnostic</h2>
            <span class="rd-block-count" id="rdConvCount">—</span>
          </header>
          <div class="rd-block-body" id="rdConvBody">
            <div class="rd-block-empty">Aucune conversation.</div>
          </div>
        </section>

        <section class="rd-block" id="rdBlockFindings">
          <header class="rd-block-head">
            <span class="rd-block-tag">field_reports/</span>
            <h2>Findings enregistrés</h2>
            <span class="rd-block-count" id="rdFindingsCount">—</span>
          </header>
          <div class="rd-block-body" id="rdFindingsBody">
            <div class="rd-block-empty">Aucun finding pour ce device. L'agent en enregistre via <code>mb_record_finding</code> quand tu confirmes une panne.</div>
          </div>
        </section>
      </aside>

      <aside class="rd-col rd-col-secondary">
        <section class="rd-block" id="rdBlockTimeline">
          <header class="rd-block-head">
            <span class="rd-block-tag">activité</span>
            <h2>Timeline</h2>
          </header>
          <ol class="rd-timeline" id="rdTimelineBody"></ol>
        </section>

        <section class="rd-block" id="rdBlockPack">
          <header class="rd-block-head">
            <span class="rd-block-tag">pack</span>
            <h2>Mémoire du device</h2>
          </header>
          <div class="rd-block-body" id="rdPackBody">
            <div class="rd-block-empty">—</div>
          </div>
        </section>
      </aside>
    </div>
  </div>
```

- [ ] **Step 4: Browser verification checklist**

Run: `make run` then open `http://localhost:8000/` in a browser.

Verify:
- Home list renders exactly as before (no regression).
- DevTools → Elements: `#repairDashboard` exists under `#homeSection` and has class `hidden`.
- DevTools → Network: `styles/repair_dashboard.css` loads with status 200, no 404s.
- Nothing looks different on the home screen at first glance.

**Wait for user sign-off before committing.**

- [ ] **Step 5: Commit**

```bash
git add web/index.html web/styles/repair_dashboard.css
git commit -m "$(cat <<'EOF'
feat(web): static shell for repair dashboard

Add #repairDashboard DOM block as sibling of #homeSections inside
#homeSection, plus web/styles/repair_dashboard.css with the full
style sheet. Shell starts .hidden — no behavioral change. Next
commit adds the URL dispatch that toggles visibility and the
renderRepairDashboard() function that populates the blocks.
EOF
)" -- web/index.html web/styles/repair_dashboard.css
```

---

## Task 3: Frontend — URL routing, dispatch, and dashboard rendering

Wire the dashboard. Click a card → `?device=X&repair=R#home` → dashboard renders with all 5 blocks populated. Quitter button cleans the URL.

**Files:**
- Modify: `web/js/router.js` (export `currentSession`, `leaveSession`; update `updateChrome`)
- Modify: `web/js/main.js` (bootstrap dispatch + hashchange dispatch)
- Modify: `web/js/home.js` (card href + `renderRepairDashboard()`)
- Modify: `web/js/llm.js` (export `switchConv`)

- [ ] **Step 1: Add `currentSession()` and `leaveSession()` exports in `web/js/router.js`**

Open `web/js/router.js`. At the bottom of the file, add these two exports. `leaveSession` depends on `navigate()` and on fetching list data, so it imports lazily from `home.js` to avoid circular import issues (`home.js` already imports from `router.js` via `main.js`; keeping the dependency arrow one-way):

```js
/**
 * Return the currently active repair session, derived from URL query params.
 * A session is defined by the SIMULTANEOUS presence of ?device= and ?repair=.
 * Re-derived on every call — zero hidden state.
 */
export function currentSession() {
  const params = new URLSearchParams(window.location.search);
  const device = params.get("device");
  const repair = params.get("repair");
  if (device && repair) return { device, repair };
  return null;
}

/**
 * Quit the active session: strip ?device= + ?repair=, hash to #home, close
 * chat panel, re-render the list. Called from the dashboard's Quitter button
 * and the topbar session pill's [×].
 */
export async function leaveSession() {
  const url = new URL(window.location.href);
  url.searchParams.delete("device");
  url.searchParams.delete("repair");
  url.hash = "#home";
  window.history.replaceState({}, "", url.toString());
  // Close the chat panel if open. llmClose is a <button>; if the panel
  // isn't mounted yet the optional chaining silently skips.
  document.getElementById("llmClose")?.click();
  // Refresh chrome (drops the pill) and swap to list mode.
  navigate("home");
  // Reload the list data. Dynamic import avoids a static circular dependency
  // between router.js and home.js. hideRepairDashboard() must run explicitly
  // because history.replaceState() does NOT fire a hashchange event, so the
  // hashchange dispatch in main.js that would normally call it never runs.
  const { loadHomePacks, loadTaxonomy, loadRepairs, renderHome, hideRepairDashboard } = await import("./home.js");
  hideRepairDashboard();
  const [packs, taxonomy, repairs] = await Promise.all([
    loadHomePacks(), loadTaxonomy(), loadRepairs(),
  ]);
  renderHome(packs, taxonomy, repairs);
}
```

- [ ] **Step 2: Update mode-pill label in `router.js::updateChrome`**

Still in `router.js`, find the function `updateChrome(section, deviceSlug, pack)` and locate the block that assigns `mode` for `section === "graphe"`. **Before** the existing mode computation (i.e. at the top of `updateChrome`), add a session-aware override for `home`:

```js
  // Home's mode-pill reflects whether a session is active. Without a session,
  // it reads "JOURNAL · Réparations" (the SECTION_META default). With a session,
  // it reads "JOURNAL · Session" to signal we're on the dashboard, not the list.
  const activeSession = currentSession();
  if (section === "home" && activeSession) {
    meta = { ...meta, mode: { ...meta.mode, sub: "Session" } };
  }
```

Place this `activeSession` / override right after the existing `let meta = SECTION_META[section] || SECTION_META.home;` line, **before** `let mode = meta.mode;`. Because JavaScript `let meta` is reassignable, we can shadow/replace `meta`.

Concretely, the top of `updateChrome` becomes:

```js
function updateChrome(section, deviceSlug, pack) {
  let meta = SECTION_META[section] || SECTION_META.home;
  const activeSession = currentSession();
  if (section === "home" && activeSession) {
    meta = { ...meta, mode: { ...meta.mode, sub: "Session" } };
  }

  // Mode pill — static per-section, overridden on Graphe by pack state.
  let mode = meta.mode;
  // …rest of function unchanged
```

- [ ] **Step 3: Update `home.js::repairCardHTML` to add `#home` to the href**

Open `web/js/home.js`. Find `repairCardHTML(repair, taxEntry)` (around line 124). Replace the `href` line:

```js
  const href = `?device=${encodeURIComponent(repair.device_slug)}&repair=${encodeURIComponent(repair.repair_id)}`;
```

with:

```js
  // Explicit #home hash so the bootstrap/hashchange dispatch renders the
  // dashboard (not the list) and not the graphe either. Query params are
  // preserved across later intra-section navigation.
  const href = `?device=${encodeURIComponent(repair.device_slug)}&repair=${encodeURIComponent(repair.repair_id)}#home`;
```

- [ ] **Step 4: Export `switchConv` from `web/js/llm.js`**

Open `web/js/llm.js`. Find `function switchConv(convIdOrNew)` (around line 777). Change the declaration to:

```js
export function switchConv(convIdOrNew) {
```

Also locate the function `openPanel()` (around line 654) and change its declaration to:

```js
export function openPanel() {
```

This lets `home.js` open the chat panel on a specific conversation when the user clicks a conversation row in the dashboard.

- [ ] **Step 5: Add `renderRepairDashboard(session)` to `web/js/home.js`**

At the bottom of `web/js/home.js`, add imports at the top (near the existing `import { openPipelineProgress } from './pipeline_progress.js';`):

```js
import { leaveSession, prettifySlug } from './router.js';
import { switchConv, openPanel } from './llm.js';
```

Then, right before `/* ---------- NEW REPAIR MODAL ---------- */` (around line 224), add the dashboard renderer:

```js
// ───────────────────────────────────────────────────────────────
// Repair dashboard — the focused "session hub" state of #home.
// Activated when currentSession() returns non-null.
// ───────────────────────────────────────────────────────────────

export async function renderRepairDashboard(session) {
  const { device: slug, repair: rid } = session;

  // Toggle visibility: hide list states, show dashboard.
  document.getElementById("homeSections")?.classList.add("hidden");
  document.getElementById("homeEmpty")?.classList.add("hidden");
  document.getElementById("repairDashboard")?.classList.remove("hidden");
  // Also hide the list's H1 / CTA while in dashboard mode.
  document.querySelector("#homeSection .home-head")?.classList.add("hidden");

  // Fetch in parallel — list of Promise results, each tolerates failure.
  const [repair, convs, pack, findings, taxonomy] = await Promise.all([
    fetchJSON(`/pipeline/repairs/${encodeURIComponent(rid)}`, null),
    fetchJSON(`/pipeline/repairs/${encodeURIComponent(rid)}/conversations`, { conversations: [] }),
    fetchJSON(`/pipeline/packs/${encodeURIComponent(slug)}`, null),
    fetchJSON(`/pipeline/packs/${encodeURIComponent(slug)}/findings`, []),
    loadTaxonomy(),
  ]);

  const taxIndex = indexTaxonomyBySlug(taxonomy);
  const taxEntry = taxIndex.get(slug) || null;

  renderDashboardHeader(repair, taxEntry, slug, rid);
  renderDashboardTiles(slug, rid, pack, taxEntry);
  renderDashboardConvs(convs.conversations || [], rid);
  renderDashboardFindings(findings, rid);
  renderDashboardTimeline(repair, convs.conversations || [], findings, pack);
  renderDashboardPack(pack, slug, rid);
  wireDashboardHandlers();
}

export function hideRepairDashboard() {
  document.getElementById("repairDashboard")?.classList.add("hidden");
  document.getElementById("homeSections")?.classList.remove("hidden");
  document.querySelector("#homeSection .home-head")?.classList.remove("hidden");
}

async function fetchJSON(url, fallback) {
  try {
    const res = await fetch(url);
    if (!res.ok) return fallback;
    return await res.json();
  } catch (err) {
    console.warn("[dashboard] fetch failed", url, err);
    return fallback;
  }
}

function renderDashboardHeader(repair, taxEntry, slug, rid) {
  const slugEl = document.getElementById("rdSlug");
  const deviceEl = document.getElementById("rdDevice");
  const symptomEl = document.getElementById("rdSymptom");
  const badgesEl = document.getElementById("rdBadges");
  if (!slugEl || !deviceEl || !symptomEl || !badgesEl) return;

  slugEl.textContent = slug;
  deviceEl.textContent = taxEntry
    ? deviceName(taxEntry, { includeBrand: true })
    : (repair?.device_label || humanizeSlug(slug));
  symptomEl.textContent = repair?.symptom || "—";

  const created = repair?.created_at ? relativeTimeFr(repair.created_at) : "—";
  const status = repair?.status || "open";
  const form = taxEntry?.form_factor
    ? `<span class="badge mono">${escapeHtml(taxEntry.form_factor)}</span>`
    : "";
  badgesEl.innerHTML =
    `${statusBadgeHTML(status)}` +
    `<span class="badge mono">${escapeHtml(rid.slice(0, 8))}</span>` +
    form +
    `<span class="rd-created">créée ${escapeHtml(created)}</span>`;
}

function renderDashboardTiles(slug, rid, pack, taxEntry) {
  const qs = `?device=${encodeURIComponent(slug)}&repair=${encodeURIComponent(rid)}`;
  const pcb = document.getElementById("rdTilePcb");
  const graphe = document.getElementById("rdTileGraphe");
  const schematic = document.getElementById("rdTileSchematic");
  const memoryBank = document.getElementById("rdTileMemoryBank");
  if (pcb) pcb.href = `${qs}#pcb`;
  if (graphe) graphe.href = `${qs}#graphe`;
  if (schematic) schematic.href = `${qs}#schematic`;
  if (memoryBank) memoryBank.href = `${qs}#memory-bank`;

  // Tile metas — static text when we don't have richer data. Keep mono and
  // short so the tile stays scannable.
  const pcbMeta = document.getElementById("rdTilePcbMeta");
  if (pcbMeta) pcbMeta.textContent = taxEntry?.form_factor || "board";
  const grapheMeta = document.getElementById("rdTileGrapheMeta");
  if (grapheMeta) {
    const complete = pack && pack.has_registry && pack.has_knowledge_graph
      && pack.has_rules && pack.has_dictionary && pack.has_audit_verdict;
    grapheMeta.textContent = complete ? "APPROUVÉ" : (pack ? "en construction" : "aucune mémoire");
  }
  const schematicMeta = document.getElementById("rdTileSchematicMeta");
  if (schematicMeta) schematicMeta.textContent = pack?.has_schematic_graph ? "importé" : "non importé";
  const mbMeta = document.getElementById("rdTileMemoryBankMeta");
  if (mbMeta) mbMeta.textContent = pack?.has_rules ? "rules + findings" : "vide";
}

function renderDashboardConvs(conversations, rid) {
  const body = document.getElementById("rdConvBody");
  const count = document.getElementById("rdConvCount");
  if (!body || !count) return;
  count.textContent = String(conversations.length);
  body.innerHTML = "";
  if (conversations.length === 0) {
    body.innerHTML = '<div class="rd-block-empty">Aucune conversation — démarre une discussion avec l\'agent.</div>';
  } else {
    for (const c of conversations) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "rd-conv-row";
      row.dataset.convId = c.id;
      const tier = (c.tier || "fast").toLowerCase();
      const title = escapeHtml((c.title || `Conversation ${c.id.slice(0, 6)}`).slice(0, 80));
      const ago = c.last_turn_at ? relativeTimeFr(c.last_turn_at) : "—";
      const cost = typeof c.cost_usd === "number" ? `$${c.cost_usd.toFixed(3)}` : "—";
      row.innerHTML =
        `<span class="rd-conv-tier t-${tier}">${tier.toUpperCase()}</span>` +
        `<span class="rd-conv-title">${title}</span>` +
        `<span class="rd-conv-meta">${c.turns || 0} turns · ${cost} · ${escapeHtml(ago)}</span>`;
      row.addEventListener("click", () => {
        openPanel();
        switchConv(c.id);
      });
      body.appendChild(row);
    }
  }
  const newBtn = document.createElement("button");
  newBtn.type = "button";
  newBtn.className = "rd-conv-new";
  newBtn.textContent = "+ Nouvelle conversation";
  newBtn.addEventListener("click", () => {
    openPanel();
    switchConv("new");
  });
  body.appendChild(newBtn);
}

function renderDashboardFindings(findings, currentRid) {
  const body = document.getElementById("rdFindingsBody");
  const count = document.getElementById("rdFindingsCount");
  if (!body || !count) return;
  count.textContent = String(findings.length);
  if (findings.length === 0) {
    body.innerHTML = '<div class="rd-block-empty">Aucun finding pour ce device. L\'agent en enregistre via <code>mb_record_finding</code> quand tu confirmes une panne.</div>';
    return;
  }
  body.innerHTML = "";
  const currentShort = currentRid.slice(0, 8);
  for (const f of findings) {
    const row = document.createElement("div");
    row.className = "rd-finding-row";
    const isCurrent = f.session_id && f.session_id.startsWith(currentShort);
    const sessionChip = isCurrent
      ? `<span class="rd-finding-session current">ce repair</span>`
      : (f.session_id
          ? `<span class="rd-finding-session">${escapeHtml(f.session_id.slice(0, 8))}</span>`
          : `<span class="rd-finding-session">—</span>`);
    const notes = f.notes
      ? `<p class="rd-finding-notes">${escapeHtml(f.notes)}</p>`
      : "";
    row.innerHTML =
      `<div class="rd-finding-top">` +
        `<span class="rd-finding-refdes">${escapeHtml(f.refdes)}</span>` +
        `<span class="rd-finding-symptom">${escapeHtml(f.symptom)}</span>` +
        sessionChip +
      `</div>` +
      `<p class="rd-finding-cause">${escapeHtml(f.confirmed_cause || "—")}</p>` +
      notes;
    body.appendChild(row);
  }
}

function renderDashboardTimeline(repair, conversations, findings, pack) {
  const body = document.getElementById("rdTimelineBody");
  if (!body) return;
  const events = [];
  if (repair?.created_at) {
    events.push({ when: repair.created_at, label: "Session ouverte", kind: "cyan" });
  }
  for (const c of conversations) {
    if (c.last_turn_at) {
      events.push({
        when: c.last_turn_at,
        label: `Activité · ${(c.tier || "fast").toLowerCase()} · ${c.turns || 0} turns`,
        kind: "emerald",
      });
    }
  }
  for (const f of findings) {
    if (f.created_at) {
      events.push({
        when: f.created_at,
        label: `Finding ${f.refdes || "?"} confirmé`,
        kind: "violet",
      });
    }
  }
  if (pack?.audit_verdict) {
    events.push({
      when: repair?.created_at || new Date().toISOString(),
      label: `Pack audité — ${pack.audit_verdict}`,
      kind: pack.audit_verdict === "APPROVED" ? "emerald" : "amber",
    });
  }
  events.sort((a, b) => (b.when || "").localeCompare(a.when || ""));
  const MAX = 8;
  const shown = events.slice(0, MAX);
  body.innerHTML = shown.map(e => (
    `<li class="rd-timeline-item">` +
      `<span class="rd-timeline-node ${e.kind}"></span>` +
      `<span class="rd-timeline-when">${escapeHtml(relativeTimeFr(e.when))}</span>` +
      `<span class="rd-timeline-label">${escapeHtml(e.label)}</span>` +
    `</li>`
  )).join("");
  if (events.length > MAX) {
    body.innerHTML += `<li class="rd-timeline-item"><span class="rd-timeline-node"></span><span class="rd-timeline-label">+${events.length - MAX} plus anciens</span></li>`;
  }
  if (events.length === 0) {
    body.innerHTML = '<li class="rd-block-empty">Aucune activité.</li>';
  }
}

function renderDashboardPack(pack, slug, rid) {
  const body = document.getElementById("rdPackBody");
  if (!body) return;
  if (!pack) {
    body.innerHTML = '<div class="rd-block-empty">Aucun pack — la mémoire du device n\'est pas encore construite.</div>';
    return;
  }
  const complete = pack.has_registry && pack.has_knowledge_graph
    && pack.has_rules && pack.has_dictionary && pack.has_audit_verdict;
  const statusLabel = complete ? "APPROUVÉ" : "en construction";
  const statusClass = complete ? "ok" : "warn";
  const arts = [
    { key: "has_registry", label: "registry" },
    { key: "has_knowledge_graph", label: "graph" },
    { key: "has_rules", label: "rules" },
    { key: "has_dictionary", label: "dictionary" },
    { key: "has_audit_verdict", label: "audit" },
  ];
  const chips = arts.map(a => (
    `<span class="rd-pack-artefact ${pack[a.key] ? "present" : ""}">${pack[a.key] ? "✓ " : "· "}${a.label}</span>`
  )).join("");
  body.innerHTML =
    `<div class="rd-pack-status">` +
      `<span class="rd-pack-status-label ${statusClass}">${statusLabel}</span>` +
    `</div>` +
    `<div class="rd-pack-artefacts">${chips}</div>`;
}

let _dashboardHandlersWired = false;
function wireDashboardHandlers() {
  if (_dashboardHandlersWired) return;
  _dashboardHandlersWired = true;
  document.getElementById("rdLeaveBtn")?.addEventListener("click", () => {
    leaveSession();
  });
}
```

- [ ] **Step 6: Update `main.js` bootstrap dispatch**

Open `web/js/main.js`. Find the import line:

```js
import { loadHomePacks, loadTaxonomy, loadRepairs, renderHome, initNewRepairModal } from './home.js';
```

Change it to:

```js
import { loadHomePacks, loadTaxonomy, loadRepairs, renderHome, initNewRepairModal, renderRepairDashboard, hideRepairDashboard } from './home.js';
```

Also update the router import to include `currentSession`:

```js
import { APP_VERSION, currentSection, navigate, wireRouter, currentSession } from './router.js';
```

Find the `bootstrap` IIFE. Replace the block:

```js
  const hash = window.location.hash;
  const params = new URLSearchParams(window.location.search);
  const slug = params.get("device");

  // Precedence: explicit hash > slug-implies-graphe > home default
  const initial = hash ? currentSection() : (slug ? "graphe" : "home");
  navigate(initial);
```

with:

```js
  const hash = window.location.hash;
  const params = new URLSearchParams(window.location.search);
  const slug = params.get("device");
  const repairId = params.get("repair");

  // Precedence: explicit hash > session-implies-home > slug-implies-graphe > home default
  const initial = hash
    ? currentSection()
    : (slug && repairId ? "home"
       : slug ? "graphe"
       : "home");
  navigate(initial);
```

Then, in the same `bootstrap` IIFE, find the `else if (initial === "home")` branch:

```js
  } else if (initial === "home") {
    const [packs, taxonomy, repairs] = await Promise.all([loadHomePacks(), loadTaxonomy(), loadRepairs()]);
    renderHome(packs, taxonomy, repairs);
  }
```

Replace it with:

```js
  } else if (initial === "home") {
    const session = currentSession();
    if (session) {
      renderRepairDashboard(session);
    } else {
      hideRepairDashboard();
      const [packs, taxonomy, repairs] = await Promise.all([loadHomePacks(), loadTaxonomy(), loadRepairs()]);
      renderHome(packs, taxonomy, repairs);
    }
  }
```

- [ ] **Step 7: Update `main.js` hashchange dispatch**

Still in `main.js`, find the `hashchange` listener and the `else if (sec === "home")` branch:

```js
    else if (sec === "home") {
      const [packs, taxonomy, repairs] = await Promise.all([loadHomePacks(), loadTaxonomy(), loadRepairs()]);
      renderHome(packs, taxonomy, repairs);
    }
```

Replace it with:

```js
    else if (sec === "home") {
      const session = currentSession();
      if (session) {
        renderRepairDashboard(session);
      } else {
        hideRepairDashboard();
        const [packs, taxonomy, repairs] = await Promise.all([loadHomePacks(), loadTaxonomy(), loadRepairs()]);
        renderHome(packs, taxonomy, repairs);
      }
    }
```

- [ ] **Step 8: Browser verification checklist**

Run: `make run` then open `http://localhost:8000/`.

Verify in order:
1. **Cold start `/`** → list renders, no regression. Breadcrumb: `wrench-board / Journal des réparations`. Mode pill: `JOURNAL · Réparations`.
2. **Click a repair card** → URL becomes `/?device=<slug>&repair=<rid>#home`. The Journal list disappears, the dashboard shell appears populated with device name, symptom, status badge, repair id, tile shortcuts, conversations list (or empty state), findings list (or empty state), timeline, pack status. Mode pill reads `JOURNAL · Session`. Chat panel auto-opens on the right.
3. **Click a tile (e.g. PCB)** → hash changes to `#pcb` (query params preserved). Dashboard is swapped for the PCB section. URL stays `?device=X&repair=R#pcb`.
4. **Click rail Journal icon** → back to dashboard (not list), because session is still active.
5. **Click Quitter la session** (button in `.rd-head-right`) → URL reset to `/#home`, list re-renders, chat panel closes, mode pill back to `JOURNAL · Réparations`.
6. **Click a conversation row** → chat panel opens on that conversation (the WS reconnects with `?conv=<id>`).
7. **Reload any URL with `?device=&repair=#home`** → dashboard renders directly (bookmarkable).
8. **Reload `/?device=X#graphe`** (no repair) → graphe renders without the dashboard, no session pill expected (pill comes in Task 4).

**Wait for user sign-off before committing.**

- [ ] **Step 9: Run lint + test suite**

```bash
make lint && make test
```

Expected: both pass.

- [ ] **Step 10: Commit**

```bash
git add web/index.html web/js/router.js web/js/main.js web/js/home.js web/js/llm.js
git commit -m "$(cat <<'EOF'
feat(web): journal — dashboard mode when a session is active

Click a repair card → #home renders a focused dashboard (header +
4 tool tiles + conversations + findings + timeline + pack status)
instead of the list. currentSession() and leaveSession() derive
state from the URL (zero hidden state). Quitter la session cleans
query params and returns to the list. Chat panel auto-opens as
before; conversation rows open the panel directly on their conv.
EOF
)" -- web/index.html web/js/router.js web/js/main.js web/js/home.js web/js/llm.js
```

---

## Task 4: Frontend — Session pill in topbar

Persistent chip in the topbar whenever a session is active. Click body → dashboard. Click `[×]` → Quitter.

**Files:**
- Modify: `web/index.html` (add pill markup in topbar)
- Modify: `web/styles/repair_dashboard.css` (add `.session-pill` block)
- Modify: `web/js/router.js` (show/hide in `updateChrome`)
- Modify: `web/js/main.js` (wire click handlers)

- [ ] **Step 1: Add the pill markup to `web/index.html`**

Open `web/index.html`. Find the topbar — look for `<div class="crumbs" id="crumbs">` (around line 36) and the mode pill wrapper. The topbar typically has a left cluster (crumbs + mode pill) and a right cluster (global controls). Add the session pill right after the mode pill wrapper and before the right cluster.

Concretely, find the mode pill structure (something like `<div class="mode-pill" id="modePill">`) and locate the element that follows it in the topbar. Insert this as a **sibling of the mode pill**:

```html
<div class="session-pill hidden" id="sessionPill" role="button" tabindex="0"
     aria-label="Session active — clic pour ouvrir le dashboard"
     title="Revenir au dashboard de la session">
  <svg class="session-pill-dot" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2.2" fill="currentColor"/>
  </svg>
  <span class="session-pill-text">
    <span class="session-pill-tag mono">SESSION</span>
    <span class="session-pill-device" id="sessionPillDevice">—</span>
    <span class="session-pill-sep">·</span>
    <span class="session-pill-rid mono" id="sessionPillRid">—</span>
  </span>
  <button type="button" class="session-pill-close" id="sessionPillClose"
          aria-label="Quitter la session" title="Quitter la session">
    <svg class="icon icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <path d="M6 6l12 12M18 6L6 18"/>
    </svg>
  </button>
</div>
```

If the topbar has no obvious "right cluster" wrapper, just insert the pill after the mode pill and let flex layout space it.

- [ ] **Step 2: Add pill CSS to `web/styles/repair_dashboard.css`**

Append to `web/styles/repair_dashboard.css`:

```css
/* ─────────────────────────────────────────────────────────────
 * Session pill — topbar indicator of an active session.
 * Visible wherever currentSession() returns non-null.
 * ───────────────────────────────────────────────────────────── */

.session-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 4px 4px 4px 10px;
  background: var(--panel-2);
  border: 1px solid var(--cyan);
  border-radius: 999px;
  color: var(--text);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
  transition: background .15s, border-color .15s, transform .15s;
  user-select: none;
}
.session-pill.hidden { display: none; }
.session-pill:hover { background: var(--panel); transform: translateY(-1px); }
.session-pill:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }

.session-pill-dot {
  width: 12px;
  height: 12px;
  color: var(--cyan);
  flex-shrink: 0;
}

.session-pill-text {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}
.session-pill-tag {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: .4px;
  color: var(--cyan);
}
.session-pill-device { color: var(--text); font-weight: 500; }
.session-pill-sep { color: var(--text-3); }
.session-pill-rid { color: var(--text-2); font-size: 10.5px; }

.session-pill-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  padding: 0;
  margin-left: 2px;
  background: transparent;
  border: none;
  border-radius: 50%;
  color: var(--text-3);
  cursor: pointer;
  transition: background .15s, color .15s;
}
.session-pill-close:hover { background: var(--panel); color: var(--amber); }
```

- [ ] **Step 3: Show/hide the pill in `router.js::updateChrome`**

Open `web/js/router.js`. In `updateChrome(section, deviceSlug, pack)`, **after** the existing mode-pill wiring (`pill.className = ...` / `pillText.textContent = ...` block), add:

```js
  // Session pill — persistent across sections when a session is active.
  const sessionPill = document.getElementById("sessionPill");
  if (sessionPill) {
    const sess = currentSession();
    if (sess) {
      sessionPill.classList.remove("hidden");
      const devEl = document.getElementById("sessionPillDevice");
      const ridEl = document.getElementById("sessionPillRid");
      if (devEl) devEl.textContent = prettifySlug(sess.device);
      if (ridEl) ridEl.textContent = sess.repair.slice(0, 8);
    } else {
      sessionPill.classList.add("hidden");
    }
  }
```

- [ ] **Step 4: Wire pill click handlers in `main.js`**

Open `web/js/main.js`. Extend the import from `router.js` to include `leaveSession`:

```js
import { APP_VERSION, currentSection, navigate, wireRouter, currentSession, leaveSession } from './router.js';
```

Then, inside the existing `wireTopLevelControls` IIFE (bottom of the file), append:

```js
  // Session pill — click body to go to dashboard, click [×] to quit session.
  const sessionPill = document.getElementById("sessionPill");
  const sessionPillClose = document.getElementById("sessionPillClose");
  if (sessionPill) {
    sessionPill.addEventListener("click", (ev) => {
      if (sessionPillClose && sessionPillClose.contains(ev.target)) return;
      window.location.hash = "#home";
    });
    sessionPill.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        if (sessionPillClose && sessionPillClose.contains(document.activeElement)) return;
        window.location.hash = "#home";
      }
    });
  }
  if (sessionPillClose) {
    sessionPillClose.addEventListener("click", (ev) => {
      ev.stopPropagation();
      leaveSession();
    });
  }
```

- [ ] **Step 5: Browser verification checklist**

Run: `make run` then open `http://localhost:8000/`.

Verify:
1. **Cold start `/`** → no pill visible. Only mode pill shows `JOURNAL · Réparations`.
2. **Open a repair card** → dashboard renders. Session pill visible in topbar, reads `SESSION · <Device Name> · <rid[:8]>`.
3. **Navigate to `#graphe`** (click graphe tile) → pill stays visible in topbar. Mode pill updates to graphe text.
4. **Navigate to `#pcb`, `#schematic`, `#memory-bank`** → pill stays visible across all sections.
5. **Click the pill body (not the [×])** → jumps to `#home`, dashboard re-renders.
6. **Click the pill [×]** → URL cleans, list re-renders, pill disappears, chat panel closes.
7. **Keyboard**: tab to pill, press Enter → same as body click.
8. **Reload mid-session `?device=X&repair=R#pcb`** → pill visible from the very first paint.

**Wait for user sign-off before committing.**

- [ ] **Step 6: Run lint + test suite**

```bash
make lint && make test
```

Expected: both pass.

- [ ] **Step 7: Commit**

```bash
git add web/index.html web/styles/repair_dashboard.css web/js/router.js web/js/main.js
git commit -m "$(cat <<'EOF'
feat(web): persistent session pill in topbar

Cyan rounded chip that appears wherever currentSession() is
non-null. Shows "SESSION · <Device> · <rid[:8]>". Click body →
#home (dashboard). Click [×] → leaveSession() cleans the URL
and returns to the list. Keyboard-accessible (tabindex + Enter).
Wired from updateChrome so the pill state is derived centrally
on every navigation event.
EOF
)" -- web/index.html web/styles/repair_dashboard.css web/js/router.js web/js/main.js
```

---

## Self-Review Checklist (performed before handoff)

**Spec coverage:**

- §2.1 URL = source of truth → Task 3 `currentSession()` derives on every call. ✓
- §2.2 Two-state Journal → Task 2 + 3 (shell + dispatch). ✓
- §2.3 Entry = dashboard not graphe → Task 3 Step 3 (card href) + Step 6 (bootstrap rule). ✓
- §2.4 Rich dashboard (header + 4 tiles + 4 blocks) → Task 2 (markup) + Task 3 (render functions). ✓
- §2.5 Session pill in topbar → Task 4. ✓
- §2.6 Chat auto-opens → existing `openLLMPanelIfRepairParam()` unchanged. ✓
- §2.7 Quitter la session = URL clean → Task 3 Step 1 (`leaveSession`). ✓
- §2.8 Rail Journal contextual → dispatched via `hashchange` in Task 3 Step 7. ✓
- §2.9 No new tokens → only existing tokens used. ✓
- §2.10 List not rewritten → confirmed, `renderHome()` untouched. ✓
- §3 URL matrix → all rows covered by Tasks 2/3/4. ✓
- §4.1 DOM hierarchy → Task 2 Step 3 mirrors the spec's DOM. ✓
- §4.2–4.7 Block contents → Task 3 Step 5 (`renderDashboard*` functions). ✓
- §5 New findings route → Task 1. ✓
- §6 Pill markup + style + wiring → Task 4. ✓
- §7.1–7.3 Files touched → match Tasks 1–4. ✓
- §8 Walkthrough → covered by browser verification checklists in Tasks 3 & 4. ✓

**Placeholder scan:** no TBD, no "add error handling", no "similar to". Each step has concrete code or a concrete command.

**Type consistency:**

- `currentSession()` → `{device, repair} | null` used consistently in Tasks 3 & 4.
- `leaveSession()` → async function, called without `await` on UI handlers (handlers don't care about resolution).
- `renderRepairDashboard(session)` and `hideRepairDashboard()` — both exported, both referenced in main.js bootstrap + hashchange.
- `switchConv` and `openPanel` exports added to `llm.js` in Task 3 Step 4 and consumed in Task 3 Step 5.

**Gaps filled:** none found.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-23-journal-repair-dashboard.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using `executing-plans`, batch execution with checkpoints.

Which approach?
