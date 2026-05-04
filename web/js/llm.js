// Diagnostic agent panel — WS client to /ws/diagnostic/{device_slug}.
// The panel is push-mode: when open, body.llm-open is set and the main
// content zones shrink 420px on the right.
//
// Wire protocol (matches api/agent/runtime_{managed,direct}.py):
//   send: {type: "message", text: "..."}
//         {type: "client.capabilities", camera_available, ...}     (Files+Vision)
//         {type: "client.upload_macro", base64, mime, filename}    (Flow A)
//         {type: "client.capture_response", request_id, base64,    (Flow B)
//                  mime, device_label}
//         {type: "client.protocol_confirmation", tool_use_id,      (Pattern 4)
//                  decision: "accept"|"reject", reason?}
//   recv: {type: "session_ready", mode, device_slug, session_id?, memory_store_id?}
//         {type: "message", role: "assistant", text}
//         {type: "tool_use", name, input}
//         {type: "thinking", text}                 (managed mode only)
//         {type: "error", text}
//         {type: "session_terminated"}
//         {type: "server.capture_request", request_id, tool_use_id, reason}
//         {type: "server.upload_macro_error", reason}
//         {type: "protocol_pending_confirmation", tool_use_id, title, …}
//         {type: "protocol_confirmation_timeout", tool_use_id}
//
// Activated by ⌘/Ctrl+J and by clicking the topbar "Agent" button.

import {
  blobToBase64,
  captureFrame,
  isCameraAvailable,
  selectedCameraDeviceId,
  selectedCameraLabel,
} from './camera.js';
import {
  closePreview,
  isPreviewOpen,
  openPreview,
} from './camera_preview.js';
import { mountMascot, setMascotState } from './mascot.js';

const MAX_UPLOAD_BYTES = 5 * 1024 * 1024;  // 5MB raw, mirrors backend cap

let ws = null;
let currentTier = "deep";
// Cached <svg.mascot> mounted into #llmMascot at panel-fragment init time.
// `setPanelMascot()` is the single chokepoint for animating it from WS events
// and form submission — null-safe when the fragment hasn't loaded yet.
let panelMascot = null;
let _errorRecoveryT = null;
function setPanelMascot(state) {
  if (!panelMascot) return;
  if (_errorRecoveryT) { clearTimeout(_errorRecoveryT); _errorRecoveryT = null; }
  setMascotState(panelMascot, state);
  if (state === "error") {
    _errorRecoveryT = setTimeout(() => {
      setMascotState(panelMascot, "idle");
      _errorRecoveryT = null;
    }, 1800);
  }
}
// True once the tech has explicitly chosen a tier this page-load (clicked
// the popover, or any path that calls switchTier). Until that happens,
// session_ready may auto-realign currentTier with the resumed conv's
// preferred tier — opening a Sonnet/Haiku conv shouldn't silently land on
// its almost-empty Opus thread because the URL default was `deep`.
let userPickedTier = false;
// Multi-conversation state. `currentConvId` is captured from session_ready.
// `conversationsCache` backs the popover render. `pendingConvParam` is the
// ?conv value to use on the next connect() — "new" to force a fresh conv,
// a concrete id to target an existing one, null to let the backend resolve
// to the active conv.
let currentConvId = null;
let conversationsCache = [];
let pendingConvParam = null;
// Session cost accumulator — reset on each (re)connect. The backend emits
// `turn_cost` after every agent inference turn; we attach a chip to the most
// recent assistant message and bump the running total in the status bar.
let sessionCostUsd = 0;
import { ICON_CHECK } from './icons.js';

let sessionTurns = 0;
let lastTurnCostUsd = 0;

// Turn-block state machine.
// currentTurn is the DOM node receiving the next incoming thinking / tool_use /
// message event. A user.message closes it (set to null). An assistant.message
// that arrives when currentTurn already has a .turn-message opens a new turn
// (agent emitted two messages back-to-back without a user interjection).
let currentTurn = null;

// Family icons for tool-call steps. 12×12, inline SVG, stroke currentColor.
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
// MEM = MA-native filesystem ops on the device's memory store (read / write /
// edit / grep / glob), surfaced via the agent_toolset_20260401 toolset. Cylinder
// = persistent storage. Distinct from MB (knowledge bank queries via mb_*).
const ICON_MEM =
  '<svg class="step-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" ' +
  'aria-hidden="true"><ellipse cx="12" cy="5" rx="8" ry="2.5"/>' +
  '<path d="M4 5v14c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5V5"/>' +
  '<path d="M4 12c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5"/></svg>';

// Localized paraphrase + family icon for each known tool name. Each entry
// is a function receiving the tool input object and returning
// {icon, phraseHTML}. phraseHTML may embed a <span class="refdes"> or
// <span class="net"> for typographic emphasis on the target; all user
// input is passed through escapeHTML before interpolation. Strings come
// from i18n via window.t() so they re-render on locale switch.
const TOOL_PHRASES = {
  // --- MB (memory bank — perception / reading) ---
  mb_get_component: (i) => ({
    icon: ICON_MB,
    phraseHTML: t('chat.tool.mb_get_component', { refdes: escapeHTML(i?.refdes || "?") }),
  }),
  mb_get_rules_for_symptoms: (i) => {
    const syms = Array.isArray(i?.symptoms) ? i.symptoms.join(", ") : (i?.symptoms || "");
    return {
      icon: ICON_MB,
      phraseHTML: t('chat.tool.mb_get_rules_for_symptoms', { symptoms: escapeHTML(syms) }),
    };
  },
  mb_list_findings: (i) => ({
    icon: ICON_MB,
    phraseHTML: i?.device
      ? t('chat.tool.mb_list_findings_for', { device: escapeHTML(i.device) })
      : t('chat.tool.mb_list_findings'),
  }),
  mb_record_finding: () => ({
    icon: ICON_MB,
    phraseHTML: t('chat.tool.mb_record_finding'),
  }),
  mb_expand_knowledge: (i) => {
    const scope = [i?.component, i?.symptom].filter(Boolean).join(" / ");
    return {
      icon: ICON_MB,
      phraseHTML: scope
        ? t('chat.tool.mb_expand_knowledge_scope', { scope: escapeHTML(scope) })
        : t('chat.tool.mb_expand_knowledge'),
    };
  },
  mb_schematic_graph: () => ({
    icon: ICON_MB,
    phraseHTML: t('chat.tool.mb_schematic_graph'),
  }),

  // --- BV (boardview — action) ---
  bv_highlight: (i) => {
    const r = Array.isArray(i?.refdes) ? i.refdes.join(", ") : (i?.refdes || "?");
    return {
      icon: ICON_BV,
      phraseHTML: t('chat.tool.bv_highlight', { refdes: escapeHTML(r) }),
    };
  },
  bv_focus: (i) => ({
    icon: ICON_BV,
    phraseHTML: t('chat.tool.bv_focus', { refdes: escapeHTML(i?.refdes || "?") }),
  }),
  bv_reset_view: () => ({ icon: ICON_BV, phraseHTML: t('chat.tool.bv_reset_view') }),
  bv_highlight_net: (i) => ({
    icon: ICON_BV,
    phraseHTML: t('chat.tool.bv_highlight_net', { net: escapeHTML(i?.net || "?") }),
  }),
  bv_flip: () => ({ icon: ICON_BV, phraseHTML: t('chat.tool.bv_flip') }),
  bv_annotate: (i) => {
    let phraseHTML;
    if (i?.refdes) {
      phraseHTML = t('chat.tool.bv_annotate_near', { refdes: escapeHTML(i.refdes) });
    } else if (Number.isFinite(i?.x) && Number.isFinite(i?.y)) {
      phraseHTML = t('chat.tool.bv_annotate_at', { x: i.x, y: i.y });
    } else {
      phraseHTML = t('chat.tool.bv_annotate_blank');
    }
    return { icon: ICON_BV, phraseHTML };
  },
  bv_filter_by_type: (i) => ({
    icon: ICON_BV,
    phraseHTML: t('chat.tool.bv_filter_by_type', { prefix: escapeHTML(i?.prefix || "?") }),
  }),
  bv_draw_arrow: (i) => ({
    icon: ICON_BV,
    phraseHTML: t('chat.tool.bv_draw_arrow', {
      from: escapeHTML(i?.from_refdes || "?"),
      to: escapeHTML(i?.to_refdes || "?"),
    }),
  }),
  bv_measure: (i) => ({
    icon: ICON_BV,
    phraseHTML: t('chat.tool.bv_measure', {
      a: escapeHTML(i?.refdes_a || "?"),
      b: escapeHTML(i?.refdes_b || "?"),
    }),
  }),
  bv_show_pin: (i) => ({
    icon: ICON_BV,
    phraseHTML: t('chat.tool.bv_show_pin', {
      pin: escapeHTML(String(i?.pin ?? "?")),
      refdes: escapeHTML(i?.refdes || "?"),
    }),
  }),
  bv_dim_unrelated: () => ({ icon: ICON_BV, phraseHTML: t('chat.tool.bv_dim_unrelated') }),
  bv_layer_visibility: (i) => ({
    icon: ICON_BV,
    phraseHTML: t('chat.tool.bv_layer_visibility', { layer: escapeHTML(i?.layer || "?") }),
  }),
  bv_scene: (i) => {
    const parts = [];
    const hl = Array.isArray(i?.highlights) ? i.highlights.length : 0;
    const an = Array.isArray(i?.annotations) ? i.annotations.length : 0;
    const ar = Array.isArray(i?.arrows) ? i.arrows.length : 0;
    if (hl) parts.push(t(hl > 1 ? 'chat.tool.scene_highlight_many' : 'chat.tool.scene_highlight_one', { n: hl }));
    if (an) parts.push(t(an > 1 ? 'chat.tool.scene_annotation_many' : 'chat.tool.scene_annotation_one', { n: an }));
    if (ar) parts.push(t(ar > 1 ? 'chat.tool.scene_arrow_many' : 'chat.tool.scene_arrow_one', { n: ar }));
    if (i?.focus?.refdes) parts.push(t('chat.tool.scene_focus', { refdes: escapeHTML(i.focus.refdes) }));
    if (i?.dim_unrelated) parts.push(t('chat.tool.scene_dim'));
    if (i?.reset) parts.unshift(t('chat.tool.scene_reset'));
    return {
      icon: ICON_BV,
      phraseHTML: parts.length ? t('chat.tool.bv_scene', { parts: parts.join(", ") }) : t('chat.tool.bv_scene_empty'),
    };
  },
};

function toolFallback(name) {
  return {
    icon: "",
    phraseHTML: `<span class="tool-name-raw">${escapeHTML(name)}</span>`,
  };
}

// Strip the `/mnt/memory/{slug}/` MA-mount prefix so memory paths read as
// short relative paths (`outcomes/abc.json`) instead of full absolute ones.
function memPath(p) {
  if (!p) return "";
  const m = String(p).match(/^\/mnt\/memory\/[^/]+\/(.+)$/);
  return m ? m[1] : String(p);
}

function memPathChip(p) {
  return `<code class="mem-path">${escapeHTML(memPath(p))}</code>`;
}

// Localized paraphrase + ICON_MEM for each MA-native filesystem tool. Same
// shape contract as TOOL_PHRASES — receives the tool input object and
// returns {icon, phraseHTML}.
const MEMORY_TOOL_PHRASES = {
  read: (i) => ({
    icon: ICON_MEM,
    phraseHTML: t('chat.memtool.read', { path: memPathChip(i?.file_path || i?.path) }),
  }),
  write: (i) => ({
    icon: ICON_MEM,
    phraseHTML: t('chat.memtool.write', { path: memPathChip(i?.file_path || i?.path) }),
  }),
  edit: (i) => ({
    icon: ICON_MEM,
    phraseHTML: t('chat.memtool.edit', { path: memPathChip(i?.file_path || i?.path) }),
  }),
  view: (i) => ({
    icon: ICON_MEM,
    phraseHTML: t('chat.memtool.view', { path: memPathChip(i?.file_path || i?.path) }),
  }),
  grep: (i) => ({
    icon: ICON_MEM,
    phraseHTML: i?.path
      ? t('chat.memtool.grep_in', { pattern: escapeHTML(String(i?.pattern || "")), path: memPathChip(i.path) })
      : t('chat.memtool.grep', { pattern: escapeHTML(String(i?.pattern || "")) }),
  }),
  glob: (i) => ({
    icon: ICON_MEM,
    phraseHTML: t('chat.memtool.glob', { pattern: escapeHTML(String(i?.pattern || "")) }),
  }),
  list: (i) => ({
    icon: ICON_MEM,
    phraseHTML: i?.path
      ? t('chat.memtool.list_at', { path: memPathChip(i.path) })
      : t('chat.memtool.list'),
  }),
  ls: (i) => ({
    icon: ICON_MEM,
    phraseHTML: i?.path
      ? t('chat.memtool.list_at', { path: memPathChip(i.path) })
      : t('chat.memtool.list'),
  }),
};

function memToolFallback(name) {
  return {
    icon: ICON_MEM,
    phraseHTML: `<span class="tool-name-raw">${escapeHTML(name)}</span>`,
  };
}

function fmtUsd(amount) {
  if (amount >= 1) return `$${amount.toFixed(2)}`;
  if (amount >= 0.01) return `$${amount.toFixed(3)}`;
  if (amount >= 0.0001) return `$${amount.toFixed(4)}`;
  return amount > 0 ? `<$0.0001` : `$0.00`;
}

function updateCostTotal() {
  const el2 = el("llmCostTotal");
  if (!el2) return;
  if (sessionTurns === 0) {
    el2.style.display = "none";
    return;
  }
  el2.style.display = "";
  const deltaPart = lastTurnCostUsd > 0 ? ` · +${fmtUsd(lastTurnCostUsd)} last` : "";
  el2.textContent = `${fmtUsd(sessionCostUsd)} · ${sessionTurns} turn${sessionTurns > 1 ? "s" : ""}${deltaPart}`;
  el2.classList.toggle("hot", sessionCostUsd >= 0.50 || lastTurnCostUsd >= 0.10);
}

function el(id) { return document.getElementById(id); }

function statusTone(tone, label) {
  const s = el("llmStatus");
  s.classList.remove("connecting", "connected", "closed", "error");
  if (tone) s.classList.add(tone);
  el("llmStatusText").textContent = label;
}

function logRow(cls, innerHTML) {
  const log = el("llmLog");
  const row = document.createElement("div");
  row.className = cls;
  row.innerHTML = innerHTML;
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
  return row;
}

function logMessage(role, text, isReplay = false) {
  const roleLabel = role === "user" ? t('chat.roles.user') : t('chat.roles.agent');
  const cls = `msg ${role}${isReplay ? " replay" : ""}`;
  const replaySuffix = isReplay ? t('chat.roles.replay_suffix') : "";
  logRow(
    cls,
    `<span class="role">${escapeHTML(roleLabel)}${escapeHTML(replaySuffix)}</span>${escapeHTML(text)}`,
  );
}

// Distinct card rendered when MA dropped the prior session AND we had
// no local JSONL backup to summarize from. The agent was recreated from
// scratch; it has zero memory of the prior turns. Amber alert (different
// from the violet "resumed-with-summary" card) so the tech knows their
// next message hits a blank-slate model.
function renderContextLost(payload) {
  const oldId = payload?.old_session_id || "";
  const newId = payload?.new_session_id || "";
  const reason = payload?.reason === "ma_events_empty"
    ? t('chat.context_lost.reason_ma_empty')
    : t('chat.context_lost.reason_generic');
  // `preserved` summarises what survived on disk independently of MA.
  // The backend already pushed these facts to the fresh agent (intro
  // block on resumed=False, synthetic user.message on resumed=True).
  // This UI just tells the tech which artefacts the agent now has so they
  // know what NOT to re-explain.
  const preserved = payload?.preserved || {};
  const mCount = Number(preserved.measurements || 0);
  const proto = preserved.protocol;
  const outcome = !!preserved.outcome;
  const preservedItems = [];
  if (mCount > 0) {
    preservedItems.push(t(mCount > 1 ? 'chat.context_lost.preserved_measurements_many' : 'chat.context_lost.preserved_measurements_one', { n: mCount }));
  }
  if (proto) {
    preservedItems.push(t('chat.context_lost.preserved_protocol', {
      title: escapeHTML(proto.title || ""),
      completed: proto.completed || 0,
      total: proto.total || 0,
    }));
  }
  if (outcome) preservedItems.push(t('chat.context_lost.preserved_outcome'));
  const preservedHTML = preservedItems.length
    ? `<p class="preserved"><strong>${escapeHTML(t('chat.context_lost.preserved_label'))}</strong> : ${preservedItems.join(" · ")}.</p>`
    : `<p class="preserved muted">${escapeHTML(t('chat.context_lost.preserved_none'))}</p>`;
  logRow(
    "context-lost",
    `<header>
       <span class="icon-dot"></span>
       <span class="title">${escapeHTML(t('chat.context_lost.title'))}</span>
     </header>
     <div class="body">
       <p>${escapeHTML(reason)}</p>
       <p>${escapeHTML(t('chat.context_lost.context_reinject'))}</p>
       ${preservedHTML}
       <p>${t('chat.context_lost.lost_explainer')}</p>
       ${oldId ? `<p class="meta">old=<code>${escapeHTML(oldId)}</code> · new=<code>${escapeHTML(newId)}</code></p>` : ""}
     </div>`,
  );
}

// Distinct card rendered when an expired MA session had to be recreated and
// Haiku summarised the prior conversation for the fresh agent. Shows the
// same block the new agent is seeing, so the tech knows what carried over.
function renderResumeSummary(payload) {
  const summary = payload?.summary || "";
  const tokIn = payload?.tokens_in ?? "—";
  const tokOut = payload?.tokens_out ?? "—";
  let bodyHTML = escapeHTML(summary);
  if (typeof window.marked !== "undefined" && typeof window.DOMPurify !== "undefined") {
    try {
      bodyHTML = window.DOMPurify.sanitize(window.marked.parse(summary));
    } catch (e) { /* keep escaped fallback */ }
  }
  logRow(
    "resume-summary",
    `<header>
       <span class="icon-dot"></span>
       <span class="title">${escapeHTML(t('chat.resume.title'))}</span>
       <span class="meta">${escapeHTML(t('chat.resume.meta_summary', { tok_in: tokIn, tok_out: tokOut }))}</span>
     </header>
     <div class="body">${bodyHTML}</div>`,
  );
}

function logSys(text, isErr = false) {
  logRow(isErr ? "sys err" : "sys", escapeHTML(text));
}

function escapeHTML(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

// Append a small terminal-state chip to the chat log (abandoned / completed).
// Distinct visual from agent/user messages — centered, muted, mono — so the
// tech can scroll back through the conv and see when a sequence was dropped
// or finished, and why. Idempotent on protocol_id+kind to avoid duplicates
// when the same event arrives twice (replay race, double-emit, etc.).
function appendProtocolSystemEvent(kind, { protocol_id, reason } = {}) {
  const log = el("llmLog");
  if (!log) return;
  const dedupeKey = `${kind}:${protocol_id || "none"}`;
  if (log.querySelector(`.protocol-system-event[data-key="${dedupeKey}"]`)) return;
  const chip = document.createElement("div");
  chip.className = `protocol-system-event is-${kind}`;
  chip.dataset.key = dedupeKey;
  const label = kind === "abandoned"
    ? (window.t?.("protocol.system_event.abandoned") || "Protocol abandoned")
    : (window.t?.("protocol.system_event.completed") || "Protocol completed");
  // Inline SVG matches the project's icon convention (16/12 px,
  // stroke="currentColor", stroke-width=1.6) — no font icon dependency.
  const icon = kind === "abandoned"
    ? `<svg class="pse-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 6l12 12M18 6l-12 12"/></svg>`
    : `<svg class="pse-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12l5 5L20 7"/></svg>`;
  chip.innerHTML = icon + `<span>${escapeHTML(label)}</span>`;
  if (reason && reason !== "tech_dismiss") {
    const r = document.createElement("span");
    r.className = "pse-reason";
    r.textContent = `· ${reason}`;
    chip.appendChild(r);
  }
  log.appendChild(chip);
  log.scrollTop = log.scrollHeight;
}

// Mode C — inline protocol step card in the chat stream when no board is loaded.
// Renders only the active step (past steps are summarized in the wizard).
// One card per active step id; subsequent events for the same id no-op.
function renderInlineProtocolCard(_ev) {
  const proto = window.Protocol?.getProtocol?.();
  if (!proto) return;
  const active = proto.steps.find((s) => s.id === proto.current_step_id);
  if (!active) return;
  const log = el("llmLog");
  if (!log) return;
  if (log.querySelector(`.protocol-inline-card[data-step="${active.id}"]`)) return;
  const card = document.createElement("div");
  card.className = "protocol-inline-card";
  card.dataset.step = active.id;
  card.innerHTML =
    `<div class="protocol-step-target">${escapeHTML(active.target || active.test_point || "—")}</div>` +
    `<p class="protocol-step-instruction">${escapeHTML(active.instruction)}</p>` +
    `<p class="protocol-step-rationale">${escapeHTML(active.rationale)}</p>`;
  if (window.Protocol?.buildStepForm) {
    card.appendChild(window.Protocol.buildStepForm(active));
  }
  log.appendChild(card);
  log.scrollTop = log.scrollHeight;
}

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

function ensurePendingNode(turn, label) {
  const rail = turn.querySelector(".turn-rail");
  if (!rail || rail.querySelector(".step.pending")) return;
  const finalLabel = label != null ? label : t('chat.pending.thinking');
  const step = document.createElement("div");
  step.className = "step pending";
  step.innerHTML =
    `<span class="node"></span>` +
    `<span class="step-phrase">${escapeHTML(finalLabel)}` +
    `<span class="pending-dots"><span>.</span><span>.</span><span>.</span></span>` +
    `</span>`;
  rail.appendChild(step);
  el("llmLog").scrollTop = el("llmLog").scrollHeight;
}

function clearPendingNode(turn) {
  const p = turn.querySelector(".step.pending");
  if (p) p.remove();
}

// Append a .step into the turn's rail. kind ∈ {"thinking","mb","bv"}.
// phraseHTML is trusted HTML (callers escape user-provided fragments
// themselves — currently only tool names + refdes which are validated).
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

function addExpandToStep(step, payloadObj) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "step-expand";
  btn.setAttribute("aria-expanded", "false");
  btn.title = t('chat.step.expand_title');
  btn.innerHTML =
    '<svg class="chevron" width="10" height="10" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
    '<polyline points="9 6 15 12 9 18"/></svg>';
  step.appendChild(btn);

  const pre = document.createElement("pre");
  pre.className = "step-payload";
  const hasResult = payloadObj && typeof payloadObj === "object" && "result" in payloadObj;
  const body = hasResult
    ? JSON.stringify(payloadObj, null, 2)
    : JSON.stringify(payloadObj, null, 2) + "\n\n" + t('chat.step.no_result');
  pre.textContent = body;
  step.appendChild(pre);

  btn.addEventListener("click", () => {
    const expanded = step.classList.toggle("expanded");
    btn.setAttribute("aria-expanded", expanded ? "true" : "false");
  });
}

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
  clearPendingNode(turn);
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
    html = escapeHTML(text).replaceAll("\n", "<br>");
  }
  container.innerHTML = html;
  decorateChipsIn(container);
}

// Walk all text nodes under `root` and replace validated refdes / net
// tokens with clickable chips, plus unknown-refdes ⟨?U999⟩ with amber
// span. Text inside <code> is skipped (agent's verbatim intent).
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
  // Resolve overlaps: earliest-start first, ties broken by longest-wins.
  matches.sort((a, b) => a.start - b.start || (b.end - b.start) - (a.end - a.start));
  const cleaned = [];
  let cursor = 0;
  for (const m of matches) {
    if (m.start < cursor) continue;
    cleaned.push(m);
    cursor = m.end;
  }
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

// Chip-click target: switch the main view to #pcb if we're not already there,
// then run the boardview action. The panel is push-mode so the board shows
// to the left while the chat stays visible on the right (420 px strip). When
// we have to navigate, wait two animation frames so the section becomes
// visible and brd_viewer's ResizeObserver sees non-zero canvas dimensions —
// otherwise the focus pan would compute against a 0×0 canvas and end up off
// screen (the ResizeObserver now flushes any pending focus on its own, but
// the nav-then-apply ordering also lets non-focus actions see the real DOM).
function gotoBoardviewThen(fn) {
  if (window.location.hash === "#pcb") {
    fn();
    return;
  }
  window.location.hash = "#pcb";
  requestAnimationFrame(() => requestAnimationFrame(fn));
}

function makeChipNode(match) {
  if (match.kind === "refdes") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip-refdes";
    btn.dataset.refdes = match.raw;
    btn.textContent = match.raw;
    btn.addEventListener("click", () => {
      gotoBoardviewThen(() => window.Boardview?.focusRefdes?.(match.raw));
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
      gotoBoardviewThen(() => window.Boardview?.highlightNet?.(match.raw));
    });
    return btn;
  }
  const span = document.createElement("span");
  span.className = "refdes-unknown";
  span.textContent = match.raw;
  return span;
}

function appendTurnFoot(turn, payload) {
  // Terminal signal for this turn — clear transient indicators.
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

function safeJSON(v) {
  try { return JSON.stringify(v ?? {}); } catch { return String(v); }
}

function currentDeviceSlug() {
  return new URLSearchParams(window.location.search).get("device");
}

function currentRepairId() {
  return new URLSearchParams(window.location.search).get("repair") || null;
}

function wsURL(slug, tier, repairId, convParam) {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const params = new URLSearchParams();
  if (tier) params.set("tier", tier);
  if (repairId) params.set("repair", repairId);
  if (convParam) params.set("conv", convParam);
  const q = params.toString() ? `?${params.toString()}` : "";
  return `${scheme}://${window.location.host}/ws/diagnostic/${encodeURIComponent(slug)}${q}`;
}

function setSendEnabled(enabled) {
  el("llmSend").disabled = !enabled;
  el("llmStop").disabled = !enabled;
}

// --- Files+Vision (Flow A + Flow B) ---------------------------------------

function sendCapabilities() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  try {
    ws.send(JSON.stringify({
      type: "client.capabilities",
      camera_available: isCameraAvailable(),
      selected_device_id: selectedCameraDeviceId(),
    }));
  } catch (err) {
    console.warn("[llm] sendCapabilities failed", err);
  }
}

// Optimistic image bubble in the chat log. URL is either a blob: URL
// (Flow A optimistic local render) or a /api/macros/... URL (replay).
function appendImageBubble(role, srcUrl, captionText) {
  const log = el("llmLog");
  if (!log) return;
  const row = document.createElement("div");
  row.className = `msg ${role} msg-image`;
  const roleLabel = role === "user" ? t('chat.roles.user') : t('chat.roles.agent');
  const img = document.createElement("img");
  img.src = srcUrl;
  img.alt = captionText || t('chat.image_bubble.alt');
  img.className = "llm-bubble-img";
  img.addEventListener("click", () => openImageModal(srcUrl, captionText));
  const cap = document.createElement("div");
  cap.className = "llm-bubble-caption";
  cap.textContent = captionText || "";
  row.innerHTML = `<span class="role">${escapeHTML(roleLabel)}</span>`;
  row.appendChild(img);
  if (captionText) row.appendChild(cap);
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
}

function openImageModal(srcUrl, captionText) {
  let modal = document.getElementById("llmImageModal");
  if (!modal) {
    modal = document.createElement("div");
    modal.id = "llmImageModal";
    modal.className = "llm-image-modal";
    modal.addEventListener("click", () => modal.remove());
    document.body.appendChild(modal);
  } else {
    modal.innerHTML = "";
  }
  const img = document.createElement("img");
  img.src = srcUrl;
  img.alt = captionText || "";
  modal.appendChild(img);
}

async function handleMacroUpload(file) {
  if (!file) return;
  if (file.size > MAX_UPLOAD_BYTES) {
    logSys(t('chat.upload.too_large', { size: (file.size / 1024 / 1024).toFixed(1) }), true);
    return;
  }
  if (!["image/png", "image/jpeg"].includes(file.type)) {
    logSys(t('chat.upload.unsupported', { mime: file.type }), true);
    return;
  }
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    logSys(t('chat.upload.socket_closed'), true);
    return;
  }
  // Optimistic local render — blob URL stays valid for the page lifetime.
  const url = URL.createObjectURL(file);
  appendImageBubble("user", url, t('chat.image_bubble.macro_caption'));
  try {
    const base64 = await blobToBase64(file);
    ws.send(JSON.stringify({
      type: "client.upload_macro",
      base64,
      mime: file.type,
      filename: file.name || "macro.jpg",
    }));
  } catch (err) {
    logSys(t('chat.upload.failed', { error: err.message || err }), true);
  }
}

async function handleCaptureRequest(payload) {
  const { request_id, tool_use_id, reason } = payload;
  const deviceId = selectedCameraDeviceId();
  if (!deviceId) {
    // Tool exposed but no camera selected — surface to the tech and let
    // the backend's is_error response close the loop on the agent side.
    logSys(t('chat.capture.no_camera'), true);
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: "client.capture_response",
        request_id, base64: "", mime: "", device_label: "",
      }));
    }
    return;
  }
  logSys(t('chat.capture.requested', { reason: reason || t('chat.capture.no_reason') }));
  try {
    const blob = await captureFrame({
      deviceId, mime: "image/jpeg", quality: 0.92,
    });
    if (!blob) throw new Error("canvas.toBlob returned null");
    const base64 = await blobToBase64(blob);
    // Optimistic render so the tech sees what the agent received.
    const url = URL.createObjectURL(blob);
    appendImageBubble("user", url, t('chat.image_bubble.capture_caption', { label: selectedCameraLabel() }));
    ws.send(JSON.stringify({
      type: "client.capture_response",
      request_id,
      base64,
      mime: "image/jpeg",
      device_label: selectedCameraLabel(),
    }));
  } catch (err) {
    console.error("captureFrame failed", err);
    logSys(t('chat.capture.failed', { error: err.message || err }), true);
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: "client.capture_response",
        request_id, base64: "", mime: "", device_label: "",
      }));
    }
  }
}

// Expose for camera.js to re-trigger after the user changes the picker.
window.LLM = window.LLM || {};
window.LLM.sendCapabilities = sendCapabilities;

// --- end Files+Vision ----------------------------------------------------

// Interrupt the live agent turn. The server translates this into an
// official `user.interrupt` session event (see
// https://platform.claude.com/docs/en/managed-agents/events-and-streaming).
// MA guarantees the agent halts mid-execution; the session stays alive so
// the tech can keep typing right after without reconnecting.
function interruptAgent() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  logSys(t('chat.session.interrupt_sent'));
  try {
    ws.send(JSON.stringify({ type: "interrupt" }));
  } catch (err) {
    console.warn("interrupt send failed", err);
  }
}

function connect() {
  const slug = currentDeviceSlug();
  if (!slug) {
    console.warn("[llm] connect() called without ?device= in the URL — aborting.");
    return;
  }
  const repairId = currentRepairId();
  el("llmDevice").textContent = repairId
    ? t('chat.session.device_label_with_repair', { slug, repair: repairId.slice(0, 8) })
    : t('chat.session.device_label_simple', { slug });
  el("llmDevice").style.display = "";
  const title = el("llmTitle");
  if (title) {
    const human = slug.replace(/[-_]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    title.textContent = human;
  }
  // New connection = new cost scope. Replayed history doesn't re-bill so we
  // reset here and let live turns accumulate fresh.
  sessionCostUsd = 0;
  sessionTurns = 0;
  lastTurnCostUsd = 0;
  currentTurn = null;
  currentConvId = null;
  // Clear the log — the next session_ready / history_replay_start will
  // rebuild the right content. Without this, switching conv or tier
  // appends the replayed history below the old conv's visible messages.
  const log = el("llmLog");
  if (log) {
    log.innerHTML = "";
    log.classList.remove("replay");
  }
  updateCostTotal();
  const url = wsURL(slug, currentTier, repairId, pendingConvParam);
  pendingConvParam = null;  // consume after this connect
  statusTone("connecting", t('chat.status.connecting', { slug, tier: currentTier }));

  try {
    ws = new WebSocket(url);
    window.__diagnosticWS = ws;
  } catch (err) {
    statusTone("error", t('chat.status.url_invalid'));
    logSys(t('chat.send.connect_failed', { error: err.message }), true);
    return;
  }

  ws.addEventListener("open", () => {
    statusTone("connected", t('chat.status.connected', { slug, tier: currentTier }));
    setSendEnabled(true);
    // Files+Vision : announce camera availability so the backend gates
    // cam_capture in the manifest (runtime_direct) and can short-circuit
    // empty captures (managed runtime).
    sendCapabilities();
  });

  ws.addEventListener("close", () => {
    statusTone("closed", t('chat.status.closed'));
    setSendEnabled(false);
    setPanelMascot("idle");
  });

  ws.addEventListener("error", () => {
    statusTone("error", t('chat.status.error_socket'));
    setSendEnabled(false);
    setPanelMascot("error");
  });

  ws.addEventListener("message", ev => {
    let payload;
    try { payload = JSON.parse(ev.data); }
    catch { payload = { type: "message", role: "assistant", text: ev.data }; }

    // Boardview events are visual mutations — not chat content. Route them
    // to the renderer (or its pending buffer if the renderer hasn't mounted).
    if (typeof payload.type === "string" && payload.type.startsWith("boardview.")) {
      window.Boardview.apply(payload);
      return;
    }

    // Protocol events drive the stepwise diagnostic wizard. Route them to
    // the Protocol module which owns state + renderer (protocol.js). When
    // no board is loaded, also render an inline card in the chat stream
    // (Mode C) so the tech still sees the active step + form even without
    // the wizard column visible.
    if (typeof payload.type === "string" && payload.type.startsWith("protocol_")) {
      window.Protocol?.applyEvent(payload);
      // Surface terminal-state chips in the chat stream regardless of board
      // mode — abandon and completion are session-level events the tech
      // should see in their scrollback. Reason is the human-supplied
      // textarea entry from the abandon modal (or "tech_dismiss" default).
      if (payload.type === "protocol_updated") {
        if (payload.action === "abandoned" || payload.status === "abandoned") {
          appendProtocolSystemEvent("abandoned", {
            protocol_id: payload.protocol_id,
            reason: payload.reason || payload.history_tail?.slice(-1)?.[0]?.reason,
          });
        } else if (payload.status === "completed") {
          appendProtocolSystemEvent("completed", {
            protocol_id: payload.protocol_id,
          });
        }
      } else if (payload.type === "protocol_completed") {
        appendProtocolSystemEvent("completed", {
          protocol_id: payload.protocol_id,
        });
      }
      // Pending confirmation + timeout drive the modal only — do not render
      // an inline card in the chat stream (the modal is a global blocker).
      const isModalOnly = (
        payload.type === "protocol_pending_confirmation" ||
        payload.type === "protocol_confirmation_timeout"
      );
      const noBoard = !window.Boardview?.hasBoard?.();
      if (!isModalOnly && noBoard && payload.type !== "protocol_completed") {
        renderInlineProtocolCard(payload);
      }
      return;
    }

    // Simulation observation events mirror the agent's measurement tools
    // onto the schematic UI in real time. Same one-way channel, different
    // controller (SimulationController lives in schematic.js).
    if (typeof payload.type === "string" && payload.type.startsWith("simulation.")) {
      const SC = window.SimulationController;
      if (payload.type === "simulation.observation_set" && SC) {
        const parsed = (typeof payload.target === "string" && payload.target.includes(":"))
          ? payload.target.split(":", 2) : [null, null];
        const kind = parsed[0] === "rail" ? "rail" : parsed[0] === "comp" ? "comp" : null;
        const key = parsed[1];
        if (kind && key) SC.setObservation(kind, key, payload.mode, payload.measurement);
      } else if (payload.type === "simulation.observation_clear" && SC) {
        SC.clearObservations();
      } else if (payload.type === "simulation.repair_validated") {
        const btn = document.getElementById("dashboardFixBtn");
        if (btn) {
          // Clear any pending safety timeout from the dashboard-side.
          if (btn._fixTimeoutId) { clearTimeout(btn._fixTimeoutId); btn._fixTimeoutId = null; }
          const n = payload.fixes_count || 1;
          btn.innerHTML = ICON_CHECK + " " + escapeHTML(t(n > 1 ? 'chat.fix.validated_many' : 'chat.fix.validated_one', { n }));
          btn.classList.add("is-validated");
          btn.disabled = true;
        }
      }
      return;
    }

    switch (payload.type) {
      case "session_ready": {
        const model = payload.model || "claude";
        const mode = payload.mode || "managed";
        const repairShort = payload.repair_id ? payload.repair_id.slice(0, 8) : null;
        const sub = el("llmSubline");
        if (sub) {
          sub.textContent = repairShort
            ? t('chat.session.subline_with_repair', { model, mode, repair: repairShort })
            : t('chat.session.subline', { model, mode });
        }
        logSys(repairShort
          ? t('chat.session.ready_with_repair', { mode, model, repair: repairShort })
          : t('chat.session.ready', { mode, model }));
        currentConvId = payload.conv_id || null;
        loadConversations();
        // Auto-align tier with the resumed conv when the tech hasn't
        // explicitly picked one this session AND the conv was created on
        // a different tier. Without this, defaulting to fast/Haiku silently
        // resumed the (almost empty) per-tier thread of a Sonnet conv —
        // the user saw "0 messages" on a 31-turn conversation because
        // they were looking at the wrong thread.
        const convTier = payload.conv_tier;
        if (
          convTier &&
          convTier !== currentTier &&
          !userPickedTier &&
          ["fast", "normal", "deep"].includes(convTier)
        ) {
          logSys(t('chat.session.tier_auto_align', { tier: convTier }));
          // Mirror switchTier logic but skip the "user-chose" mark so a
          // future explicit tier pick still gates this auto-align.
          currentTier = convTier;
          const chip = el("llmTierChip");
          if (chip) {
            chip.dataset.tier = convTier;
            const label = chip.querySelector(".tier-label");
            if (label) label.textContent = convTier.toUpperCase();
          }
          document.querySelectorAll(".llm-tier-popover button[data-tier]").forEach(btn => {
            btn.classList.toggle("on", btn.dataset.tier === convTier);
          });
          if (ws && ws.readyState <= 1) {
            try { ws.close(); } catch (_) { /* ignore */ }
          }
          ws = null;
          // Keep the same conv_id on reconnect so we land on the right thread.
          pendingConvParam = currentConvId || null;
          connect();
        }
        break;
      }
      case "history_replay_start":
        el("llmLog").classList.add("replay");
        logSys(t('chat.session.replay_count', { n: payload.count }));
        break;
      case "history_replay_end":
        el("llmLog").classList.remove("replay");
        logSys(t('chat.session.replay_done'));
        closeTurn();
        break;
      case "context_loaded":
        logSys(t('chat.session.context_loaded'));
        break;
      case "session_resumed":
        logSys(t('chat.session.session_resumed'));
        break;
      case "session_resumed_summary":
        renderResumeSummary(payload);
        break;
      case "context_lost":
        // The Anthropic Managed Agents session has been silently dropped /
        // compacted by the beta backend, AND we had no local JSONL backup
        // to summarize. The freshly created session has no memory of the
        // prior turns — anything the tech asks now will be answered as if
        // it's the first turn of the conversation. Without this card the
        // chat panel pretends nothing happened and the tech wastes minutes
        // wondering why the agent forgot the symptom they discussed.
        renderContextLost(payload);
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
        const renderer = TOOL_PHRASES[name];
        const { icon, phraseHTML } = renderer ? renderer(payload.input || {}) : toolFallback(name);
        const step = appendStep(turn, kind, `${icon}${phraseHTML}`);
        const payloadJSON = {
          args: payload.input || {},
          ...(payload.result != null ? { result: payload.result } : {}),
        };
        addExpandToStep(step, payloadJSON);
        setPanelMascot("working");
        break;
      }
      case "memory_tool_use": {
        // MA-native filesystem ops on /mnt/memory/{slug}/. These can arrive
        // after the assistant's message (next agent inference step starting),
        // so open a fresh turn in that case — otherwise the new step would
        // render in the rail above the message.
        let turn = ensureTurn();
        if (turn.querySelector(".turn-message")) {
          closeTurn();
          turn = ensureTurn();
        }
        const name = payload.name || "?";
        const renderer = MEMORY_TOOL_PHRASES[name];
        const { icon, phraseHTML } = renderer ? renderer(payload.input || {}) : memToolFallback(name);
        const step = appendStep(turn, "mem", `${icon}${phraseHTML}`);
        addExpandToStep(step, { args: payload.input || {} });
        setPanelMascot("working");
        break;
      }
      case "thinking": {
        const turn = ensureTurn();
        appendStep(turn, "thinking", escapeHTML(payload.text || "…"));
        setPanelMascot("thinking");
        break;
      }
      case "turn_cost":
        lastTurnCostUsd = Number(payload.cost_usd || 0);
        sessionCostUsd += lastTurnCostUsd;
        sessionTurns += 1;
        updateCostTotal();
        if (currentTurn) appendTurnFoot(currentTurn, payload);
        clearTimeout(window._llmConvRefreshT);
        window._llmConvRefreshT = setTimeout(() => loadConversations(), 500);
        break;
      case "error":
        logSys(t('chat.error.generic', { text: payload.text }), true);
        // If the dashboard fix button is pending, clear its spinner so the
        // tech can retry instead of staring at "… Claude valide" forever.
        if (typeof window.__resetDashboardFixBtn === "function") {
          window.__resetDashboardFixBtn();
        }
        setPanelMascot("error");
        break;
      case "session_terminated":
        logSys(t('chat.session.session_terminated'), true);
        closeTurn();
        setPanelMascot("idle");
        break;
      case "server.capture_request":
        // Flow B: agent called cam_capture. Snap from the metabar-selected
        // device and post back client.capture_response (success or empty).
        handleCaptureRequest(payload).catch((err) => {
          console.error("capture handler crash", err);
        });
        break;
      case "server.upload_macro_error":
        logSys(t('chat.upload.rejected', { reason: payload.reason || t('chat.error.unknown_reason') }), true);
        break;
      case "turn_complete":
        // Internal signal for benchmarks (end of an agent tech-turn). UI
        // doesn't render it — turn boundaries are already conveyed by the
        // turn_cost foot and the next user message.
        setPanelMascot("idle");
        break;
      default:
        // Unknown WS event type — typically means the backend grew a new
        // signal that the frontend handler list hasn't caught up with, OR
        // the browser is serving a stale cached llm.js. Either way, raw
        // JSON in the chat log is meaningless to the tech (looked like
        // garbage `? {...}` rows in production). Surface to the dev console
        // for debug instead so the chat stream stays readable.
        console.warn("[llm] unhandled WS event:", payload?.type, payload);
    }
  });
}

// `targetConv` (optional): "new" or an existing conv id. When set, the panel
// opens directly on that conversation in a single connect() — avoids the
// double-connect race where openPanel() first lands on the active conv and
// switchConv() then immediately reconnects to the right one (which spawned
// useless 0-turn slots before the lazy-materialize landing).
export function openPanel(targetConv) {
  if (targetConv && targetConv !== currentConvId) {
    pendingConvParam = targetConv;
    if (ws && ws.readyState <= 1) {
      try { ws.close(); } catch (_) { /* ignore */ }
    }
    ws = null;
  }
  el("llmPanel").classList.add("open");
  el("llmPanel").setAttribute("aria-hidden", "false");
  document.body.classList.add("llm-open");
  el("llmToggle").classList.add("on");
  if (!ws || ws.readyState === WebSocket.CLOSED) connect();
  setTimeout(() => el("llmInput").focus(), 50);
}

function closePanel() {
  el("llmPanel").classList.remove("open");
  el("llmPanel").setAttribute("aria-hidden", "true");
  document.body.classList.remove("llm-open");
  el("llmToggle").classList.remove("on");
}

// Force-close the chat panel and tear down the live WS when the conversation
// it points at has just been deleted from the dashboard. Prevents the panel
// from sitting on a dead conv_id and trying to send to a now-404 session.
export function closePanelIfConv(convId) {
  if (!convId || convId !== currentConvId) return;
  if (ws && ws.readyState <= 1) {
    try { ws.close(); } catch (_) { /* ignore */ }
  }
  ws = null;
  currentConvId = null;
  if (el("llmPanel").classList.contains("open")) closePanel();
}

function togglePanel() {
  if (el("llmPanel").classList.contains("open")) closePanel();
  else openPanel();
}

function switchTier(newTier) {
  if (newTier === currentTier) return;
  // Mark as explicit user choice — disables the conv_tier auto-align that
  // session_ready otherwise applies on default landings.
  userPickedTier = true;
  currentTier = newTier;
  const chip = el("llmTierChip");
  if (chip) {
    chip.dataset.tier = newTier;
    const label = chip.querySelector(".tier-label");
    if (label) label.textContent = newTier.toUpperCase();
  }
  document.querySelectorAll(".llm-tier-popover button[data-tier]").forEach(btn => {
    btn.classList.toggle("on", btn.dataset.tier === newTier);
  });
  logSys(t('chat.session.tier_switch', { tier: newTier }));
  if (ws && ws.readyState <= 1) {
    try { ws.close(); } catch (_) { /* ignore */ }
  }
  ws = null;
  pendingConvParam = "new";  // new tier = new conversation
  connect();
}

// Auto-open the panel when the URL carries ?repair=<id>. Called from the
// main bootstrap so that clicking a repair card on Home lands the user
// directly in the conversation — no extra click needed.
export function openLLMPanelIfRepairParam() {
  const rid = currentRepairId();
  const slug = new URLSearchParams(window.location.search).get("device");
  if (rid && slug) {
    // Defer one frame so the DOM is definitely wired (openPanel touches
    // llmInput, llmToggle, etc.) and the status bar has mounted.
    requestAnimationFrame(() => openPanel());
  }
}

// ============ Conversation switcher helpers ============

async function loadConversations() {
  const rid = currentRepairId();
  if (!rid) { conversationsCache = []; renderConvItems(); return; }
  try {
    const res = await fetch(`/pipeline/repairs/${encodeURIComponent(rid)}/conversations`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    conversationsCache = Array.isArray(data.conversations) ? data.conversations : [];
    renderConvItems();
  } catch (err) {
    console.warn("[llm] loadConversations failed", err);
  }
}

async function deleteConvFromPanel(convId) {
  const rid = currentRepairId();
  if (!rid || !convId) return;
  let res;
  try {
    res = await fetch(
      `/pipeline/repairs/${encodeURIComponent(rid)}/conversations/${encodeURIComponent(convId)}`,
      { method: "DELETE" },
    );
  } catch (_) {
    logSys(t('chat.conv.delete_failed'));
    return;
  }
  if (!res.ok) {
    logSys(t('chat.conv.delete_failed'));
    return;
  }
  const wasCurrent = convId === currentConvId;
  conversationsCache = conversationsCache.filter(c => c.id !== convId);

  if (wasCurrent) {
    if (ws && ws.readyState <= 1) {
      try { ws.close(); } catch (_) { /* ignore */ }
    }
    ws = null;
    currentConvId = null;
    const fallback = conversationsCache[0]?.id || "new";
    pendingConvParam = fallback;
    if (el("llmPanel").classList.contains("open")) {
      connect();
    }
  }
  await loadConversations();
}

function renderConvItems() {
  const list = el("llmConvList");
  const label = el("llmConvLabel");
  if (!list || !label) return;
  list.innerHTML = "";
  if (conversationsCache.length === 0) {
    label.textContent = t('chat.conv.label_empty');
    return;
  }
  const activeIdx = Math.max(0, conversationsCache.findIndex(c => c.id === currentConvId));
  label.textContent = t('chat.conv.label_count', { idx: activeIdx + 1, total: conversationsCache.length });
  conversationsCache.forEach((c, idx) => {
    const row = document.createElement("div");
    row.className = "conv-item" + (c.id === currentConvId ? " active" : "");
    row.dataset.convId = c.id;
    const tier = (c.tier || "deep").toLowerCase();
    const fallbackTitle = t('chat.conv.default_title', { idx: idx + 1 });
    const title = escapeHTML((c.title || fallbackTitle).slice(0, 80));
    const cost = Number(c.cost_usd || 0);
    const ago = c.last_turn_at ? humanAgo(c.last_turn_at) : t('chat.conv.ago_unknown');
    const turnsCount = c.turns || 0;
    const turnsLabel = t(turnsCount === 1 ? 'chat.conv.turns_one' : 'chat.conv.turns_many', { n: turnsCount });

    const open = document.createElement("button");
    open.type = "button";
    open.className = "conv-item-open";
    open.innerHTML =
      `<span class="conv-item-head">` +
        `<span class="conv-item-tier t-${tier}">${tier.toUpperCase()}</span>` +
        `<span class="conv-item-title">${title}</span>` +
      `</span>` +
      `<span class="conv-item-meta">` +
        `<span>${escapeHTML(turnsLabel)}</span>` +
        `<span class="conv-item-sep">·</span>` +
        `<span>${fmtUsd(cost)}</span>` +
        `<span class="conv-item-sep">·</span>` +
        `<span>${escapeHTML(ago)}</span>` +
      `</span>`;
    open.addEventListener("click", () => {
      if (c.id === currentConvId) { closeConvPopover(); return; }
      switchConv(c.id);
      closeConvPopover();
    });

    const del = document.createElement("button");
    del.type = "button";
    del.className = "conv-item-delete";
    del.title = t('chat.conv.delete_aria');
    del.setAttribute("aria-label", t('chat.conv.delete_aria'));
    del.innerHTML =
      '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" ' +
      'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M3 4h10M6.5 4V2.5h3V4M5 4l.5 9a1 1 0 001 1h3a1 1 0 001-1l.5-9"/>' +
      '<path d="M7 7v5M9 7v5"/></svg>';
    del.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      if (!confirm(t('chat.conv.delete_confirm'))) return;
      await deleteConvFromPanel(c.id);
    });

    row.appendChild(open);
    row.appendChild(del);
    list.appendChild(row);
  });
}

function humanAgo(iso) {
  try {
    const then = new Date(iso).getTime();
    const diff = Math.max(0, Date.now() - then) / 1000;
    if (diff < 60) return t('chat.conv.ago_seconds', { n: Math.floor(diff) });
    if (diff < 3600) return t('chat.conv.ago_minutes', { n: Math.floor(diff / 60) });
    if (diff < 86400) return t('chat.conv.ago_hours', { n: Math.floor(diff / 3600) });
    return t('chat.conv.ago_days', { n: Math.floor(diff / 86400) });
  } catch { return t('chat.conv.ago_unknown'); }
}

export function switchConv(convIdOrNew) {
  if (convIdOrNew === currentConvId) return;
  logSys(t('chat.conv.switching', { id: convIdOrNew }));
  if (ws && ws.readyState <= 1) {
    try { ws.close(); } catch (_) {}
  }
  ws = null;
  // Route connect() to target the requested conv on reopen.
  pendingConvParam = convIdOrNew;
  // Picking a different conv defers tier choice back to the conv: its
  // active per-tier thread is what the tech actually means to see, even
  // if they had picked a tier earlier in this session. "+ Nouvelle
  // conversation" keeps the explicit tier choice intact (no conv_tier
  // to align to anyway).
  if (convIdOrNew !== "new") {
    userPickedTier = false;
  }
  connect();
}

function openConvPopover() {
  const chip = el("llmConvChip");
  const pop = el("llmConvPopover");
  if (!chip || !pop) return;
  loadConversations(); // refresh on open
  pop.hidden = false;
  chip.setAttribute("aria-expanded", "true");
}
function closeConvPopover() {
  const chip = el("llmConvChip");
  const pop = el("llmConvPopover");
  if (!chip || !pop) return;
  pop.hidden = true;
  chip.setAttribute("aria-expanded", "false");
}
function toggleConvPopover() {
  const pop = el("llmConvPopover");
  if (!pop) return;
  if (pop.hidden) openConvPopover(); else closeConvPopover();
}

// Fetch the chat panel fragment from web/llm_panel.html and inject it
// into #llmRoot. Isolating the markup in its own file keeps parallel
// work on web/index.html from colliding with chat-panel edits.
async function mountPanelFragment() {
  const root = el("llmRoot");
  if (!root) return false;
  if (root.childElementCount > 0) return true; // already mounted (hot-reload guard)
  try {
    const res = await fetch("llm_panel.html", { cache: "no-cache" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    root.innerHTML = await res.text();
    // Translate the freshly-injected markup. Wait for dictionaries first
    // so the initial paint is in the right language; the i18n core falls
    // back to the inline English text if anything is missing.
    if (window.i18n) {
      try {
        await window.i18n.ready;
        window.i18n.applyDom(root);
      } catch (e) { /* keep the inline fallback */ }
    }
    return true;
  } catch (err) {
    console.warn("[llm] failed to mount panel fragment:", err);
    return false;
  }
}

export async function initLLMPanel() {
  const mounted = await mountPanelFragment();
  if (!mounted) return;

  panelMascot = mountMascot(el("llmMascot"), { size: "sm", state: "idle" });

  // Re-render imperative bits (status pill, conv chip) on locale switch.
  // The static markup is handled by `data-i18n` + i18n.applyDom; everything
  // emitted from JS (status text, conv label, replayed log lines) needs an
  // explicit redraw hook.
  if (window.i18n && typeof window.i18n.onChange === "function") {
    window.i18n.onChange(() => {
      // Conv chip (label + items) reads localized strings on every render.
      renderConvItems();
      // Status text — only refresh the current tone's label so we don't
      // overwrite an active "connecting" with a stale "idle".
      const statusEl = el("llmStatus");
      if (statusEl) {
        const txt = el("llmStatusText");
        if (txt && statusEl.classList.contains("connected")) {
          const slug = currentDeviceSlug();
          if (slug) txt.textContent = t('chat.status.connected', { slug, tier: currentTier });
        } else if (txt && !statusEl.classList.contains("connecting") &&
                   !statusEl.classList.contains("closed") &&
                   !statusEl.classList.contains("error")) {
          txt.textContent = t('chat.status.idle');
        }
      }
    });
  }

  el("llmToggle")?.addEventListener("click", togglePanel);
  el("llmClose")?.addEventListener("click", closePanel);
  el("llmStop")?.addEventListener("click", interruptAgent);

  // Tier chip → popover → switchTier.
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
  document.addEventListener("click", (e) => {
    if (tierPopover && !tierPopover.hidden &&
        !tierPopover.contains(e.target) && e.target !== tierChip &&
        !tierChip?.contains(e.target)) {
      closeTierPopover();
    }
  }, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && tierPopover && !tierPopover.hidden) {
      closeTierPopover();
    }
  });

  // Conversation chip + popover.
  const convChip = el("llmConvChip");
  const convPopover = el("llmConvPopover");
  const convNew = el("llmConvNew");
  convChip?.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleConvPopover();
  });
  convNew?.addEventListener("click", () => {
    switchConv("new");
    closeConvPopover();
  });
  document.addEventListener("click", (e) => {
    if (convPopover && !convPopover.hidden &&
        !convPopover.contains(e.target) && e.target !== convChip &&
        !convChip?.contains(e.target)) {
      closeConvPopover();
    }
  }, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && convPopover && !convPopover.hidden) {
      closeConvPopover();
    }
  });

  const input = el("llmInput");
  const form = el("llmForm");

  function autoGrow() {
    if (!input) return;
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
  }
  input?.addEventListener("input", autoGrow);

  input?.addEventListener("keydown", (e) => {
    // Enter (without Shift) → submit. Shift+Enter → newline (default).
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
      logSys(t('chat.send.socket_closed'), true);
      return;
    }
    logMessage("user", text);
    ws.send(JSON.stringify({ type: "message", text }));
    // Immediate feedback: open a fresh turn and show the pending indicator
    // before the backend has produced its first event. Subsequent tool_use /
    // thinking / message events reuse this turn via ensureTurn().
    closeTurn();
    const turn = ensureTurn();
    ensurePendingNode(turn);
    setPanelMascot("thinking");
    if (input) {
      input.value = "";
      autoGrow();
    }
  });

  document.addEventListener("keydown", e => {
    // ⌘J / Ctrl+J toggle
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "j") {
      e.preventDefault();
      togglePanel();
      return;
    }
    // Escape when panel focused: if the agent is live + connected, interrupt
    // it first; second Escape closes the panel.
    if (e.key === "Escape" && document.body.classList.contains("llm-open")) {
      if (document.activeElement && el("llmPanel").contains(document.activeElement)) {
        if (ws && ws.readyState === WebSocket.OPEN) {
          e.preventDefault();
          interruptAgent();
        } else {
          closePanel();
        }
      }
    }
  });

  // --- Files+Vision: upload button + drag-drop + preview toggle --------
  const uploadBtn = el("llmUploadBtn");
  const uploadInput = el("llmUploadInput");
  const dropzone = el("llmDropzone");
  const panelEl = el("llmPanel");
  const previewBtn = el("cameraPreviewBtn");

  function syncPreviewBtn() {
    if (!previewBtn) return;
    const on = isPreviewOpen();
    previewBtn.setAttribute("aria-pressed", on ? "true" : "false");
  }

  previewBtn?.addEventListener("click", async () => {
    if (isPreviewOpen()) {
      closePreview();
      syncPreviewBtn();
      return;
    }
    const id = selectedCameraDeviceId();
    if (!id) {
      logSys(t('chat.preview.select_camera_first'), true);
      return;
    }
    const ok = await openPreview(id, selectedCameraLabel());
    if (!ok) {
      logSys(t('chat.preview.open_failed'), true);
    }
    syncPreviewBtn();
  });
  syncPreviewBtn();

  uploadBtn?.addEventListener("click", () => uploadInput?.click());
  uploadInput?.addEventListener("change", (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = "";  // allow re-uploading the same file
    handleMacroUpload(file);
  });

  if (panelEl && dropzone) {
    let dragDepth = 0;
    panelEl.addEventListener("dragenter", (e) => {
      if (!e.dataTransfer || !e.dataTransfer.types || !e.dataTransfer.types.includes("Files")) {
        return;
      }
      dragDepth += 1;
      dropzone.hidden = false;
    });
    panelEl.addEventListener("dragleave", () => {
      dragDepth -= 1;
      if (dragDepth <= 0) {
        dragDepth = 0;
        dropzone.hidden = true;
      }
    });
    panelEl.addEventListener("dragover", (e) => e.preventDefault());
    panelEl.addEventListener("drop", (e) => {
      e.preventDefault();
      dragDepth = 0;
      dropzone.hidden = true;
      const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      handleMacroUpload(file);
    });
  }
}
