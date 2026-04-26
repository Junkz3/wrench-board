# Agent Chat Panel Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the diagnostic agent chat panel as a turn-block timeline with typed vertical rails (MB / BV / thinking), paraphrased expandable tool-call lines, markdown-rendered agent messages with clickable refdes + net chips, typewriter caret on streaming, and polished chrome (two-line header, tier popover chip, textarea auto-grow, unified cost with delta).

**Architecture:** Pure frontend refactor. The WebSocket protocol (`api/agent/runtime_{managed,direct}.py`) is unchanged. The panel's state machine shifts from "append each event as a flat log row" to "group consecutive agent events (thinking + tool_use + message) under a turn-block." Markdown rendering added via CDN libraries. Chip interactivity piggy-backs on the existing `window.Boardview` public API, extended with four small helpers.

**Tech Stack:** Vanilla HTML/CSS/JS (no build step), D3.js v7 (pre-existing, untouched), **new:** marked.js 11 (MIT) + DOMPurify 3 (Apache 2.0) via CDN.

**Spec:** `docs/superpowers/specs/2026-04-23-agent-chat-panel-design.md` (commit `1407a79`).

**Design system contract:** All new CSS must use tokens from `web/styles/tokens.css` — `--bg`, `--bg-2`, `--panel`, `--panel-2`, `--text`, `--text-2`, `--text-3`, `--border`, `--border-soft`, `--amber`, `--cyan`, `--emerald`, `--violet`, `--mono`. Never hard-code hex for semantic meaning.

---

## Task 1: CDN dependencies + Boardview public-API helpers

Lay the plumbing that later tasks depend on: markdown libraries and four new helpers on `window.Boardview` for chip-click interactions.

**Files:**
- Modify: `web/index.html` (add two `<script>` tags in `<head>`)
- Modify: `web/brd_viewer.js` (extend the `window.Boardview` object around line 1333)

- [ ] **Step 1: Add marked.js + DOMPurify CDN scripts to `web/index.html`**

Open `web/index.html`. Find the `<head>` block that ends around line 20 (`<link rel="stylesheet" href="styles/llm.css">`). Add two lines **just before** `</head>`:

```html
<!-- Markdown rendering + sanitization for agent chat (MIT / Apache 2.0) -->
<script src="https://cdn.jsdelivr.net/npm/marked@11.1.1/marked.min.js" defer></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.0.8/dist/purify.min.js" defer></script>
```

Use `defer` so they don't block the initial render and are guaranteed to be available when `js/main.js` (also `defer`-loaded) executes.

- [ ] **Step 2: Verify libraries load in the browser**

Run: `make run` in one terminal.

Open `http://localhost:8000` in the browser, open DevTools console, type:

```js
typeof marked
typeof DOMPurify
```

Both should return `"object"`. If either is `undefined`, check Network tab for a blocked / 404 request.

- [ ] **Step 3: Add hasBoard / hasRefdes / hasNet / focusRefdes / highlightNet helpers to `window.Boardview`**

Open `web/brd_viewer.js`. Find the `window.Boardview = { … }` block around line 1333. After the last convenience method (`layer_visibility: _applyLayerVisibility,`), insert these helpers **inside** the same object literal:

```js
    // Lookups used by the chat panel to decide whether a refdes/net in
    // agent text should be rendered as a clickable chip. No-op when no
    // board is loaded. Case-sensitive match — the board parser preserves
    // original casing, and agent text tends to cite the canonical form.
    hasBoard() { return !!state.board; },
    hasRefdes(refdes) {
      return !!(state.partByRefdes && state.partByRefdes.get(String(refdes).trim()));
    },
    hasNet(name) {
      return !!(state.pinsByNet && state.pinsByNet.has(String(name).trim()));
    },

    // Chip-compatible focus: the existing `focus` ({refdes, bbox, zoom})
    // needs the caller to supply a bbox (backend-only info in the event
    // envelope). The frontend has the bbox locally in `partBodyBboxes`,
    // so this wrapper resolves it from the loaded board and delegates.
    focusRefdes(refdes) {
      const r = String(refdes).trim();
      if (!state.partByRefdes || !state.partByRefdes.get(r)) return;
      const bb = (state.partBodyBboxes && state.partBodyBboxes.get(r))
                 || state.partByRefdes.get(r).bbox;
      _applyFocus({ refdes: r, bbox: bb, zoom: 2.5 });
    },

    // Chip-compatible net highlight. The existing `highlight_net` already
    // takes {net}; this is a named alias for readability at the call site.
    highlightNet(name) {
      _applyHighlightNet({ net: String(name).trim() });
    },
```

- [ ] **Step 4: Manual browser verification of the helpers**

Reload the page. Navigate to a repair with a known board loaded (check Home section, pick any card with a board). Once the board renders, open DevTools console:

```js
window.Boardview.hasBoard()        // true
window.Boardview.hasRefdes("U12")  // true if U12 exists in that board
window.Boardview.hasRefdes("ZZ99") // false
window.Boardview.focusRefdes("U12") // board pans + highlights U12
window.Boardview.highlightNet("GND") // net lights up (if present)
```

Visually confirm that `focusRefdes` produces the same result as a backend-driven `boardview.focus` event.

- [ ] **Step 5: Commit**

```bash
git add web/index.html web/brd_viewer.js
git commit -m "$(cat <<'EOF'
feat(web): CDN deps + Boardview lookup helpers for chat panel chips

Adds marked@11 + DOMPurify@3 (MIT / Apache 2.0) via CDN to support
markdown rendering in the diagnostic agent chat. Extends
window.Boardview with hasBoard/hasRefdes/hasNet/focusRefdes/highlightNet
so the chat panel can cheaply test candidates and dispatch clicks
without synthesizing WS events locally.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- web/index.html web/brd_viewer.js
```

---

## Task 2: Turn-block core state machine + rail CSS

Replace the flat log (one row per event) with grouped turn-blocks. This is the largest task — it refactors the WebSocket event handler in `web/js/llm.js` and rewrites the `.llm-log` section of `web/styles/llm.css`.

**Files:**
- Modify: `web/js/llm.js` (state machine, new helpers around existing `logRow`/`logMessage`/`logToolUse`)
- Modify: `web/styles/llm.css` (new `.turn*` and `.step*` rules, replace `.msg.assistant` styling)

- [ ] **Step 1: Add CSS for `.turn`, `.turn-rail`, `.step`, `.turn-message`, `.turn-foot`**

Open `web/styles/llm.css`. At the end of the file (after the last rule), append:

```css
/* ============ Turn-block — grouping for agent narrative ============ */
/* A .turn wraps one agent reasoning cycle: [thinking?][tool_use*][message].
   The left vertical rail carries the chronology; nodes are typed by color
   (cyan = MB read, violet = BV action, grey = thinking). */

.turn {
  display: grid;
  grid-template-columns: 1fr;
  padding: 2px 0 10px;
}
.turn + .turn,
.turn + .msg,
.msg + .turn {
  border-top: 1px dashed var(--border-soft);
  margin-top: 8px;
  padding-top: 12px;
}

.turn-rail {
  border-left: 1px solid rgba(192,132,252,.35);
  margin-left: 4px;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
}
/* Collapse if no steps were added (pure message tour). */
.turn-rail:empty { display: none; }

.turn-message {
  padding-left: 22px;
  margin-top: 8px;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--text);
  overflow-wrap: anywhere;
}
.turn-message > :first-child { margin-top: 0; }
.turn-message > :last-child  { margin-bottom: 0; }
.turn-message p { margin: 6px 0; }
.turn-message ul, .turn-message ol { margin: 6px 0; padding-left: 22px; }
.turn-message li { margin: 2px 0; }
.turn-message code {
  font-family: var(--mono); font-size: 11px;
  background: rgba(148,163,184,.08);
  padding: 1px 4px; border-radius: 3px;
  color: var(--text-2);
}
.turn-message strong { font-weight: 600; color: var(--text); }
.turn-message em { font-style: italic; color: var(--text-2); }

.turn-foot {
  padding-left: 22px;
  margin-top: 6px;
  font-family: var(--mono);
  font-size: 10px;
  color: var(--text-3);
  display: flex;
  gap: 8px;
  letter-spacing: .3px;
}
.turn-foot .foot-sep { opacity: .5 }

/* ----- Steps inside the rail ----- */
.step {
  position: relative;
  padding: 5px 0;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11.5px;
  color: var(--text-2);
  min-height: 20px;
}
.step .node {
  position: absolute;
  left: -22px;
  top: 10px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-3);
}
.step.thinking {
  font-style: italic;
  color: var(--text-3);
}
.step.thinking .node { background: var(--text-3); }
.step.mb .node {
  background: var(--cyan);
  box-shadow: 0 0 0 2px rgba(56,189,248,.15);
}
.step.bv .node {
  background: var(--violet);
  box-shadow: 0 0 0 2px rgba(192,132,252,.15);
}
.step .step-phrase {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.step .refdes, .step .net {
  font-family: var(--mono);
  font-size: 10.5px;
}
.step.mb .refdes, .step.mb .net { color: var(--cyan); }
.step.bv .refdes, .step.bv .net { color: var(--violet); }

/* Turns inherited in replay mode — whole log dims, not per-row. */
.llm-log.replay .turn,
.llm-log.replay .msg { opacity: .55; }
```

**Then** find the existing `.llm-log .msg.assistant` rule (around line 96 of `web/styles/llm.css`) and change it so the assistant row used by the legacy flat path stops fighting the turn-block — the cleanest approach is to **remove** the legacy `.msg.assistant` and `.msg.assistant .role` rules entirely. All assistant rendering now flows through `.turn-message`.

Delete these two lines from the existing file:

```css
.llm-log .msg.assistant{background:var(--panel);border-left:2px solid var(--border);color:var(--text)}
.llm-log .msg.assistant .role{color:var(--text-2)}
```

Also delete the `.llm-log .tool` and `.llm-log .tool *` rules (lines 104-111) — the rail's `.step.*` rules replace them:

```css
.llm-log .tool{
  font-family:var(--mono);font-size:10.5px;
  padding:4px 10px;color:var(--text-3);
  display:flex;align-items:center;gap:8px;
}
.llm-log .tool .arrow{color:var(--violet)}
.llm-log .tool .name{color:var(--violet);font-weight:500}
.llm-log .tool .args{color:var(--text-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:220px}
```

The `.msg.user`, `.msg.replay`, `.sys`, and `.cost-chip` rules stay for now.

- [ ] **Step 2: Add turn-block state machine to `web/js/llm.js`**

Open `web/js/llm.js`. At the top, near the other module-level `let`s (after line 22 `let sessionTurns = 0;`), add:

```js
// Turn-block state machine.
// currentTurn is the DOM node receiving the next incoming thinking / tool_use /
// message event. A user.message closes it (set to null). An assistant.message
// that arrives when currentTurn already has a .turn-message opens a new turn
// (agent emitted two messages back-to-back without a user interjection).
let currentTurn = null;
```

Still near the top, add these helpers (place them right after the `escapeHTML` function, around line 106):

```js
// Create a fresh turn-block container and append it to the log.
function createTurn() {
  const log = el("llmLog");
  const turn = document.createElement("div");
  turn.className = "turn";
  const rail = document.createElement("div");
  rail.className = "turn-rail";
  turn.appendChild(rail);
  log.appendChild(turn);
  log.scrollTop = log.scrollHeight;
  return turn;
}

function ensureTurn() {
  if (!currentTurn) currentTurn = createTurn();
  return currentTurn;
}

function closeTurn() {
  currentTurn = null;
}

// Append a .step into the turn's rail. kind ∈ {"thinking","mb","bv"}.
// phraseHTML is trusted HTML (callers escape user-provided fragments
// themselves — currently only tool names + refdes which are validated).
function appendStep(turn, kind, phraseHTML) {
  const rail = turn.querySelector(".turn-rail");
  const step = document.createElement("div");
  step.className = `step ${kind}`;
  step.innerHTML = `<span class="node"></span><span class="step-phrase">${phraseHTML}</span>`;
  rail.appendChild(step);
  el("llmLog").scrollTop = el("llmLog").scrollHeight;
  return step;
}

// Append the assistant text into the current turn. Plain textContent for
// now — Task 4 replaces this with markdown + chip rendering.
function appendTurnMessage(turn, text) {
  let msg = turn.querySelector(".turn-message");
  if (msg) {
    // An assistant message already landed in this turn — open a new one.
    closeTurn();
    turn = ensureTurn();
    msg = null;
  }
  msg = document.createElement("div");
  msg.className = "turn-message";
  msg.textContent = text ?? "";
  turn.appendChild(msg);
  el("llmLog").scrollTop = el("llmLog").scrollHeight;
  return msg;
}

function appendTurnFoot(turn, payload) {
  let foot = turn.querySelector(".turn-foot");
  if (!foot) {
    foot = document.createElement("div");
    foot.className = "turn-foot";
    turn.appendChild(foot);
  }
  const priceLabel = payload.priced ? fmtUsd(payload.cost_usd) : "—";
  const modelLabel = payload.model ? payload.model.replace("claude-", "") : "?";
  const tokensLabel = `${(payload.input_tokens || 0) + (payload.cache_read_input_tokens || 0) + (payload.cache_creation_input_tokens || 0)}→${payload.output_tokens || 0} tok`;
  foot.innerHTML =
    `<span class="foot-cost">${priceLabel}</span>` +
    `<span class="foot-sep">·</span>` +
    `<span class="foot-tokens">${tokensLabel}</span>` +
    `<span class="foot-sep">·</span>` +
    `<span class="foot-model">${escapeHTML(modelLabel)}</span>`;
}
```

- [ ] **Step 3: Route WS events through the turn-block state machine**

Still in `web/js/llm.js`, find the `ws.addEventListener("message", …)` handler (around line 181). Replace the `switch (payload.type)` block so that:
- `"message"` with role=`"user"` closes the turn AND calls `logMessage` (standalone user row — left cyan-border as before).
- `"message"` with role=`"assistant"` routes to `appendTurnMessage`.
- `"tool_use"` routes to `appendStep` with kind `"mb"` or `"bv"` based on the tool name prefix.
- `"thinking"` routes to `appendStep` with kind `"thinking"`.
- `"turn_cost"` calls `appendTurnFoot`.
- everything else keeps `logSys`.

Replace the whole switch with:

```js
    switch (payload.type) {
      case "session_ready": {
        const model = payload.model || "claude";
        const mode = payload.mode || "managed";
        el("llmModel").textContent = `${model} · ${mode}`;
        const rid = payload.repair_id ? ` · repair ${payload.repair_id.slice(0, 8)}` : "";
        logSys(`session prête — ${mode} · ${model}${rid}`);
        break;
      }
      case "history_replay_start":
        el("llmLog").classList.add("replay");
        logSys(`replay · ${payload.count} events précédents`);
        break;
      case "history_replay_end":
        el("llmLog").classList.remove("replay");
        logSys("replay terminé — reprends où tu t'étais arrêté");
        closeTurn();
        break;
      case "context_loaded":
        logSys("contexte device + symptôme chargé · l'agent attend ton premier message");
        break;
      case "session_resumed":
        logSys("session reprise · historique et mémoire agent restaurés");
        break;
      case "message":
        if ((payload.role || "assistant") === "user") {
          closeTurn();
          logMessage("user", payload.text || "", payload.replay === true);
        } else {
          const turn = ensureTurn();
          appendTurnMessage(turn, payload.text || "");
        }
        break;
      case "tool_use": {
        const turn = ensureTurn();
        const name = payload.name || "?";
        const kind = name.startsWith("bv_") ? "bv" :
                     name.startsWith("mb_") ? "mb" : "mb";
        const argsStr = safeJSON(payload.input);
        appendStep(turn, kind,
          `<span class="tool-name">${escapeHTML(name)}</span> ` +
          `<span class="tool-args">${escapeHTML(argsStr)}</span>`);
        break;
      }
      case "thinking": {
        const turn = ensureTurn();
        appendStep(turn, "thinking", escapeHTML(payload.text || "…"));
        break;
      }
      case "turn_cost":
        sessionCostUsd += Number(payload.cost_usd || 0);
        sessionTurns += 1;
        updateCostTotal();
        if (currentTurn) appendTurnFoot(currentTurn, payload);
        break;
      case "error":
        logSys(`erreur : ${payload.text}`, true);
        break;
      case "session_terminated":
        logSys("session terminée", true);
        closeTurn();
        break;
      default:
        logSys(`? ${JSON.stringify(payload)}`);
    }
```

Also add this small helper next to `escapeHTML`:

```js
function safeJSON(v) {
  try { return JSON.stringify(v ?? {}); } catch { return String(v); }
}
```

And reset `currentTurn` on every new `connect()`. In the `connect()` function, right after `sessionTurns = 0;` (around line 153), add:

```js
  currentTurn = null;
```

- [ ] **Step 4: Remove the now-dead `logToolUse` and `attachCostChipToLastAssistant` call paths**

The `logToolUse` function (lines 85-93) and the `attachCostChipToLastAssistant(payload)` call inside the `turn_cost` case are no longer reached. Remove both:

- Delete the `logToolUse` function body entirely.
- `attachCostChipToLastAssistant` is already bypassed by the new `turn_cost` case (we call `appendTurnFoot`). Leave its definition in place for one more task cycle — Task 6 deletes it along with the cost-chip strip migration.

- [ ] **Step 5: Manual browser verification**

Restart `make run`. Open a repair in the browser, send a message like `donne-moi le composant U12`.

Expected:
- Your user message appears as a cyan-bordered `.msg.user` row, standalone.
- A `.turn` appears below, with a vertical violet-tinted rail on its left.
- Inside the rail: one or more `.step` dots — likely a `.step.mb` (cyan node) for `mb_get_component`.
- Below the rail: a `.turn-message` block with the plain-text agent reply (markdown comes in Task 4).
- Below that: a `.turn-foot` with `$0.00x · N→M tok · haiku-4-5-20251001` (or whichever tier).

Pipe through at least one BV tool call (ask the agent to highlight a component). Confirm the `.step.bv` node is violet.

Send a **second** user message. Confirm a fresh `.turn` appears below, with its own rail. The previous turn stays intact above.

Open a **repair with history** (pick an existing repair from Home). Confirm `.llm-log.replay` dims all prior turns to ~55%, but the structure matches live turns.

Re-send a message post-replay. Confirm the new live turn renders at full opacity (replay class must have been removed).

- [ ] **Step 6: Commit**

```bash
git add web/js/llm.js web/styles/llm.css
git commit -m "$(cat <<'EOF'
refactor(web/llm): group agent events into turn-blocks with typed rail

Replaces the flat chronological log with a turn-block state machine:
each cycle of thinking + tool_use* + assistant message is grouped in a
.turn container with a vertical rail on the left. Nodes on the rail
are color-typed — cyan for mb_* reads, violet for bv_* actions, grey
for thinking events. The turn foot replaces the per-message cost chip.

No protocol change; this is a frontend-only rendering shift.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- web/js/llm.js web/styles/llm.css
```

---

## Task 3: Paraphrased tool-call lines with expand

Replace the raw `tool_name {args}` rendering with a French paraphrase, a family icon (eye for MB, target for BV), and a chevron to expand the full JSON payload.

**Files:**
- Modify: `web/js/llm.js` (add `TOOL_PHRASES`, rewire `appendStep` for tool_use)
- Modify: `web/styles/llm.css` (icon + chevron + `.step-payload` styles)

- [ ] **Step 1: Add `TOOL_PHRASES` table and icon SVGs**

In `web/js/llm.js`, near the top (after `let currentTurn = null;`), add:

```js
// Family icons for tool-call steps. 12×12, stroke currentColor, per CLAUDE.md §icons.
const ICON_MB =
  '<svg class="step-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" ' +
  'aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/>' +
  '<circle cx="12" cy="12" r="3"/></svg>';
const ICON_BV =
  '<svg class="step-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" ' +
  'aria-hidden="true"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/>' +
  '<circle cx="12" cy="12" r="1.2" fill="currentColor"/></svg>';

// French paraphrase + family icon for each known tool name. Each entry is a
// function receiving the tool input object and returning {icon, phraseHTML}.
// phraseHTML can embed a <span class="refdes"> or <span class="net"> for
// typographic emphasis on the target; escape all inputs via escapeHTML.
const TOOL_PHRASES = {
  // --- MB (memory bank — perception / reading) ---
  mb_get_component: (i) => ({
    icon: ICON_MB,
    phraseHTML: `Consultation de <span class="refdes">${escapeHTML(i?.refdes || "?")}</span>`,
  }),
  mb_get_rules_for_symptoms: (i) => {
    const syms = Array.isArray(i?.symptoms) ? i.symptoms.join(", ") : (i?.symptoms || "");
    return {
      icon: ICON_MB,
      phraseHTML: `Lecture des règles pour « ${escapeHTML(syms)} »`,
    };
  },
  mb_list_findings: (i) => ({
    icon: ICON_MB,
    phraseHTML: `Revue des findings${i?.device ? ` pour <span class="refdes">${escapeHTML(i.device)}</span>` : ""}`,
  }),
  mb_record_finding: () => ({
    icon: ICON_MB,
    phraseHTML: `Enregistrement d'un finding`,
  }),
  mb_expand_knowledge: (i) => {
    const scope = [i?.component, i?.symptom].filter(Boolean).join(" / ");
    return {
      icon: ICON_MB,
      phraseHTML: `Extension du pack${scope ? ` — ${escapeHTML(scope)}` : ""}`,
    };
  },
  mb_schematic_graph: () => ({
    icon: ICON_MB,
    phraseHTML: `Lecture du graphe schématique`,
  }),

  // --- BV (boardview — action) ---
  bv_highlight_component: (i) => ({
    icon: ICON_BV,
    phraseHTML: `Mise en évidence de <span class="refdes">${escapeHTML(i?.refdes || "?")}</span>`,
  }),
  bv_focus_component: (i) => ({
    icon: ICON_BV,
    phraseHTML: `Focus sur <span class="refdes">${escapeHTML(i?.refdes || "?")}</span>`,
  }),
  bv_reset_view: () => ({
    icon: ICON_BV,
    phraseHTML: `Réinitialisation de la vue`,
  }),
  bv_highlight_net: (i) => ({
    icon: ICON_BV,
    phraseHTML: `Highlight du net <span class="net">${escapeHTML(i?.net || "?")}</span>`,
  }),
  bv_flip_board: () => ({
    icon: ICON_BV,
    phraseHTML: `Retournement du board`,
  }),
  bv_annotate: (i) => {
    const tgt = i?.refdes ? `près de <span class="refdes">${escapeHTML(i.refdes)}</span>` :
                (Number.isFinite(i?.x) && Number.isFinite(i?.y) ? `en (${i.x}, ${i.y})` : "");
    return {
      icon: ICON_BV,
      phraseHTML: `Annotation ${tgt}`,
    };
  },
  bv_filter_by_type: (i) => ({
    icon: ICON_BV,
    phraseHTML: `Filtrage par type — ${escapeHTML(i?.type || "?")}`,
  }),
  bv_draw_arrow: (i) => ({
    icon: ICON_BV,
    phraseHTML: `Flèche de <span class="refdes">${escapeHTML(i?.from || "?")}</span> ` +
                `vers <span class="refdes">${escapeHTML(i?.to || "?")}</span>`,
  }),
  bv_measure_distance: (i) => ({
    icon: ICON_BV,
    phraseHTML: `Mesure entre <span class="refdes">${escapeHTML(i?.a || "?")}</span> ` +
                `et <span class="refdes">${escapeHTML(i?.b || "?")}</span>`,
  }),
  bv_show_pin: (i) => ({
    icon: ICON_BV,
    phraseHTML: `Pin ${escapeHTML(String(i?.pin ?? "?"))} de <span class="refdes">${escapeHTML(i?.refdes || "?")}</span>`,
  }),
  bv_dim_unrelated: () => ({
    icon: ICON_BV,
    phraseHTML: `Atténuation des éléments non liés`,
  }),
  bv_layer_visibility: (i) => ({
    icon: ICON_BV,
    phraseHTML: `Visibilité de la couche ${escapeHTML(i?.layer || "?")}`,
  }),
};

// Fallback for tools without a paraphrase entry: render tool name mono gray.
function toolFallback(name) {
  return {
    icon: "",
    phraseHTML: `<span class="tool-name-raw">${escapeHTML(name)}</span>`,
  };
}
```

- [ ] **Step 2: Rewrite the `tool_use` case in the WS handler**

In `web/js/llm.js`, find the `case "tool_use":` block in the switch (added in Task 2). Replace its body with:

```js
      case "tool_use": {
        const turn = ensureTurn();
        const name = payload.name || "?";
        const kind = name.startsWith("bv_") ? "bv" :
                     name.startsWith("mb_") ? "mb" : "mb";
        const renderer = TOOL_PHRASES[name];
        const { icon, phraseHTML } = renderer ? renderer(payload.input || {}) : toolFallback(name);
        const step = appendStep(turn, kind, `${icon}${phraseHTML}`);
        // Attach the expand affordance only if we have structured args to show.
        const payloadJSON = {
          args: payload.input || {},
          ...(payload.result != null ? { result: payload.result } : {}),
        };
        addExpandToStep(step, payloadJSON);
        break;
      }
```

And add `addExpandToStep` right after `appendStep`:

```js
function addExpandToStep(step, payloadObj) {
  // Chevron button.
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "step-expand";
  btn.setAttribute("aria-expanded", "false");
  btn.title = "Voir le payload";
  btn.innerHTML =
    '<svg class="chevron" width="10" height="10" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
    '<polyline points="9 6 15 12 9 18"/></svg>';
  step.appendChild(btn);

  // Hidden payload block.
  const pre = document.createElement("pre");
  pre.className = "step-payload";
  const hasResult = payloadObj && typeof payloadObj === "object" && "result" in payloadObj;
  const body = hasResult
    ? JSON.stringify(payloadObj, null, 2)
    : JSON.stringify(payloadObj, null, 2) + "\n\n— result non rendu par le runtime";
  pre.textContent = body;
  step.appendChild(pre);

  btn.addEventListener("click", () => {
    const expanded = step.classList.toggle("expanded");
    btn.setAttribute("aria-expanded", expanded ? "true" : "false");
  });
}
```

- [ ] **Step 3: Add CSS for step icon, chevron, and expanded payload**

In `web/styles/llm.css`, right after the `.step.bv .refdes, .step.bv .net { color: var(--violet); }` rule added in Task 2, append:

```css
.step .step-icon {
  width: 12px;
  height: 12px;
  flex-shrink: 0;
}
.step.mb .step-icon { color: var(--cyan); }
.step.bv .step-icon { color: var(--violet); }

.step-expand {
  background: none;
  border: 0;
  color: var(--text-3);
  cursor: pointer;
  padding: 2px 4px;
  border-radius: 3px;
  transition: color .15s, background .15s;
  flex-shrink: 0;
}
.step-expand:hover { color: var(--text-2); background: var(--panel); }
.step-expand .chevron { transition: transform .15s; display: block; }
.step-expand[aria-expanded="true"] .chevron { transform: rotate(90deg); }

.step-payload {
  display: none;
  grid-column: 1 / -1;
  width: calc(100% - 4px);
  margin: 6px 0 2px 0;
  background: var(--panel);
  border-left: 2px solid currentColor;
  padding: 8px 10px;
  border-radius: 4px;
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--text-2);
  white-space: pre-wrap;
  overflow-x: auto;
  line-height: 1.4;
}
.step.mb .step-payload { border-left-color: var(--cyan); }
.step.bv .step-payload { border-left-color: var(--violet); }
.step.expanded .step-payload { display: block; }

/* The step is a flex row; the payload must break onto its own line. */
.step.expanded { flex-wrap: wrap; align-items: center; }

/* Fallback rendering for unknown tool names. */
.tool-name-raw {
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--text-3);
}
```

- [ ] **Step 4: Manual browser verification**

Restart `make run`. Open a repair, ask the agent to do something that triggers multiple tools (ex: `où est U12 et montre-le`).

Expected:
- Each tool step shows the family icon (eye for MB, target for BV), family color.
- The phrase is in French (ex: `Consultation de U12`, `Mise en évidence de U12`).
- The refdes is in JetBrains Mono, same family color.
- A chevron appears at the right end of each step.
- Clicking the chevron expands a JSON payload block with `args`, plus a `result` if the runtime sent it (or the `— result non rendu` note).
- Re-click collapses.
- Unknown tool names render as raw mono gray (no icon, no phrase) — you can verify by temporarily injecting `{type:"tool_use", name:"custom_unknown_tool", input:{foo:1}}` via the DevTools WS panel (optional).

- [ ] **Step 5: Commit**

```bash
git add web/js/llm.js web/styles/llm.css
git commit -m "$(cat <<'EOF'
feat(web/llm): paraphrased tool-call lines with expandable payload

Each tool_use step now renders as: family icon (eye for mb_*, target
for bv_*) + French phrase with the refdes/net in inline mono + chevron
that reveals the JSON args (and result when the runtime surfaces it).
Unknown tool names degrade to a plain mono label without error.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- web/js/llm.js web/styles/llm.css
```

---

## Task 4: Markdown + clickable refdes/net chips in agent messages

Upgrade `appendTurnMessage` to render markdown and wrap validated refdes / net tokens as clickable chips.

**Files:**
- Modify: `web/js/llm.js` (rewrite `appendTurnMessage`)
- Modify: `web/styles/llm.css` (add `.chip-refdes`, `.chip-net`, `.refdes-unknown`)

- [ ] **Step 1: Add the markdown + chip pipeline to `web/js/llm.js`**

Open `web/js/llm.js`. Find the current `appendTurnMessage` (from Task 2). Replace its body with a version that renders markdown, sanitizes, then walks text nodes to inject chips:

```js
// Regex shapes. Kept loose — the semantic filter is the Boardview lookup.
const RE_REFDES = /\b[A-Z]{1,3}\d{1,4}\b/g;
// Nets: common naming conventions used in iPhone / Mac / Pi schematics.
// Over-matches on purpose; Boardview.hasNet is the truth gate.
const RE_NET = /\b(?:PP_[A-Z0-9_]+|[PN]P_[A-Z0-9_]+|L\d{1,3}|VCC(?:_[A-Z0-9_]+)?|VDD(?:_[A-Z0-9_]+)?|AVDD(?:_[A-Z0-9_]+)?|DVDD(?:_[A-Z0-9_]+)?|GND(?:_[A-Z0-9_]+)?|[A-Z][A-Z0-9_]{3,})\b/g;
const RE_UNKNOWN_REFDES = /⟨\?([A-Z]{1,3}\d{1,4})⟩/g;

function appendTurnMessage(turn, text) {
  let msg = turn.querySelector(".turn-message");
  if (msg) {
    // Second assistant message in the same turn — open a new turn.
    closeTurn();
    turn = ensureTurn();
    msg = null;
  }
  msg = document.createElement("div");
  msg.className = "turn-message";
  renderAgentMarkup(msg, text || "");
  turn.appendChild(msg);
  el("llmLog").scrollTop = el("llmLog").scrollHeight;
  return msg;
}

// Parse markdown → sanitize → walk text nodes → replace validated tokens
// with clickable chips. If marked / DOMPurify aren't on the page, fall back
// to plain text (defensive: network hiccup loading the CDN).
function renderAgentMarkup(container, text) {
  let html;
  if (typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
    const raw = marked.parse(text, { breaks: true, gfm: true });
    html = DOMPurify.sanitize(raw, {
      ALLOWED_TAGS: ["p", "br", "strong", "em", "ul", "ol", "li", "code"],
      ALLOWED_ATTR: [],
    });
  } else {
    // Minimal fallback: just escape + <br> for newlines.
    html = escapeHTML(text).replaceAll("\n", "<br>");
  }
  container.innerHTML = html;
  decorateChipsIn(container);
}

// Walk all text nodes under `root` and replace validated refdes / net tokens
// with clickable chips, plus unknown-refdes ⟨?U999⟩ wrappers with amber span.
// Text inside <code> is skipped (we keep the agent's verbatim intent).
function decorateChipsIn(root) {
  const hasBoard = !!(window.Boardview && window.Boardview.hasBoard && window.Boardview.hasBoard());
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      if (!n.nodeValue) return NodeFilter.FILTER_REJECT;
      if (n.parentElement && n.parentElement.closest("code, .refdes-unknown, .chip-refdes, .chip-net"))
        return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const targets = [];
  while (walker.nextNode()) targets.push(walker.currentNode);
  for (const textNode of targets) decorateOneTextNode(textNode, hasBoard);
}

function decorateOneTextNode(textNode, hasBoard) {
  const original = textNode.nodeValue;
  // Collect matches across all three regexes, tag by kind, sort by offset,
  // then rebuild the node as a sequence of text + element fragments.
  const matches = [];
  for (const m of original.matchAll(RE_UNKNOWN_REFDES)) {
    matches.push({ kind: "unknown", start: m.index, end: m.index + m[0].length, raw: m[0], inner: m[1] });
  }
  for (const m of original.matchAll(RE_REFDES)) {
    if (hasBoard && window.Boardview.hasRefdes(m[0])) {
      matches.push({ kind: "refdes", start: m.index, end: m.index + m[0].length, raw: m[0] });
    }
  }
  for (const m of original.matchAll(RE_NET)) {
    if (hasBoard && window.Boardview.hasNet(m[0])) {
      matches.push({ kind: "net", start: m.index, end: m.index + m[0].length, raw: m[0] });
    }
  }
  if (matches.length === 0) return;
  // Resolve overlaps by earliest-start, longest-wins.
  matches.sort((a, b) => a.start - b.start || b.end - b.start - (a.end - a.start));
  const cleaned = [];
  let cursor = 0;
  for (const m of matches) {
    if (m.start < cursor) continue; // overlap with prior match
    cleaned.push(m);
    cursor = m.end;
  }
  // Rebuild the node.
  const frag = document.createDocumentFragment();
  let i = 0;
  for (const m of cleaned) {
    if (m.start > i) frag.appendChild(document.createTextNode(original.slice(i, m.start)));
    frag.appendChild(makeChipNode(m));
    i = m.end;
  }
  if (i < original.length) frag.appendChild(document.createTextNode(original.slice(i)));
  textNode.parentNode.replaceChild(frag, textNode);
}

function makeChipNode(match) {
  if (match.kind === "refdes") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip-refdes";
    btn.dataset.refdes = match.raw;
    btn.textContent = match.raw;
    btn.addEventListener("click", () => {
      if (window.Boardview && window.Boardview.focusRefdes) {
        window.Boardview.focusRefdes(match.raw);
      }
    });
    return btn;
  }
  if (match.kind === "net") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip-net";
    btn.dataset.net = match.raw;
    btn.textContent = match.raw;
    btn.addEventListener("click", () => {
      if (window.Boardview && window.Boardview.highlightNet) {
        window.Boardview.highlightNet(match.raw);
      }
    });
    return btn;
  }
  // unknown
  const span = document.createElement("span");
  span.className = "refdes-unknown";
  span.textContent = match.raw;
  return span;
}
```

- [ ] **Step 2: Add chip CSS**

In `web/styles/llm.css`, at the end of the file, append:

```css
/* ============ Chips in turn-message ============ */
.turn-message .chip-refdes,
.turn-message .chip-net {
  display: inline-block;
  font-family: var(--mono);
  font-size: 10.5px;
  padding: 1px 6px;
  margin: 0 1px;
  border-radius: 3px;
  cursor: pointer;
  background: transparent;
  transition: background .15s, border-color .15s;
  vertical-align: baseline;
  line-height: 1.25;
}
.turn-message .chip-refdes {
  color: var(--cyan);
  border: 1px solid rgba(56,189,248,.3);
}
.turn-message .chip-refdes:hover {
  background: rgba(56,189,248,.12);
  border-color: rgba(56,189,248,.55);
}
.turn-message .chip-net {
  color: var(--emerald);
  border: 1px solid rgba(52,211,153,.3);
}
.turn-message .chip-net:hover {
  background: rgba(52,211,153,.12);
  border-color: rgba(52,211,153,.55);
}
.turn-message .refdes-unknown {
  color: var(--amber);
  font-family: var(--mono);
  font-size: 10.5px;
  background: rgba(245,158,11,.06);
  padding: 0 4px;
  border-radius: 3px;
  border: 1px dashed rgba(245,158,11,.35);
}
```

- [ ] **Step 3: Manual browser verification**

Restart `make run`. Open a repair **with a board loaded** (e.g. `demo-pi`).

Ask the agent: `résume les rails d'alim principales et mentionne quelques composants pour exemple`.

Expected:
- Agent reply rendered with markdown (bolded words appear bold, bullet lists indent correctly).
- Inline code (backticked) shows in mono with subtle background.
- Any refdes in the reply that matches a real board part appears as a **cyan chip** — hover shows slight bg + brighter border.
- Any net name that matches (GND, PP_*, VCC_*) appears as an **emerald chip**.
- Clicking a cyan chip pans/zooms the board to that refdes (same effect as clicking it in the board's own UI).
- Clicking an emerald chip highlights that net across the board.
- If the agent produces `⟨?U999⟩` (trigger this by asking about a fictional component the sanitizer will wrap), it renders as amber with a dashed border.

Open a repair **without** a board loaded (or uncover one that isn't in `board_assets/`). Confirm chips are **not** rendered — refdes stay as plain text. `Boardview.hasBoard()` must return `false` in the console for this case.

Network resilience test: open DevTools → Network → block `cdn.jsdelivr.net`. Reload. The fallback path renders plain text (no markdown formatting, no chips) without errors.

- [ ] **Step 4: Commit**

```bash
git add web/js/llm.js web/styles/llm.css
git commit -m "$(cat <<'EOF'
feat(web/llm): markdown + clickable refdes/net chips in agent replies

Agent messages now render through marked + DOMPurify with a narrow tag
allowlist (p, br, strong, em, ul, ol, li, code). A post-pass walks the
resulting DOM and wraps validated refdes as cyan chips that call
Boardview.focusRefdes, validated nets as emerald chips that call
Boardview.highlightNet, and sanitizer-wrapped ⟨?U999⟩ as amber dashed
badges. Without a loaded board, chip substitution is skipped.

Falls back to escaped plain text when the CDN libraries fail to load.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- web/js/llm.js web/styles/llm.css
```

---

## Task 5: Live feedback — typewriter caret + pulse node

Add visible "agent is working" indicators: a blinking caret at the end of a streaming message, and a pulsing phantom node at the bottom of the rail when the agent is between tools but hasn't started a message yet.

**Files:**
- Modify: `web/styles/llm.css` (keyframes + caret/pulse rules)
- Modify: `web/js/llm.js` (inject pulse before message, swap out when message arrives)

**Note on streaming:** today the backend emits one full `message` event per agent turn (no per-token streaming on the WS). The caret in this task is a **turn-level** indicator: it appears the moment a `tool_use` lands and disappears the moment the message text arrives. When per-token streaming is added later (see backend WS work), the caret will naturally become a true typewriter cursor with no extra frontend work.

- [ ] **Step 1: Add CSS for the caret and pulse keyframes**

In `web/styles/llm.css`, append at the end:

```css
/* ============ Live feedback ============ */
.turn-message .caret {
  display: inline-block;
  width: 2px;
  height: 1em;
  margin-left: 2px;
  background: var(--violet);
  animation: caret-blink 1s step-end infinite;
  vertical-align: text-bottom;
}
@keyframes caret-blink {
  0%, 50%   { opacity: 1 }
  51%, 100% { opacity: 0 }
}

/* Phantom "agent is working" node at the bottom of the rail. */
.step.pending {
  color: var(--text-3);
  font-style: italic;
  opacity: .85;
}
.step.pending .node {
  background: var(--violet);
  box-shadow: 0 0 0 3px rgba(192,132,252,.15);
  animation: node-pulse 1.4s ease-in-out infinite;
}
@keyframes node-pulse {
  0%, 100% { box-shadow: 0 0 0 3px rgba(192,132,252,.15) }
  50%      { box-shadow: 0 0 0 6px rgba(192,132,252,.45) }
}
```

- [ ] **Step 2: Show pulse when a turn has steps but no message yet**

In `web/js/llm.js`, add two helpers near `appendStep`:

```js
function ensurePendingNode(turn) {
  const rail = turn.querySelector(".turn-rail");
  if (!rail || rail.querySelector(".step.pending")) return;
  const step = document.createElement("div");
  step.className = "step pending";
  step.innerHTML = `<span class="node"></span><span class="step-phrase">l'agent travaille…</span>`;
  rail.appendChild(step);
}

function clearPendingNode(turn) {
  const p = turn.querySelector(".step.pending");
  if (p) p.remove();
}
```

Then modify `appendStep` to clear any existing pending node at the start (so it never sits above a live step), and re-add it at the end (so it sits below the last step). Find `appendStep` and replace its body with:

```js
function appendStep(turn, kind, phraseHTML) {
  clearPendingNode(turn);
  const rail = turn.querySelector(".turn-rail");
  const step = document.createElement("div");
  step.className = `step ${kind}`;
  step.innerHTML = `<span class="node"></span><span class="step-phrase">${phraseHTML}</span>`;
  rail.appendChild(step);
  ensurePendingNode(turn);
  el("llmLog").scrollTop = el("llmLog").scrollHeight;
  return step;
}
```

Modify `appendTurnMessage` so that its **first** action is to clear the pending node, and its last action is to append the caret. Replace `appendTurnMessage` body with:

```js
function appendTurnMessage(turn, text) {
  let msg = turn.querySelector(".turn-message");
  if (msg) {
    closeTurn();
    turn = ensureTurn();
    msg = null;
  }
  clearPendingNode(turn);
  msg = document.createElement("div");
  msg.className = "turn-message";
  renderAgentMarkup(msg, text || "");
  // Caret: visible while we wait for turn_cost (or the next WS event that
  // implies the turn is done). Removed in appendTurnFoot.
  const caret = document.createElement("span");
  caret.className = "caret";
  msg.appendChild(caret);
  turn.appendChild(msg);
  el("llmLog").scrollTop = el("llmLog").scrollHeight;
  return msg;
}
```

Modify `appendTurnFoot` so it strips the caret when the cost event lands. Change the function to:

```js
function appendTurnFoot(turn, payload) {
  // Terminal signal for this turn — clear transient indicators.
  const caret = turn.querySelector(".turn-message .caret");
  if (caret) caret.remove();
  clearPendingNode(turn);
  let foot = turn.querySelector(".turn-foot");
  if (!foot) {
    foot = document.createElement("div");
    foot.className = "turn-foot";
    turn.appendChild(foot);
  }
  const priceLabel = payload.priced ? fmtUsd(payload.cost_usd) : "—";
  const modelLabel = payload.model ? payload.model.replace("claude-", "") : "?";
  const tokensLabel = `${(payload.input_tokens || 0) + (payload.cache_read_input_tokens || 0) + (payload.cache_creation_input_tokens || 0)}→${payload.output_tokens || 0} tok`;
  foot.innerHTML =
    `<span class="foot-cost">${priceLabel}</span>` +
    `<span class="foot-sep">·</span>` +
    `<span class="foot-tokens">${tokensLabel}</span>` +
    `<span class="foot-sep">·</span>` +
    `<span class="foot-model">${escapeHTML(modelLabel)}</span>`;
}
```

- [ ] **Step 3: Suppress pulse and caret during replay**

In the `history_replay_start` case, the pending pulse would be distracting if it showed on replayed turns. Add a CSS guard at the end of `web/styles/llm.css`:

```css
.llm-log.replay .step.pending,
.llm-log.replay .turn-message .caret { display: none; }
```

- [ ] **Step 4: Manual browser verification**

Restart `make run`. Open a repair, send a message that triggers several tools.

Expected sequence:
1. Send message → `.msg.user` appears.
2. Within ~half a second, a `.turn` appears with a rail.
3. A `.step.pending` appears at the bottom of the rail, with a **pulsing violet dot** and italic text "l'agent travaille…".
4. As each tool_use lands, the pending node is removed, the new step is added, then pending re-appears below it (rail grows downward, pulse always at the tail).
5. When the first message text arrives, pending disappears, and the `.turn-message` renders with a **blinking violet caret** at its end.
6. When `turn_cost` lands, the caret disappears; the `.turn-foot` renders.

Replay test: open a repair with history. Confirm no pulse and no caret on replayed turns.

- [ ] **Step 5: Commit**

```bash
git add web/js/llm.js web/styles/llm.css
git commit -m "$(cat <<'EOF'
feat(web/llm): live pulse node + typewriter caret during agent work

While tools are firing but no message text exists yet, a pulsing violet
node sits at the tail of the rail under a "l'agent travaille…" hint.
Once the message arrives the caret takes over at its end, and both
indicators clear when turn_cost terminates the turn. Disabled during
history replay.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- web/js/llm.js web/styles/llm.css
```

---

## Task 6: Chrome polish — header, tier chip + popover, cost delta, textarea

Four coordinated chrome changes. All land in a single commit (same user-visible change: "the panel shell feels denser and more intentional").

**Files:**
- Modify: `web/index.html` (header + tier + input markup)
- Modify: `web/js/llm.js` (tier popover logic, textarea auto-grow, cost delta, kill dead `attachCostChipToLastAssistant`)
- Modify: `web/styles/llm.css` (header two-line, tier chip, popover, textarea column, hide `.llm-tiers` + `.cost-chip`)

- [ ] **Step 1: Restructure the header, tier, and input markup in `web/index.html`**

Open `web/index.html`. Find the `<aside class="llm-panel" id="llmPanel" …>` block (around line 429). Replace its contents (everything from `<aside>` to `</aside>`) with:

```html
<aside class="llm-panel" id="llmPanel" aria-hidden="true" aria-label="Agent diagnostic">
  <header class="llm-head">
    <span class="type-badge action">Agent</span>
    <div class="title-col">
      <h3 id="llmTitle">Diagnostic</h3>
      <div class="llm-subline" id="llmSubline">—</div>
    </div>
    <div class="llm-head-actions">
      <button class="llm-tier-chip" id="llmTierChip" data-tier="fast" aria-haspopup="menu" aria-expanded="false" title="Tier de modèle">
        <span class="tier-label">FAST</span>
        <svg class="chevron-down" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
      </button>
      <div class="llm-tier-popover" id="llmTierPopover" role="menu" hidden>
        <button role="menuitem" data-tier="fast" class="on">Fast · Haiku 4.5</button>
        <button role="menuitem" data-tier="normal">Normal · Sonnet 4.6</button>
        <button role="menuitem" data-tier="deep">Deep · Opus 4.7</button>
      </div>
      <button class="close-x" id="llmClose" aria-label="Fermer le panel" type="button">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </div>
  </header>
  <div class="llm-status" id="llmStatus">
    <span class="dot"></span>
    <span id="llmStatusText">inactif</span>
    <span class="device-tag" id="llmDevice" style="display:none"></span>
    <span class="cost-total" id="llmCostTotal" style="display:none">$0.00</span>
  </div>
  <div class="llm-log" id="llmLog" aria-live="polite"></div>
  <form class="llm-input" id="llmForm">
    <textarea id="llmInput" rows="1" placeholder="Pose ta question à l'agent… (Enter pour envoyer, Shift+Enter pour un retour à la ligne)" autocomplete="off" spellcheck="false"></textarea>
    <div class="llm-input-actions">
      <button type="button" class="llm-stop" id="llmStop" title="Interrompre l'agent (Esc)" disabled>
        <svg class="icon icon-sm" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg>
        Stop
      </button>
      <button type="submit" id="llmSend" disabled>Envoyer</button>
    </div>
  </form>
</aside>
```

Keep the old `id="llmModel"` hidden-but-reachable if any other code touches it. Grep first:

```bash
grep -rn "llmModel" web/
```

If results are only in `llm.js`, you can change those references (see step 3). If external code references it, keep a hidden span. For this project, only `llm.js` uses it — safe to replace with `llmSubline`.

- [ ] **Step 2: CSS for the new header, tier chip, popover, and textarea**

In `web/styles/llm.css`, **replace** the existing `.llm-head` block (around lines 25-39) with:

```css
/* --- head --- */
.llm-head{
  display:flex;align-items:flex-start;gap:10px;
  padding:8px 14px 10px;border-bottom:1px solid var(--border);
  background:linear-gradient(180deg,rgba(192,132,252,.06),transparent);
  position:relative;
}
.llm-head .title-col{flex:1;display:flex;flex-direction:column;gap:2px;min-width:0}
.llm-head h3{margin:0;font-size:13px;font-weight:600;color:var(--text);
             overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.llm-head .llm-subline{
  font-family:var(--mono);font-size:10.5px;color:var(--text-3);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;letter-spacing:.2px;
}
.llm-head-actions{display:flex;align-items:center;gap:6px;position:relative}
```

**Replace** the existing `.llm-tiers`, `.llm-tier` rules (lines ~42-61) with the collapse chip + popover + hide the grid:

```css
/* Hide the old tier grid — replaced by chip + popover. */
.llm-tiers{display:none}

/* --- tier chip + popover --- */
.llm-tier-chip{
  display:inline-flex;align-items:center;gap:4px;
  padding:4px 8px;
  background:var(--panel);border:1px solid var(--border);border-radius:4px;
  cursor:pointer;transition:all .15s;
  font-family:var(--mono);font-size:10px;letter-spacing:.4px;color:var(--text-2);
  text-transform:uppercase;
}
.llm-tier-chip:hover{color:var(--text);border-color:#2e4468}
.llm-tier-chip[data-tier="fast"]{color:var(--emerald);border-color:rgba(52,211,153,.35);background:rgba(52,211,153,.08)}
.llm-tier-chip[data-tier="normal"]{color:var(--cyan);border-color:rgba(56,189,248,.35);background:rgba(56,189,248,.08)}
.llm-tier-chip[data-tier="deep"]{color:var(--violet);border-color:rgba(192,132,252,.35);background:rgba(192,132,252,.08)}
.llm-tier-chip .chevron-down{transition:transform .15s;opacity:.7}
.llm-tier-chip[aria-expanded="true"] .chevron-down{transform:rotate(180deg)}

.llm-tier-popover{
  position:absolute;top:calc(100% + 4px);right:30px;min-width:180px;z-index:40;
  background:rgba(20,32,48,.96);backdrop-filter:blur(10px);
  -webkit-backdrop-filter:blur(10px);
  border:1px solid var(--border);border-radius:6px;
  box-shadow:0 8px 24px rgba(0,0,0,.35);padding:4px;
  display:flex;flex-direction:column;gap:2px;
}
.llm-tier-popover[hidden]{display:none}
.llm-tier-popover button{
  text-align:left;background:transparent;border:0;
  padding:6px 10px;border-radius:4px;cursor:pointer;
  font-family:inherit;font-size:11.5px;color:var(--text-2);
}
.llm-tier-popover button:hover{background:var(--panel-2);color:var(--text)}
.llm-tier-popover button.on{color:var(--text);background:rgba(192,132,252,.08)}
```

**Replace** the `.llm-input` block (around lines 120-146) with the textarea + actions layout:

```css
/* --- input --- */
.llm-input{
  display:flex;flex-direction:column;gap:8px;padding:10px 12px;
  border-top:1px solid var(--border);
  background:var(--bg-2);
}
.llm-input textarea{
  flex:1;width:100%;
  background:var(--panel);border:1px solid var(--border);color:var(--text);
  border-radius:5px;padding:8px 10px;
  font-family:inherit;font-size:12.5px;line-height:1.45;
  outline:none;resize:none;
  min-height:36px;max-height:120px;overflow-y:auto;
  transition:border-color .15s;
}
.llm-input textarea:focus{border-color:var(--cyan)}
.llm-input textarea::placeholder{color:var(--text-3)}
.llm-input-actions{display:flex;gap:6px;justify-content:space-between;align-items:center}
.llm-input button{
  background:rgba(56,189,248,.12);color:var(--cyan);
  border:1px solid rgba(56,189,248,.3);border-radius:5px;
  padding:6px 14px;cursor:pointer;font-family:inherit;font-size:11px;
  letter-spacing:.4px;text-transform:uppercase;transition:all .15s;
  display:inline-flex;align-items:center;gap:6px;
}
.llm-input button:hover{background:rgba(56,189,248,.18);border-color:rgba(56,189,248,.5)}
.llm-input button:disabled{opacity:.45;cursor:not-allowed}
.llm-input button.llm-stop{
  background:rgba(245,158,11,.08);color:var(--amber);border-color:rgba(245,158,11,.3);
  padding:6px 10px;
}
.llm-input button.llm-stop:hover{background:rgba(245,158,11,.16);border-color:rgba(245,158,11,.55)}
.llm-input button.llm-stop:disabled{opacity:.35}
```

Finally, hide the now-unused cost-chip and the user-row role label (visual polish per spec §7):

Append at the end of the file:

```css
/* Cost-chip migrated into .turn-foot — hide any residual instances. */
.llm-log .cost-chip { display: none; }
/* Spec §7 — the cyan left border is attribution enough, drop the label. */
.llm-log .msg.user .role { display: none; }
```

- [ ] **Step 3: Tier popover + textarea auto-grow + cost delta in `web/js/llm.js`**

In `web/js/llm.js`:

**(a) Replace the `llmModel` textContent assignment** in the `session_ready` handler (around line 197). Find:

```js
      case "session_ready": {
        const model = payload.model || "claude";
        const mode = payload.mode || "managed";
        el("llmModel").textContent = `${model} · ${mode}`;
        const rid = payload.repair_id ? ` · repair ${payload.repair_id.slice(0, 8)}` : "";
        logSys(`session prête — ${mode} · ${model}${rid}`);
        break;
      }
```

Replace with:

```js
      case "session_ready": {
        const model = payload.model || "claude";
        const mode = payload.mode || "managed";
        const rid = payload.repair_id ? ` · repair ${payload.repair_id.slice(0, 8)}` : "";
        const sub = el("llmSubline");
        if (sub) sub.textContent = `${model} · ${mode}${rid}`;
        logSys(`session prête — ${mode} · ${model}${rid}`);
        break;
      }
```

**(b) Title from repair + device.** Update `connect()` (around line 147) to set the title from the URL params. Find:

```js
  el("llmDevice").textContent = repairId ? `${slug} · ${repairId.slice(0, 8)}` : slug;
  el("llmDevice").style.display = "";
```

Immediately after those two lines, add:

```js
  const title = el("llmTitle");
  if (title) {
    const human = slug.replace(/[-_]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    title.textContent = human;
  }
```

(The subline gets the precise `model · mode · repair` on `session_ready`; the title uses the slug humanized as a stable readable anchor.)

**(c) Cost delta in the status strip.** Replace `updateCostTotal` (around line 31) with:

```js
let lastTurnCostUsd = 0;

function updateCostTotal() {
  const el2 = el("llmCostTotal");
  if (!el2) return;
  if (sessionTurns === 0) {
    el2.style.display = "none";
    return;
  }
  el2.style.display = "";
  const deltaPart = lastTurnCostUsd > 0 ? ` · +${fmtUsd(lastTurnCostUsd)} dernier` : "";
  el2.textContent = `${fmtUsd(sessionCostUsd)} · ${sessionTurns} turn${sessionTurns > 1 ? "s" : ""}${deltaPart}`;
  el2.classList.toggle("hot", sessionCostUsd >= 0.50 || lastTurnCostUsd >= 0.10);
}
```

And in the `turn_cost` case of the WS switch, capture the delta before the increment. Find:

```js
      case "turn_cost":
        sessionCostUsd += Number(payload.cost_usd || 0);
        sessionTurns += 1;
        updateCostTotal();
        if (currentTurn) appendTurnFoot(currentTurn, payload);
        break;
```

Replace with:

```js
      case "turn_cost":
        lastTurnCostUsd = Number(payload.cost_usd || 0);
        sessionCostUsd += lastTurnCostUsd;
        sessionTurns += 1;
        updateCostTotal();
        if (currentTurn) appendTurnFoot(currentTurn, payload);
        break;
```

In `connect()`, reset `lastTurnCostUsd = 0;` alongside the other counters.

**(d) Delete `attachCostChipToLastAssistant`** entirely — it's dead code now. Remove lines 43-55 from the original file (the whole function).

**(e) Tier chip + popover wiring.** Replace the existing `document.querySelectorAll(".llm-tier").forEach(…)` block in `initLLMPanel` (around line 305) with the popover wiring:

```js
  // Tier chip → popover → switchTier
  const tierChip = el("llmTierChip");
  const tierPopover = el("llmTierPopover");
  function openTierPopover() {
    if (!tierChip || !tierPopover) return;
    tierPopover.hidden = false;
    tierChip.setAttribute("aria-expanded", "true");
  }
  function closeTierPopover() {
    if (!tierChip || !tierPopover) return;
    tierPopover.hidden = true;
    tierChip.setAttribute("aria-expanded", "false");
  }
  function toggleTierPopover() {
    if (!tierPopover) return;
    if (tierPopover.hidden) openTierPopover(); else closeTierPopover();
  }
  tierChip?.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleTierPopover();
  });
  tierPopover?.querySelectorAll("button[data-tier]").forEach(btn => {
    btn.addEventListener("click", () => {
      const t = btn.dataset.tier;
      switchTier(t);
      closeTierPopover();
    });
  });
  // Clicks outside close the popover; Esc too.
  document.addEventListener("click", (e) => {
    if (tierPopover && !tierPopover.hidden &&
        !tierPopover.contains(e.target) && e.target !== tierChip) {
      closeTierPopover();
    }
  }, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && tierPopover && !tierPopover.hidden) {
      closeTierPopover();
    }
  });
```

Update `switchTier` (around line 270) to also update the chip's visual state:

```js
function switchTier(newTier) {
  if (newTier === currentTier) return;
  currentTier = newTier;
  // Chip visual state
  const chip = el("llmTierChip");
  if (chip) {
    chip.dataset.tier = newTier;
    const label = chip.querySelector(".tier-label");
    if (label) label.textContent = newTier.toUpperCase();
  }
  // Popover item highlight
  document.querySelectorAll(".llm-tier-popover button[data-tier]").forEach(btn => {
    btn.classList.toggle("on", btn.dataset.tier === newTier);
  });
  logSys(`→ changement de tier : ${newTier}. Nouvelle conversation.`);
  if (ws && ws.readyState <= 1) {
    try { ws.close(); } catch (_) { /* ignore */ }
  }
  ws = null;
  connect();
}
```

**(f) Textarea auto-grow + Enter to submit.** In `initLLMPanel`, find the form submit listener (around line 309). Replace the existing block starting with `el("llmForm")?.addEventListener("submit", …)` with:

```js
  const input = el("llmInput");
  const form = el("llmForm");

  function autoGrow() {
    if (!input) return;
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  }
  input?.addEventListener("input", autoGrow);

  input?.addEventListener("keydown", (e) => {
    // Enter (without shift) → submit. Shift+Enter → newline (default).
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form?.requestSubmit();
    }
  });

  form?.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = (input?.value || "").trim();
    if (!text) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      logSys("impossible d'envoyer : socket non ouvert", true);
      return;
    }
    logMessage("user", text);
    ws.send(JSON.stringify({ type: "message", text }));
    if (input) {
      input.value = "";
      autoGrow();
    }
  });
```

- [ ] **Step 4: Manual browser verification**

Restart `make run`.

**Header.** Open a repair. Confirm:
- Line 1 shows `[AGENT badge]` + device name (e.g. `Iphone X Logic Board`) + tier chip + close.
- Line 2 shows mono subline `claude-haiku-4-5-xxxxx · managed · repair 9a3f…`.
- The tier chip is colored per tier (green FAST / cyan NORMAL / violet DEEP).

**Tier popover.** Click the `FAST ▾` chip. A glass popover appears below-right with three options, the current one highlighted. Click `Deep · Opus 4.7` → popover closes, chip becomes `DEEP ▾` in violet, WS reconnects, a `sys` line logs the tier switch. Click outside or press Esc to dismiss without picking — popover closes.

**Cost delta.** Send 3-4 messages. Watch the `.cost-total` in the status strip: `$0.045 · 3 turns · +$0.012 dernier`. After an expensive turn (change to Deep), the `+$…` jumps; if ≥ $0.10, the chip goes amber (`.hot`).

**Textarea.** Click in the input, type a 1-line sentence — box stays 36px. Type `Shift+Enter` several times, watching the textarea grow up to ~120px then scroll. Press `Enter` (no shift) → message sends, textarea resets. Re-send a long message to confirm.

**Cost-chip absence.** Confirm **no** per-message `$0.004` chip appears under assistant messages anymore (migrated into `.turn-foot`).

- [ ] **Step 5: Commit**

```bash
git add web/index.html web/js/llm.js web/styles/llm.css
git commit -m "$(cat <<'EOF'
feat(web/llm): chrome polish — 2-line header, tier chip, textarea, cost delta

Header now carries a readable device title on line 1 and a mono subline
(model · mode · repair-short-id) on line 2. The 3-pill tier grid is
replaced by a compact mono chip with a glass popover. The status
strip's cost-total gains a rolling delta ("+$0.012 dernier") and the
per-message cost chip is removed in favor of the turn-foot. Input
becomes a textarea that auto-grows from 1 to ~5 lines; Enter sends,
Shift+Enter newline.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- web/index.html web/js/llm.js web/styles/llm.css
```

---

## Post-implementation — verify and close

- [ ] **Step 1: Run the full test suite**

```bash
make test
```

Expected: all tests pass (backend unchanged, so no regressions expected).

- [ ] **Step 2: Run lint**

```bash
make lint
```

Expected: clean. No new Python was added, but ruff sweeps `api/` + `tests/`, so it should be a no-op.

- [ ] **Step 3: End-to-end browser smoke**

With `make run` going, walk through a full flow in the browser:

1. Start on Home. Create a new repair on a device that has a pack (e.g. `demo-pi`).
2. Open the chat panel (⌘J or the Agent button in the topbar).
3. Send a question that induces multi-tool reasoning. Watch the rail fill with typed nodes, then the message render with chips, then the turn-foot close.
4. Click a cyan chip → board focuses that refdes. Click an emerald chip → net lights up. Click a chevron → JSON expands.
5. Switch tier to `normal` via the chip popover → WS reconnects → send another message.
6. Press Stop during a long tool chain → agent interrupts.
7. Close the panel (Esc twice, or the × button). Reopen → panel restores.
8. Reload the page. Panel re-hydrates the repair via replay; turns appear in dimmed `.replay` state; new live turns render at full opacity.

- [ ] **Step 4: Update the CLAUDE.md layout pointer (optional)**

If Task 6 shifted module line counts past the ~300-line heuristic in the file description, consider updating `CLAUDE.md §Layout` to reflect the new `llm.js` size. No functional change — judgement call; skip if counts are close.

---

## Self-review checklist

Run through these before handing off:

- Every spec section (§3 turn-block, §4 tool-call grammar, §5 message rendering, §6 chrome) has a corresponding task.
- Every step contains actual code or an actual command — no "TBD" / "add appropriate handling".
- Types match across tasks: `appendStep(turn, kind, phraseHTML)` defined in Task 2 is called with the same signature in Tasks 3 and 5.
- `clearPendingNode` is defined before it's used (Task 5 step 2 defines both `ensurePendingNode` and `clearPendingNode` at the same time, and modifies `appendStep` to use `clearPendingNode` — both references resolve).
- `Boardview.focusRefdes` / `.highlightNet` / `.hasRefdes` / `.hasNet` / `.hasBoard` are defined in Task 1 and called from Task 4 — signatures match.
- The CDN script tags (Task 1) are loaded before `js/main.js` — both use `defer`, so execution order follows DOM order; the scripts go before `<script type="module" src="js/main.js">`. ✓
- Each commit message begins with a conventional-commits prefix (`feat(web/llm)`, `refactor(web/llm)`, `feat(web)`).
- Each `git commit` uses the `-- <paths>` form (CLAUDE.md parallel-agent hygiene rule).
- No backend files are touched.
- Licenses of new deps satisfy CLAUDE.md Hard Rule #3 (marked: MIT ✓, DOMPurify: Apache 2.0 ✓).
