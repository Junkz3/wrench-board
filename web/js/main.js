// Entry point for the web app. Imports focused modules (router, home,
// graph) and drives the page lifecycle: section routing, initial render,
// and a section-agnostic wiring block for the Tweaks panel + boardview
// colour pickers.

import { APP_VERSION, currentSection, navigate, wireRouter, currentSession } from './router.js';
import { loadHomePacks, loadTaxonomy, loadRepairs, renderHome, initNewRepairModal, renderRepairDashboard, hideRepairDashboard } from './home.js';
import { loadGraphFromBackend, setEmptyState, initGraphWithData } from './graph.js';
import { initMemoryBank, loadMemoryBank } from './memory_bank.js';
import { initProfileSection } from './profile.js';
import { initPipelineProgress } from './pipeline_progress.js';
import { initLLMPanel, openLLMPanelIfRepairParam } from './llm.js';

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
  // Stamp the static version once — chrome state is then handled by navigate().
  document.getElementById("appVersion").textContent = APP_VERSION;
  wireRouter();
  initNewRepairModal();
  initMemoryBank();
  initPipelineProgress();
  await initLLMPanel();
  openLLMPanelIfRepairParam();

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

  if (initial === "graphe" && slug) {
    const fetched = await loadGraphFromBackend();
    if (fetched && fetched.nodes && fetched.nodes.length > 0) {
      setEmptyState(false);
      initGraphWithData(fetched);
    } else {
      setEmptyState(true);
    }
  } else if (initial === "home") {
    const session = currentSession();
    if (session) {
      renderRepairDashboard(session);
    } else {
      hideRepairDashboard();
      const [packs, taxonomy, repairs] = await Promise.all([loadHomePacks(), loadTaxonomy(), loadRepairs()]);
      renderHome(packs, taxonomy, repairs);
    }
  } else if (initial === "memory-bank") {
    loadMemoryBank();
  } else if (initial === "profile") {
    initProfileSection();
  }

  // Sections that need their data refetched when the user navigates back to
  // them — the router only toggles DOM visibility, side-effects live here.
  window.addEventListener("hashchange", async () => {
    const sec = currentSection();
    if (sec === "memory-bank") loadMemoryBank();
    else if (sec === "profile") initProfileSection();
    else if (sec === "home") {
      const session = currentSession();
      if (session) {
        renderRepairDashboard(session);
      } else {
        hideRepairDashboard();
        const [packs, taxonomy, repairs] = await Promise.all([loadHomePacks(), loadTaxonomy(), loadRepairs()]);
        renderHome(packs, taxonomy, repairs);
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
  if (tweaksPanelEl && tweaksToggleEl) {
    tweaksToggleEl.addEventListener("click", () => tweaksPanelEl.classList.toggle("show"));
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
  const syncInputs = () => {
    const current = (window.getBoardviewColors && window.getBoardviewColors()) || {};
    document.querySelectorAll('input[type="color"][data-cat]').forEach(inp => {
      const cat = inp.dataset.cat;
      if (current[cat]) inp.value = current[cat];
    });
  };
  document.querySelectorAll('input[type="color"][data-cat]').forEach(inp => {
    inp.addEventListener('input', (e) => {
      window.setBoardviewNetColor?.(inp.dataset.cat, e.target.value);
    });
  });
  document.getElementById("brdColReset")?.addEventListener("click", () => {
    window.resetBoardviewColors?.();
    syncInputs();
  });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", syncInputs);
  } else {
    // DOM is already ready — but the deferred module may not have executed yet.
    // Poll briefly until window.getBoardviewColors is defined (typically 1-2 frames).
    let tries = 0;
    const tick = () => {
      if (window.getBoardviewColors) { syncInputs(); return; }
      if (++tries < 40) requestAnimationFrame(tick);
    };
    tick();
  }
})();
