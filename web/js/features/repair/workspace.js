// web/js/features/repair/workspace.js
// Repair workspace shell. Given a parsed repair route { id, vue }, it mounts the
// right view + side-effects. The DOM sections are the existing ones (the
// homeSection dashboard for the diagnostic vue, the pcb stub, the schematic
// section, the canvas/memoryBank for graph); this shell only SEQUENCES the
// per-vue loaders — it owns no new DOM and no new styling. The active
// device/repair context is already in the store (await syncContextFromUrl ran
// upstream in main.js).
//
// Import the schematic module with the SAME ?v=fitzoom query main.js uses — ESM
// keys modules by URL, so a different (or missing) query would create a second
// module instance with its own STATE.
import { currentSession, currentViewMode, applyMemoireMode } from "../../router.js";
import { renderRepairDashboard } from "./diagnostic/dashboard.js";
import { loadSchematic } from "../../schematic.js?v=fitzoom";
import { loadMemoryBank } from "../../memory_bank.js";
import { openLLMPanelIfRepairParam } from "../../llm.js";
import { firstDiagTourPending } from "./diagnostic/coaching.js";

/**
 * Mount the active repair vue. `deps.maybeLoadGraph` is injected by main.js to
 * avoid a circular import (main.js owns the graph-mount guard + window shim).
 */
export async function mountRepairVue(route, { maybeLoadGraph }) {
  const session = currentSession();
  switch (route.vue) {
    case "diagnostic":
      // The diagnostic vue is the repair dashboard (header / data grid /
      // conversations / findings / timeline / pack) + the chat overlay.
      if (session) renderRepairDashboard(session);
      // Hold the chat auto-open back while the first-run tour is playing/owed,
      // so its early bubbles aren't covered; the tour's final step invites the
      // tech to open the chat themselves.
      if (!firstDiagTourPending()) openLLMPanelIfRepairParam();
      break;
    case "pcb":
      // Boardview init is handled by router.navigate()'s pcb branch
      // (window.initBoardview), which fires on the DOM-visibility toggle.
      break;
    case "schematic":
      loadSchematic();
      break;
    case "graph": {
      const mode = currentViewMode();
      applyMemoireMode(mode);
      await maybeLoadGraph();
      if (mode === "md") loadMemoryBank();
      break;
    }
  }
}
