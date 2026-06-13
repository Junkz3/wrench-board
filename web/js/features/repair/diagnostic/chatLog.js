// Diagnostic chat — chat-log DOM rendering + turn-block state machine (Phase
// D.6 extraction from llm.js). Owns every node that lands in #llmLog: plain
// message/system rows, the resume/context-lost cards, protocol system chips and
// inline step cards, and the per-turn rail (thinking / tool steps / message /
// cost foot). The WS message dispatcher in llm.js drives this module — it opens
// a turn via ensureTurn(), feeds steps/messages into it, and closes it.
//
// `currentTurn` is the single piece of mutable state here: the DOM node that
// receives the next incoming thinking / tool_use / message event. llm.js no
// longer holds it directly — it reads it back via getCurrentTurn() (cost foot)
// and resets it via closeTurn() on (re)connect / replay-end / terminate.
//
// `t` resolves through the global window.t (i18n.js, a classic non-ESM script)
// at CALL time so strings re-render on locale switch — mirrors the toolPhrases
// convention. window.marked / window.DOMPurify are global CDN scripts, read
// bare. escapeHTML guards every interpolated value.

import { escapeHtml as escapeHTML } from '../../../shared/dom.js';
import { renderAgentMarkup } from './chatMarkup.js';
import { fmtUsd } from './costDisplay.js';
// Same ?v=quest4 query main.js / llm.js use — ESM keys modules by URL, so a
// bare './protocol.js' would create a second instance missing main.js's
// Protocol.init() wiring.
import * as Protocol from '../../../protocol.js?v=quest4';

const t = (key, params) => (window.t ? window.t(key, params) : key);
const el = (id) => document.getElementById(id);

// Turn-block state machine.
// currentTurn is the DOM node receiving the next incoming thinking / tool_use /
// message event. A user.message closes it (set to null). An assistant.message
// that arrives when currentTurn already has a .turn-message opens a new turn
// (agent emitted two messages back-to-back without a user interjection).
let currentTurn = null;

export function logRow(cls, innerHTML) {
  const log = el("llmLog");
  const row = document.createElement("div");
  row.className = cls;
  row.innerHTML = innerHTML;
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
  return row;
}

export function logMessage(role, text, isReplay = false) {
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
export function renderContextLost(payload) {
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
export function renderResumeSummary(payload) {
  const summary = payload?.summary || "";
  const tokIn = payload?.tokens_in ?? "n/a";
  const tokOut = payload?.tokens_out ?? "n/a";
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

export function logSys(text, isErr = false) {
  logRow(isErr ? "sys err" : "sys", escapeHTML(text));
}

// Append a small terminal-state chip to the chat log (abandoned / completed).
// Distinct visual from agent/user messages — centered, muted, mono — so the
// tech can scroll back through the conv and see when a sequence was dropped
// or finished, and why. Idempotent on protocol_id+kind to avoid duplicates
// when the same event arrives twice (replay race, double-emit, etc.).
export function appendProtocolSystemEvent(kind, { protocol_id, reason } = {}) {
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
export function renderInlineProtocolCard(_ev) {
  const proto = Protocol.getProtocol?.();
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
    `<div class="protocol-step-target">${escapeHTML(active.target || active.test_point || "…")}</div>` +
    `<p class="protocol-step-instruction">${escapeHTML(active.instruction)}</p>` +
    `<p class="protocol-step-rationale">${escapeHTML(active.rationale)}</p>`;
  if (Protocol.buildStepForm) {
    card.appendChild(Protocol.buildStepForm(active));
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

export function ensureTurn() {
  if (!currentTurn) currentTurn = createTurn();
  return currentTurn;
}

export function closeTurn() {
  currentTurn = null;
}

// The turn currently receiving events, or null. llm.js reads this to attach
// the cost foot to the live turn after a turn_cost event.
export function getCurrentTurn() {
  return currentTurn;
}

export function ensurePendingNode(turn, label) {
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

// Append a .step into the turn's rail. kind ∈ {"thinking","mb","bv","mem",...}.
// phraseHTML is trusted HTML (callers escape user-provided fragments
// themselves — currently only tool names + refdes which are validated).
//
// `group` (optional) = { key, item } enables coalescing: when the agent fires
// the SAME tool several times back-to-back (five component lookups, a burst of
// memory reads, repeated globs) we don't stack five rows — we fold each new
// occurrence's target chip into the previous step's inline list and dim the
// whole run. A lone call (no following same-key call) renders normally. Returns
// the live step node either way (the existing one when merged) so the caller's
// addExpandToStep() attaches/accumulates the payload onto it.
export function appendStep(turn, kind, phraseHTML, group = null) {
  clearPendingNode(turn);
  const rail = turn.querySelector(".turn-rail");
  if (group && group.key) {
    const steps = rail.querySelectorAll(".step:not(.pending)");
    const last = steps[steps.length - 1];
    if (last && last.dataset.groupKey === group.key) {
      mergeIntoGroup(last, group);
      ensurePendingNode(turn);
      el("llmLog").scrollTop = el("llmLog").scrollHeight;
      return last;
    }
  }
  const step = document.createElement("div");
  step.className = `step ${kind}`;
  step.innerHTML = `<span class="node"></span><span class="step-phrase">${phraseHTML}</span>`;
  if (group && group.key) {
    step.dataset.groupKey = group.key;
    step.dataset.groupCount = "1";
  }
  rail.appendChild(step);
  ensurePendingNode(turn);
  el("llmLog").scrollTop = el("llmLog").scrollHeight;
  return step;
}

// Fold one more same-tool occurrence into an existing step. With a target chip
// (refdes / path / pattern) it joins a comma-separated inline list; without one
// (variable-less ops) it just bumps a small ×N counter. `.grouped` dims the run
// and lets the phrase wrap (see llm.css) so the full list stays readable.
function mergeIntoGroup(step, group) {
  const count = (Number(step.dataset.groupCount) || 1) + 1;
  step.dataset.groupCount = String(count);
  step.classList.add("grouped");
  const phrase = step.querySelector(".step-phrase");
  if (!phrase) return;
  if (group.item) {
    let items = phrase.querySelector(".step-items");
    if (!items) {
      items = document.createElement("span");
      items.className = "step-items";
      phrase.appendChild(items);
    }
    // group.item is trusted HTML built by toolPhrases.js (already escaped).
    items.insertAdjacentHTML("beforeend", `<span class="step-item-sep">, </span>${group.item}`);
  } else {
    let badge = phrase.querySelector(".step-count");
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "step-count";
      phrase.appendChild(badge);
    }
    badge.textContent = ` ×${count}`;
  }
}

export function addExpandToStep(step, payloadObj) {
  // Grouped steps accumulate every occurrence's payload behind ONE chevron:
  // when appendStep() merges a run, it returns the same node here repeatedly, so
  // we push onto step._payloads and re-render rather than stacking buttons.
  if (!step._payloads) step._payloads = [];
  step._payloads.push(payloadObj);

  let btn = step.querySelector(":scope > .step-expand");
  let pre = step.querySelector(":scope > .step-payload");
  if (!btn) {
    btn = document.createElement("button");
    btn.type = "button";
    btn.className = "step-expand";
    btn.setAttribute("aria-expanded", "false");
    btn.title = t('chat.step.expand_title');
    btn.innerHTML =
      '<svg class="chevron" width="10" height="10" viewBox="0 0 24 24" fill="none" ' +
      'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
      '<polyline points="9 6 15 12 9 18"/></svg>';
    step.appendChild(btn);

    pre = document.createElement("pre");
    pre.className = "step-payload";
    step.appendChild(pre);

    btn.addEventListener("click", () => {
      const expanded = step.classList.toggle("expanded");
      btn.setAttribute("aria-expanded", expanded ? "true" : "false");
    });
  }

  const payloads = step._payloads;
  if (payloads.length === 1) {
    const p = payloads[0];
    const hasResult = p && typeof p === "object" && "result" in p;
    pre.textContent = hasResult
      ? JSON.stringify(p, null, 2)
      : JSON.stringify(p, null, 2) + "\n\n" + t('chat.step.no_result');
  } else {
    // A coalesced run: dump the per-occurrence payloads as an ordered array.
    pre.textContent = JSON.stringify(payloads, null, 2);
  }
}

export function appendTurnMessage(turn, text) {
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

export function appendTurnFoot(turn, payload) {
  // Terminal signal for this turn — clear transient indicators.
  clearPendingNode(turn);
  let foot = turn.querySelector(".turn-foot");
  if (!foot) {
    foot = document.createElement("div");
    foot.className = "turn-foot";
    turn.appendChild(foot);
  }
  const priceLabel = payload.priced ? fmtUsd(payload.cost_usd) : "n/a";
  const modelLabel = payload.model ? payload.model.replace("claude-", "") : "?";
  const tokensLabel = `${(payload.input_tokens || 0) + (payload.cache_read_input_tokens || 0) + (payload.cache_creation_input_tokens || 0)}→${payload.output_tokens || 0} tok`;
  foot.innerHTML =
    `<span class="foot-cost">${priceLabel}</span>` +
    `<span class="foot-sep">·</span>` +
    `<span class="foot-tokens">${tokensLabel}</span>` +
    `<span class="foot-sep">·</span>` +
    `<span class="foot-model">${escapeHTML(modelLabel)}</span>`;
}
