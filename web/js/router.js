// Hash-based section router + chrome (topbar crumbs, mode pill, metabar).
// Owns navigation between the 8 app sections and refreshes the chrome when
// the active section or current device changes.

export const SECTIONS = ["home", "pcb", "schematic", "graphe", "stock", "profile"];

// SECTION_META holds i18n keys instead of literal strings — resolved at
// render time inside updateChrome(). On locale switch, refreshChrome() is
// re-invoked through the i18n.onChange hook below.
const SECTION_META = {
  home:          {crumbKey: "router.section.home",      mode: {tagKey: "router.mode.journal_tag", subKey: "router.mode.journal_repairs",   color: "cyan"}},
  pcb:           {crumbKey: "router.section.pcb",       mode: {tagKey: "router.mode.tool_tag",    subKey: "router.mode.tool_boardview",    color: "cyan"}},
  schematic:     {crumbKey: "router.section.schematic", mode: {tagKey: "router.mode.tool_tag",    subKey: "router.mode.tool_schematic",    color: "emerald"}},
  graphe:        {crumbKey: "router.section.graphe",    mode: {tagKey: "router.mode.wait_tag",    subKey: "router.mode.wait_no_memory",    color: "amber"}},
  stock:         {crumbKey: "router.section.stock",     mode: {tagKey: "router.mode.tool_tag",    subKey: "router.mode.tool_stock",        color: "emerald"}},
  profile:       {crumbKey: "router.section.profile",   mode: {tagKey: "router.mode.profile_tag", subKey: "router.mode.profile_sub",       color: "cyan"}},
};

// prettifySlug now lives in shared/dom.js (single source of truth). Imported for
// internal use (updateChrome) AND re-exported to preserve the public API
// consumed by main.js and landing.js.
import { prettifySlug } from "./shared/dom.js";
export { prettifySlug };
import { setContext, getDeviceSlug, getRepairId } from "./shared/context.js";
import { getRepair } from "./services/repairs.js";

// ── Phase C: 2-level hash route grammar ──────────────────────────────────
// Global routes:  #home | #stock | #profile | #landing
// Repair routes:  #repair/<id>/<vue>  with vue ∈ REPAIR_VUES (default diagnostic)
// The repair `graph` vue maps to the internal "graphe" section DOM (VUE_TO_SECTION).
// `?view=md` stays in the REAL query string (before the #) — orthogonal sub-state
// of the graph vue, read/written by currentViewMode()/applyMemoireMode().
export const REPAIR_VUES = ["diagnostic", "pcb", "schematic", "graph"];
const VUE_TO_SECTION = { diagnostic: "home", pcb: "pcb", schematic: "schematic", graph: "graphe" };
const GLOBAL_ROUTES = ["home", "stock", "profile", "landing"];

/**
 * Parse window.location.hash into a structured route:
 *   { level: "repair", id, vue }  for #repair/<id>/<vue>
 *   { level: "global", name }     for #home | #stock | #profile | #landing
 * Unknown/empty → { level: "global", name: "home" }. Tolerates a trailing
 * "?view=md" hash-fragment query by splitting it off (view lives in the real
 * query string, but be defensive).
 */
export function parseRoute() {
  const raw = (window.location.hash || "").replace(/^#/, "");
  const [path] = raw.split("?");
  const segs = path.split("/").filter(Boolean);
  if (segs[0] === "repair" && segs[1]) {
    let vue = segs[2] || "diagnostic";
    if (!REPAIR_VUES.includes(vue)) vue = "diagnostic";
    return { level: "repair", id: decodeURIComponent(segs[1]), vue };
  }
  const name = GLOBAL_ROUTES.includes(segs[0]) ? segs[0] : "home";
  return { level: "global", name };
}

/** Build a canonical repair-route hash (#repair/<id>/<vue>). */
export function repairHash(id, vue = "diagnostic") {
  const v = REPAIR_VUES.includes(vue) ? vue : "diagnostic";
  return `#repair/${encodeURIComponent(id)}/${v}`;
}

// repair_id → device_slug, resolved lazily and cached for the page load.
// In-app navigations seed this (seedSlugForRepair) so they stay synchronous;
// only a cold deep-link/reload pays the getRepair round-trip.
const _slugByRepair = new Map();

/** Fast-path: seed the slug cache when the slug is already known (home card
 *  click, landing nav, pipeline redirect) so syncContextFromUrl resolves without
 *  a fetch. No-op on falsy id/slug (never caches a bad value). */
export function seedSlugForRepair(id, slug) {
  if (id && slug) _slugByRepair.set(id, slug);
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

// Repair metadata cache for the topbar — keyed by repair_id, populated
// lazily by ensureRepairMeta on cache miss. Lets the breadcrumb show the
// human symptom and the session-pill show the start date instead of the
// raw UUID, without making updateChrome async.
const _repairCache = new Map();
const _repairCacheInFlight = new Map();

async function ensureRepairMeta(repairId) {
  if (!repairId) return null;
  if (_repairCache.has(repairId)) return _repairCache.get(repairId);
  if (_repairCacheInFlight.has(repairId)) return _repairCacheInFlight.get(repairId);
  const p = (async () => {
    try {
      const res = await fetch(`/pipeline/repairs/${encodeURIComponent(repairId)}`);
      if (!res.ok) return null;
      const data = await res.json();
      _repairCache.set(repairId, data);
      return data;
    } catch (_err) {
      return null;
    }
  })();
  _repairCacheInFlight.set(repairId, p);
  try { return await p; } finally { _repairCacheInFlight.delete(repairId); }
}

const _dateFmt = new Intl.DateTimeFormat("fr-FR", {
  day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
});
function formatRepairDate(iso) {
  if (!iso) return "…";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "…";
  // fr-FR formats as "26 avr., 14:32" — drop the comma to read as one phrase.
  return _dateFmt.format(d).replace(/,\s*/g, " ");
}

function truncateForCrumb(text, max = 38) {
  if (!text) return "";
  return text.length > max ? text.slice(0, max - 1).trimEnd() + "…" : text;
}

function renderCrumbs(items) {
  const el = document.getElementById("crumbs");
  el.innerHTML = "";
  items.forEach((it, i) => {
    if (i > 0) {
      const sep = document.createElement("span");
      sep.className = "sep";
      sep.textContent = "/";
      el.appendChild(sep);
    }
    const text = typeof it === "string" ? it : it.text;
    const title = typeof it === "string" ? null : it.title;
    const span = document.createElement("span");
    if (i === items.length - 1) span.classList.add("active");
    span.textContent = text;
    if (title) span.title = title;
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
  const t = (window.t || ((k) => k));
  let meta = SECTION_META[section] || SECTION_META.home;
  // Home's mode-pill reflects whether a session is active. Without a session,
  // it reads the journal/repairs default. With a session, it reads "Session"
  // to signal we're on the dashboard, not the list.
  const activeSession = currentSession();
  if (section === "home" && activeSession) {
    meta = { ...meta, mode: { ...meta.mode, subKey: "router.mode.journal_session" } };
  }

  // Mode pill — static per-section, overridden on Graphe by pack state.
  let mode = meta.mode;
  if (section === "graphe") {
    if (!deviceSlug) {
      mode = {tagKey: "router.mode.wait_tag", subKey: "router.mode.wait_no_repair", color: "amber"};
    } else if (isPackComplete(pack)) {
      mode = {tagKey: "router.mode.memory_tag", subKey: "router.mode.memory_graph", color: "cyan"};
    } else if (pack) {
      mode = {tagKey: "router.mode.build_tag", subKey: "router.mode.build_in_progress", color: "amber"};
    } else {
      mode = {tagKey: "router.mode.wait_tag", subKey: "router.mode.wait_unbuilt", color: "amber"};
    }
  }
  const pill = document.getElementById("modePill");
  pill.className = `mode-pill ${mode.color}`;
  document.getElementById("modePillText").textContent = `${t(mode.tagKey)} · ${t(mode.subKey)}`;

  // Repair metadata for the active session — drives the symptom in the
  // breadcrumb and the start date in the session-pill. Synchronous read;
  // a cache miss kicks off an async fetch + re-render at the bottom.
  const sessionMeta = activeSession ? _repairCache.get(activeSession.repair) : null;

  // Session pill — persistent across sections when a session is active.
  const sessionPill = document.getElementById("sessionPill");
  if (sessionPill) {
    if (activeSession) {
      sessionPill.classList.remove("hidden");
      const devEl = document.getElementById("sessionPillDevice");
      const ridEl = document.getElementById("sessionPillRid");
      if (devEl) devEl.textContent = prettifySlug(activeSession.device);
      if (ridEl) {
        if (sessionMeta && sessionMeta.created_at) {
          ridEl.textContent = formatRepairDate(sessionMeta.created_at);
          ridEl.title = `Repair ${activeSession.repair}`;
        } else {
          ridEl.textContent = activeSession.repair.slice(0, 8);
          ridEl.title = `Repair ${activeSession.repair}`;
        }
      }
    } else {
      sessionPill.classList.add("hidden");
    }
  }

  // Breadcrumbs — contextual path: device / symptom / section.
  // The brand name already lives in the .brand block on the left, so we
  // don't repeat it here. The symptom comes from the repair metadata; on
  // cache miss we fall back to the UUID-short and refresh asynchronously.
  const crumbs = [];
  if (activeSession) {
    crumbs.push(prettifySlug(activeSession.device));
    if (sessionMeta && sessionMeta.symptom) {
      crumbs.push({ text: truncateForCrumb(sessionMeta.symptom), title: sessionMeta.symptom });
    } else {
      crumbs.push(activeSession.repair.slice(0, 8));
    }
  } else if (deviceSlug) {
    crumbs.push(prettifySlug(deviceSlug));
  }
  crumbs.push(t(meta.crumbKey));
  renderCrumbs(crumbs);

  // Async upgrade — fetch repair meta on miss, re-render the chrome once
  // it lands. The cache prevents the recursive call from looping.
  if (activeSession && !sessionMeta) {
    ensureRepairMeta(activeSession.repair).then(m => {
      if (m) updateChrome(section, deviceSlug, pack);
    });
  }

  // Metabar — Graphe-only. body.no-metabar pulls .canvas/.home/.stub up.
  document.body.classList.toggle("no-metabar", section !== "graphe");
  // Section-specific class so scoped styles (boardview colour config rows in
  // the Tweaks panel, etc.) can show / hide per active section.
  document.body.dataset.section = section;
  if (section !== "graphe") return;

  const deviceEl = document.getElementById("metaDevice");
  const statusEl = document.getElementById("metaStatus");
  if (!deviceSlug) {
    deviceEl.innerHTML = `<span style="color:var(--text-3)">${t("router.metabar.no_repair")}</span>`;
    statusEl.className = "warn info";
    statusEl.innerHTML = `<svg class="icon icon-sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>${t("router.metabar.open_repair_to_view_graph")}`;
    return;
  }

  deviceEl.innerHTML = `<span class="tag">${deviceSlug}</span><span>·</span><span>${prettifySlug(deviceSlug)}</span>`;

  if (!pack) {
    statusEl.className = "warn";
    statusEl.innerHTML = `<svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.01"/></svg>${t("router.metabar.no_memory_for_device")}`;
  } else if (isPackComplete(pack)) {
    statusEl.className = "warn ok";
    statusEl.innerHTML = `<svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M5 12l5 5L20 7"/></svg>${t("router.metabar.memory_loaded_approved")}`;
  } else {
    const missing = packMissingFiles(pack);
    statusEl.className = "warn";
    statusEl.innerHTML = `<svg class="icon icon-sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>${t("router.metabar.memory_building_missing", { missing: missing.join(", ") })}`;
  }
}

function refreshChrome(section) {
  const slug = getDeviceSlug();

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

// Section DOM key for the current route. Repair vues map through VUE_TO_SECTION
// (graph→graphe); global routes map to their own section (landing→home DOM, the
// overlay sits on top). SECTIONS stays the section-DOM-key set used by navigate's
// guard — distinct from GLOBAL_ROUTES.
export function currentSection() {
  const route = parseRoute();
  if (route.level === "repair") return VUE_TO_SECTION[route.vue];
  return route.name === "landing" ? "home" : route.name;
}

// Highlight the active rail button by route. In a repair the active key is the
// vue; globally it's the route name. Buttons carry data-rail (Phase C).
function setActiveRail(route) {
  const active = route.level === "repair" ? route.vue : route.name;
  document.querySelectorAll(".rail-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.rail === active);
  });
}

// Show the rail's global button group outside a repair, the repair group inside.
// Buttons/separators carry data-rail-level="global|repair"; .hidden is global CSS.
function applyRailLevel(route) {
  document.querySelectorAll(".rail [data-rail-level]").forEach(el => {
    el.classList.toggle("hidden", el.dataset.railLevel !== route.level);
  });
}

export function navigate(section) {
  if (!SECTIONS.includes(section)) section = "home";
  const route = parseRoute();
  setActiveRail(route);
  applyRailLevel(route);
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

// `deps.maybeLoadGraph` is injected by main.js (which owns the graph-mount
// guard) so the view-toggle handler can trigger an idempotent graph reload
// without reaching through a window.* global — mirrors the mountRepairVue
// injection.
export function wireRouter({ maybeLoadGraph } = {}) {
  // NOTE: the hashchange listener lives in main.js (single owner) — it runs the
  // full async sequence migrateLegacyUrl → syncContextFromUrl → navigate →
  // mountRoute. Don't add a second navigate() here (it would double-navigate off
  // a possibly-stale store).
  //
  // Re-render the topbar chrome (mode pill, breadcrumbs, metabar status text)
  // when the user toggles EN/FR. The DOM-level [data-i18n] elements are
  // refreshed by i18n.applyDom; chrome content that is built imperatively
  // from current section + pack state must be redrawn here.
  if (window.i18n && typeof window.i18n.onChange === "function") {
    window.i18n.onChange(() => refreshChrome(currentSection()));
  }
  // Rail click → route-aware navigation. Global buttons jump to #<name>; repair
  // vue buttons jump to #repair/<currentId>/<vue> (only when a repair is active).
  document.querySelectorAll(".rail-btn[data-rail]").forEach(btn => {
    btn.addEventListener("click", () => {
      const target = btn.dataset.rail;
      if (GLOBAL_ROUTES.includes(target)) {
        window.location.hash = "#" + target;
        return;
      }
      const route = parseRoute();
      const id = route.level === "repair" ? route.id : getRepairId();
      if (id) window.location.hash = repairHash(id, target);
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
        maybeLoadGraph?.();
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
 * Derive {device, repair} from the current hash route and write it into the
 * store (shared/context.js) — the single read surface for views. For a repair
 * route, resolves the slug from the repair id (cache, else getRepair). For a
 * global route, clears the context. Returns a Promise that settles once the
 * store reflects the route — await it before mounting views on a deep-link.
 */
export async function syncContextFromUrl() {
  const route = parseRoute();
  if (route.level !== "repair") {
    setContext({ device: null, repair: null });
    return;
  }
  let slug = _slugByRepair.get(route.id);
  if (!slug) {
    try {
      const meta = await getRepair(route.id);   // RepairSummary { device_slug, ... }
      slug = meta?.device_slug || null;
      if (slug) _slugByRepair.set(route.id, slug);
    } catch (err) {
      console.warn("[router] could not resolve repair", route.id, err);
      slug = null;
    }
  }
  setContext({ device: slug, repair: route.id });
}

/**
 * Return the currently active repair session, derived from URL query params.
 * A session is defined by the SIMULTANEOUS presence of ?device= and ?repair=.
 * Re-derived on every call — zero hidden state.
 */
export function currentSession() {
  const device = getDeviceSlug();
  const repair = getRepairId();
  if (device && repair) return { device, repair };
  return null;
}

/**
 * Quit the active session: strip ?device= + ?repair=, hash to #home, close
 * chat panel, re-render the list. Called from the dashboard's Quitter button
 * and the topbar session pill's [×].
 */
export async function leaveSession() {
  // Clear context first so currentSession() reads null immediately.
  setContext({ device: null, repair: null });
  // Close the chat panel if open. llmClose is a <button>; if the panel
  // isn't mounted yet the optional chaining silently skips.
  document.getElementById("llmClose")?.click();
  // Navigate to the global repairs list. Setting the hash fires hashchange →
  // main.js's listener re-syncs context + mounts #home. We still drop the
  // dashboard + show the landing explicitly below in case we were already on
  // #home (no hashchange fires when the hash is unchanged).
  window.location.hash = "#home";
  navigate("home");
  // Quitting a session always returns to the landing hero — the tech is
  // declaring "I'm done with this repair", so the start screen (where they
  // can pick another device or open a new diagnostic) is the right next step.
  const { hideRepairDashboard } = await import("./features/repair/diagnostic/dashboard.js");
  hideRepairDashboard();
  const { showLanding } = await import("./features/global/landing/index.js");
  showLanding();
}

/**
 * Rewrite any pre-Phase-C URL into the new grammar, in place (replaceState, no
 * reload). Covers ?device=&repair=#section, ?tool=stock, bare #memory-bank /
 * #graphe. Safe no-op on already-canonical URLs. Keeps view=md in the real query
 * string (Decision A). Call before syncContextFromUrl on boot + each hashchange.
 */
export function migrateLegacyUrl() {
  const url = new URL(window.location.href);
  const params = url.searchParams;
  const device = params.get("device");
  const repair = params.get("repair");
  const tool = params.get("tool");
  let changed = false;

  if (tool === "stock") { params.delete("tool"); url.hash = "#stock"; changed = true; }

  if (repair && device) {
    const oldHash = (url.hash || "").replace(/^#/, "").split("?")[0];
    const sectionToVue = { home: "diagnostic", pcb: "pcb", schematic: "schematic",
                           graphe: "graph", "memory-bank": "graph" };
    const vue = sectionToVue[oldHash] || "diagnostic";
    if (oldHash === "memory-bank") params.set("view", "md");   // query string (Decision A)
    seedSlugForRepair(repair, device);                          // we know the slug — skip the fetch
    params.delete("device"); params.delete("repair");
    url.hash = repairHash(repair, vue);
    changed = true;
  } else if (device && !repair) {
    // Device-only legacy link (pack browse). No repair to scope to under the new
    // grammar → send to the global list; the pack is reachable by opening a repair.
    params.delete("device");
    url.hash = "#home";
    changed = true;
  }

  // Bare legacy section hashes with no repair are no longer canonical routes
  // (pcb/schematic/graphe/memory-bank are repair-only vues) → fall back to #home.
  const bareHash = (url.hash || "").replace(/^#/, "").split("?")[0].split("/")[0];
  if (bareHash === "memory-bank" || bareHash === "graphe") { url.hash = "#home"; changed = true; }

  if (changed) window.history.replaceState({}, "", url.toString());
}
