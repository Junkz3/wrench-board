// First-diagnostic coaching — a one-shot guided tour of the repair workspace,
// played the very first time a diagnostic dashboard opens (gated by the
// profile's `state.first_diag_seen` flag — server-side so it's cross-device —
// via onboarding_state.js, with localStorage as a fast pre-gate). Unlike a static card, this walks the
// real surfaces in workflow order with anchored mascot bubbles, and actually
// NAVIGATES the rail views (PCB / schematic / graph) so the technician sees each
// page change in context — closing the "abandoned on the repair screen" gap.
//
// Tone: a small discreet mascot accompanies the bubbles (continuity with the
// landing onboarding), but the dense pro workbench is otherwise untouched. Every
// step is escapable ("Skip the tour") and the whole thing plays once.
//
// The persistent, replayable counterpart is the "?" affordance in the dashboard
// header → openInfoModal("repair") (wired in dashboard.js); this module owns the
// scripted first-run only. Styling reuses .mascot-bubble + .ob-* in
// web/styles/onboarding.css; the mascot host is .ob-coach-mascot.

import { t } from "../../../i18n.js";
import { mountMascot, setMascotState } from "../../../mascot.js";
import { showBubble, hideBubble } from "../../../mascot_bubble.js";
import { repairHash } from "../../../router.js";
import { hasSeenOnboarding, markOnboardingSeen } from "../../../onboarding_state.js";

// On the shipped demo device the tour is "live": it highlights a real component
// on the board and pre-fills the chat with a concrete question, so the value is
// shown rather than described. Gated on the slug — a real device gets the plain
// tour. U14 is the MNT Reform's standby-3V3 regulator (rail LPC_VCC), the first
// part you probe when the board won't power on.
const DEMO_SLUG = "mnt-reform-motherboard";
const DEMO_HIGHLIGHT_REFDES = "U14";

let _running = false;   // re-entry guard (renderRepairDashboard re-runs on nav)
let _forceNext = false; // one-shot bypass: replay the tour once despite the seen flag
let _aborted = false;   // set when the tech hits "Skip the tour"
let _wasDemo = false;   // true while the demo (example) tour is the one playing
let _onDone = null;     // optional completion hook (example-device handoff)
let _mascot = null;     // mounted mascot <svg>
let _mascotHost = null; // fixed-position host element

function _mountMascot() {
  _mascotHost = document.createElement("div");
  _mascotHost.className = "ob-coach-mascot";
  _mascotHost.setAttribute("aria-hidden", "true");
  document.body.appendChild(_mascotHost);
  _mascot = mountMascot(_mascotHost, { size: "sm", state: "idle" });
}

// Switch the workspace to a repair vue and resolve once it's actually on screen.
// Navigation is async (main.js owns hashchange → syncContextFromUrl → navigate),
// so we poll the rail's active state — the reliable "navigate() finished" signal
// — then wait one frame so the target section has painted. Resolves regardless
// after a short deadline so the tour can never wedge on a missed signal.
function _navTo(rid, vue) {
  window.location.hash = repairHash(rid, vue);
  return new Promise((resolve) => {
    const sel = `.rail-btn[data-rail="${vue}"][data-rail-level="repair"]`;
    const tick = (tries) => {
      const btn = document.querySelector(sel);
      if (btn && btn.classList.contains("active")) {
        requestAnimationFrame(() => resolve());
        return;
      }
      if (tries <= 0) { resolve(); return; }
      setTimeout(() => tick(tries - 1), 60);
    };
    tick(25); // ~1.5 s ceiling
  });
}

// Show one bubble and resolve when the tech advances. "Skip the tour" sets
// _aborted and resolves too — the caller checks the flag and tears down.
function _step({ anchor, text, placement = "bottom", mascot = "idle", last = false, doneLabel = null, spotlight = false }) {
  if (_mascot) setMascotState(_mascot, mascot);
  return new Promise((resolve) => {
    showBubble({
      anchor: typeof anchor === "string" ? document.querySelector(anchor) : anchor,
      placement,
      text,
      spotlight,
      nextLabel: last ? (doneLabel || t("onboarding.coach.done")) : t("onboarding.next"),
      skipLabel: t("onboarding.coach.skip"),
      next: () => resolve(),
      skip: () => { _aborted = true; resolve(); },
    });
  });
}

// Arm a one-shot replay of the workspace tour, ignoring the persisted "seen"
// flag for the next dashboard render. Used by the landing onboarding's "see the
// example" handoff so the demo tour plays even for a tech who already saw it.
export function forceNextDiagCoaching() {
  _forceNext = true;
}

export async function maybeShowFirstDiagCoaching(rid, { onDone = null, slug = null } = {}) {
  if (_running) return;
  if (_forceNext) {
    _forceNext = false; // consume the one-shot bypass
  } else if (hasSeenOnboarding("first_diag_seen")) {
    return; // already toured (server truth; localStorage fallback pre-hydration)
  }

  const isDemo = slug === DEMO_SLUG; // live tour (highlight + prefilled chat)
  _wasDemo = isDemo;
  _onDone = onDone; // forwarded by dashboard.js from the example-device handoff
  _running = true;
  _aborted = false;
  _mountMascot();

  const cancelled = () => _aborted || !document.body.contains(_mascotHost);

  // ── Phase 1: the diagnostic dashboard (current view) ──────────────────────
  await _step({ anchor: ".rd-head", text: t("onboarding.coach.session"), placement: "bottom", mascot: "success", spotlight: true });
  if (cancelled()) return finishFirstDiagCoaching();
  await _step({ anchor: "#rdCap", text: t("onboarding.coach.cap"), placement: "bottom", mascot: "scanning", spotlight: true });
  if (cancelled()) return finishFirstDiagCoaching();
  await _step({ anchor: "#rdCards", text: t("onboarding.coach.cards"), placement: "top", mascot: "idle", spotlight: true });
  if (cancelled()) return finishFirstDiagCoaching();

  // ── Phase 2: walk the rail views (the page actually changes) ──────────────
  // Anchored to each rail button (far left) so the arrow ties "this button →
  // this page". Skipped wholesale when there's no repair id to navigate with.
  if (rid) {
    const railSel = (vue) => `.rail-btn[data-rail="${vue}"][data-rail-level="repair"]`;
    // Neither the schematic nor the graph exposes a programmatic select API — both
    // are D3 with mouse handlers — so the demo drives them by dispatching a real
    // click on the node element (see the views' .on("click") handlers).
    const synthClick = (elOrSel) => {
      const el = typeof elOrSel === "string" ? document.querySelector(elOrSel) : elOrSel;
      if (el) el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
      return !!el;
    };
    const waitForEl = (sel, ms = 4000) => new Promise((res) => {
      const t0 = Date.now();
      const tick = () => {
        const e = document.querySelector(sel);
        if (e) return res(e);
        if (Date.now() - t0 > ms) return res(null);
        setTimeout(tick, 80);
      };
      tick();
    });

    // ── PCB — light up a real component so the bubble points at something concrete.
    await _navTo(rid, "pcb");
    if (cancelled()) return finishFirstDiagCoaching();
    if (isDemo) {
      try { window.Boardview?.apply?.({ type: "boardview.highlight", refdes: DEMO_HIGHLIGHT_REFDES }); } catch { /* best effort */ }
    }
    await _step({ anchor: railSel("pcb"), text: t(isDemo ? "onboarding.coach.demo_pcb" : "onboarding.coach.pcb"), placement: "right", mascot: "idle" });
    if (cancelled()) return finishFirstDiagCoaching();

    // ── Schematic — on the demo, click the LPC_VCC rail (fallback U14) so the
    // tech sees the schematic isolate the rail + its cascade, not just hears about it.
    await _navTo(rid, "schematic");
    if (cancelled()) return finishFirstDiagCoaching();
    if (isDemo) {
      await waitForEl("#schLayerNodes g.sch-node");
      synthClick('g.sch-node[data-rail="LPC_VCC"]') || synthClick('g.sch-node[data-refdes="U14"]');
    }
    await _step({ anchor: railSel("schematic"), text: t(isDemo ? "onboarding.coach.demo_schematic" : "onboarding.coach.schematic"), placement: "right", mascot: "idle" });
    if (cancelled()) return finishFirstDiagCoaching();

    // ── Graph (memory) — on the demo, click a node so the right-hand detail panel
    // opens (the "how it reasons" panel), then flip to the Raw tab to show the
    // underlying memory, then restore Visual.
    await _navTo(rid, "graph");
    if (cancelled()) return finishFirstDiagCoaching();
    if (isDemo) {
      await waitForEl("#layerNodes g.node");
      synthClick(
        document.querySelector("#layerNodes g.node.type-symptom") ||
        document.querySelector("#layerNodes g.node.type-action") ||
        document.querySelector("#layerNodes g.node"),
      );
      await _step({ anchor: railSel("graph"), text: t("onboarding.coach.demo_graph"), placement: "right", mascot: "scanning" });
      if (cancelled()) return finishFirstDiagCoaching();
      const rawBtn = document.querySelector('.view-toggle-btn[data-view="md"]');
      if (rawBtn) {
        synthClick(rawBtn);                                               // flip to Raw
        await _step({ anchor: railSel("graph"), text: t("onboarding.coach.demo_graph_raw"), placement: "right", mascot: "idle" });
        synthClick('.view-toggle-btn[data-view="graph"]');               // restore Visual
      }
      // #inspector is a global <aside> (sibling of #canvas) — left .open it would
      // persist over every other view. Close it before leaving the graph.
      synthClick("#inspectorClose");
      if (cancelled()) return finishFirstDiagCoaching();
    } else {
      await _step({ anchor: railSel("graph"), text: t("onboarding.coach.graph"), placement: "right", mascot: "scanning" });
      if (cancelled()) return finishFirstDiagCoaching();
    }

    // Back to the diagnostic dashboard for the closing step.
    await _navTo(rid, "diagnostic");
    if (cancelled()) return finishFirstDiagCoaching();
  }

  // ── Phase 3: how to actually diagnose ─────────────────────────────────────
  // On the demo, open the chat and pre-fill a concrete question so the tech can
  // send it and watch the agent reason on this real, analyzed board — the value
  // shown, not just described. (Sending is their choice; the tour never sends.)
  if (isDemo) {
    // Sit on the PCB view so the agent's board annotations are visible, then
    // replay TWO real recorded Opus (deep) sessions in small NARRATED BEATS,
    // each paused on an explainer bubble advanced with "Next". Free — no live
    // LLM call. CONV 1 = the device's power-up sequence drawn on the board;
    // CONV 2 (after a genuine "new conversation" gesture) = a "dead board"
    // diagnostic that runs a measurement protocol and turns out the supply is
    // actually healthy. Fixtures are per-locale (fr/en); fall back to fr.
    await _navTo(rid, "pcb");
    if (cancelled()) return finishFirstDiagCoaching();
    await _step({ anchor: "#llmToggle", text: t("onboarding.coach.demo_chat"), placement: "left", mascot: "idle", doneLabel: t("onboarding.coach.demo_play") });
    if (cancelled()) return finishFirstDiagCoaching();
    hideBubble();
    const { loadRecvFrames, beginDemoReplay, playFrames, endDemoReplay, waitForBoard } = await import("./demoReplay.js");
    // The conversation switcher is driven offline (no backend) straight on the
    // real chat-panel DOM, so the tech sees the actual gesture, not a caption.
    const { handleDiagnosticFrame, replaySeedConversations, replayOpenConvPopover, replayCloseConvPopover } = await import("../../../llm.js");
    const loc = (window.i18n && window.i18n.locale) || "fr";
    const grab = async (name) => {
      let f = await loadRecvFrames(`/demos/${name}.${loc}.json`);
      if (!f.length && loc !== "fr") f = await loadRecvFrames(`/demos/${name}.fr.json`);
      return f;
    };
    const conv1 = await grab("hero-conv1");
    const conv2 = await grab("hero-conv2");
    if (conv1.length && conv2.length) {
      await waitForBoard(); // arrows + annotations project through the board camera; wait for it

      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      const cue = (sel, on) => { const e = document.querySelector(sel); if (e) e.classList.toggle("wb-demo-cue", on); };
      const clearCues = () => { cue("#llmConvChip", false); cue("#llmConvNew", false); };
      const bail = () => { clearCues(); try { replayCloseConvPopover(); } catch { /* not open */ } endDemoReplay(); finishFirstDiagCoaching(); };
      // Cosmetic conversation row the offline switcher renders (mirrors the
      // live `conversations` payload shape: id/title/tier/turns/cost/last seen).
      const convRow = { id: "demo-conv-1", title: t("onboarding.coach.demo_conv1_title"), tier: "deep", turns: 1, cost_usd: 0.86, last_turn_at: new Date(Date.now() - 90_000).toISOString() };

      // ── CONV 1: the power-up sequence, drawn on the board ──
      beginDemoReplay({ userText: t("onboarding.coach.demo_q1") });
      await playFrames(conv1.slice(0, 28), { gapCapMs: 550 });   // reads schematic graph + components
      if (cancelled()) return bail();
      await _step({ anchor: _mascotHost, text: t("onboarding.coach.demo_c1_explore"), placement: "top", mascot: "scanning" });
      if (cancelled()) return bail();
      await playFrames(conv1.slice(28, 55), { gapCapMs: 320 });  // draws the cascade (highlights, annotations, arrows)
      if (cancelled()) return bail();
      await _step({ anchor: _mascotHost, text: t("onboarding.coach.demo_c1_board"), placement: "top", mascot: "working" });
      if (cancelled()) return bail();
      await playFrames(conv1.slice(55, 58), { gapCapMs: 400 });  // the phase-by-phase explanation
      if (cancelled()) return bail();
      await _step({ anchor: _mascotHost, text: t("onboarding.coach.demo_c1_done"), placement: "top", mascot: "success" });
      if (cancelled()) return bail();

      // ── INTERLUDE: a real "new conversation" gesture on the live UI (offline) ──
      // The explainer bubble (z-index 1600) sits ABOVE the conversation popover
      // (z-index 40), so it MUST be dismissed before the popover opens — else it
      // hides the very gesture we're showing. Explain first (popover closed),
      // then play the gesture silently — it's self-evident once seen.
      replaySeedConversations([{ ...convRow, active: true }]); // chip now reads CONV 1/1
      await _step({ anchor: "#llmConvChip", text: t("onboarding.coach.demo_switch"), placement: "left", mascot: "idle" });
      if (cancelled()) return bail();
      hideBubble();
      cue("#llmConvChip", true); await sleep(550);
      if (cancelled()) return bail();
      replayOpenConvPopover();                               // the real popover opens — the seeded list shows
      cue("#llmConvChip", false); await sleep(850);
      if (cancelled()) return bail();
      cue("#llmConvNew", true); await sleep(800);            // spotlight the "+ New conversation" control
      if (cancelled()) return bail();
      replaySeedConversations([
        { id: "demo-conv-2", title: t("onboarding.coach.demo_new_conv_title"), tier: "deep", turns: 0, cost_usd: 0, last_turn_at: new Date().toISOString(), active: true },
        convRow,
      ]);
      await sleep(900); cue("#llmConvNew", false);           // hold so the refreshed list reads
      replayCloseConvPopover(); await sleep(400);
      if (cancelled()) return bail();

      // The fresh conversation must start CLEAN. The replay SKIP-lists
      // protocol_cleared and beginDemoReplay only wipes the CHAT LOG, so CONV 1's
      // board overlays + protocol wizard would otherwise bleed into CONV 2.
      // reset() clears the board overlays; a direct protocol_cleared nulls
      // state.proto (also no-ops any trailing protocol_updated — no zombie wizard).
      try { window.Boardview?.reset?.(); } catch { /* board not ready */ }
      handleDiagnosticFrame({ type: "protocol_cleared" });
      await sleep(450);                                      // let the board visibly empty before the new question

      // ── CONV 2: a "dead board" diagnostic — protocol, measurements, twist ──
      // Sliced into SMALL beats (one message or two per beat) so the tech can
      // actually follow it — the agent is otherwise a long monologue. Each beat
      // pauses on a bubble before the next chunk plays.
      beginDemoReplay({ userText: t("onboarding.coach.demo_q2") });
      await playFrames(conv2.slice(0, 29), { gapCapMs: 550 });   // explores, validates refdes, lays out the reasoning
      if (cancelled()) return bail();
      await _step({ anchor: _mascotHost, text: t("onboarding.coach.demo_c2_diag"), placement: "top", mascot: "working" });
      if (cancelled()) return bail();
      await playFrames(conv2.slice(29, 53), { gapCapMs: 450 });  // draws the chain + proposes a 6-step protocol
      if (cancelled()) return bail();
      await _step({ anchor: _mascotHost, text: t("onboarding.coach.demo_c2_protocol"), placement: "top", mascot: "working" });
      if (cancelled()) return bail();
      await playFrames(conv2.slice(53, 66), { gapCapMs: 500 });  // cold tests: F1 continuity + VIN→GND short
      if (cancelled()) return bail();
      await _step({ anchor: _mascotHost, text: t("onboarding.coach.demo_c2_cold"), placement: "top", mascot: "scanning" });
      if (cancelled()) return bail();
      await playFrames(conv2.slice(66, 75), { gapCapMs: 500 });  // powered: 24 V present at the input
      if (cancelled()) return bail();
      await _step({ anchor: _mascotHost, text: t("onboarding.coach.demo_c2_power"), placement: "top", mascot: "scanning" });
      if (cancelled()) return bail();
      await playFrames(conv2.slice(75, 95), { gapCapMs: 450 });  // VIN healthy → pivots the suspect to U14
      if (cancelled()) return bail();
      await _step({ anchor: _mascotHost, text: t("onboarding.coach.demo_c2_u14"), placement: "top", mascot: "working" });
      if (cancelled()) return bail();
      await playFrames(conv2.slice(95, 120), { gapCapMs: 450 }); // LPC_VCC = 3.3 V → the twist
      if (cancelled()) return bail();
      await _step({ anchor: _mascotHost, text: t("onboarding.coach.demo_c2_verdict"), placement: "top", mascot: "success", last: true, doneLabel: t("onboarding.coach.demo_done") });
      clearCues();
      endDemoReplay();
    }
  } else {
    await _step({ anchor: "#llmToggle", text: t("onboarding.coach.chat"), placement: "bottom", mascot: "working", last: true });
  }
  finishFirstDiagCoaching();
}

// True while the first-run tour is playing OR still owed (flag unset). Callers
// use this to hold back competing surfaces during the tour — notably the chat
// panel auto-open, so the early bubbles (header / capabilities / cards) aren't
// covered. The tour's closing step points the tech at the chat toggle to open
// it themselves.
export function firstDiagTourPending() {
  if (_running || _forceNext) return true;
  return !hasSeenOnboarding("first_diag_seen");
}

export function finishFirstDiagCoaching() {
  hideBubble();
  // Safety net: if the tech skips mid-tour, a detail panel opened by the demo's
  // synthetic clicks (graph #inspector is a global aside; schematic #schInspector)
  // would otherwise stay open across views. Close both.
  try {
    document.getElementById("inspector")?.classList.remove("open");
    document.getElementById("schInspector")?.classList.remove("open");
  } catch { /* best effort */ }
  if (_mascotHost) { _mascotHost.remove(); _mascotHost = null; _mascot = null; }
  markOnboardingSeen("first_diag_seen"); // server (cross-device) + localStorage cache
  _running = false;
  // Fire the completion hook last, after teardown + flag, so the handoff caller
  // (landing onboarding, via window.__wbExampleTourOnDone) resumes cleanly.
  const cb = _onDone; _onDone = null;
  const wasDemo = _wasDemo; _wasDemo = false;
  if (typeof cb === "function") {
    cb();
  } else if (wasDemo) {
    // "Ok, my turn" / skip / direct-open of the example with no landing handoff:
    // leave the read-only example workspace instead of stranding the tech on it.
    try { window.location.hash = "#landing"; } catch { /* best effort */ }
  }
}
