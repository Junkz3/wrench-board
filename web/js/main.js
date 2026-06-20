// Entry point for the web app. Imports focused modules (router, home,
// graph) and drives the page lifecycle: section routing, initial render,
// and a section-agnostic wiring block for the Tweaks panel + boardview
// colour pickers.

import { currentSection, navigate, wireRouter, currentSession, leaveSession, syncContextFromUrl, parseRoute, migrateLegacyUrl, repairHash } from './router.js';
import { getDeviceSlug, getRepairId } from './shared/context.js';
import { mountRepairVue } from './features/repair/workspace.js';
import { initHome, hideRepairDashboard } from './features/repair/diagnostic/dashboard.js';
import { loadGraphFromBackend, setEmptyState, initGraphWithData } from './graph.js';
import { initMemoryBank } from './memory_bank.js';
import { initProfileSection } from './profile.js';
import { initStockSection } from './stock.js';
import { initPipelineProgress } from './pipeline_progress.js';
import { initLLMPanel } from './llm.js';
import { initCameraPicker } from './camera.js';
import { updatePreviewDevice } from './camera_preview.js';
import { closeSchematicInspector } from './schematic.js?v=fitzoom';
import { initLanding, showLanding, hideLanding } from './features/global/landing/index.js';
import { hydrateOnboardingState } from './onboarding_state.js';
import { planHints } from './cloud_hints.js';
import { mountMascot } from './mascot.js';
import { sendDiagnostic } from './services/diagnosticSocket.js';
import { sendCapabilities } from './features/repair/diagnostic/filesVision.js';
import * as Protocol from './protocol.js?v=quest4';

// Tracks which device slug the graph has already been mounted for. Guards
// against a second initGraphWithData() call on re-navigation to #graphe —
// that function spins up a d3 force simulation and a requestAnimationFrame
// loop, neither of which tear themselves down on re-entry.
let _graphLoadedSlug = null;

async function maybeLoadGraph() {
  const slug = getDeviceSlug();
  if (!slug) {
    setEmptyState(true);
    return;
  }
  if (slug === _graphLoadedSlug) return;  // already mounted for this slug
  // If the canvas is currently hidden (e.g. user landed in Brut mode),
  // clientWidth is 0 — layoutNodes + fitToScreen would compute nonsense
  // positions that get burned in. Skip init without marking the slug
  // as loaded so the next call (when canvas becomes visible) retries.
  const canvasEl = document.getElementById("canvas");
  if (!canvasEl || canvasEl.clientWidth === 0) return;
  const fetched = await loadGraphFromBackend();
  if (fetched && fetched.nodes && fetched.nodes.length > 0) {
    setEmptyState(false);
    initGraphWithData(fetched);
    _graphLoadedSlug = slug;
  } else {
    setEmptyState(true);
  }
}

// Route-driven side-effect dispatch — mounts the data/view for the parsed route.
// Global routes load their section; repair routes delegate to the workspace shell
// (features/repair/workspace.js), which sequences the per-vue loaders. The active
// device/repair context is already in the store (await syncContextFromUrl upstream).
async function mountRoute(route) {
  if (route.level === "global") {
    if (route.name === "stock") initStockSection();
    else if (route.name === "profile") initProfileSection();
    else if (route.name === "home") {
      // #home is the global home = the landing overlay (which lists all repairs
      // via its sidebar). showLandingNow / the hashchange handler govern its
      // visibility; here we just make sure the repair dashboard is hidden.
      hideRepairDashboard();
    }
    // landing: overlay handled by show/hideLanding; nothing to mount here.
    return;
  }
  await mountRepairVue(route, { maybeLoadGraph });
}

// Early stub: collect boardview.* events in __pending until brd_viewer
// mounts and replaces this with the real implementation. Without this,
// events sent before the tech navigates to #pcb are silently lost.
if (!window.Boardview) {
  window.Boardview = {
    __pending: [],
    apply(ev) { this.__pending.push(ev); },
  };
}

/* ---------- INIT ---------- */
(async function bootstrap() {
  // Wait for i18n dictionaries before any module renders dynamic strings.
  if (window.i18n && window.i18n.ready) await window.i18n.ready;
  // Profile is the source of truth for the user's language AND the one-shot
  // onboarding flags (cross-device). localStorage is just a paint-hint / pre-gate
  // cache. Kick the single /profile hydration off here so it overlaps the init
  // below; it's awaited further down (before routing) so the synchronous
  // onboarding gates see server truth, not an empty localStorage on a new device.
  const _profileHydrated = hydrateOnboardingState().then((env) => {
    const pref = env?.profile?.preferences?.language;
    if (pref && pref !== window.i18n.locale && window.i18n.SUPPORTED.includes(pref)) {
      return window.i18n.setLocale(pref);
    }
  }).catch(() => {});
  mountMascot(document.getElementById("brandMascot"), { size: "xs", state: "idle" });
  // Hosted edition: the cloud front-door injects window.__wbPlanHints. Flip the
  // wordmark to "WrenchBoardCloud" (the "Cloud" suffix + perched cloud reveal
  // under body.wb-hosted); self-host stays plain "WrenchBoard". Cosmetic only.
  if (planHints()) document.body.classList.add("wb-hosted");
  wireRouter({ maybeLoadGraph });
  syncContextFromUrl();   // Phase C.1: populate store.device/repair before any view mounts
  initHome();             // wires the dashboard locale-refresh (new-repair modal removed in D.2)
  initMemoryBank();
  initPipelineProgress();
  await initLLMPanel();
  // NOTE: the chat panel is auto-opened by mountRoute's diagnostic branch AFTER
  // `await syncContextFromUrl()` resolves the slug — not eagerly here, which would
  // see an empty store on a deep-link/reload of #repair/<id>/diagnostic (C11).

  // Files+Vision : camera picker in the LLM panel head. On change :
  //   - notify the diag WS via client.capabilities (gates cam_capture)
  //   - swap the preview window's stream if the preview is currently open
  initCameraPicker((deviceId, label) => {
    sendCapabilities();
    updatePreviewDevice(deviceId, label);
  });

  // Protocol module — init with a deferred send that reads the live WS at
  // call time (the socket is opened lazily by llm.js on first panel open).
  // llm.js + chatLog.js import the same ./protocol.js?v=quest4 module directly,
  // so this init() wiring is shared with them (ESM single instance per URL).
  Protocol.init({
    send: (payload) => sendDiagnostic(payload),
    hasBoard: !!window.Boardview?.hasBoard?.(),
  });

  // Landing hero — initialise listeners; the route decides whether it shows
  // (below). Stock is now a normal global destination (#stock), not a tool mode.
  initLanding();

  // Migrate any legacy URL (?device=&repair=#section, ?tool=stock, bare
  // #memory-bank/#graphe) into the new grammar in place, then resolve the
  // route's device/repair into the store BEFORE mounting any view.
  migrateLegacyUrl();
  await syncContextFromUrl();
  // Ensure server onboarding flags are loaded before showLanding() / mountRoute()
  // run their synchronous one-shot gates (landing tour + first-diag coaching).
  await _profileHydrated;

  // Landing IS the global home (Phase D.1): it shows on #home and #landing (and
  // a bare load → parseRoute returns global "home"). #stock/#profile hide it; a
  // repair route mounts the dashboard. The landing's sidebar lists all repairs,
  // so there's no separate journal grid anymore.
  const route = parseRoute();
  const showLandingNow = route.level === "global"
    && (route.name === "home" || route.name === "landing");
  if (showLandingNow) showLanding(); else hideLanding();
  // The pre-paint gate (index.html) may have masked the chrome with
  // `pending-landing`; now that show/hideLanding governs, drop it so the chrome
  // paints (it is never removed elsewhere).
  document.body.classList.remove("pending-landing");

  // Landing top-right "Stock" link → the global #stock destination.
  const __stockLink = document.getElementById("landingStockLink");
  if (__stockLink) {
    __stockLink.addEventListener("click", (ev) => {
      ev.preventDefault();
      window.location.hash = "#stock";
    });
  }

  navigate(currentSection());
  await mountRoute(route);

  // Schematic inspector close button — wired once, guarded against absence.
  document.getElementById("schInspClose")?.addEventListener("click", closeSchematicInspector);

  // Single hashchange owner (router.js no longer navigates on hashchange):
  // re-derive the route, re-sync context (may resolve a slug), re-mount.
  window.addEventListener("hashchange", async () => {
    migrateLegacyUrl();
    await syncContextFromUrl();
    const r = parseRoute();
    if (r.level === "global" && (r.name === "home" || r.name === "landing")) showLanding(); else hideLanding();
    navigate(currentSection());
    await mountRoute(r);
  });
})();

/* Wire section-agnostic top-bar controls at the top level so they stay
   reachable whether or not the graph init (and its enclosing function,
   which historically owned these handlers) runs. Covers the Tweaks panel
   open/close buttons AND the boardview colour pickers inside that panel.
   Script lives at the end of <body>, so run immediately rather than
   waiting for DOMContentLoaded (which may already have fired). */
(function wireTopLevelControls() {
  // ---- Tweaks panel open/close (previously wired inside initGraphWithData
  // and therefore never bound on #home / #pcb / etc.) ----
  const tweaksPanelEl  = document.getElementById("tweaksPanel");
  const tweaksToggleEl = document.getElementById("tweaksToggle");
  const tweaksCloseEl  = document.getElementById("tweaksClose");
  // Refresh the pin-count pills next to each colour row from the
  // currently-loaded board. Called when the panel opens (board may
  // have been swapped while the panel was closed) and after every
  // colour change (cosmetic — the count itself doesn't change with
  // colour, but cheap enough to keep the path uniform).
  const refreshPinCounts = () => {
    const counts = (window.Boardview && window.Boardview.getPinCounts && window.Boardview.getPinCounts()) || null;
    document.querySelectorAll('[data-cat-count]').forEach(span => {
      const cat = span.dataset.catCount;
      span.textContent = counts && counts[cat] != null ? counts[cat] : '';
    });
  };
  if (tweaksPanelEl && tweaksToggleEl) {
    tweaksToggleEl.addEventListener("click", () => {
      tweaksPanelEl.classList.toggle("show");
      if (tweaksPanelEl.classList.contains("show")) refreshPinCounts();
    });
  }
  if (tweaksPanelEl && tweaksCloseEl) {
    tweaksCloseEl.addEventListener("click", () => tweaksPanelEl.classList.remove("show"));
  }

  // ---- Boardview colour pickers ----
  // The `input` listeners can be attached immediately — the <input type="color">
  // nodes are already in the DOM. But syncing their initial values depends on
  // `window.getBoardviewColors` which is defined by pcb_viewer.js (a classic
  // script near the end of <body>), so we run the initial sync after
  // DOMContentLoaded when that script is guaranteed to have executed.
  const paintDot = (row, hex) => {
    const dot = row && row.querySelector('.brd-color-dot');
    if (!dot || !hex) return;
    dot.style.background = hex;
    dot.style.boxShadow = `0 0 6px ${hex}`;
  };
  // Per-category Pickr instance, keyed by `data-cat`. Built lazily
  // when the Pickr library + pcb_viewer.js's `getBoardviewColors` are
  // both ready — Pickr is loaded as a non-deferred CDN script so it
  // usually beats this code, but we tick in case it doesn't.
  const pickrByCategory = {};
  const buildPickrs = () => {
    if (typeof Pickr === 'undefined') return false;
    const current = (window.getBoardviewColors && window.getBoardviewColors()) || {};
    document.querySelectorAll('.brd-color-row .brd-color-dot[data-cat]').forEach(dot => {
      const cat = dot.dataset.cat;
      if (pickrByCategory[cat]) return;
      const initial = current[cat] || '#a9b6cc';
      paintDot(dot.closest('.brd-color-row'), initial);
      const pickr = Pickr.create({
        el: dot,
        theme: 'classic',
        useAsButton: true,         // dot itself is the trigger
        default: initial,
        defaultRepresentation: 'HEX',
        appClass: 'brd-pickr',     // namespace for any future tweaks
        position: 'left-middle',   // popover opens to the LEFT of the
                                   // panel (which is pinned right) so
                                   // it stays fully on-screen
        components: {
          preview: true,
          opacity: false,
          hue: true,
          // `clear` reverts that single row to its parse-time default.
          // Especially useful on `boardFill` — the default is bg-deep,
          // so clear == "no fill" (the substrate becomes invisible
          // again). Saves the user from a separate "Reset colors"
          // round trip when they only wanted to undo one row.
          interaction: { hex: true, rgba: false, input: true, save: false, clear: true },
        },
      });
      pickr.on('change', (color) => {
        const hex = color.toHEXA().toString().slice(0, 7);  // drop alpha
        window.setBoardviewNetColor?.(cat, hex);
        paintDot(dot.closest('.brd-color-row'), hex);
      });
      pickr.on('clear', () => {
        const defaults = (window.getBoardviewColorDefaults && window.getBoardviewColorDefaults()) || {};
        const defaultHex = defaults[cat];
        if (!defaultHex) return;
        window.setBoardviewNetColor?.(cat, defaultHex);
        paintDot(dot.closest('.brd-color-row'), defaultHex);
        pickr.setColor(defaultHex, true);
      });
      pickrByCategory[cat] = pickr;
    });
    return true;
  };
  const syncInputs = () => {
    const current = (window.getBoardviewColors && window.getBoardviewColors()) || {};
    document.querySelectorAll('.brd-color-row .brd-color-dot[data-cat]').forEach(dot => {
      const cat = dot.dataset.cat;
      const hex = current[cat];
      if (!hex) return;
      paintDot(dot.closest('.brd-color-row'), hex);
      if (pickrByCategory[cat]) {
        pickrByCategory[cat].setColor(hex, /* silent */ true);
      }
    });
    refreshPinCounts();
  };
  document.getElementById("brdColReset")?.addEventListener("click", () => {
    window.resetBoardviewColors?.();
    syncInputs();
  });
  // Wait for Pickr + pcb_viewer.js's window.getBoardviewColors before
  // building the pickers and hydrating their initial colours.
  let tries = 0;
  const init = () => {
    if (typeof Pickr !== 'undefined' && window.getBoardviewColors) {
      buildPickrs();
      syncInputs();
      return;
    }
    if (++tries < 60) requestAnimationFrame(init);
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Session pill — click body to go to the active repair's dashboard, click [×]
  // to quit. Under the 2-level grammar #home is the GLOBAL list, so the pill must
  // target #repair/<id>/diagnostic when a repair is active (C6).
  const sessionPill = document.getElementById("sessionPill");
  const sessionPillClose = document.getElementById("sessionPillClose");
  const gotoSessionDashboard = () => {
    const id = getRepairId();
    window.location.hash = id ? repairHash(id, "diagnostic") : "#home";
  };
  if (sessionPill) {
    sessionPill.addEventListener("click", (ev) => {
      if (sessionPillClose && sessionPillClose.contains(ev.target)) return;
      gotoSessionDashboard();
    });
    sessionPill.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        if (sessionPillClose && sessionPillClose.contains(document.activeElement)) return;
        gotoSessionDashboard();
      }
    });
  }
  if (sessionPillClose) {
    sessionPillClose.addEventListener("click", (ev) => {
      ev.stopPropagation();
      leaveSession();
    });
  }
})();
