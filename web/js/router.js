// Hash-based section router + chrome (topbar crumbs, mode pill, metabar).
// Owns navigation between the 8 app sections and refreshes the chrome when
// the active section or current device changes.

export const APP_VERSION = "v0.5.0";

export const SECTIONS = ["home", "pcb", "schematic", "graphe", "profile"];

const SECTION_META = {
  home:          {crumb: "Journal des réparations", mode: {tag: "JOURNAL",  sub: "Réparations",            color: "cyan"}},
  pcb:           {crumb: "Boardview",                mode: {tag: "OUTIL",    sub: "Boardview",              color: "cyan"}},
  schematic:     {crumb: "Schematic",                mode: {tag: "OUTIL",    sub: "Graphe électrique",      color: "emerald"}},
  graphe:        {crumb: "Mémoire",                  mode: {tag: "ATTENTE",  sub: "Aucune mémoire chargée", color: "amber"}},
  profile:       {crumb: "Profil",                   mode: {tag: "PROFIL",   sub: "Technicien",             color: "cyan"}},
};

export function prettifySlug(slug) {
  if (!slug) return "";
  return slug.replace(/-/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}

async function loadPackSummary(slug) {
  if (!slug) return null;
  try {
    const res = await fetch(`/pipeline/packs/${encodeURIComponent(slug)}`);
    if (!res.ok) return null;
    return await res.json();
  } catch (err) {
    console.warn("loadPackSummary failed", err);
    return null;
  }
}

function renderCrumbs(items) {
  const el = document.getElementById("crumbs");
  el.innerHTML = "";
  items.forEach((text, i) => {
    if (i > 0) {
      const sep = document.createElement("span");
      sep.className = "sep";
      sep.textContent = "/";
      el.appendChild(sep);
    }
    const span = document.createElement("span");
    if (i === items.length - 1) span.classList.add("active");
    span.textContent = text;
    el.appendChild(span);
  });
}

function isPackComplete(pack) {
  return !!(pack && pack.has_registry && pack.has_knowledge_graph
         && pack.has_rules && pack.has_dictionary && pack.has_audit_verdict);
}

function packMissingFiles(pack) {
  if (!pack) return [];
  const missing = [];
  if (!pack.has_registry)        missing.push("registry");
  if (!pack.has_knowledge_graph) missing.push("graph");
  if (!pack.has_rules)           missing.push("rules");
  if (!pack.has_dictionary)      missing.push("dictionary");
  if (!pack.has_audit_verdict)   missing.push("audit");
  return missing;
}

function updateChrome(section, deviceSlug, pack) {
  let meta = SECTION_META[section] || SECTION_META.home;
  // Home's mode-pill reflects whether a session is active. Without a session,
  // it reads "JOURNAL · Réparations" (the SECTION_META default). With a session,
  // it reads "JOURNAL · Session" to signal we're on the dashboard, not the list.
  const activeSession = currentSession();
  if (section === "home" && activeSession) {
    meta = { ...meta, mode: { ...meta.mode, sub: "Session" } };
  }

  // Mode pill — static per-section, overridden on Graphe by pack state.
  let mode = meta.mode;
  if (section === "graphe") {
    if (!deviceSlug) {
      mode = {tag: "ATTENTE", sub: "Aucune réparation en cours", color: "amber"};
    } else if (isPackComplete(pack)) {
      mode = {
        tag: "MÉMOIRE",
        sub: document.body.classList.contains("guided-mode") ? "Ce que je sais" : "Graphe de connaissances",
        color: "cyan",
      };
    } else if (pack) {
      mode = {tag: "CONSTRUCTION", sub: "Mémoire en cours", color: "amber"};
    } else {
      mode = {tag: "ATTENTE", sub: "Mémoire non construite", color: "amber"};
    }
  }
  const pill = document.getElementById("modePill");
  pill.className = `mode-pill ${mode.color}`;
  document.getElementById("modePillText").textContent = `${mode.tag} · ${mode.sub}`;

  // Session pill — persistent across sections when a session is active.
  const sessionPill = document.getElementById("sessionPill");
  if (sessionPill) {
    const sess = currentSession();
    if (sess) {
      sessionPill.classList.remove("hidden");
      const devEl = document.getElementById("sessionPillDevice");
      const ridEl = document.getElementById("sessionPillRid");
      if (devEl) devEl.textContent = prettifySlug(sess.device);
      if (ridEl) ridEl.textContent = sess.repair.slice(0, 8);
    } else {
      sessionPill.classList.add("hidden");
    }
  }

  // Breadcrumbs
  const crumbs = ["microsolder-agent"];
  if (section === "graphe" && deviceSlug) {
    crumbs.push("Réparations", prettifySlug(deviceSlug), "Mémoire");
  } else {
    crumbs.push(meta.crumb);
  }
  renderCrumbs(crumbs);

  // Metabar — Graphe-only. body.no-metabar pulls .canvas/.home/.stub up.
  document.body.classList.toggle("no-metabar", section !== "graphe");
  // Section-specific class so scoped styles (boardview colour config rows in
  // the Tweaks panel, etc.) can show / hide per active section.
  document.body.dataset.section = section;
  if (section !== "graphe") return;

  const deviceEl = document.getElementById("metaDevice");
  const statusEl = document.getElementById("metaStatus");
  if (!deviceSlug) {
    deviceEl.innerHTML = `<span style="color:var(--text-3)">Aucune réparation en cours</span>`;
    statusEl.className = "warn info";
    statusEl.innerHTML = `<svg class="icon icon-sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>Ouvre une réparation depuis le Journal pour voir son graphe.`;
    return;
  }

  deviceEl.innerHTML = `<span class="tag">${deviceSlug}</span><span>·</span><span>${prettifySlug(deviceSlug)}</span>`;

  if (!pack) {
    statusEl.className = "warn";
    statusEl.innerHTML = `<svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.01"/></svg>Aucune mémoire pour ce device — crée une réparation pour la construire.`;
  } else if (isPackComplete(pack)) {
    statusEl.className = "warn ok";
    statusEl.innerHTML = `<svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M5 12l5 5L20 7"/></svg>Mémoire chargée · audit APPROUVÉ`;
  } else {
    const missing = packMissingFiles(pack);
    statusEl.className = "warn";
    statusEl.innerHTML = `<svg class="icon icon-sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Mémoire en construction — manque ${missing.join(", ")}`;
  }
}

function refreshChrome(section) {
  const params = new URLSearchParams(window.location.search);
  const slug = params.get("device");

  // Provisional synchronous update (no pack yet) — prevents FOUC.
  updateChrome(section, slug, null);

  // For Graphe with a device, fetch pack summary and refine.
  if (section === "graphe" && slug) {
    loadPackSummary(slug).then(pack => {
      // Guard: user may have navigated away while fetch was in flight.
      if (currentSection() === section) updateChrome(section, slug, pack);
    });
  }
}

export function currentSection() {
  const h = (window.location.hash || "#home").slice(1);
  return SECTIONS.includes(h) ? h : "home";
}

function setActiveRail(which) {
  document.querySelectorAll(".rail-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.section === which);
  });
}

export function navigate(section) {
  if (!SECTIONS.includes(section)) section = "home";
  setActiveRail(section);
  // Hide all known section DOMs, show the target.
  document.getElementById("homeSection").classList.toggle("hidden", section !== "home");
  // The "graphe" section is a merged Mémoire view — the visible child
  // (canvas vs memoryBank) is driven by the view mode (graph|md).
  // When leaving this section, hide both children so they don't leak
  // into another route.
  const inMemoire = section === "graphe";
  if (!inMemoire) {
    document.getElementById("canvas").classList.add("hidden");
    document.getElementById("memoryBank").classList.add("hidden");
  } else {
    applyMemoireMode(currentViewMode());
  }
  document.getElementById("profileSection").classList.toggle("hidden", section !== "profile");
  document.querySelectorAll("[data-section-stub]").forEach(el => {
    el.classList.toggle("hidden", el.dataset.sectionStub !== section);
  });
  refreshChrome(section);
  if (section === "pcb") {
    // brd_viewer.js loads as a deferred module; on first-load navigation
    // (user hits /#pcb directly) the function may not be defined yet when
    // navigate() runs from the boot IIFE. Try now, and retry once when
    // the module is guaranteed to have executed.
    const runPcbInit = () => {
      const root = document.getElementById("brdRoot");
      if (root && typeof window.initBoardview === "function") {
        window.initBoardview(root);
        return true;
      }
      return false;
    };
    if (!runPcbInit()) {
      window.addEventListener("load", runPcbInit, { once: true });
    }
  }
}

export function wireRouter() {
  window.addEventListener("hashchange", () => navigate(currentSection()));
  document.querySelectorAll(".rail-btn[data-section]").forEach(btn => {
    btn.addEventListener("click", () => {
      window.location.hash = "#" + btn.dataset.section;
    });
  });
  // Toggle buttons: clicking sets the mode + re-applies. The actual
  // memory-bank data fetch on first entry in md mode is handled by
  // main.js (which owns loadMemoryBank).
  document.querySelectorAll(".view-toggle-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.view;
      applyMemoireMode(mode);
      // On first entry into Brut mode, make sure the memory bank is
      // populated — loadMemoryBank is idempotent.
      if (mode === "md") {
        import("./memory_bank.js").then(m => m.loadMemoryBank?.());
      } else {
        // Switching back to Visuel: the canvas just became visible with
        // real dimensions. Trigger the graph load (idempotent via
        // _graphLoadedSlug guard in main.js) so layoutNodes + fitToScreen
        // see correct clientWidth/clientHeight. If we don't do this, a
        // load attempted while canvas was hidden bails out without
        // marking the slug mounted, and the view would stay empty.
        window.__maybeLoadGraph?.();
      }
    });
  });
}

/**
 * Which memoire view is active, derived from the `view` query param.
 * Defaults to "graph" when absent or invalid.
 */
export function currentViewMode() {
  const v = new URLSearchParams(window.location.search).get("view");
  return v === "md" ? "md" : "graph";
}

/**
 * Apply the memoire view mode — toggle DOM visibility of canvas vs
 * memoryBank, update the toggle-button active state, hide/show the
 * graph-specific filter chips in the metabar, and update the URL's
 * `view` param without reloading the page.
 */
export function applyMemoireMode(mode) {
  mode = mode === "md" ? "md" : "graph";
  document.getElementById("canvas").classList.toggle("hidden", mode !== "graph");
  document.getElementById("memoryBank").classList.toggle("hidden", mode !== "md");
  // Graph-specific filter chips + search live in .metabar .filters.
  const filtersEl = document.querySelector(".metabar .filters");
  if (filtersEl) filtersEl.classList.toggle("hidden", mode !== "graph");
  document.querySelectorAll(".view-toggle-btn").forEach(btn => {
    const on = btn.dataset.view === mode;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  });
  // Persist the choice in the URL without reloading — replaceState keeps
  // history clean (toggling back and forth shouldn't pollute back-button).
  const url = new URL(window.location.href);
  if (mode === "md") {
    url.searchParams.set("view", "md");
  } else {
    url.searchParams.delete("view");
  }
  window.history.replaceState({}, "", url.toString());
}

/**
 * Return the currently active repair session, derived from URL query params.
 * A session is defined by the SIMULTANEOUS presence of ?device= and ?repair=.
 * Re-derived on every call — zero hidden state.
 */
export function currentSession() {
  const params = new URLSearchParams(window.location.search);
  const device = params.get("device");
  const repair = params.get("repair");
  if (device && repair) return { device, repair };
  return null;
}

/**
 * Quit the active session: strip ?device= + ?repair=, hash to #home, close
 * chat panel, re-render the list. Called from the dashboard's Quitter button
 * and the topbar session pill's [×].
 */
export async function leaveSession() {
  const url = new URL(window.location.href);
  url.searchParams.delete("device");
  url.searchParams.delete("repair");
  url.hash = "#home";
  window.history.replaceState({}, "", url.toString());
  // Close the chat panel if open. llmClose is a <button>; if the panel
  // isn't mounted yet the optional chaining silently skips.
  document.getElementById("llmClose")?.click();
  // Refresh chrome (drops the pill) and swap to list mode.
  navigate("home");
  // Reload the list data. Dynamic import avoids a static circular dependency
  // between router.js and home.js. hideRepairDashboard() must run explicitly
  // because history.replaceState() does NOT fire a hashchange event, so the
  // hashchange dispatch in main.js that would normally call it never runs.
  const { loadHomePacks, loadTaxonomy, loadRepairs, renderHome, hideRepairDashboard } = await import("./home.js");
  hideRepairDashboard();
  const [packs, taxonomy, repairs] = await Promise.all([
    loadHomePacks(), loadTaxonomy(), loadRepairs(),
  ]);
  renderHome(packs, taxonomy, repairs);
}

// ============ Mode (guidé / expert) ============
//
// The shell has two modes:
//   - guided  : landing + Claude.ai-style repair workspace (default)
//   - expert  : original pro-tool workbench with the rail (current behavior)
//
// State is stored on `<body>` as `guided-mode` or `expert-mode` and persisted
// in localStorage under "microsolder.mode". The rest of the app reads from
// these classes via plain CSS selectors (no JS event bus needed).

const MODE_KEY = "microsolder.mode";
export const MODES = Object.freeze({ GUIDED: "guided", EXPERT: "expert" });

export function getMode() {
  const raw = localStorage.getItem(MODE_KEY);
  return raw === MODES.EXPERT ? MODES.EXPERT : MODES.GUIDED;
}

export function setMode(mode) {
  const next = mode === MODES.EXPERT ? MODES.EXPERT : MODES.GUIDED;
  localStorage.setItem(MODE_KEY, next);
  applyModeClass(next);
}

export function toggleMode() {
  setMode(getMode() === MODES.GUIDED ? MODES.EXPERT : MODES.GUIDED);
}

function applyModeClass(mode) {
  document.body.classList.toggle("guided-mode", mode === MODES.GUIDED);
  document.body.classList.toggle("expert-mode", mode === MODES.EXPERT);
}

export function initMode() {
  applyModeClass(getMode());
}
