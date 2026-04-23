// Hash-based section router + chrome (topbar crumbs, mode pill, metabar).
// Owns navigation between the 8 app sections and refreshes the chrome when
// the active section or current device changes.

export const APP_VERSION = "v0.5.0";

export const SECTIONS = ["home", "pcb", "schematic", "graphe", "memory-bank", "agent", "profile", "aide"];

const SECTION_META = {
  home:          {crumb: "Journal des réparations", mode: {tag: "JOURNAL",  sub: "Réparations",            color: "cyan"}},
  pcb:           {crumb: "Boardview",                mode: {tag: "OUTIL",    sub: "Boardview",              color: "cyan"}},
  schematic:     {crumb: "Schematic",                mode: {tag: "OUTIL",    sub: "Schematic",              color: "cyan"}},
  graphe:        {crumb: "Graphe",                   mode: {tag: "ATTENTE",  sub: "Aucune mémoire chargée", color: "amber"}},
  "memory-bank": {crumb: "Memory Bank",              mode: {tag: "JOURNAL",  sub: "Memory Bank",            color: "cyan"}},
  agent:         {crumb: "Agent",                    mode: {tag: "AGENT",    sub: "Configuration",          color: "violet"}},
  profile:       {crumb: "Profil",                   mode: {tag: "PROFIL",   sub: "Technicien",             color: "cyan"}},
  aide:          {crumb: "Aide",                     mode: {tag: "AIDE",     sub: "Raccourcis",             color: "cyan"}},
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
      mode = {tag: "MÉMOIRE", sub: "Graphe de connaissances", color: "cyan"};
    } else if (pack) {
      mode = {tag: "CONSTRUCTION", sub: "Mémoire en cours", color: "amber"};
    } else {
      mode = {tag: "ATTENTE", sub: "Mémoire non construite", color: "amber"};
    }
  }
  const pill = document.getElementById("modePill");
  pill.className = `mode-pill ${mode.color}`;
  document.getElementById("modePillText").textContent = `${mode.tag} · ${mode.sub}`;

  // Breadcrumbs
  const crumbs = ["microsolder-agent"];
  if (section === "graphe" && deviceSlug) {
    crumbs.push("Réparations", prettifySlug(deviceSlug), "Graphe");
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
  document.getElementById("canvas").classList.toggle("hidden", section !== "graphe");
  document.getElementById("memoryBank").classList.toggle("hidden", section !== "memory-bank");
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
  // between router.js and home.js.
  const { loadHomePacks, loadTaxonomy, loadRepairs, renderHome } = await import("./home.js");
  const [packs, taxonomy, repairs] = await Promise.all([
    loadHomePacks(), loadTaxonomy(), loadRepairs(),
  ]);
  renderHome(packs, taxonomy, repairs);
}
