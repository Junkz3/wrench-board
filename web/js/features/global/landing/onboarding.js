// First-run guided onboarding — plays once on the landing cockpit, escapable
// at every step. A blocking welcome modal hands off to the mascot, which walks
// the technician through: profile capture → product concept → first diagnostic,
// revealing the hero zones one at a time. "Skip" anywhere reveals everything
// and ends the run.
//
// State model (see spec 2026-06-01-onboarding-and-home-ux-design):
//   - The scripted run is gated by the `wb_onboarding_seen` localStorage flag,
//     and only triggers when the profile is incomplete OR there are 0 repairs.
//   - Persistent derived nudges (pill pulse, empty sidebar) live elsewhere and
//     are not this module's concern.
//
// Reveal mechanism: `.ob-running` on #landing-overlay dims every [data-ob-reveal]
// target; the orchestrator adds `.is-revealed` per step. finish() drops
// `.ob-running` so the cockpit returns to its normal (fully visible) state.

import { mountMascot } from "../../../mascot.js";
import { showBubble, hideBubble } from "../../../mascot_bubble.js";
import { t } from "../../../i18n.js";
import { apiGet } from "../../../shared/api.js";
import { hasSeenOnboarding, markOnboardingSeen } from "../../../onboarding_state.js";
import { forceNextDiagCoaching } from "../../repair/diagnostic/coaching.js";
import { openProfileWizard } from "./profile_modal.js";
import { hideUploads } from "../../../cloud_hints.js";

const FLAG = "wb_onboarding_seen";
const EXAMPLE_REPAIR_ID = "example-mnt-reform";

let _ctl = {};      // { setMascotState }
let _env = null;    // cached profile envelope
let _host = null;   // injected modal/panel host

function _overlay() { return document.getElementById("landing-overlay"); }
function _mascotState(s) { if (typeof _ctl.setMascotState === "function") _ctl.setMascotState(s); }

function _reveal(selector) {
  document.querySelectorAll(selector).forEach((el) => el.classList.add("is-revealed"));
}

function _ensureHost() {
  if (_host && document.body.contains(_host)) return _host;
  _host = document.createElement("div");
  _host.className = "ob-host";
  document.body.appendChild(_host);
  return _host;
}

function _clearHost() {
  if (_host) { _host.remove(); _host = null; }
}

// Synchronous pre-gate: dim the hero from the first paint of the landing so a
// first-run never flashes the full cockpit before the staged reveal. Cheap
// localStorage check only; the async maybeStartOnboarding() decides for real
// and un-gates if it turns out not to run.
export function preGateOnboarding() {
  if (localStorage.getItem(FLAG)) return;
  _overlay()?.classList.add("ob-running");
}

export async function maybeStartOnboarding(ctl) {
  _ctl = ctl || {};
  // Always load profile + repairs first: the profile gate is INDEPENDENT of the
  // tour flag (a missing name must prompt even on a returning device / after the
  // tour was skipped), so we can't early-return on `onboarding_seen` here.
  let repairsCount = 0;
  try {
    const res = await fetch("/pipeline/repairs");
    if (res.ok) {
      const j = await res.json();
      repairsCount = Array.isArray(j) ? j.length : 0;
    }
  } catch (err) {
    console.warn("[onboarding] repairs count failed", err);
  }
  try {
    _env = await apiGet("/profile");
  } catch (err) {
    console.warn("[onboarding] profile load failed", err);
    _env = null;
  }

  const incomplete = !_env?.profile?.identity?.name;

  // ── Gate 1: mandatory profile (language first, name required) ────────────
  // Server-persisted + tenant-scoped (PUT /profile/*), so once the name is set
  // it never re-prompts on reconnection. Can't be escaped by refusing the tour.
  if (incomplete) {
    _overlay()?.classList.add("ob-running");
    _mascotState("scanning");
    document.getElementById("landingProfile")?.classList.add("ob-spotlight");
    openProfileWizard(_env, {
      mandatory: true,
      onComplete: () => {
        document.getElementById("landingProfile")?.classList.remove("ob-spotlight");
        _mascotState("success");
        _maybeOfferTour(repairsCount);
      },
    });
    return;
  }

  // Profile already complete → consider the optional tour.
  _maybeOfferTour(repairsCount);
}

// ── Gate 2: optional guided tour (one-shot, server-backed onboarding_seen) ──
function _maybeOfferTour(repairsCount) {
  if (hasSeenOnboarding("onboarding_seen") || repairsCount > 0) {
    // Nothing to tour — undo the pre-gate and leave the cockpit alone.
    _overlay()?.classList.remove("ob-running");
    return;
  }
  _overlay()?.classList.add("ob-running");
  _mascotState("scanning");
  _stepWelcome();
}

// ── Tour step 1: offer modal (blocking) — refusable ───────────────────────
// Language and profile are already handled by Gate 1, so this is purely the
// "want a quick tour?" prompt; refusing ends the run (and marks it seen).
function _stepWelcome() {
  const host = _ensureHost();
  host.innerHTML = `
    <div class="ob-backdrop" id="obBackdrop">
      <div class="ob-modal" role="dialog" aria-modal="true" aria-labelledby="obWelcomeTitle">
        <div class="ob-modal-mascot" id="obWelcomeMascot" aria-hidden="true"></div>
        <span class="ob-kicker">${t("onboarding.welcome.kicker")}</span>
        <h2 class="ob-title" id="obWelcomeTitle">${t("onboarding.welcome.title")}</h2>
        <p class="ob-body">${t("onboarding.welcome.body")}</p>
        <div class="ob-actions">
          <button type="button" class="ob-btn ob-btn-ghost" id="obWelcomeSkip">${t("onboarding.welcome.skip")}</button>
          <button type="button" class="ob-btn ob-btn-primary" id="obWelcomeCta">${t("onboarding.welcome.cta")}</button>
        </div>
      </div>
    </div>`;
  mountMascot(host.querySelector("#obWelcomeMascot"), { size: "md", state: "idle" });
  host.querySelector("#obWelcomeSkip").addEventListener("click", finish);
  host.querySelector("#obWelcomeCta").addEventListener("click", () => {
    _clearHost();
    _stepConcept();
  });
}

// ── Step 3: product concept ───────────────────────────────────────────────
// A centred card (not a pointer bubble): the concept is a general statement, so
// a bubble anchored to the hero title ended up covering the subtitle/form.
function _stepConcept() {
  hideBubble();
  _mascotState("idle");
  const host = _ensureHost();
  host.innerHTML = `
    <div class="ob-backdrop" id="obBackdrop">
      <div class="ob-modal ob-modal--concept" role="dialog" aria-modal="true">
        <div class="ob-modal-mascot" id="obConceptMascot" aria-hidden="true"></div>
        <p class="ob-body">${t("onboarding.concept.bubble")}</p>
        <div class="ob-actions">
          <button type="button" class="ob-btn ob-btn-ghost" id="obConceptSkip">${t("onboarding.skip")}</button>
          <button type="button" class="ob-btn ob-btn-primary" id="obConceptNext">${t("onboarding.next")}</button>
        </div>
      </div>
    </div>`;
  mountMascot(host.querySelector("#obConceptMascot"), { size: "sm", state: "idle" });
  host.querySelector("#obConceptSkip").addEventListener("click", finish);
  host.querySelector("#obConceptNext").addEventListener("click", () => {
    _clearHost();
    _stepExampleIntro();
  });
}

// ── Step 3.5: offer a real example before the user's own first diag ────────
// Opens the shipped MNT Reform device; the workspace coaching tour plays there
// (full tabs), then hands back here via onDone → the "now your turn" pointers.
function _stepExampleIntro() {
  hideBubble();
  _mascotState("idle");
  const host = _ensureHost();
  host.innerHTML = `
    <div class="ob-backdrop" id="obBackdrop">
      <div class="ob-modal ob-modal--concept" role="dialog" aria-modal="true">
        <div class="ob-modal-mascot" id="obExampleMascot" aria-hidden="true"></div>
        <p class="ob-body">${t("onboarding.example.bubble")}</p>
        <div class="ob-actions">
          <button type="button" class="ob-btn ob-btn-ghost" id="obExampleSkip">${t("onboarding.skip")}</button>
          <button type="button" class="ob-btn ob-btn-primary" id="obExampleCta">${t("onboarding.example.cta")}</button>
        </div>
      </div>
    </div>`;
  mountMascot(host.querySelector("#obExampleMascot"), { size: "sm", state: "idle" });
  host.querySelector("#obExampleSkip").addEventListener("click", () => { _clearHost(); _afterExample(); });
  host.querySelector("#obExampleCta").addEventListener("click", () => { _clearHost(); _openExample(); });
}

function _openExample() {
  // Ensure the workspace tour will play even for a tech who already saw it:
  // arm a one-shot bypass of the persisted `first_diag_seen` flag (server truth
  // now, so we can't just clear a localStorage key to force the replay).
  forceNextDiagCoaching();
  // When the workspace tour finishes (dashboard.js forwards this as onDone),
  // return to the landing and resume the user's own first-diag pointers.
  window.__wbExampleTourOnDone = () => { window.__wbExampleTourOnDone = null; _returnFromExample(); };
  // Navigate into the example workspace; dashboard.js fires the tour on render.
  window.location.hash = `#repair/${EXAMPLE_REPAIR_ID}/diagnostic`;
}

function _returnFromExample() {
  // Back to the landing cockpit, then resume with the user's own first diag.
  // The hashchange → showLanding render is async, so poll for the device input
  // (the anchor the resumed pointers need) rather than guessing a fixed delay.
  window.location.hash = "#landing";
  let tries = 30; // ~3 s ceiling at 100 ms
  const resume = () => {
    if (document.getElementById("landingDevice") || tries-- <= 0) {
      _overlay()?.classList.add("ob-running");
      _afterExample();
      return;
    }
    setTimeout(resume, 100);
  };
  setTimeout(resume, 80);
}

function _afterExample() {
  _reveal('[data-ob-reveal].landing-title, [data-ob-reveal].landing-sub');
  _stepDevice();
}

// ── Step 4: first diagnostic (device → symptom → launch) ──────────────────
function _stepDevice() {
  _reveal('.landing-input-wrap[data-ob-reveal]');
  showBubble({
    anchor: document.getElementById("landingDevice"),
    placement: "bottom",
    text: t("onboarding.firstdiag.bubble_device"),
    next: _stepSymptom,
    skip: finish,
  });
}

function _stepSymptom() {
  _reveal('#landingSymptom[data-ob-reveal]');
  showBubble({
    anchor: document.getElementById("landingSymptom"),
    placement: "bottom",
    text: t("onboarding.firstdiag.bubble_symptom"),
    next: _stepLaunch,
    skip: finish,
  });
}

function _stepLaunch() {
  _reveal('.landing-actions[data-ob-reveal]');
  showBubble({
    anchor: document.getElementById("landingSubmit"),
    placement: "top",
    text: t("onboarding.firstdiag.bubble_launch"),
    next: _stepKnowledge,
    skip: finish,
  });
}

// ── Step 5: feature mentions (Add knowledge + Stock) ──────────────────────
// Short pointers only; the detailed explanation lives in the on-click info
// modal (info_modal.js), reachable anytime via the "?" affordance.
function _stepKnowledge() {
  // Plan free (mode managé, cloud_hints) : « Add knowledge » est masqué —
  // ne pas ancrer une bulle sur un élément invisible, enchaîner sur le stock.
  if (hideUploads()) return _stepStock();
  showBubble({
    anchor: document.getElementById("landingKnowledgeBtn"),
    placement: "top",
    text: t("onboarding.tour.knowledge"),
    next: _stepStock,
    skip: finish,
  });
}

function _stepStock() {
  showBubble({
    anchor: document.getElementById("landingStockLink"),
    placement: "bottom",
    text: t("onboarding.tour.stock"),
    next: finish,
    nextLabel: t("onboarding.done"),
    skip: finish,
  });
}

// ── End: drop the gate, mark seen ─────────────────────────────────────────
export function finish() {
  hideBubble();
  _clearHost();
  document.getElementById("landingProfile")?.classList.remove("ob-spotlight");
  _overlay()?.classList.remove("ob-running");
  markOnboardingSeen("onboarding_seen"); // server (cross-device) + localStorage cache
  _mascotState("idle");
}
