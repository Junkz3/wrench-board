// Entry point for the web app. Imports focused modules (router, home,
// graph) and drives the page lifecycle: section routing, initial render,
// and a section-agnostic wiring block for the Tweaks panel + boardview
// colour pickers.

import { currentSection, navigate, wireRouter, currentSession, leaveSession, applyMemoireMode, currentViewMode } from './router.js';
import { loadTaxonomy, loadRepairs, renderHome, initNewRepairModal, renderRepairDashboard, hideRepairDashboard } from './home.js';
import { loadGraphFromBackend, setEmptyState, initGraphWithData } from './graph.js';
import { initMemoryBank, loadMemoryBank } from './memory_bank.js';
import { initProfileSection } from './profile.js';
import { initPipelineProgress } from './pipeline_progress.js';
import { initLLMPanel, openLLMPanelIfRepairParam } from './llm.js';
import { initCameraPicker } from './camera.js';
import { updatePreviewDevice } from './camera_preview.js';
import { loadSchematic, closeSchematicInspector } from './schematic.js?v=fitzoom';
import { initLanding, showLanding } from './landing.js';
import { mountMascot } from './mascot.js';
import * as Protocol from './protocol.js?v=quest4';

// Tracks which device slug the graph has already been mounted for. Guards
// against a second initGraphWithData() call on re-navigation to #graphe —
// that function spins up a d3 force simulation and a requestAnimationFrame
// loop, neither of which tear themselves down on re-entry.
let _graphLoadedSlug = null;

async function maybeLoadGraph() {
  const slug = new URLSearchParams(window.location.search).get("device");
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

// Expose on window so router.js can trigger a lazy load when the user
// toggles from Brut back to Visuel — at that point the canvas becomes
// visible with real dimensions and we want to mount the graph.
window.__maybeLoadGraph = maybeLoadGraph;

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
  mountMascot(document.getElementById("brandMascot"), { size: "xs", state: "idle" });
  wireRouter();
  initNewRepairModal();
  initMemoryBank();
  initPipelineProgress();
  await initLLMPanel();
  openLLMPanelIfRepairParam();

  // Files+Vision : camera picker in the LLM panel head. On change :
  //   - notify the diag WS via client.capabilities (gates cam_capture)
  //   - swap the preview window's stream if the preview is currently open
  initCameraPicker((deviceId, label) => {
    if (window.LLM && typeof window.LLM.sendCapabilities === 'function') {
      window.LLM.sendCapabilities();
    }
    updatePreviewDevice(deviceId, label);
  });

  // Protocol module — init with a deferred send that reads the live WS at
  // call time (the socket is opened lazily by llm.js on first panel open).
  Protocol.init({
    send: (payload) => window.__diagnosticWS?.send(JSON.stringify(payload)),
    hasBoard: !!window.Boardview?.hasBoard?.(),
  });
  window.Protocol = Protocol;

  // Landing hero — initialise listeners; show only if no repair param.
  initLanding();
  const __landingParams = new URLSearchParams(window.location.search);
  if (!__landingParams.get("repair") && !__landingParams.get("device")) {
    showLanding();
  }

  // Legacy redirect: #memory-bank is merged into #graphe with view=md.
  if (window.location.hash === "#memory-bank") {
    const url = new URL(window.location.href);
    url.searchParams.set("view", "md");
    url.hash = "#graphe";
    window.history.replaceState({}, "", url.toString());
  }

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

  if (initial === "graphe") {
    const mode = currentViewMode();
    applyMemoireMode(mode);
    await maybeLoadGraph();
    if (mode === "md") loadMemoryBank();
  } else if (initial === "home") {
    const session = currentSession();
    if (session) {
      renderRepairDashboard(session);
    } else {
      hideRepairDashboard();
      const [taxonomy, repairs] = await Promise.all([loadTaxonomy(), loadRepairs()]);
      renderHome(taxonomy, repairs);
    }
  } else if (initial === "schematic") {
    loadSchematic();
  } else if (initial === "profile") {
    initProfileSection();
  }

  // Schematic inspector close button — wired once, guarded against absence.
  document.getElementById("schInspClose")?.addEventListener("click", closeSchematicInspector);

  // Sections that need their data refetched when the user navigates back to
  // them — the router only toggles DOM visibility, side-effects live here.
  window.addEventListener("hashchange", async () => {
    const sec = currentSection();
    if (sec === "schematic") loadSchematic();
    else if (sec === "profile") initProfileSection();
    else if (sec === "graphe") {
      const mode = currentViewMode();
      applyMemoireMode(mode);
      maybeLoadGraph();
      if (mode === "md") loadMemoryBank();
    }
    else if (sec === "home") {
      const session = currentSession();
      if (session) {
        renderRepairDashboard(session);
      } else {
        hideRepairDashboard();
        const [taxonomy, repairs] = await Promise.all([loadTaxonomy(), loadRepairs()]);
        renderHome(taxonomy, repairs);
      }
    }
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
  // `window.getBoardviewColors` which is defined by brd_viewer.js (an ES module
  // with implicit `defer`), so we run the initial sync after DOMContentLoaded
  // when deferred modules are guaranteed to have executed.
  const paintDot = (row, hex) => {
    const dot = row && row.querySelector('.brd-color-dot');
    if (!dot || !hex) return;
    dot.style.background = hex;
    dot.style.boxShadow = `0 0 6px ${hex}`;
  };
  // Per-category Pickr instance, keyed by `data-cat`. Built lazily
  // when the Pickr library + brd_viewer.js's `getBoardviewColors` are
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
  // Wait for Pickr + brd_viewer.js's window.getBoardviewColors before
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
})();
