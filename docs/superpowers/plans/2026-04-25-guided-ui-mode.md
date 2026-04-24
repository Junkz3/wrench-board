# Plan d'implémentation — Mode Guidé (Workspace par Repair)

> **Pour worker agentique :** SUB-SKILL OBLIGATOIRE — utilise `superpowers:subagent-driven-development` (recommandé) ou `superpowers:executing-plans` pour exécuter ce plan tâche par tâche. Les étapes utilisent la syntaxe checkbox `- [ ]`.

**But :** Ajouter un mode guidé style Claude.ai (landing + workspace par repair + widgets inline pop par l'agent + sidebar conversations) qui coexiste avec le mode expert actuel via un toggle ⚙. Aucune régression sur l'expérience pro.

**Architecture :** Deux modes (`body.guided-mode` / `body.expert-mode`) dans le même shell `web/index.html`. Aucun nouveau routeur, aucune dépendance ajoutée. Backend : un seul endpoint `POST /pipeline/classify-intent` (Haiku forced-tool). Frontend : 1 nouvelle landing, sidebar gauche pour le repair courant, widgets inline dans le chat sur tool-calls `bv_*` et `mb_schematic_graph`. Le mode expert restaure 100 % le shell actuel.

**Tech stack :** Python 3.11 / FastAPI / Pydantic v2 / `anthropic ~= 0.96` (backend). Vanilla HTML/CSS/JS, D3, marked + DOMPurify (frontend). Aucun ajout de dépendance.

**Spec source :** `docs/superpowers/specs/2026-04-25-guided-ui-mode-design.md` (commit `6b6d33a`).

---

## Pré-requis avant d'attaquer

- [ ] Travailler sur la branche `main` (pas de worktree pour ce hackathon — l'evolve loop tourne sur `main`).
- [ ] **Stopper l'evolve loop** si elle tourne (`pkill -f microsolder-evolve` ou via Skill tool). Risque sinon : commits parasites pendant qu'on travaille sur le frontend.
- [ ] Tagger un commit stable de référence pour le rollback : `git tag pre-guided-ui-2026-04-25`.
- [ ] Vérifier `make test` au vert avant de commencer : `make test` doit afficher 937 passed.

---

## File structure

**Nouveaux fichiers** :
- `api/pipeline/intent_classifier.py` (~120 lignes) — module métier classifier Haiku forced-tool.
- `tests/pipeline/test_intent_classifier.py` (~80 lignes) — tests unitaires mockés.
- `web/js/landing.js` (~180 lignes) — logique landing : champ texte, chips, fetch classifier, transition vers workspace.
- `web/styles/landing.css` (~100 lignes) — styles landing hero (overlay, hero, champ, chips, animation).
- `web/styles/guided.css` (~400 lignes) — tous les styles du mode guidé (sidebar, topbar tabs, chat plein écran, widgets inline, workbench-detail).

**Fichiers modifiés** :
- `api/pipeline/__init__.py` — ajout endpoint `/pipeline/classify-intent` (~50 lignes ajoutées).
- `web/index.html` — ajout overlay landing + sidebar guidée + topbar tabs (~150 lignes ajoutées).
- `web/js/main.js` — gating landing au boot, init du mode (~40 lignes ajoutées).
- `web/js/router.js` — helper `setMode()` + persistance localStorage (~30 lignes ajoutées).
- `web/js/llm.js` — promotion popover conversations en sidebar permanente, rendu inline widgets agent (~120 lignes ajoutées).

**Fichiers strictement non touchés** : `api/agent/*`, `api/board/*`, `api/pipeline/schematic/*`, `api/pipeline/scout.py`, `api/pipeline/registry.py`, `api/pipeline/writers.py`, `api/pipeline/auditor.py`, `web/brd_viewer.js`, `web/js/schematic.js`, `web/js/graph.js`, `web/js/memory_bank.js`, `web/js/profile.js`, `web/js/home.js`, `web/styles/layout.css`, `web/styles/llm.css`, `web/styles/brd.css`, `web/styles/schematic.css`, `web/styles/graph.css`, `web/styles/tokens.css`.

---

## Phase 1 — Backend : classifier d'intention

### Task 1.1 — Pydantic schemas (TDD)

**Files :**
- Create: `api/pipeline/intent_classifier.py`
- Test: `tests/pipeline/test_intent_classifier.py`

- [ ] **Step 1 — Test : validation des schemas**

Crée `tests/pipeline/test_intent_classifier.py` :

```python
"""Unit tests for the intent classifier (offline, mocked)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.pipeline.intent_classifier import IntentCandidate, IntentClassification


def test_intent_candidate_requires_slug():
    with pytest.raises(ValidationError):
        IntentCandidate(label="ok", confidence=0.5, pack_exists=True)


def test_intent_candidate_confidence_bounds():
    with pytest.raises(ValidationError):
        IntentCandidate(slug="x", label="x", confidence=1.5, pack_exists=True)
    with pytest.raises(ValidationError):
        IntentCandidate(slug="x", label="x", confidence=-0.1, pack_exists=True)


def test_intent_classification_max_three_candidates():
    cands = [
        IntentCandidate(slug=f"d{i}", label=f"D{i}", confidence=0.5, pack_exists=True)
        for i in range(4)
    ]
    with pytest.raises(ValidationError):
        IntentClassification(symptoms="x", candidates=cands)


def test_intent_classification_empty_candidates_ok():
    obj = IntentClassification(symptoms="rien de connu", candidates=[])
    assert obj.candidates == []
```

- [ ] **Step 2 — Run test, expect FAIL (module manquant)**

```bash
.venv/bin/pytest tests/pipeline/test_intent_classifier.py -v
```

Expected : `ImportError: cannot import name 'IntentCandidate' from 'api.pipeline.intent_classifier'`.

- [ ] **Step 3 — Implémente les schemas**

Crée `api/pipeline/intent_classifier.py` :

```python
"""Haiku-driven intent classifier for the landing hero.

Takes a free-text user input (e.g. "MNT Reform — pas de boot") and
returns up to 3 candidate device slugs ranked by confidence. Used by the
landing page to funnel a non-expert user into the right repair workspace
without asking them to pick from a dropdown.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class IntentCandidate(BaseModel):
    slug: str = Field(min_length=1, description="Canonical device slug (e.g. 'mnt-reform-motherboard').")
    label: str = Field(min_length=1, description="Human-readable device label (French).")
    confidence: float = Field(ge=0.0, le=1.0, description="Classifier confidence 0..1.")
    pack_exists: bool = Field(description="True if memory/{slug}/ exists on disk with a knowledge pack.")


class IntentClassification(BaseModel):
    symptoms: str = Field(default="", description="Normalised symptom description extracted from user input.")
    candidates: list[IntentCandidate] = Field(default_factory=list, max_length=3)
```

- [ ] **Step 4 — Run test, expect PASS**

```bash
.venv/bin/pytest tests/pipeline/test_intent_classifier.py -v
```

Expected : 4 passed.

- [ ] **Step 5 — Commit**

```bash
git add api/pipeline/intent_classifier.py tests/pipeline/test_intent_classifier.py
git commit -m "feat(pipeline): IntentCandidate / IntentClassification schemas for the landing classifier" -- api/pipeline/intent_classifier.py tests/pipeline/test_intent_classifier.py
```

---

### Task 1.2 — Fonction `classify_intent()` (TDD avec mock Anthropic)

**Files :**
- Modify: `api/pipeline/intent_classifier.py`
- Modify: `tests/pipeline/test_intent_classifier.py`

- [ ] **Step 1 — Test : appel mocké, candidat unique haute confiance**

Append à `tests/pipeline/test_intent_classifier.py` :

```python
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from api.pipeline.intent_classifier import classify_intent


def _make_anthropic_response(payload: dict):
    """Build a fake Anthropic Messages response wrapping a tool_use block."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "report_intent"
    block.input = payload
    block.id = "toolu_test"
    resp = MagicMock()
    resp.content = [block]
    resp.stop_reason = "tool_use"
    resp.usage = MagicMock(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return resp


@pytest.mark.asyncio
async def test_classify_intent_single_high_confidence(tmp_path: Path):
    pack_dir = tmp_path / "mnt-reform-motherboard"
    pack_dir.mkdir()
    (pack_dir / "registry.json").write_text('{"device_label": "MNT Reform — carte mère"}')

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(
        return_value=_make_anthropic_response({
            "symptoms": "MNT Reform — pas de boot",
            "candidates": [
                {"slug": "mnt-reform-motherboard", "label": "MNT Reform — carte mère", "confidence": 0.92},
            ],
        })
    )

    with patch("api.pipeline.intent_classifier._get_memory_root", return_value=tmp_path):
        result = await classify_intent("MNT Reform ne démarre pas, écran noir", client=fake_client)

    assert result.symptoms.startswith("MNT Reform")
    assert len(result.candidates) == 1
    assert result.candidates[0].slug == "mnt-reform-motherboard"
    assert result.candidates[0].pack_exists is True
    assert result.candidates[0].confidence == pytest.approx(0.92)


@pytest.mark.asyncio
async def test_classify_intent_unknown_pack_marked_false(tmp_path: Path):
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(
        return_value=_make_anthropic_response({
            "symptoms": "iPhone 11 — charge",
            "candidates": [
                {"slug": "iphone-11", "label": "iPhone 11", "confidence": 0.8},
            ],
        })
    )
    with patch("api.pipeline.intent_classifier._get_memory_root", return_value=tmp_path):
        result = await classify_intent("iPhone 11 charge plus", client=fake_client)
    assert result.candidates[0].pack_exists is False


@pytest.mark.asyncio
async def test_classify_intent_truncates_to_three(tmp_path: Path):
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(
        return_value=_make_anthropic_response({
            "symptoms": "vague",
            "candidates": [
                {"slug": f"d{i}", "label": f"D{i}", "confidence": 0.5 - i * 0.1}
                for i in range(5)
            ],
        })
    )
    with patch("api.pipeline.intent_classifier._get_memory_root", return_value=tmp_path):
        result = await classify_intent("ordinateur en panne", client=fake_client)
    assert len(result.candidates) == 3
    # sorted desc by confidence
    confs = [c.confidence for c in result.candidates]
    assert confs == sorted(confs, reverse=True)
```

- [ ] **Step 2 — Run test, expect FAIL (function manquante)**

```bash
.venv/bin/pytest tests/pipeline/test_intent_classifier.py -v
```

- [ ] **Step 3 — Implémente `classify_intent()`**

Append à `api/pipeline/intent_classifier.py` :

```python
from pathlib import Path

from anthropic import AsyncAnthropic

from api.config import get_settings


_TOOL_NAME = "report_intent"

_TOOL_SCHEMA = {
    "name": _TOOL_NAME,
    "description": "Report the user's diagnostic intent: symptoms + 0..3 candidate devices ranked by confidence.",
    "input_schema": {
        "type": "object",
        "properties": {
            "symptoms": {
                "type": "string",
                "description": "One-sentence normalised description of what the user says is wrong (in French).",
            },
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string", "description": "Canonical device slug, lowercase, hyphenated."},
                        "label": {"type": "string", "description": "French human-readable label."},
                        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    },
                    "required": ["slug", "label", "confidence"],
                },
                "maxItems": 3,
            },
        },
        "required": ["symptoms", "candidates"],
    },
}


def _get_memory_root() -> Path:
    return Path(get_settings().memory_root)


def _list_known_packs() -> list[tuple[str, str]]:
    """Return [(slug, label)] for every directory under memory/ that has a registry.json with a device_label."""
    import json

    root = _get_memory_root()
    if not root.exists():
        return []
    out: list[tuple[str, str]] = []
    for pack_dir in sorted(root.iterdir(), key=lambda p: p.name):
        if not pack_dir.is_dir() or pack_dir.name.startswith("_"):
            continue
        registry = pack_dir / "registry.json"
        if not registry.exists():
            out.append((pack_dir.name, pack_dir.name))
            continue
        try:
            data = json.loads(registry.read_text(encoding="utf-8"))
            label = data.get("device_label") or pack_dir.name
        except (OSError, json.JSONDecodeError):
            label = pack_dir.name
        out.append((pack_dir.name, label))
    return out


def _build_system_prompt() -> str:
    packs = _list_known_packs()
    if packs:
        catalog = "\n".join(f"- `{slug}` — {label}" for slug, label in packs)
    else:
        catalog = "(no packs on disk yet)"
    return (
        "You are a strict intent classifier for a hardware repair workbench.\n"
        "Given a free-text user description (in French or English), decide which device they are talking about\n"
        "and extract a one-sentence symptom summary.\n\n"
        "Always call the `report_intent` tool. Return 0 to 3 candidates ranked by confidence.\n"
        "Prefer slugs from the catalog below when there is any plausible match.\n"
        "If the user input is vague or off-topic, return an empty `candidates` list rather than guessing.\n\n"
        "Catalog of known device slugs:\n"
        f"{catalog}\n"
    )


async def classify_intent(text: str, *, client: AsyncAnthropic) -> IntentClassification:
    """Run a Haiku one-shot forced-tool classifier.

    The caller is responsible for instantiating the AsyncAnthropic client (so tests
    can pass in a mock). Side effects: none on disk; only an Anthropic API call.
    """
    settings = get_settings()
    response = await client.messages.create(
        model=settings.anthropic_model_fast,
        max_tokens=512,
        system=[
            {"type": "text", "text": _build_system_prompt(), "cache_control": {"type": "ephemeral"}}
        ],
        messages=[{"role": "user", "content": text}],
        tools=[_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": _TOOL_NAME},
    )

    payload: dict | None = None
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == _TOOL_NAME:
            payload = block.input
            break
    if payload is None:
        return IntentClassification(symptoms="", candidates=[])

    raw_candidates = payload.get("candidates") or []
    raw_candidates = sorted(raw_candidates, key=lambda c: c.get("confidence", 0.0), reverse=True)[:3]

    known_slugs = {slug for slug, _ in _list_known_packs()}
    cleaned: list[IntentCandidate] = []
    for c in raw_candidates:
        slug = (c.get("slug") or "").strip()
        if not slug:
            continue
        cleaned.append(
            IntentCandidate(
                slug=slug,
                label=c.get("label") or slug,
                confidence=float(c.get("confidence") or 0.0),
                pack_exists=slug in known_slugs,
            )
        )

    return IntentClassification(symptoms=str(payload.get("symptoms") or ""), candidates=cleaned)
```

- [ ] **Step 4 — Run test, expect PASS**

```bash
.venv/bin/pytest tests/pipeline/test_intent_classifier.py -v
```

Expected : 7 passed.

- [ ] **Step 5 — Commit**

```bash
git add api/pipeline/intent_classifier.py tests/pipeline/test_intent_classifier.py
git commit -m "feat(pipeline): classify_intent() — Haiku forced-tool classifier with pack-existence cross-check" -- api/pipeline/intent_classifier.py tests/pipeline/test_intent_classifier.py
```

---

### Task 1.3 — Endpoint HTTP `POST /pipeline/classify-intent`

**Files :**
- Modify: `api/pipeline/__init__.py`
- Test: `tests/pipeline/test_classify_intent_route.py` (nouveau)

- [ ] **Step 1 — Test : route répond 200 avec body valide**

Crée `tests/pipeline/test_classify_intent_route.py` :

```python
"""Integration test for POST /pipeline/classify-intent."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from api.main import app
from api.pipeline.intent_classifier import IntentCandidate, IntentClassification

client = TestClient(app)


def test_classify_intent_returns_classification():
    fake = IntentClassification(
        symptoms="MNT Reform — pas de boot",
        candidates=[
            IntentCandidate(slug="mnt-reform-motherboard", label="MNT Reform — carte mère", confidence=0.92, pack_exists=True),
        ],
    )
    with patch("api.pipeline.classify_intent", new=AsyncMock(return_value=fake)):
        res = client.post("/pipeline/classify-intent", json={"text": "MNT Reform ne démarre pas"})
    assert res.status_code == 200
    body = res.json()
    assert body["symptoms"] == "MNT Reform — pas de boot"
    assert body["candidates"][0]["slug"] == "mnt-reform-motherboard"
    assert body["candidates"][0]["pack_exists"] is True


def test_classify_intent_rejects_empty_text():
    res = client.post("/pipeline/classify-intent", json={"text": "   "})
    assert res.status_code == 422


def test_classify_intent_returns_503_on_anthropic_failure():
    async def raise_runtime(*_a, **_k):
        raise RuntimeError("anthropic down")

    with patch("api.pipeline.classify_intent", new=raise_runtime):
        res = client.post("/pipeline/classify-intent", json={"text": "rien"})
    assert res.status_code == 503
```

- [ ] **Step 2 — Run test, expect FAIL (404 sur la route)**

```bash
.venv/bin/pytest tests/pipeline/test_classify_intent_route.py -v
```

- [ ] **Step 3 — Ajoute la route dans `api/pipeline/__init__.py`**

Recherche le bloc d'imports existant en haut du fichier et ajoute (si pas déjà présent) :

```python
from anthropic import AsyncAnthropic

from api.pipeline.intent_classifier import IntentClassification, classify_intent
```

Puis, ajoute en bas du fichier (avant la fin de fichier, après les autres routes `@router.post / @router.get`) :

```python
class ClassifyIntentRequest(BaseModel):
    text: str = Field(min_length=1, max_length=400)


@router.post("/classify-intent", response_model=IntentClassification)
async def classify_intent_route(payload: ClassifyIntentRequest) -> IntentClassification:
    """Run the landing-page intent classifier (Haiku forced tool)."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="Anthropic API key not configured")
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        return await classify_intent(payload.text.strip(), client=client)
    except Exception as exc:  # network / Anthropic
        raise HTTPException(status_code=503, detail=f"intent classifier failed: {exc}") from exc
```

Vérifie que `BaseModel`, `Field`, `HTTPException`, `get_settings` sont déjà importés dans le fichier ; sinon ajoute aux imports.

- [ ] **Step 4 — Run test, expect PASS**

```bash
.venv/bin/pytest tests/pipeline/test_classify_intent_route.py -v
```

Expected : 3 passed.

- [ ] **Step 5 — Lance la suite rapide complète**

```bash
make test
```

Expected : 940 passed (937 + 3 nouveaux), 1 skipped, 1 xfailed, 44 deselected.

- [ ] **Step 6 — Commit**

```bash
git add api/pipeline/__init__.py tests/pipeline/test_classify_intent_route.py
git commit -m "feat(pipeline): POST /pipeline/classify-intent — landing intent endpoint (Haiku, 503 on failure)" -- api/pipeline/__init__.py tests/pipeline/test_classify_intent_route.py
```

---

## Phase 2 — Mode foundation (CSS classes + état localStorage)

### Task 2.1 — Helper `setMode()` dans `router.js`

**Files :**
- Modify: `web/js/router.js`

- [ ] **Step 1 — Lecture du fichier**

```bash
.venv/bin/python -c "print(open('web/js/router.js').read()[:300])"
```

Repère où sont définies les fonctions exportées (probablement en bas).

- [ ] **Step 2 — Ajoute le helper en fin de fichier**

Append à `web/js/router.js` :

```javascript
// ============ Mode (guidé / expert) ============
//
// The shell has two modes:
//   - guided  : landing + Claude.ai-style repair workspace (default)
//   - expert  : original pro-tool workbench with the 8-section rail
//
// State is stored on `<body>` as `guided-mode` or `expert-mode` and persisted
// in localStorage under "microsolder.mode". The rest of the app reads from
// these classes via plain CSS selectors (no JS event bus needed).

const MODE_KEY = "microsolder.mode";
export const MODES = Object.freeze({ GUIDED: "guided", EXPERT: "expert" });

export function getMode() {
  const raw = localStorage.getItem(MODE_KEY);
  return raw === MODES.EXPERT ? MODES.EXPERT : MODES.GUIDED;
}

export function setMode(mode) {
  const next = mode === MODES.EXPERT ? MODES.EXPERT : MODES.GUIDED;
  localStorage.setItem(MODE_KEY, next);
  applyModeClass(next);
}

export function toggleMode() {
  setMode(getMode() === MODES.GUIDED ? MODES.EXPERT : MODES.GUIDED);
}

function applyModeClass(mode) {
  document.body.classList.toggle("guided-mode", mode === MODES.GUIDED);
  document.body.classList.toggle("expert-mode", mode === MODES.EXPERT);
}

export function initMode() {
  applyModeClass(getMode());
}
```

- [ ] **Step 3 — Manual check**

Aucun test unitaire JS dans ce projet. Vérification : `make run` puis ouvrir la console navigateur et tester :

```javascript
// dans la console de http://localhost:8000
import("/js/router.js").then(m => { m.setMode("guided"); console.log(document.body.className); });
```

Expected : la classe `guided-mode` apparaît sur `<body>`.

- [ ] **Step 4 — Commit**

```bash
git add web/js/router.js
git commit -m "feat(web): setMode/getMode/toggleMode helpers in router.js (localStorage-backed)" -- web/js/router.js
```

---

### Task 2.2 — Init du mode au boot

**Files :**
- Modify: `web/js/main.js`

- [ ] **Step 1 — Lecture du fichier**

```bash
.venv/bin/python -c "print(open('web/js/main.js').read())"
```

Repère le premier import et la fonction de boot (probablement nommée `boot`, `init`, ou un IIFE).

- [ ] **Step 2 — Ajoute l'import + appel `initMode()`**

Modifie `web/js/main.js` : ajoute l'import en haut :

```javascript
import { initMode } from "./router.js";
```

(Si `router.js` est déjà importé, ajoute simplement `initMode` à la liste.)

Puis dans la fonction de boot (ou dès la première instruction au top-level), AVANT toute manipulation DOM :

```javascript
initMode();
```

L'appel doit se faire avant que le routeur dispatch une section (sinon la page apparaît un instant en mode guidé puis bascule).

- [ ] **Step 3 — Manual check**

```bash
make run
```

Ouvre `http://localhost:8000` puis dans la console :

```javascript
console.log(document.body.className);
// Doit contenir "guided-mode"
localStorage.setItem("microsolder.mode", "expert");
location.reload();
console.log(document.body.className);
// Doit contenir "expert-mode"
```

- [ ] **Step 4 — Commit**

```bash
git add web/js/main.js
git commit -m "feat(web): initialize mode (guided/expert) at boot from localStorage" -- web/js/main.js
```

---

## Phase 3 — Landing hero (HTML + CSS + JS)

### Task 3.1 — Structure HTML de la landing

**Files :**
- Modify: `web/index.html`

- [ ] **Step 1 — Repère l'élément racine**

Le shell actuel commence ligne 32 par `<div class="topbar">`. Insère la landing AVANT le topbar (et après `<body>`).

- [ ] **Step 2 — Insère l'overlay landing**

Ouvre `web/index.html`. Trouve la ligne `<div class="topbar">` (autour de la ligne 32) et insère JUSTE AVANT :

```html
<!-- Landing hero — visible only when body has class .show-landing -->
<div id="landing-overlay" class="landing" hidden>
  <div class="landing-bg" aria-hidden="true"></div>
  <main class="landing-hero">
    <div class="landing-mark">
      <svg viewBox="0 0 24 24" width="32" height="32" stroke="currentColor"
           stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round">
        <path d="M13 2L3 14h7l-1 8 11-13h-7z" fill="currentColor" stroke="none"/>
      </svg>
      <span>microsolder</span>
    </div>
    <h1 class="landing-title">Ton assistant de réparation hardware.</h1>
    <p class="landing-sub">Décris ce qui ne marche pas — je m'occupe du diagnostic.</p>
    <form class="landing-form" id="landingForm" autocomplete="off">
      <input class="landing-input" id="landingInput" type="text"
             placeholder="ex. mon Framework ne s'allume plus"
             aria-label="Décris ton problème"
             maxlength="400" required />
      <button class="landing-submit" type="submit" id="landingSubmit">
        Diagnostiquer
        <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor"
             stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round">
          <path d="M5 12h14M13 5l7 7-7 7"/>
        </svg>
      </button>
    </form>
    <div class="landing-chips" id="landingChips">
      <button type="button" class="landing-chip" data-text="MNT Reform — pas de boot, écran noir">MNT Reform — pas de boot</button>
      <button type="button" class="landing-chip" data-text="iPhone 11 ne charge plus">iPhone 11 — pas de charge</button>
      <button type="button" class="landing-chip" data-text="Framework laptop écran noir au démarrage">Framework — écran noir</button>
    </div>
    <div class="landing-status" id="landingStatus" aria-live="polite"></div>
  </main>
</div>
```

- [ ] **Step 3 — Manual check (visible si on force la classe)**

Lance `make run`, ouvre `http://localhost:8000`, console :

```javascript
document.body.classList.add("show-landing");
document.getElementById("landing-overlay").hidden = false;
```

Pour l'instant aucun style appliqué — l'overlay s'affichera comme du HTML brut. C'est attendu, on ajoute la CSS à la tâche suivante.

- [ ] **Step 4 — Commit**

```bash
git add web/index.html
git commit -m "feat(web): landing hero HTML structure (overlay, hero, form, example chips)" -- web/index.html
```

---

### Task 3.2 — CSS de la landing hero

**Files :**
- Create: `web/styles/landing.css`
- Modify: `web/index.html`

- [ ] **Step 1 — Crée le fichier**

Crée `web/styles/landing.css` :

```css
/* Landing hero — visible when body has class .show-landing.
 * Pure presentation: visibility is driven by JS toggling the body class.
 * Reuses tokens.css; introduces no new colors. */

.landing[hidden] { display: none !important; }

.landing {
  position: fixed;
  inset: 0;
  z-index: 1000;
  background: var(--bg-deep);
  overflow: hidden;
  display: grid;
  place-items: center;
}

.landing-bg {
  position: absolute;
  inset: -10%;
  background:
    radial-gradient(ellipse at 30% 30%, rgba(125, 211, 252, 0.10), transparent 55%),
    radial-gradient(ellipse at 70% 60%, rgba(167, 139, 250, 0.06), transparent 55%),
    var(--bg-deep);
  filter: blur(40px);
  opacity: 0.9;
  pointer-events: none;
}

.landing-hero {
  position: relative;
  width: min(640px, 92vw);
  padding: 32px;
  text-align: center;
  animation: landing-rise .35s cubic-bezier(.2, .8, .2, 1);
}

@keyframes landing-rise {
  from { opacity: 0; transform: translateY(12px); }
  to   { opacity: 1; transform: translateY(0); }
}

.landing-mark {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--text-2);
  font-size: 13px;
  letter-spacing: 0.5px;
  margin-bottom: 32px;
}

.landing-mark svg { color: var(--cyan, #7dd3fc); }

.landing-title {
  font-family: 'Inter', sans-serif;
  font-size: 32px;
  font-weight: 600;
  line-height: 1.2;
  color: var(--text);
  margin: 0 0 12px 0;
  letter-spacing: -0.5px;
}

.landing-sub {
  font-size: 15px;
  color: var(--text-2);
  margin: 0 0 32px 0;
}

.landing-form {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}

.landing-input {
  flex: 1;
  background: var(--panel);
  border: 1px solid var(--border);
  color: var(--text);
  font-size: 15px;
  padding: 14px 16px;
  border-radius: 10px;
  font-family: inherit;
  transition: border-color .15s, background .15s;
}

.landing-input:focus {
  outline: none;
  border-color: var(--border-hover, var(--cyan));
  background: var(--panel-2);
}

.landing-input::placeholder { color: var(--text-3); }

.landing-submit {
  background: var(--cyan);
  color: var(--bg-deep);
  border: none;
  font-weight: 600;
  font-size: 14px;
  padding: 0 20px;
  border-radius: 10px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  transition: filter .15s, transform .05s;
  font-family: inherit;
}

.landing-submit:hover { filter: brightness(1.1); }
.landing-submit:active { transform: translateY(1px); }
.landing-submit:disabled { opacity: 0.5; cursor: progress; }

.landing-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
  margin-bottom: 24px;
}

.landing-chip {
  background: transparent;
  border: 1px solid var(--border-soft, var(--border));
  color: var(--text-2);
  font-size: 12px;
  padding: 6px 12px;
  border-radius: 999px;
  cursor: pointer;
  font-family: inherit;
  transition: border-color .15s, background .15s, color .15s;
}

.landing-chip:hover {
  border-color: var(--border-hover, var(--cyan));
  color: var(--text);
  background: var(--panel);
}

.landing-status {
  font-size: 12px;
  color: var(--text-3);
  min-height: 18px;
}

.landing-status.error { color: #f87171; }
```

- [ ] **Step 2 — Wire le CSS dans `index.html`**

Dans `web/index.html`, repère la liste des `<link rel="stylesheet">` dans le `<head>` et ajoute :

```html
<link rel="stylesheet" href="/styles/landing.css">
```

(Place-le après `tokens.css` pour respecter la cascade des tokens, mais avant les CSS spécifiques sections comme `brd.css`.)

- [ ] **Step 3 — Manual check**

```bash
make run
```

Ouvre `http://localhost:8000`, dans la console :

```javascript
document.body.classList.add("show-landing");
document.getElementById("landing-overlay").hidden = false;
```

Vérifie visuellement : titre centré, champ texte arrondi, bouton « Diagnostiquer » cyan, 3 chips cliquables. Animation à l'apparition. **DEMANDE VALIDATION VISUELLE À ALEXIS** (cf. memory `feedback_visual_changes_require_user_verify.md`).

- [ ] **Step 4 — Commit (après validation Alexis)**

```bash
git add web/styles/landing.css web/index.html
git commit -m "feat(web): landing hero styles (centered hero, gradient bg, animated rise)" -- web/styles/landing.css web/index.html
```

---

### Task 3.3 — Logique JS de la landing (champ + chips, sans classifier)

**Files :**
- Create: `web/js/landing.js`
- Modify: `web/index.html`

- [ ] **Step 1 — Crée le module**

Crée `web/js/landing.js` :

```javascript
// Landing hero logic — handles form submission, chip clicks, classifier
// fetch, repair creation, and transition to the workspace. Owned entirely
// by this module: shows/hides itself via the body.show-landing class.

const STATUS_NEUTRAL = "";
const STATUS_LOADING = "loading";
const STATUS_ERROR = "error";

let isSubmitting = false;

export function showLanding() {
  document.body.classList.add("show-landing");
  const ov = document.getElementById("landing-overlay");
  if (ov) ov.hidden = false;
  setTimeout(() => document.getElementById("landingInput")?.focus(), 50);
}

export function hideLanding() {
  document.body.classList.remove("show-landing");
  const ov = document.getElementById("landing-overlay");
  if (ov) ov.hidden = true;
}

function setStatus(msg, kind) {
  const el = document.getElementById("landingStatus");
  if (!el) return;
  el.textContent = msg || "";
  el.classList.remove("error");
  if (kind === STATUS_ERROR) el.classList.add("error");
}

function setSubmitting(on) {
  isSubmitting = on;
  const btn = document.getElementById("landingSubmit");
  if (btn) btn.disabled = on;
}

async function onSubmit(ev) {
  ev.preventDefault();
  if (isSubmitting) return;
  const input = document.getElementById("landingInput");
  const text = (input?.value || "").trim();
  if (text.length < 3) {
    setStatus("Décris un peu plus ce qui ne marche pas.", STATUS_ERROR);
    return;
  }
  setStatus("Je cherche…", STATUS_LOADING);
  setSubmitting(true);
  try {
    await processIntent(text);
  } catch (err) {
    console.error("[landing] submit failed", err);
    setStatus("Impossible de classifier — bascule en mode manuel.", STATUS_ERROR);
    // Phase 4 will add the dropdown fallback. For now, surface the error.
  } finally {
    setSubmitting(false);
  }
}

// Stub — Phase 4 fills this in.
async function processIntent(text) {
  console.log("[landing] would classify:", text);
  setStatus("(classifier wiring viendra en Phase 4)", STATUS_NEUTRAL);
}

function onChipClick(ev) {
  const btn = ev.target.closest(".landing-chip");
  if (!btn) return;
  const input = document.getElementById("landingInput");
  if (input) {
    input.value = btn.dataset.text || btn.textContent;
    input.focus();
  }
}

export function initLanding() {
  const form = document.getElementById("landingForm");
  if (form) form.addEventListener("submit", onSubmit);
  const chips = document.getElementById("landingChips");
  if (chips) chips.addEventListener("click", onChipClick);
}
```

- [ ] **Step 2 — Charge le module**

Dans `web/index.html`, repère la balise `<script type="module" src="/js/main.js"></script>` près de la fin du `<body>`. Ajoute juste avant :

```html
<script type="module" src="/js/landing.js"></script>
```

Puis dans `web/js/main.js`, ajoute en haut :

```javascript
import { initLanding, showLanding, hideLanding } from "./landing.js";
```

Et dans la fonction de boot, après `initMode()` :

```javascript
initLanding();
// Show landing if no current repair selected; else go straight to workspace.
const params = new URLSearchParams(location.search);
if (!params.get("repair")) {
  showLanding();
}
```

- [ ] **Step 3 — Manual check**

```bash
make run
```

Ouvre `http://localhost:8000` (sans `?repair=...`). La landing apparaît. Tape « test », clique un chip → l'input se remplit. Submit → tu vois le message stub dans le status. **VALIDATION VISUELLE ALEXIS** (animation, focus, chips, statut).

- [ ] **Step 4 — Commit**

```bash
git add web/js/landing.js web/index.html web/js/main.js
git commit -m "feat(web): landing module with form/chip handlers and show/hide helpers (classifier wiring stubbed)" -- web/js/landing.js web/index.html web/js/main.js
```

---

### Task 3.4 — Branchement classifier + transition vers workspace

**Files :**
- Modify: `web/js/landing.js`

- [ ] **Step 1 — Implémente `processIntent()` en remplaçant le stub**

Remplace la fonction `processIntent` dans `web/js/landing.js` :

```javascript
const CONFIDENCE_AUTO_THRESHOLD = 0.7;

async function processIntent(text) {
  // Step 1: classify
  let classification;
  try {
    const res = await fetch("/pipeline/classify-intent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    classification = await res.json();
  } catch (err) {
    console.error("[landing] classify failed", err);
    setStatus("Le classificateur est indisponible — choisis un appareil :", STATUS_ERROR);
    showFallbackPicker();
    return;
  }

  // Step 2: pick best candidate
  const top = (classification.candidates || [])[0];
  const autoConfirm = top && top.pack_exists && top.confidence >= CONFIDENCE_AUTO_THRESHOLD;

  if (autoConfirm) {
    setStatus(`Reconnu : ${top.label}. J'ouvre le diagnostic…`, STATUS_NEUTRAL);
    await openWorkspaceForSlug(top.slug, classification.symptoms || text);
    return;
  }

  // Step 3: low confidence — open workspace and let agent ask via confirmation widget
  if (top) {
    setStatus(`Pas sûr… j'ouvre quand même, l'agent va te demander confirmation.`, STATUS_NEUTRAL);
    await openWorkspaceForSlug(top.slug, classification.symptoms || text, { needsConfirm: true, candidates: classification.candidates });
    return;
  }

  // Step 4: no candidate at all
  setStatus("Je n'ai pas reconnu ton appareil. Choisis dans la liste :", STATUS_ERROR);
  showFallbackPicker();
}

async function openWorkspaceForSlug(slug, symptoms, opts = {}) {
  // Create a repair (POST /pipeline/repairs) and navigate to it.
  try {
    const res = await fetch("/pipeline/repairs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_slug: slug, title: (symptoms || "Diagnostic").slice(0, 80) }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const repair = await res.json();
    const rid = repair.repair_id || repair.id;
    if (!rid) throw new Error("missing repair id in response");
    // Redirect with ?repair= so the workspace boot picks it up.
    const url = new URL(location.href);
    url.searchParams.set("repair", rid);
    url.searchParams.delete("landing");
    if (opts.needsConfirm) url.searchParams.set("confirm_intent", "1");
    location.href = url.toString();
  } catch (err) {
    console.error("[landing] repair create failed", err);
    setStatus("Impossible d'ouvrir le diagnostic — réessaie.", STATUS_ERROR);
  }
}

async function showFallbackPicker() {
  // Phase 4 stretch: real dropdown of existing packs.
  // Minimal fallback for now: list existing packs as chips below.
  const status = document.getElementById("landingStatus");
  try {
    const res = await fetch("/pipeline/packs");
    if (!res.ok) return;
    const packs = await res.json();
    if (!packs.length) return;
    const node = document.createElement("div");
    node.className = "landing-chips";
    node.style.marginTop = "8px";
    packs.slice(0, 6).forEach(p => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "landing-chip";
      b.textContent = p.device_slug;
      b.addEventListener("click", () => openWorkspaceForSlug(p.device_slug, "Diagnostic"));
      node.appendChild(b);
    });
    status?.parentNode?.insertBefore(node, status.nextSibling);
  } catch (err) {
    console.warn("[landing] fallback packs fetch failed", err);
  }
}
```

- [ ] **Step 2 — Manual check (chemin heureux)**

```bash
make run
```

`http://localhost:8000`, tape « MNT Reform — pas de boot », submit. Attendu :
- Le status change à « Je cherche… »
- Si la classification réussit (Haiku doit reconnaître MNT Reform), redirection vers `?repair=<rid>`.
- L'overlay disparaît au reload, le workspace existant s'affiche.

Si ANTHROPIC_API_KEY pas configuré → 503 → fallback picker affiche les packs sur disque.

- [ ] **Step 3 — Manual check (chemin fallback)**

Pour forcer le fallback : tape « blablabla appareil inexistant 1234 » → la classification renverra des candidats faibles ou aucun → status d'erreur + fallback picker visible.

- [ ] **Step 4 — Commit**

```bash
git add web/js/landing.js
git commit -m "feat(web): landing wires classify-intent, auto-confirms above 0.7 confidence, falls back to packs picker" -- web/js/landing.js
```

---

## Phase 4 — Workspace shell (sidebar guidée + topbar repair tabs)

### Task 4.1 — Structure HTML : sidebar guidée

**Files :**
- Modify: `web/index.html`

- [ ] **Step 1 — Insère la sidebar avant `<div class="rail">`**

Repère ligne 72 : `<div class="rail">`. Insère JUSTE AVANT :

```html
<!-- Guided-mode sidebar — visible only when body.guided-mode + a repair is open -->
<aside class="g-sidebar" id="guidedSidebar" aria-label="Mes diagnostics et conversations">
  <header class="g-sidebar-head">
    <button class="g-sidebar-home" id="gSidebarHome" title="Retour à l'accueil">
      <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor"
           stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round">
        <path d="M3 12L12 3l9 9"/><path d="M5 10v10h14V10"/>
      </svg>
      Accueil
    </button>
  </header>

  <section class="g-sidebar-section">
    <h3 class="g-sidebar-label">Mes diagnostics</h3>
    <div class="g-repair-list" id="gRepairList"></div>
    <button class="g-sidebar-new" type="button" id="gNewRepair">
      + nouveau diagnostic
    </button>
  </section>

  <section class="g-sidebar-section">
    <h3 class="g-sidebar-label">Conversations</h3>
    <div class="g-conv-list" id="gConvList"></div>
    <button class="g-sidebar-new g-conv-new" type="button" id="gNewConv">
      + nouvelle conversation
    </button>
  </section>
</aside>
```

- [ ] **Step 2 — Ajoute les onglets repair-scoped dans la topbar**

Repère la topbar (lignes ~32-70). Trouve la section qui contient `<div class="crumbs">` et `<div class="mode-pill">`. Insère juste avant le bouton `tweaksToggle` (ou en dernière position dans le centre topbar) :

```html
<nav class="topbar-tabs" id="topbarTabs" aria-label="Vues du repair">
  <button class="tb-tab" data-detail="memory" type="button">Memory</button>
  <button class="tb-tab" data-detail="schematic" type="button">Schéma</button>
  <button class="tb-tab" data-detail="graphe" type="button">Graphe</button>
</nav>
<button class="top-btn mode-toggle" id="modeToggle" title="Mode expert (⚙)">
  <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor"
       stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="12" cy="12" r="3"/>
    <path d="M19 12c0 .4-.04.78-.1 1.16l2.06 1.61-2 3.46-2.43-.97a7.05 7.05 0 0 1-2.01 1.16l-.37 2.58h-4l-.37-2.58a7.05 7.05 0 0 1-2.01-1.16l-2.43.97-2-3.46 2.06-1.61A7 7 0 0 1 5 12c0-.4.04-.78.1-1.16L3.04 9.23l2-3.46 2.43.97a7.05 7.05 0 0 1 2.01-1.16L9.85 3h4l.37 2.58a7.05 7.05 0 0 1 2.01 1.16l2.43-.97 2 3.46-2.06 1.61c.06.38.1.76.1 1.16z"/>
  </svg>
</button>
```

- [ ] **Step 3 — Manual check**

`make run`, ouvre `http://localhost:8000?repair=<un_rid_existant>`. Sans CSS la sidebar et les onglets vont s'afficher en HTML brut — c'est attendu.

- [ ] **Step 4 — Commit**

```bash
git add web/index.html
git commit -m "feat(web): guided-mode sidebar + topbar repair tabs + mode toggle button (HTML structure)" -- web/index.html
```

---

### Task 4.2 — CSS du mode guidé (sidebar + topbar tabs + chat fullscreen)

**Files :**
- Create: `web/styles/guided.css`
- Modify: `web/index.html`

- [ ] **Step 1 — Crée `web/styles/guided.css`**

Crée `web/styles/guided.css` avec ce contenu (~270 lignes) :

```css
/* Guided mode — Claude.ai-style repair workspace.
 *
 * Activates via body.guided-mode. Hides the rail and metabar, shows the
 * left sidebar (#guidedSidebar) and the topbar tabs (#topbarTabs), and
 * makes the chat panel (#agent section) full-screen.
 *
 * No edits to layout.css / llm.css / brd.css — this file is an additive layer.
 */

/* ============ Visibility toggles ============ */

#guidedSidebar { display: none; }
#topbarTabs    { display: none; }
#modeToggle    { display: none; }
.landing       { display: none; }

body.guided-mode #guidedSidebar { display: flex; }
body.guided-mode #topbarTabs    { display: flex; }
body.guided-mode #modeToggle    { display: inline-flex; }
body.guided-mode.show-landing .landing { display: grid; }
body.guided-mode.show-landing .topbar,
body.guided-mode.show-landing #guidedSidebar,
body.guided-mode.show-landing .rail,
body.guided-mode.show-landing .metabar,
body.guided-mode.show-landing .workspace,
body.guided-mode.show-landing .statusbar { visibility: hidden; }

body.expert-mode #modeToggle { display: inline-flex; }

/* In guided mode, hide the rail + metabar entirely. */
body.guided-mode .rail    { display: none !important; }
body.guided-mode .metabar { display: none !important; }

/* ============ Sidebar ============ */

.g-sidebar {
  position: fixed;
  top: 48px;          /* below topbar */
  left: 0;
  bottom: 28px;       /* above statusbar */
  width: 220px;
  background: var(--bg);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  z-index: 50;
}

.g-sidebar-head {
  padding: 10px 12px;
  border-bottom: 1px solid var(--border-soft, var(--border));
}

.g-sidebar-home {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: transparent;
  border: 1px solid var(--border-soft, var(--border));
  color: var(--text-2);
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 11px;
  cursor: pointer;
  font-family: inherit;
  transition: border-color .15s, color .15s, background .15s;
}
.g-sidebar-home:hover {
  border-color: var(--cyan);
  color: var(--text);
  background: var(--panel);
}

.g-sidebar-section {
  padding: 12px;
  border-bottom: 1px solid var(--border-soft, var(--border));
}

.g-sidebar-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  color: var(--text-3);
  margin: 0 0 8px 0;
  font-family: 'JetBrains Mono', monospace;
  font-weight: 500;
}

.g-repair-list, .g-conv-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.g-repair-item, .g-conv-item-btn {
  display: block;
  width: 100%;
  text-align: left;
  background: transparent;
  border: none;
  border-left: 2px solid transparent;
  color: var(--text-2);
  font-size: 12px;
  padding: 6px 10px;
  border-radius: 4px;
  cursor: pointer;
  font-family: inherit;
  transition: background .12s, color .12s, border-color .12s;
}
.g-repair-item:hover, .g-conv-item-btn:hover {
  background: var(--panel);
  color: var(--text);
}
.g-repair-item.active, .g-conv-item-btn.active {
  background: var(--panel);
  border-left-color: var(--cyan);
  color: var(--text);
}

.g-repair-item-meta, .g-conv-item-meta {
  display: block;
  font-size: 10px;
  color: var(--text-3);
  margin-top: 2px;
  font-family: 'JetBrains Mono', monospace;
}

.g-sidebar-new {
  display: block;
  width: 100%;
  background: transparent;
  border: 1px dashed var(--border-soft, var(--border));
  color: var(--cyan, #7dd3fc);
  font-size: 12px;
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
  margin-top: 8px;
  font-family: inherit;
  transition: border-color .15s, background .15s;
}
.g-sidebar-new:hover {
  border-color: var(--cyan);
  background: rgba(125, 211, 252, 0.05);
}

/* ============ Topbar tabs ============ */

.topbar-tabs {
  display: flex;
  gap: 2px;
  margin-left: 16px;
}

.tb-tab {
  background: transparent;
  border: 1px solid transparent;
  color: var(--text-2);
  font-size: 12px;
  padding: 4px 10px;
  border-radius: 5px;
  cursor: pointer;
  font-family: inherit;
  transition: background .12s, color .12s, border-color .12s;
}
.tb-tab:hover {
  background: var(--panel);
  color: var(--text);
  border-color: var(--border);
}
.tb-tab.active {
  background: var(--panel-2, var(--panel));
  color: var(--text);
  border-color: var(--cyan);
}

/* ============ Chat fullscreen in guided mode ============ */

body.guided-mode .workspace {
  /* The original workspace expects a rail (52px) and metabar (44px).
   * In guided mode those are hidden, so we shift the workspace right
   * by the sidebar width (220px) instead. */
  left: 220px !important;
  top: 48px !important;
}

/* The agent / llm panel becomes the entire main area. */
body.guided-mode .section { display: none; }
body.guided-mode .section[data-section="agent"] {
  display: flex !important;
  flex-direction: column;
  position: absolute;
  inset: 0;
}

/* Hide the original conversations popover chip in guided mode — sidebar replaces it. */
body.guided-mode #llmConvChip { display: none; }

/* ============ Inline widgets (chat tool-call cards) ============ */

.chat-widget {
  margin: 8px 0;
  border: 1px solid var(--border);
  background: var(--panel);
  border-radius: 8px;
  overflow: hidden;
  animation: widget-fade .2s ease-out;
}

@keyframes widget-fade {
  from { opacity: 0; transform: translateY(4px); }
  to   { opacity: 1; transform: translateY(0); }
}

.chat-widget-head {
  padding: 6px 10px;
  background: var(--panel-2, var(--panel));
  border-bottom: 1px solid var(--border-soft, var(--border));
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11px;
  color: var(--text-2);
}

.chat-widget-title {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
}

.chat-widget-detail {
  background: transparent;
  border: 1px solid var(--border-soft, var(--border));
  color: var(--cyan, #7dd3fc);
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-family: inherit;
}
.chat-widget-detail:hover {
  border-color: var(--cyan);
  background: rgba(125, 211, 252, 0.05);
}

.chat-widget-body {
  padding: 10px;
  min-height: 60px;
}

.chat-widget-body.mini-board { height: 320px; padding: 0; }
.chat-widget-body.placeholder { color: var(--text-3); font-size: 12px; text-align: center; padding: 20px; }

/* ============ Workbench-detail mode (inside guided) ============ */

body.guided-mode.detail-view #guidedSidebar { width: 60px; }
body.guided-mode.detail-view .g-sidebar-label,
body.guided-mode.detail-view .g-sidebar-new,
body.guided-mode.detail-view .g-sidebar-home span,
body.guided-mode.detail-view .g-repair-item-meta,
body.guided-mode.detail-view .g-conv-item-meta { display: none; }
body.guided-mode.detail-view .workspace { left: 60px !important; }
body.guided-mode.detail-view .section[data-section="agent"] { display: none !important; }

body.guided-mode.detail-view .section[data-section="pcb"]      { display: flex !important; }
body.guided-mode.detail-view[data-detail="schematic"] .section[data-section="schematic"] { display: flex !important; }
body.guided-mode.detail-view[data-detail="graphe"] .section[data-section="graphe"] { display: flex !important; }
body.guided-mode.detail-view[data-detail="memory"] .section[data-section="memory-bank"] { display: flex !important; }

/* "Back to chat" button when in detail-view. Injected by JS into the section header. */
.detail-back {
  position: fixed;
  top: 56px;
  left: 72px;
  z-index: 100;
  background: var(--panel-2, var(--panel));
  border: 1px solid var(--border);
  color: var(--text);
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 12px;
  cursor: pointer;
  font-family: inherit;
  display: none;
}
body.guided-mode.detail-view .detail-back { display: inline-flex; align-items: center; gap: 6px; }
.detail-back:hover { border-color: var(--cyan); }
```

- [ ] **Step 2 — Wire le CSS dans `index.html`**

Dans `<head>` de `index.html`, ajoute (après `landing.css`) :

```html
<link rel="stylesheet" href="/styles/guided.css">
```

- [ ] **Step 3 — Manual check**

`make run`, `http://localhost:8000?repair=<un_rid>`. Ferme la landing manuellement (`document.body.classList.remove('show-landing')`) si elle s'ouvre. Vérifie :
- Sidebar gauche 220 px visible (vide, c'est attendu — alimentation en Phase 5)
- Topbar : onglets « Memory | Schéma | Graphe » + bouton ⚙
- Le chat occupe le reste
- Rail et métabar invisibles
- Bascule expert : `localStorage.setItem('microsolder.mode','expert'); location.reload()` → tout redevient comme avant.

**VALIDATION VISUELLE ALEXIS.**

- [ ] **Step 4 — Commit**

```bash
git add web/styles/guided.css web/index.html
git commit -m "feat(web): guided.css — sidebar, topbar tabs, fullscreen chat, detail-view (expert mode untouched)" -- web/styles/guided.css web/index.html
```

---

### Task 4.3 — Bouton ⚙ : toggle mode + retour landing

**Files :**
- Modify: `web/js/main.js`

- [ ] **Step 1 — Wire le toggle**

Dans `web/js/main.js`, après `initLanding()` :

```javascript
import { toggleMode, MODES, getMode } from "./router.js";

// ...

// Mode toggle button (⚙)
const modeBtn = document.getElementById("modeToggle");
if (modeBtn) {
  modeBtn.addEventListener("click", () => {
    toggleMode();
    // Visual feedback: pulse the button briefly
    modeBtn.style.transform = "scale(0.92)";
    setTimeout(() => { modeBtn.style.transform = ""; }, 120);
  });
}

// Sidebar "Accueil" button → return to landing (clears repair param).
const homeBtn = document.getElementById("gSidebarHome");
if (homeBtn) {
  homeBtn.addEventListener("click", () => {
    const url = new URL(location.href);
    url.searchParams.delete("repair");
    url.searchParams.delete("conv");
    location.href = url.toString();
  });
}

// "+ nouveau diagnostic" → reopen landing as overlay
const newRepairBtn = document.getElementById("gNewRepair");
if (newRepairBtn) {
  newRepairBtn.addEventListener("click", () => {
    showLanding();
  });
}
```

- [ ] **Step 2 — Manual check**

`make run`. Clique ⚙ → bascule entre guidé et expert (rail apparaît/disparaît). Clique « Accueil » dans la sidebar → retour à la landing.

- [ ] **Step 3 — Commit**

```bash
git add web/js/main.js
git commit -m "feat(web): wire mode toggle button and guided sidebar home/new buttons" -- web/js/main.js
```

---

## Phase 5 — Sidebar : repairs + conversations

### Task 5.1 — Liste « Mes diagnostics » alimentée par /pipeline/taxonomy

**Files :**
- Modify: `web/js/main.js`

- [ ] **Step 1 — Ajoute un loader pour les repairs**

Append à `web/js/main.js` :

```javascript
async function loadGuidedRepairs() {
  const list = document.getElementById("gRepairList");
  if (!list) return;
  try {
    // Use existing taxonomy endpoint to enumerate packs and their repairs.
    const res = await fetch("/pipeline/taxonomy");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const tree = await res.json();

    // Flatten brand > model > entries into a single sorted list (by label).
    const flat = [];
    Object.entries(tree.brands || {}).forEach(([brand, models]) => {
      Object.entries(models).forEach(([_model, entries]) => {
        entries.forEach(e => flat.push(e));
      });
    });
    (tree.uncategorized || []).forEach(e => flat.push(e));
    flat.sort((a, b) => (a.device_label || a.device_slug).localeCompare(b.device_label || b.device_slug));

    const params = new URLSearchParams(location.search);
    const currentRid = params.get("repair");

    list.innerHTML = "";
    if (!flat.length) {
      list.innerHTML = '<div class="g-empty" style="font-size:11px;color:var(--text-3);padding:8px">Aucun diagnostic encore.</div>';
      return;
    }

    // Fetch repairs for each pack — best-effort, fall through if endpoint missing.
    for (const entry of flat) {
      try {
        const r = await fetch(`/pipeline/repairs?slug=${encodeURIComponent(entry.device_slug)}`);
        if (!r.ok) continue;
        const repairs = await r.json();
        if (!Array.isArray(repairs)) continue;
        repairs.forEach(rep => {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "g-repair-item" + (rep.repair_id === currentRid ? " active" : "");
          btn.innerHTML =
            `<span>${escapeHTML(rep.title || entry.device_label || entry.device_slug)}</span>` +
            `<span class="g-repair-item-meta">${escapeHTML(entry.device_slug)}</span>`;
          btn.addEventListener("click", () => {
            const url = new URL(location.href);
            url.searchParams.set("repair", rep.repair_id);
            url.searchParams.delete("conv");
            location.href = url.toString();
          });
          list.appendChild(btn);
        });
      } catch (err) {
        console.warn("[guided] repair fetch failed for", entry.device_slug, err);
      }
    }
  } catch (err) {
    console.warn("[guided] loadGuidedRepairs failed", err);
  }
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}
```

Et appelle `loadGuidedRepairs()` à la fin du boot, après `initLanding()` :

```javascript
loadGuidedRepairs();
```

**Note :** si l'endpoint `/pipeline/repairs?slug=` n'existe pas (rechercher avec `grep -n "@router.get.*repairs" api/pipeline/__init__.py`), tu auras besoin de l'ajouter. Si ce n'est pas le cas, fallback : itère manuellement sur les répertoires `memory/{slug}/repairs/*/repair.json` via un nouvel endpoint listé. Cette liste est *cosmetic* pour la sidebar — si aucun endpoint existe, montre simplement `flat[]` (les packs eux-mêmes) comme entrées « ouvrir un diagnostic » sans repair_id et la création se fait à la volée.

- [ ] **Step 2 — Manual check**

`make run`. La sidebar « Mes diagnostics » liste les repairs existants. Clique → switch repair.

- [ ] **Step 3 — Commit**

```bash
git add web/js/main.js
git commit -m "feat(web): populate guided sidebar 'Mes diagnostics' from /pipeline/taxonomy + repairs" -- web/js/main.js
```

---

### Task 5.2 — Promotion popover conversations en sidebar

**Files :**
- Modify: `web/js/llm.js`

- [ ] **Step 1 — Modifie `renderConvItems()` pour cibler le bon container selon le mode**

Dans `web/js/llm.js`, repère la fonction `renderConvItems()` (autour ligne 773). Modifie-la pour rendre dans BOTH le popover (mode expert) ET la sidebar guidée :

```javascript
function renderConvItems() {
  const list = el("llmConvList");
  const label = el("llmConvLabel");
  const guidedList = document.getElementById("gConvList");

  if (!list && !guidedList) return;
  if (list) list.innerHTML = "";
  if (guidedList) guidedList.innerHTML = "";

  if (conversationsCache.length === 0) {
    if (label) label.textContent = "CONV 0/0";
    if (guidedList) guidedList.innerHTML = '<div class="g-empty" style="font-size:11px;color:var(--text-3);padding:6px">Aucune conversation pour ce repair.</div>';
    return;
  }

  const activeIdx = Math.max(0, conversationsCache.findIndex(c => c.id === currentConvId));
  if (label) label.textContent = `CONV ${activeIdx + 1}/${conversationsCache.length}`;

  conversationsCache.forEach((c, idx) => {
    const tier = (c.tier || "fast").toLowerCase();
    const title = escapeHTML((c.title || `Conversation ${idx + 1}`).slice(0, 80));
    const cost = Number(c.cost_usd || 0);
    const ago = c.last_turn_at ? humanAgo(c.last_turn_at) : "—";

    // Popover variant (expert mode)
    if (list) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "conv-item" + (c.id === currentConvId ? " active" : "");
      btn.dataset.convId = c.id;
      btn.innerHTML =
        `<span class="conv-item-head">` +
          `<span class="conv-item-tier t-${tier}">${tier.toUpperCase()}</span>` +
          `<span class="conv-item-title">${title}</span>` +
        `</span>` +
        `<span class="conv-item-meta">` +
          `<span>${c.turns || 0} turn${(c.turns || 0) === 1 ? "" : "s"}</span>` +
          `<span class="conv-item-sep">·</span>` +
          `<span>${fmtUsd(cost)}</span>` +
          `<span class="conv-item-sep">·</span>` +
          `<span>${ago}</span>` +
        `</span>`;
      btn.addEventListener("click", () => {
        if (c.id === currentConvId) { closeConvPopover(); return; }
        switchConv(c.id);
        closeConvPopover();
      });
      list.appendChild(btn);
    }

    // Guided sidebar variant
    if (guidedList) {
      const gbtn = document.createElement("button");
      gbtn.type = "button";
      gbtn.className = "g-conv-item-btn" + (c.id === currentConvId ? " active" : "");
      gbtn.innerHTML =
        `<span>${title}</span>` +
        `<span class="g-conv-item-meta">${tier} · ${c.turns || 0} t · ${ago}</span>`;
      gbtn.addEventListener("click", () => {
        if (c.id === currentConvId) return;
        switchConv(c.id);
      });
      guidedList.appendChild(gbtn);
    }
  });
}
```

- [ ] **Step 2 — Wire le bouton « + nouvelle conversation »**

Dans `web/js/main.js`, ajoute :

```javascript
const newConvBtn = document.getElementById("gNewConv");
if (newConvBtn) {
  newConvBtn.addEventListener("click", async () => {
    // switchConv("new") triggers the WS reconnect with conv=new query param.
    const llm = await import("./llm.js");
    if (llm.switchConv) llm.switchConv("new");
  });
}
```

- [ ] **Step 3 — Auto-load des conversations à l'ouverture du repair**

Dans `web/js/llm.js`, repère où `loadConversations()` est appelé (probablement uniquement à l'ouverture du popover). Ajoute un appel au boot de la connexion WS pour pré-remplir la sidebar guidée. Cherche la fonction qui démarre la WS (probablement `connect()` ou similaire), et appelle `loadConversations()` après que `currentRepairId()` est connu :

```javascript
// Inside connect() or wherever session_ready is handled:
// after currentConvId is set from the server event:
loadConversations();
```

- [ ] **Step 4 — Manual check**

`make run`. Ouvre `?repair=<rid>`. La sidebar « Conversations » se remplit. Clique sur une conv → switch. Clique « + nouvelle conversation » → nouvelle conv créée.

- [ ] **Step 5 — Commit**

```bash
git add web/js/llm.js web/js/main.js
git commit -m "feat(web): promote conversations popover to permanent guided sidebar (dual rendering)" -- web/js/llm.js web/js/main.js
```

---

## Phase 6 — Inline widgets (boardview MVP)

### Task 6.1 — Détecte les tool calls `bv_*` dans le flux WS

**Files :**
- Modify: `web/js/llm.js`

- [ ] **Step 1 — Trouve l'endroit où les tool_use blocks sont consommés**

```bash
grep -n "tool_use\|tool_name\|bv_\|requires_action" /home/alex/Documents/hackathon-microsolder/web/js/llm.js | head -20
```

Identifie le handler qui parse les évènements WS de l'agent (probablement un `switch` sur `event.type`).

- [ ] **Step 2 — Ajoute un dispatch widget**

Ajoute en haut de `web/js/llm.js` (au-dessus des fonctions, après les imports) :

```javascript
const BV_TOOL_NAMES = new Set([
  "bv_highlight_component", "bv_focus_component", "bv_highlight_net",
  "bv_flip_board", "bv_annotate", "bv_filter_by_type", "bv_draw_arrow",
  "bv_measure_distance", "bv_show_pin", "bv_dim_unrelated",
  "bv_layer_visibility", "bv_reset_view",
]);

function chatLogElement() {
  // Returns the DOM container where chat messages are appended. Adjust the
  // selector to match the existing chat list ID in index.html.
  return document.getElementById("llmLog") || document.getElementById("chatLog");
}

function appendChatWidget({ title, kind, builder, onDetail }) {
  const log = chatLogElement();
  if (!log) return null;
  const wrap = document.createElement("div");
  wrap.className = `chat-widget chat-widget-${kind}`;
  wrap.innerHTML =
    `<header class="chat-widget-head">` +
      `<span class="chat-widget-title">${escapeHTML(title)}</span>` +
      `<button type="button" class="chat-widget-detail">voir en détail →</button>` +
    `</header>` +
    `<div class="chat-widget-body chat-widget-body--inner"></div>`;
  const inner = wrap.querySelector(".chat-widget-body--inner");
  if (kind === "board") inner.classList.add("mini-board");
  log.appendChild(wrap);
  log.scrollTop = log.scrollHeight;

  if (typeof builder === "function") builder(inner);
  if (typeof onDetail === "function") {
    wrap.querySelector(".chat-widget-detail").addEventListener("click", onDetail);
  }
  return wrap;
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}
```

- [ ] **Step 3 — Wire le dispatch dans le handler tool_use**

Repère le handler. À l'endroit où un `tool_use` block arrive avec son `tool_name`, ajoute (après le traitement existant si présent) :

```javascript
// In guided mode: pop a chat widget for board-affecting tool calls.
if (document.body.classList.contains("guided-mode")) {
  if (BV_TOOL_NAMES.has(toolName)) {
    insertBoardviewMini(toolInput || {});
  } else if (toolName === "mb_schematic_graph") {
    insertSchematicMiniOrPlaceholder(toolInput || {});
  }
}
```

(Adapte les noms de variables `toolName`, `toolInput` à ce qu'utilise le handler local.)

- [ ] **Step 4 — Implémente `insertBoardviewMini()`**

Append à `web/js/llm.js` :

```javascript
function insertBoardviewMini(input) {
  appendChatWidget({
    title: "Boardview",
    kind: "board",
    builder: (container) => {
      // The main brd_viewer.js exposes window.Boardview. We reuse it by
      // requesting an embed render against this small container. The viewer
      // is expected to support init(container, { embed: true }).
      try {
        if (window.Boardview && typeof window.Boardview.embed === "function") {
          window.Boardview.embed(container, {
            highlight: input.refdes ? [input.refdes] : [],
            focus: input.refdes || null,
          });
        } else {
          // Fallback if the viewer doesn't expose an embed API: show a placeholder
          // pointing the user to the detail view.
          container.classList.remove("mini-board");
          container.classList.add("placeholder");
          container.innerHTML = `<div>Carte → cliquer "voir en détail"</div>`;
        }
      } catch (err) {
        console.warn("[widget] mini-board init failed", err);
        container.textContent = "(boardview indisponible)";
      }
    },
    onDetail: () => enterDetailView("pcb"),
  });
}
```

**Note importante :** `window.Boardview.embed()` n'existe peut-être pas encore. Si après vérification le viewer ne l'expose pas, **change le builder** pour rendre une **placeholder card** (« voir en détail »). Cf. risque §9.2 du spec. NE PAS modifier `brd_viewer.js` sans validation Alexis.

- [ ] **Step 5 — Implémente `enterDetailView()`**

Append à `web/js/llm.js` :

```javascript
function enterDetailView(section) {
  // section: "pcb" | "schematic" | "graphe" | "memory"
  const body = document.body;
  body.classList.add("detail-view");
  body.dataset.detailSection = section;

  // Inject a "back" button if not present.
  let back = document.getElementById("detailBack");
  if (!back) {
    back = document.createElement("button");
    back.id = "detailBack";
    back.className = "detail-back";
    back.innerHTML = `<span>← retour conversation</span>`;
    back.addEventListener("click", exitDetailView);
    document.body.appendChild(back);
  }
  back.style.display = "inline-flex";
}

function exitDetailView() {
  document.body.classList.remove("detail-view");
  delete document.body.dataset.detailSection;
  const back = document.getElementById("detailBack");
  if (back) back.style.display = "none";
}

// Topbar tabs trigger detail view directly.
document.addEventListener("click", (ev) => {
  const tab = ev.target.closest(".tb-tab");
  if (!tab) return;
  enterDetailView(tab.dataset.detail);
  document.querySelectorAll(".tb-tab").forEach(t => t.classList.toggle("active", t === tab));
});
```

- [ ] **Step 6 — Manual check**

`make run`, charge un repair connu (ex. MNT Reform avec le board pré-loadé via `SessionState.from_device`). Pose une question à l'agent qui le pousse à appeler `bv_focus_component` (ex. « focus U7 »).

Attendu :
- Un widget `chat-widget-board` apparaît dans le fil.
- Si `window.Boardview.embed()` existe : mini-board s'affiche avec U7 highlighté.
- Sinon : placeholder « voir en détail ».
- Clic « voir en détail → » : bascule en detail-view, board pleine taille, bouton ← visible.
- Clic ← : retour au chat.

**VALIDATION ALEXIS.**

- [ ] **Step 7 — Commit**

```bash
git add web/js/llm.js
git commit -m "feat(web): inline boardview widget on bv_* tool calls + detail-view toggle (placeholder fallback if no embed API)" -- web/js/llm.js
```

---

### Task 6.2 — Inline widget pour `mb_schematic_graph` (placeholder MVP)

**Files :**
- Modify: `web/js/llm.js`

- [ ] **Step 1 — Implémente `insertSchematicMiniOrPlaceholder()`**

Append à `web/js/llm.js` :

```javascript
function insertSchematicMiniOrPlaceholder(input) {
  const query = (input && input.query) || "info";
  const titleMap = { simulate: "Simulateur", hypothesize: "Hypothèses" };
  const title = titleMap[query] || "Schéma";

  appendChatWidget({
    title,
    kind: "placeholder",
    builder: (container) => {
      // Phase 7 (stretch) replaces this with an actual mini-schematic embed.
      // For MVP we ship a placeholder card that funnels the user to detail view.
      const summary = query === "simulate"
        ? "Simulation lancée — clique pour voir la timeline."
        : query === "hypothesize"
          ? "Hypothèses générées — clique pour voir le classement."
          : "Schéma disponible — clique pour ouvrir.";
      container.innerHTML = `<div>${escapeHTML(summary)}</div>`;
    },
    onDetail: () => enterDetailView("schematic"),
  });
}
```

- [ ] **Step 2 — Manual check**

Pose à l'agent une question qui déclenche `mb_schematic_graph(query="simulate", failures=...)`. Attendu : widget « Simulateur » placeholder dans le chat. Clic « voir en détail » → bascule sur la section `#schematic`.

- [ ] **Step 3 — Commit**

```bash
git add web/js/llm.js
git commit -m "feat(web): inline schematic widget — placeholder card with detail-view link (MVP)" -- web/js/llm.js
```

---

## Phase 7 — Polish + filets de sécurité

### Task 7.1 — Réécriture des copy strings prioritaires

**Files :**
- Modify: `web/index.html`
- Modify: `web/js/llm.js` (uniquement strings affichées)

- [ ] **Step 1 — Rebaptise les sections clés en mode guidé**

Dans `web/index.html`, modifie les libellés des `data-section` quand ils apparaissent comme texte visible. Exemples (ne pas changer les `data-section` value, juste les labels affichés) :

| Cherche | Remplace par |
|---|---|
| `>Bibliothèque<` (s'il existe) | `>Mes diagnostics<` |
| `>Graphe de connaissances<` | `>Ce que je sais de cet appareil<` |
| `>Memory Bank<` | `>Fiche appareil<` |

Utilise `grep -rn "Bibliothèque\|Memory Bank\|Graphe de connaissances" web/` pour les localiser. Modifie uniquement en mode guidé (les versions en mode expert restent intactes — tu peux ajouter une class `.guided-only` / `.expert-only` au besoin).

- [ ] **Step 2 — Adoucis les libellés tier**

Dans `web/js/llm.js`, repère où les libellés `deep / normal / fast` sont rendus à l'utilisateur (pas dans les commits, pas dans les console.log). Ajoute une fonction de traduction utilisée dans le rendu uniquement :

```javascript
function tierLabelHuman(tier) {
  return ({ deep: "approfondie", normal: "normale", fast: "rapide" })[tier] || tier;
}
```

Et utilise-la là où `tier.toUpperCase()` est rendu dans `renderConvItems()` pour la variante guidée.

- [ ] **Step 3 — Manual check**

Vérifie visuellement le mode guidé : les strings techniques sont remplacées. Mode expert : strings inchangées. **VALIDATION ALEXIS.**

- [ ] **Step 4 — Commit**

```bash
git add web/index.html web/js/llm.js
git commit -m "feat(web): humanize copy strings in guided mode (Mes diagnostics, Fiche appareil, etc.)" -- web/index.html web/js/llm.js
```

---

### Task 7.2 — Tests end-to-end manuels (3 dry runs)

**Files :** aucun.

- [ ] **Step 1 — Cold start, démo path**

```bash
make test                           # doit toujours être au vert (940 passed)
make run
```

Ouvre `http://localhost:8000` dans un navigateur en mode privé.

**Run #1 — chemin heureux :**
1. Landing visible → tape « MNT Reform — pas de boot »
2. Submit. Attendu : redirection vers `?repair=<rid>`, workspace s'ouvre en mode guidé.
3. Pose : « écran noir, pas de musique »
4. Vérifie : agent répond, tool calls déclenchent widgets inline.
5. Clique « voir en détail » sur un widget board : bascule detail-view, retour chat OK.
6. Clique ⚙ : bascule mode expert, le rail apparaît, tout l'UI précédent revient.
7. Clique ⚙ : retour mode guidé, rien ne casse.

**Run #2 — fallback classifier :**
1. Tape un truc absurde (« blabla appareil 1234 »)
2. Submit. Attendu : status d'erreur + fallback picker avec les packs sur disque.
3. Clique sur MNT Reform dans le picker → workspace s'ouvre.

**Run #3 — switch repair :**
1. Sidebar « Mes diagnostics » : clique sur un autre repair.
2. Vérifie : le workspace switche, la conv courante change.

- [ ] **Step 2 — Note les bugs, fixe-les avant Task 7.3**

S'il y a des régressions, ouvre des sous-tâches temporaires et fixe avant de continuer.

- [ ] **Step 3 — Commit éventuel**

(Pas de commit si tout passe — sinon un commit `fix(web): ...` par bug.)

---

### Task 7.3 — Stop evolve, tag de freeze, plan B Managed Agents

**Files :**
- Modify: `Makefile` (ajout cible)

- [ ] **Step 1 — Stop evolve loop**

Si la loop tourne :
```bash
pgrep -af microsolder-evolve
# pkill -f microsolder-evolve  # uniquement si la loop est encore active
```

- [ ] **Step 2 — Tag stable**

```bash
git tag pre-demo-2026-04-26 -m "Stable freeze before final demo run"
```

(Ne **pas** push sans accord d'Alexis — règle CLAUDE.md.)

- [ ] **Step 3 — Ajoute la cible `make demo-fallback`**

Dans `Makefile`, ajoute :

```makefile
.PHONY: demo-fallback
demo-fallback:
	@echo "Switching to direct (non-MA) diagnostic mode and restarting uvicorn"
	@DIAGNOSTIC_MODE=direct .venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000
```

- [ ] **Step 4 — Commit**

```bash
git add Makefile
git commit -m "chore(make): demo-fallback target for direct diagnostic mode (MA outage plan B)" -- Makefile
```

---

### Task 7.4 — `make pin-cdn` (filet local CDN)

**Files :**
- Create: `scripts/pin_cdn.sh`
- Create: `web/vendor/.gitkeep`
- Modify: `Makefile`

- [ ] **Step 1 — Crée le script de pinning**

Crée `scripts/pin_cdn.sh` :

```bash
#!/usr/bin/env bash
# Download CDN dependencies into web/vendor/ for offline demo fallback.
# Usage: bash scripts/pin_cdn.sh
set -euo pipefail

VENDOR=web/vendor
mkdir -p "$VENDOR"

# D3 v7
curl -sSL https://d3js.org/d3.v7.min.js -o "$VENDOR/d3.v7.min.js"
# marked
curl -sSL https://cdn.jsdelivr.net/npm/marked/marked.min.js -o "$VENDOR/marked.min.js"
# DOMPurify
curl -sSL https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js -o "$VENDOR/purify.min.js"

echo "✓ CDN pinned to $VENDOR"
echo "To use: replace CDN URLs in web/index.html with /vendor/<filename>"
```

```bash
chmod +x scripts/pin_cdn.sh
touch web/vendor/.gitkeep
```

- [ ] **Step 2 — Ajoute la cible Make**

Dans `Makefile`, append :

```makefile
.PHONY: pin-cdn
pin-cdn:
	bash scripts/pin_cdn.sh
```

- [ ] **Step 3 — Run le script localement (vérification)**

```bash
make pin-cdn
ls -la web/vendor/
```

Attendu : 3 fichiers `.js` téléchargés.

- [ ] **Step 4 — Commit**

```bash
git add scripts/pin_cdn.sh web/vendor/.gitkeep Makefile
git commit -m "chore(web): pin-cdn script for offline demo fallback (D3, marked, DOMPurify)" -- scripts/pin_cdn.sh web/vendor/.gitkeep Makefile
```

(Les fichiers téléchargés `web/vendor/*.js` sont **gitignored** — ne pas commiter, ils se reproduisent à la demande.)

---

## Phase 8 — Stretch (si reste du temps)

### Task 8.1 — Mini-schematic widget réel (au lieu du placeholder)

**Files :**
- Modify: `web/js/schematic.js` (extraction d'API publique)
- Modify: `web/js/llm.js`

**Risque :** `web/js/schematic.js` fait 3 520 lignes. Extraction d'une API publique sans casser le mode expert demande un soin extrême. Si après 1h tu n'as pas trouvé un point d'extension propre, **abandonne** et garde le placeholder.

- [ ] **Step 1 — Identifie un point d'extension**

```bash
grep -n "export\|window\.\|class \w" /home/alex/Documents/hackathon-microsolder/web/js/schematic.js | head -20
```

Cherche une classe ou une fonction qui prend un container DOM et un payload (timeline, observations) et qui rend.

- [ ] **Step 2 — Si trouvable : expose `window.SchematicMini.render(container, payload)`**

Sinon, garde le placeholder et passe à autre chose.

- [ ] **Step 3 — Modifie `insertSchematicMiniOrPlaceholder()` pour utiliser l'API si dispo**

```javascript
function insertSchematicMiniOrPlaceholder(input) {
  const query = (input && input.query) || "info";
  const titleMap = { simulate: "Simulateur", hypothesize: "Hypothèses" };
  const title = titleMap[query] || "Schéma";

  if (window.SchematicMini && typeof window.SchematicMini.render === "function") {
    appendChatWidget({
      title, kind: "schematic",
      builder: (container) => window.SchematicMini.render(container, input),
      onDetail: () => enterDetailView("schematic"),
    });
    return;
  }
  // ... existing placeholder code ...
}
```

- [ ] **Step 4 — Commit (uniquement si l'extraction a réussi sans casser le mode expert)**

```bash
git add web/js/schematic.js web/js/llm.js
git commit -m "feat(web): mini-schematic widget via window.SchematicMini.render API" -- web/js/schematic.js web/js/llm.js
```

---

### Task 8.2 — Confirmation widget pour low-confidence (intent flow)

**Files :**
- Modify: `web/js/llm.js`

**Quand ?** Si `?confirm_intent=1` est présent dans l'URL et que `candidates` JSON est passé via localStorage par la landing.

- [ ] **Step 1 — Stockage temporaire dans `landing.js`**

Dans `openWorkspaceForSlug()` quand `opts.needsConfirm` est true :

```javascript
if (opts.needsConfirm && opts.candidates) {
  sessionStorage.setItem("microsolder.intent_candidates", JSON.stringify(opts.candidates));
}
```

- [ ] **Step 2 — Lecture dans `llm.js` au boot WS**

À l'ouverture de la conversation, si `?confirm_intent=1` est présent :

```javascript
function maybePostIntentConfirmWidget() {
  const params = new URLSearchParams(location.search);
  if (params.get("confirm_intent") !== "1") return;
  const raw = sessionStorage.getItem("microsolder.intent_candidates");
  if (!raw) return;
  let candidates;
  try { candidates = JSON.parse(raw); } catch { return; }
  sessionStorage.removeItem("microsolder.intent_candidates");

  appendChatWidget({
    title: "Confirme l'appareil",
    kind: "placeholder",
    builder: (container) => {
      container.innerHTML = "<div>L'agent n'est pas certain. Choisis :</div>";
      const list = document.createElement("div");
      list.style.display = "flex";
      list.style.flexDirection = "column";
      list.style.gap = "6px";
      list.style.marginTop = "8px";
      candidates.forEach(c => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "g-conv-item-btn";
        b.textContent = `${c.label}  (${Math.round(c.confidence * 100)}%)`;
        b.addEventListener("click", () => {
          // Tell the agent: "user confirmed slug X"
          if (typeof sendUserMessage === "function") {
            sendUserMessage(`Je confirme : c'est ${c.label}.`);
          }
          // Remove the widget
          container.closest(".chat-widget")?.remove();
        });
        list.appendChild(b);
      });
      container.appendChild(list);
    },
  });

  // Clean URL
  const url = new URL(location.href);
  url.searchParams.delete("confirm_intent");
  history.replaceState({}, "", url.toString());
}
```

Appelle `maybePostIntentConfirmWidget()` après le premier `session_ready` (i.e. après que la conversation a été initialisée).

- [ ] **Step 3 — Manual check**

Tape une intent ambiguë sur la landing → workspace s'ouvre → l'agent montre un widget de confirmation cliquable.

- [ ] **Step 4 — Commit**

```bash
git add web/js/llm.js web/js/landing.js
git commit -m "feat(web): low-confidence intent confirmation widget in chat (stretch)" -- web/js/llm.js web/js/landing.js
```

---

## Self-review du plan

- **Spec coverage** :
  - §1 contexte/objectif : couvert par l'ensemble des phases.
  - §3.2 surfaces touchées : checklist de fichiers en haut + chaque tâche pointe sur un fichier précis.
  - §3.3 surfaces non touchées : énumérées explicitement, jamais référencées dans les tâches.
  - §4.1 landing : Phase 3 (3.1 HTML, 3.2 CSS, 3.3 JS, 3.4 classifier).
  - §4.2 workspace : Phase 4 (4.1 HTML, 4.2 CSS, 4.3 toggle).
  - §4.3 widgets inline : Phase 6.
  - §4.4 toggle expert : Task 4.3.
  - §4.5 workbench-detail : Task 6.1 (`enterDetailView()`).
  - §5 endpoint classifier : Phase 1.
  - §6 stratégie copie : Task 7.1.
  - §7 scénario démo : Task 7.2 dry-runs.
  - §9 risques : Task 6.1 fallback boardview, Task 6.2 placeholder schématique, Task 7.3 stop-evolve, Task 7.4 pin-cdn.
- **Placeholder scan** : pas de TBD/TODO. Les fallbacks sont explicitement codés ("if `window.Boardview.embed` not present → placeholder").
- **Type consistency** : `IntentCandidate` / `IntentClassification` cohérents Task 1.1 → 1.2 → 1.3. `enterDetailView(section)` accepte la même clé `pcb` / `schematic` / `graphe` / `memory` partout.
- **Frontières domaines** : commits backend (`api/`) et frontend (`web/`) jamais bundlés (CLAUDE.md rule).

Pas de gap détecté. Plan prêt à exécution.

---

## Estimation

| Phase | Estimation | Cumul |
|---|---|---|
| 1 — Backend classifier | 2h | 2h |
| 2 — Mode foundation | 0h30 | 2h30 |
| 3 — Landing hero | 3h | 5h30 |
| 4 — Workspace shell | 3h | 8h30 |
| 5 — Sidebar repairs+conv | 2h | 10h30 |
| 6 — Inline widgets | 2h | 12h30 |
| 7 — Polish + filets | 2h | 14h30 |
| 8 — Stretch (si temps) | 3h | 17h30 |

À T-36h le 25/04 au matin, le critique s'arrête à la Phase 7 (~14h30). Phase 8 si du temps reste.
