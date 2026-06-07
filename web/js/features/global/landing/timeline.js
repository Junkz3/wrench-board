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
  li.innerHTML = `<span class="landing-phase-dot"></span><span class="landing-phase-label" data-i18n="${labelKey}">${_escapeHtml(tFn(labelKey))}</span><div class="landing-phase-narration"></div>`;
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

export function setPhaseNarration(phase, text) {
  const li = document.querySelector(`.landing-phase[data-phase="${phase}"]`);
  if (!li) return;
  const slot = li.querySelector(".landing-phase-narration");
  if (!slot) return;
  slot.textContent = text;
  li.classList.add("has-narration");
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
    li.classList.remove("is-running", "is-done", "is-failed", "has-narration");
    if (p === "mapper") li.hidden = true;
    const slot = li.querySelector(".landing-phase-narration");
    if (slot) slot.textContent = "";
  });
  // Drop any dynamically-injected phase rows so a fresh run starts clean.
  document.querySelectorAll('#landingPhaseList .landing-phase').forEach((li) => {
    if (li.dataset.phase in LANDING_DYNAMIC_PHASES) li.remove();
  });
}
