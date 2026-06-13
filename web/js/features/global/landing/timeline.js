// Landing pipeline timeline — the narrated phase strip the technician watches
// while the knowledge factory builds a fresh device pack (Phase D.8 extraction
// from landing/index.js). Pure DOM rendering over the #landingTimeline /
// #landingPhaseList markup: show/hide, the elapsed-time ticker, per-phase
// state + narration, the dynamic schematic/device-kind rows, and a clean
// reset of the phase rows between runs.
//
// No orchestration state here — index.js owns the WS subscription, the
// device-kind pause panel, and `_landingPaused`; it drives this module from its
// progress-event handler. `window.t` (i18n.js) is read at call time so strings
// re-render on locale switch; _escapeHtml guards interpolated label keys.

import { escapeHtml as _escapeHtml } from '../../../shared/dom.js';

// The fixed 5-phase factory pipeline, in render order. Exported because
// index.js's progress handler + cached-timeline player iterate it.
export const PHASE_ORDER = ["scout", "registry", "mapper", "writers", "audit"];

// Dynamic phases injected only when the orchestrator emits them (schematic
// upload path). Mapped to their i18n label keys. Not in PHASE_ORDER — they
// are rendered on demand from phase_started events. Exported because index.js's
// handler tests `phase in LANDING_DYNAMIC_PHASES`.
export const LANDING_DYNAMIC_PHASES = {
  schematic_ingest: "landing.timeline.phase_schematic_ingest",
  device_kind: "landing.timeline.phase_device_kind",
};

// Wall-clock start of the in-flight run, for the elapsed-time ticker.
let pipelineStartedAt = 0;
// ETA ticker handle — module-local (was window.__landingEtaTimer; dropped the
// global in D.8 since landing is the only owner). One interval at a time.
let _etaTimer = null;

export function showTimeline() {
  const tl = document.getElementById("landingTimeline");
  if (tl) tl.hidden = false;
  initTimelineToggles();
  pipelineStartedAt = Date.now();
  startEtaTicker();
}

function startEtaTicker() {
  const eta = document.getElementById("landingTimelineEta");
  if (!eta) return;
  if (_etaTimer) clearInterval(_etaTimer);
  const t = window.t || ((k) => k);
  const tick = () => {
    const elapsed = Math.max(0, (Date.now() - pipelineStartedAt) / 1000);
    eta.textContent = t("landing.timeline.elapsed", { n: elapsed.toFixed(0) });
  };
  tick();
  _etaTimer = setInterval(tick, 250);
}

export function stopEtaTicker() {
  if (_etaTimer) {
    clearInterval(_etaTimer);
    _etaTimer = null;
  }
}

export function ensureLandingPhase(phaseKey) {
  const labelKey = LANDING_DYNAMIC_PHASES[phaseKey];
  if (!labelKey) return;
  const list = document.getElementById("landingPhaseList");
  if (!list || list.querySelector(`.landing-phase[data-phase="${phaseKey}"]`)) return;
  const tFn = window.t || ((k) => k);
  const li = document.createElement("li");
  li.className = "landing-phase";
  li.dataset.phase = phaseKey;
  li.innerHTML =
    `<span class="landing-phase-dot"></span>` +
    `<div class="landing-phase-head">` +
      `<span class="landing-phase-label" data-i18n="${labelKey}">${_escapeHtml(tFn(labelKey))}</span>` +
      `<span class="landing-phase-live" data-role="live"></span>` +
      `<button type="button" class="landing-phase-detail" data-role="detail-toggle" data-i18n="landing.timeline.detail" aria-expanded="false" hidden>${_escapeHtml(tFn("landing.timeline.detail"))}</button>` +
    `</div>` +
    `<ul class="landing-phase-log" data-role="log" hidden></ul>`;
  const scout = list.querySelector('.landing-phase[data-phase="scout"]');
  list.insertBefore(li, scout);  // null scout → appended (safe)
}

export function setPhaseState(phase, state) {
  // state ∈ "running" | "done" | "failed"
  const li = document.querySelector(`.landing-phase[data-phase="${phase}"]`);
  if (!li) return;
  li.hidden = false;  // mapper starts hidden until a phase_started arrives
  li.classList.remove("is-running", "is-done", "is-failed");
  if (state === "running") li.classList.add("is-running");
  if (state === "done") li.classList.add("is-done");
  if (state === "failed") li.classList.add("is-failed");
}

// A live sub-step landed: refresh the always-visible compact line (latest
// step) AND append to the per-phase detail log (revealed by the "détail"
// toggle). Both fed by the orchestrator's `phase_step` events. `text` is
// pre-localized by the caller (index.js phaseStepText).
export function setPhaseStep(phase, text) {
  const li = document.querySelector(`.landing-phase[data-phase="${phase}"]`);
  if (!li) return;
  const live = li.querySelector('[data-role="live"]');
  if (live) live.textContent = text;
  const log = li.querySelector('[data-role="log"]');
  if (log) {
    const item = document.createElement("li");
    item.textContent = text;
    log.appendChild(item);
  }
  // The detail toggle only earns its place once the phase has ≥1 sub-step
  // (registry / mapper emit none, so theirs stays hidden).
  const btn = li.querySelector('[data-role="detail-toggle"]');
  if (btn) btn.hidden = false;
  li.classList.add("has-steps");
}

// One delegated click listener handles every phase's "détail" toggle (static
// + dynamically-injected rows). Idempotent — guarded by a dataset flag so
// repeated showTimeline() calls don't stack listeners.
export function initTimelineToggles() {
  const list = document.getElementById("landingPhaseList");
  if (!list || list.dataset.toggleWired) return;
  list.dataset.toggleWired = "1";
  list.addEventListener("click", (e) => {
    const btn = e.target.closest('[data-role="detail-toggle"]');
    if (!btn) return;
    const li = btn.closest(".landing-phase");
    const log = li && li.querySelector('[data-role="log"]');
    if (!log) return;
    const open = li.classList.toggle("is-open");
    log.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    // Keep data-i18n in sync so a locale switch re-renders the right label.
    const key = open ? "landing.timeline.detail_hide" : "landing.timeline.detail";
    btn.setAttribute("data-i18n", key);
    btn.textContent = (window.t || ((k) => k))(key);
  });
}

export function setTimelineTitle(text) {
  const t = document.getElementById("landingTimelineTitle");
  if (t) t.textContent = text;
}

// Reset the phase ROWS for a fresh run: clear the fixed phases' state/narration
// (re-hiding mapper) and drop any dynamically-injected rows. Orchestration
// state (_landingPaused, the device-kind panel) stays in index.js, which calls
// this from its own resetTimeline() wrapper.
export function resetTimelineRows() {
  PHASE_ORDER.forEach((p) => {
    const li = document.querySelector(`.landing-phase[data-phase="${p}"]`);
    if (!li) return;
    li.classList.remove("is-running", "is-done", "is-failed", "has-steps", "is-open");
    if (p === "mapper") li.hidden = true;
    const live = li.querySelector('[data-role="live"]');
    if (live) live.textContent = "";
    const log = li.querySelector('[data-role="log"]');
    if (log) { log.innerHTML = ""; log.hidden = true; }
    const btn = li.querySelector('[data-role="detail-toggle"]');
    if (btn) {
      btn.hidden = true;
      btn.setAttribute("aria-expanded", "false");
      btn.setAttribute("data-i18n", "landing.timeline.detail");
      btn.textContent = (window.t || ((k) => k))("landing.timeline.detail");
    }
  });
  // Drop any dynamically-injected phase rows so a fresh run starts clean.
  document.querySelectorAll('#landingPhaseList .landing-phase').forEach((li) => {
    if (li.dataset.phase in LANDING_DYNAMIC_PHASES) li.remove();
  });
}
