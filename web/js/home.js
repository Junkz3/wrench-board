// Home (journal des réparations) + the "nouvelle réparation" modal.
//
// renderHome() renders the repair grid from a /pipeline/repairs response
// (taxonomy supplies the brand > model > version grouping). The pure
// pack list at /pipeline/packs is no longer surfaced here — repairs own
// the home view and reuse pack metadata via the taxonomy payload.
// initNewRepairModal() wires the modal's open/close/submit handlers plus
// its own document-level keydown interceptor. The keydown listener is
// intentionally registered before main.js adds its global Cmd+K / Esc
// handler — stopImmediatePropagation() in this handler only works if it
// runs first.

import { openPipelineProgress } from './pipeline_progress.js';
import { leaveSession } from './router.js';
import { openPanel, closePanelIfConv } from './llm.js';
import { ICON_CHECK } from './icons.js';

export async function loadTaxonomy() {
  try {
    const res = await fetch("/pipeline/taxonomy");
    if (!res.ok) return {brands: {}, uncategorized: []};
    return await res.json();
  } catch (err) {
    console.warn("loadTaxonomy failed", err);
    return {brands: {}, uncategorized: []};
  }
}

export async function loadRepairs() {
  try {
    const res = await fetch("/pipeline/repairs");
    if (!res.ok) return [];
    return await res.json();
  } catch (err) {
    console.warn("loadRepairs failed", err);
    return [];
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]
  ));
}

function humanizeSlug(slug) {
  return slug.replace(/-/g, " ").replace(/^./, c => c.toUpperCase());
}

// Strip a trailing form_factor ("motherboard", "logic board") from a label
// that was typed with the form_factor glued on. Used when we don't have a
// taxonomy.model to fall back on.
function stripFormFactor(label, formFactor) {
  if (!label || !formFactor) return label;
  const ff = formFactor.trim();
  if (!ff) return label;
  const re = new RegExp("\\s+" + ff.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\$&") + "\\s*$", "i");
  return label.replace(re, "").trim() || label;
}

// The device NAME — what the board is, not what form it takes. Prefer the
// clean `taxonomy.model` (set by the Registry Builder from the dump) over
// the raw user-typed `device_label` which usually glues the form_factor on.
// Brand is included by default so the name reads standalone; set
// `includeBrand: false` inside brand-grouped UI sections.
function deviceName(entry, { includeBrand = true } = {}) {
  const brand = entry.brand || "";
  const model = entry.model || "";
  if (brand && model) return includeBrand ? `${brand} ${model}` : model;
  if (model) return model;
  return stripFormFactor(entry.device_label || humanizeSlug(entry.device_slug), entry.form_factor);
}

// Index the taxonomy so each repair can be resolved to {brand, model,
// form_factor, version} without an extra fetch per card.
function indexTaxonomyBySlug(taxonomy) {
  const index = new Map();
  for (const [brand, models] of Object.entries(taxonomy.brands || {})) {
    for (const [modelName, packs] of Object.entries(models)) {
      for (const p of packs) {
        index.set(p.device_slug, { ...p, brand, model: modelName });
      }
    }
  }
  for (const p of (taxonomy.uncategorized || [])) {
    index.set(p.device_slug, { ...p, brand: null, model: null });
  }
  return index;
}

function relativeTimeFr(isoString) {
  if (!isoString) return "—";
  const then = new Date(isoString);
  if (isNaN(then)) return isoString;
  const diffMs = Date.now() - then.getTime();
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return t("home.time.now");
  if (mins < 60) return t("home.time.minutes_ago", { n: mins });
  const hours = Math.floor(mins / 60);
  if (hours < 24) return t("home.time.hours_ago", { n: hours });
  const days = Math.floor(hours / 24);
  if (days === 1) return t("home.time.yesterday");
  if (days < 7) return t("home.time.days_ago", { n: days });
  const localeTag = (window.i18n && window.i18n.locale === "fr") ? "fr-FR" : "en-US";
  return then.toLocaleDateString(localeTag, { day: "numeric", month: "short", year: "numeric" });
}

function statusLabel(status) {
  if (status === "closed") return t("home.status.closed");
  if (status === "in_progress") return t("home.status.in_progress");
  return t("home.status.open");
}

function statusBadgeHTML(status) {
  const label = statusLabel(status);
  const cls = status === "closed" ? "ok" : (status === "in_progress" ? "warn" : "");
  return `<span class="badge ${cls}">${escapeHtml(label)}</span>`;
}

function repairCardHTML(repair, taxEntry) {
  const when = relativeTimeFr(repair.created_at);
  const symptom = repair.symptom || "—";
  const truncated = symptom.length > 120 ? symptom.slice(0, 118) + "…" : symptom;
  const deviceContext = taxEntry
    ? deviceName(taxEntry, { includeBrand: false })
    : repair.device_slug;
  const form = taxEntry?.form_factor
    ? `<span class="badge mono">${escapeHtml(taxEntry.form_factor)}</span>`
    : "";
  // Explicit #home hash so the bootstrap/hashchange dispatch renders the
  // dashboard (not the list) and not the graphe either. Query params are
  // preserved across later intra-section navigation.
  const href = `?device=${encodeURIComponent(repair.device_slug)}&repair=${encodeURIComponent(repair.repair_id)}#home`;
  return `
    <a class="home-card" href="${href}">
      <div class="repair-top">
        <div class="slug">${escapeHtml(repair.repair_id.slice(0, 8))} · ${escapeHtml(when)}</div>
        <div class="badges">${statusBadgeHTML(repair.status)}${form}</div>
      </div>
      <div class="name">${escapeHtml(deviceContext)}</div>
      <div class="repair-symptom">${escapeHtml(truncated)}</div>
    </a>
  `;
}

function deviceBlockHTML(taxEntry, repairs) {
  const sorted = repairs.slice().sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  const cards = sorted.map(r => repairCardHTML(r, taxEntry)).join("");
  const modelName = taxEntry?.model || deviceName(taxEntry, { includeBrand: false });
  const repairsLabel = sorted.length > 1
    ? t("home.list.repairs_many")
    : t("home.list.repairs_one");
  return `
    <div class="home-model">
      <div class="home-model-head">
        <span class="home-model-name">${escapeHtml(modelName)}</span>
        <span class="home-model-count mono">${sorted.length} ${escapeHtml(repairsLabel)}</span>
      </div>
      <div class="home-grid">${cards}</div>
    </div>
  `;
}

function brandBlockHTML(brandName, devicesMap) {
  const slugs = Array.from(devicesMap.keys()).sort((a, b) => a.localeCompare(b));
  const totalRepairs = slugs.reduce((n, s) => n + devicesMap.get(s).repairs.length, 0);
  const repairsLabel = totalRepairs > 1
    ? t("home.list.repairs_many")
    : t("home.list.repairs_one");
  const devicesLabel = slugs.length > 1
    ? t("home.list.devices_many")
    : t("home.list.devices_one");
  const counter = t("home.list.summary", {
    repairs: totalRepairs,
    repairsLabel,
    devices: slugs.length,
    devicesLabel,
  });
  const body = slugs
    .map(slug => {
      const { taxEntry, repairs } = devicesMap.get(slug);
      return deviceBlockHTML(taxEntry, repairs);
    })
    .join("");
  return `
    <section class="home-brand">
      <header class="home-brand-head">
        <h2 class="home-brand-name">${escapeHtml(brandName)}</h2>
        <span class="home-brand-count mono">${escapeHtml(counter)}</span>
      </header>
      <div class="home-brand-body">${body}</div>
    </section>
  `;
}

export function renderHome(taxonomy, repairs = []) {
  const container = document.getElementById("homeSections");
  const empty = document.getElementById("homeEmpty");
  container.innerHTML = "";

  if (!repairs || repairs.length === 0) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  const taxIndex = indexTaxonomyBySlug(taxonomy);
  const uncatLabel = t("home.list.uncategorized");
  // Group repairs by brand → device_slug → list of repairs.
  const byBrand = new Map();  // brand → Map(slug → {taxEntry, repairs})
  for (const r of repairs) {
    const tax = taxIndex.get(r.device_slug) || null;
    const brand = tax?.brand || uncatLabel;
    if (!byBrand.has(brand)) byBrand.set(brand, new Map());
    const devices = byBrand.get(brand);
    if (!devices.has(r.device_slug)) {
      devices.set(r.device_slug, {
        taxEntry: tax || { device_slug: r.device_slug, device_label: r.device_label },
        repairs: [],
      });
    }
    devices.get(r.device_slug).repairs.push(r);
  }

  const brandNames = Array.from(byBrand.keys()).sort((a, b) => {
    if (a === uncatLabel) return 1;
    if (b === uncatLabel) return -1;
    return a.localeCompare(b);
  });
  container.innerHTML = brandNames
    .map(brand => brandBlockHTML(brand, byBrand.get(brand)))
    .join("");
}

// ───────────────────────────────────────────────────────────────
// Repair dashboard — the focused "session hub" state of #home.
// Activated when currentSession() returns non-null.
// ───────────────────────────────────────────────────────────────

export async function renderRepairDashboard(session) {
  const { device: slug, repair: rid } = session;

  // Toggle visibility: hide list states, show dashboard.
  document.getElementById("homeSections")?.classList.add("hidden");
  document.getElementById("homeEmpty")?.classList.add("hidden");
  document.getElementById("repairDashboard")?.classList.remove("hidden");
  // Also hide the list's H1 / CTA while in dashboard mode.
  document.querySelector("#homeSection .home-head")?.classList.add("hidden");

  // Fetch in parallel — list of Promise results, each tolerates failure.
  const [repair, convs, pack, findings, taxonomy, sourcesData] = await Promise.all([
    fetchJSON(`/pipeline/repairs/${encodeURIComponent(rid)}`, null),
    fetchJSON(`/pipeline/repairs/${encodeURIComponent(rid)}/conversations`, { conversations: [] }),
    fetchJSON(`/pipeline/packs/${encodeURIComponent(slug)}`, null),
    fetchJSON(`/pipeline/packs/${encodeURIComponent(slug)}/findings`, []),
    loadTaxonomy(),
    fetchJSON(`/pipeline/packs/${encodeURIComponent(slug)}/sources`, null),
  ]);

  const taxIndex = indexTaxonomyBySlug(taxonomy);
  const taxEntry = taxIndex.get(slug) || null;

  renderDashboardHeader(repair, taxEntry, slug, rid);
  renderDashboardData(slug, rid, pack, sourcesData);
  renderCapabilities(pack);
  renderDashboardConvs(convs.conversations || [], rid);
  renderDashboardFindings(findings, rid);
  renderDashboardTimeline(repair, convs.conversations || [], findings, pack);
  renderDashboardPack(pack, slug, rid);
  wireDashboardHandlers();
  wireUploadHandlers(slug, rid);
  wireFixButton(slug, rid);
  maybeAutoResumeBuildWatch(slug, rid, pack);
}

// Mid-dashboard re-render after an upload completes — same payload as the
// initial mount, but we don't touch conversations / findings / timeline
// because those are unaffected by a boardview/schematic upload.
async function refreshDashboardData(slug, rid) {
  const [pack, sourcesData] = await Promise.all([
    fetchJSON(`/pipeline/packs/${encodeURIComponent(slug)}`, null),
    fetchJSON(`/pipeline/packs/${encodeURIComponent(slug)}/sources`, null),
  ]);
  renderDashboardData(slug, rid, pack, sourcesData);
  renderCapabilities(pack);
  // After an upload of a schematic_pdf, the backend kicks the vision
  // pipeline in `asyncio.create_task` but doesn't push WS events for
  // it. Resume the polling watcher here so the spinner / ETA / final
  // toast all happen without a manual reload. No-op when (a) a watcher
  // is already running, (b) no PDF on disk, or (c) electrical_graph
  // already compiled.
  maybeAutoResumeBuildWatch(slug, rid, pack);
  renderDashboardPack(pack, slug, rid);
}

export function hideRepairDashboard() {
  document.getElementById("repairDashboard")?.classList.add("hidden");
  document.getElementById("homeSections")?.classList.remove("hidden");
  document.querySelector("#homeSection .home-head")?.classList.remove("hidden");
  document.getElementById("dashboardFixBtn")?.classList.add("hidden");
}

async function fetchJSON(url, fallback) {
  try {
    const res = await fetch(url);
    if (!res.ok) return fallback;
    return await res.json();
  } catch (err) {
    console.warn("[dashboard] fetch failed", url, err);
    return fallback;
  }
}

function renderDashboardHeader(repair, taxEntry, slug, rid) {
  const slugEl = document.getElementById("rdSlug");
  const deviceEl = document.getElementById("rdDevice");
  const symptomEl = document.getElementById("rdSymptom");
  const badgesEl = document.getElementById("rdBadges");
  if (!slugEl || !deviceEl || !symptomEl || !badgesEl) return;

  slugEl.textContent = slug;
  deviceEl.textContent = taxEntry
    ? deviceName(taxEntry, { includeBrand: true })
    : (repair?.device_label || humanizeSlug(slug));
  symptomEl.textContent = repair?.symptom || "—";

  const created = repair?.created_at ? relativeTimeFr(repair.created_at) : "—";
  const status = repair?.status || "open";
  const form = taxEntry?.form_factor
    ? `<span class="badge mono">${escapeHtml(taxEntry.form_factor)}</span>`
    : "";
  badgesEl.innerHTML =
    `${statusBadgeHTML(status)}` +
    `<span class="badge mono">${escapeHtml(rid.slice(0, 8))}</span>` +
    form +
    `<span class="rd-created">${escapeHtml(t("home.dashboard.created_at", { when: created }))}</span>`;
}

const ICONS = {
  arrowRight: '<svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
  upload:     '<svg viewBox="0 0 24 24"><path d="M12 17V5"/><path d="M5 12l7-7 7 7"/><path d="M5 19h14"/></svg>',
};

// Pretty file-size formatter — KB/MB with one decimal. Used in card metas
// after an upload so the tech sees "iphone-x.brd · 2.4 MB" not raw bytes.
function fmtBytes(n) {
  if (!Number.isFinite(n) || n <= 0) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

// ───────────────────────────────────────────────────────────────
// Data-aware dashboard — per-input cards + per-derived-data cards.
// Each card boils down to one of: on / off / building / loading / error.
// ───────────────────────────────────────────────────────────────

// Click-toggle handler for the diagnostic-ready badge popover.
// Bound once per session — re-calling _wireDiagPopover() after the
// first time is a no-op thanks to the `_diagWired` guard.
let _diagWired = false;
function _wireDiagPopover() {
  if (_diagWired) return;
  const badge = document.getElementById("rdCardBoardviewDiagBadge");
  const popover = document.getElementById("rdCardBoardviewDiagPopover");
  if (!badge || !popover) return;
  const setOpen = (open) => {
    popover.hidden = !open;
    badge.setAttribute("aria-expanded", open ? "true" : "false");
  };
  badge.addEventListener("click", (e) => {
    e.stopPropagation();
    setOpen(popover.hidden);
  });
  document.addEventListener("click", (e) => {
    if (popover.hidden) return;
    if (!popover.contains(e.target) && e.target !== badge) setOpen(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !popover.hidden) setOpen(false);
  });
  _diagWired = true;
}

function renderDashboardData(slug, rid, pack, sourcesData) {
  const qs = `?device=${encodeURIComponent(slug)}&repair=${encodeURIComponent(rid)}`;
  const schemVersions = sourcesData?.schematic_pdf?.versions || [];
  const bvVersions = sourcesData?.boardview?.versions || [];

  // ── INPUT 1 — Schematic PDF ────────────────────────────────────────
  setCardState("rdCardSchematic", pack?.has_schematic_pdf ? "on" : "off");
  setCardField("rdCardSchematicState", pack?.has_schematic_pdf
    ? (pack.has_electrical_graph
        ? t("home.dashboard.schematic.state_compiled")
        : t("home.dashboard.schematic.state_compiling"))
    : t("home.dashboard.schematic.state_to_import"));
  const schemMetaSuffix = schemVersions.length > 1
    ? t("home.dashboard.schematic.meta_versions_suffix", { n: schemVersions.length })
    : "";
  setCardField("rdCardSchematicMeta", pack?.has_schematic_pdf
    ? ((pack.has_electrical_graph
        ? t("home.dashboard.schematic.meta_compiled")
        : t("home.dashboard.schematic.meta_imported_compiling")) + schemMetaSuffix)
    : t("home.dashboard.schematic.meta_missing"));
  toggleEl("rdCardSchematicLoss", !pack?.has_schematic_pdf);

  const schemActions = document.getElementById("rdCardSchematicActions");
  if (schemActions) {
    schemActions.innerHTML = "";
    if (pack?.has_schematic_pdf) {
      schemActions.appendChild(linkButton(`${qs}#schematic`,
        ICONS.arrowRight + " " + escapeHtml(t("home.dashboard.schematic.open")), "is-primary"));
      schemActions.appendChild(actionButton(
        ICONS.upload + " " + escapeHtml(t("home.dashboard.schematic.import_version")), () => {
          document.getElementById("rdUploadSchematic")?.click();
        }));
    } else {
      schemActions.appendChild(actionButton(
        ICONS.upload + " " + escapeHtml(t("home.dashboard.schematic.import_pdf")), () => {
          document.getElementById("rdUploadSchematic")?.click();
        }, "is-warn"));
    }
  }
  renderVersionList("rdCardSchematic", "schematic_pdf", schemVersions, slug, rid);

  // ── INPUT 2 — Boardview ─────────────────────────────────────────────
  setCardState("rdCardBoardview", pack?.has_boardview ? "on" : "off");
  setCardField("rdCardBoardviewState", pack?.has_boardview
    ? t("home.dashboard.boardview.state_imported")
    : t("home.dashboard.boardview.state_to_import"));
  setCardField("rdCardBoardviewFmt", pack?.boardview_format
    ? t("home.dashboard.boardview.fmt_format", { format: pack.boardview_format })
    : t("home.dashboard.boardview.fmt_default"));
  const bvMetaSuffix = bvVersions.length > 1
    ? t("home.dashboard.schematic.meta_versions_suffix", { n: bvVersions.length })
    : "";
  setCardField("rdCardBoardviewMeta", pack?.has_boardview
    ? t("home.dashboard.boardview.meta_imported", {
        format: pack.boardview_format || t("home.dashboard.boardview.fmt_detected"),
        suffix: bvMetaSuffix,
      })
    : t("home.dashboard.boardview.meta_missing"));
  toggleEl("rdCardBoardviewLoss", !pack?.has_boardview);

  // Diagnostic-ready badge — visible only when the loaded boardview
  // ships manufacturer-tagged net references (XZZ post-v6 resistance
  // / voltage section). Lazy-fetched via /api/board/render so we
  // don't pay the parse cost on devices without a boardview. Click
  // the badge to open a localized FR popover (i18n-driven body).
  const diagWrapEl = document.getElementById("rdCardBoardviewDiagWrap");
  if (diagWrapEl) diagWrapEl.hidden = true;
  if (pack?.has_boardview && diagWrapEl) {
    fetch(`/api/board/render?slug=${encodeURIComponent(slug)}`)
      .then(r => r.ok ? r.json() : null)
      .then(payload => {
        const refs = payload?.net_diagnostics?.length || 0;
        if (refs > 0) {
          const countEl = document.getElementById("rdCardBoardviewDiagCount");
          if (countEl) countEl.textContent = refs.toString();
          diagWrapEl.hidden = false;
          _wireDiagPopover();
        }
      })
      .catch(() => { /* fail-quiet — the badge just stays hidden */ });
  }

  const bvActions = document.getElementById("rdCardBoardviewActions");
  if (bvActions) {
    bvActions.innerHTML = "";
    if (pack?.has_boardview) {
      bvActions.appendChild(linkButton(`${qs}#pcb`,
        ICONS.arrowRight + " " + escapeHtml(t("home.dashboard.boardview.open")), "is-primary"));
      bvActions.appendChild(actionButton(
        ICONS.upload + " " + escapeHtml(t("home.dashboard.boardview.import_version")), () => {
          document.getElementById("rdUploadBoardview")?.click();
        }));
    } else {
      bvActions.appendChild(actionButton(
        ICONS.upload + " " + escapeHtml(t("home.dashboard.boardview.import_boardview")), () => {
          document.getElementById("rdUploadBoardview")?.click();
        }, "is-warn"));
    }
  }
  renderVersionList("rdCardBoardview", "boardview", bvVersions, slug, rid);

  // ── DERIVED 1 — Knowledge graph (causal pack) ──────────────────────
  const packComplete = !!(pack && pack.has_registry && pack.has_knowledge_graph
    && pack.has_rules && pack.has_dictionary && pack.has_audit_verdict);
  const packPartial = !!(pack && (pack.has_registry || pack.has_knowledge_graph
    || pack.has_rules || pack.has_dictionary));
  const knowledgeState = packComplete ? "on" : (packPartial ? "building" : "off");
  setCardState("rdCardKnowledge", knowledgeState);
  setCardField("rdCardKnowledgeState",
    packComplete ? t("home.dashboard.knowledge.state_approved")
    : packPartial ? t("home.dashboard.knowledge.state_building")
    : t("home.dashboard.knowledge.state_empty"));
  setCardField("rdCardKnowledgeMeta",
    packComplete ? t("home.dashboard.knowledge.meta_complete")
    : packPartial ? t("home.dashboard.knowledge.meta_building")
    : t("home.dashboard.knowledge.meta_off"));
  const knowledgeActions = document.getElementById("rdCardKnowledgeActions");
  if (knowledgeActions) {
    knowledgeActions.innerHTML = "";
    if (packComplete || packPartial) {
      knowledgeActions.appendChild(linkButton(`${qs}#graphe`,
        ICONS.arrowRight + " " + escapeHtml(t("home.dashboard.knowledge.open_graph")),
        packComplete ? "is-primary" : ""));
    }
  }

  // ── DERIVED 2 — Electrical graph (compiled from schematic PDF) ──────
  const electricalState = pack?.has_electrical_graph
    ? "on"
    : (pack?.has_schematic_pdf ? "building" : "off");
  setCardState("rdCardElectrical", electricalState);
  setCardField("rdCardElectricalState", pack?.has_electrical_graph
    ? t("home.dashboard.electrical.state_compiled")
    : (pack?.has_schematic_pdf
        ? t("home.dashboard.electrical.state_compiling")
        : t("home.dashboard.electrical.state_unavailable")));
  setCardField("rdCardElectricalMeta", pack?.has_electrical_graph
    ? t("home.dashboard.electrical.meta_compiled")
    : (pack?.has_schematic_pdf
        ? t("home.dashboard.electrical.meta_compiling")
        : t("home.dashboard.electrical.meta_off")));
  const electricalActions = document.getElementById("rdCardElectricalActions");
  if (electricalActions) {
    electricalActions.innerHTML = "";
    if (pack?.has_electrical_graph) {
      electricalActions.appendChild(linkButton(`${qs}#schematic`,
        ICONS.arrowRight + " " + escapeHtml(t("home.dashboard.electrical.open")), "is-primary"));
    }
  }

  // ── DERIVED 3 — Memory bank (rules + findings + dictionary) ────────
  const memoryState = pack?.has_rules ? "on" : (pack?.has_registry ? "building" : "off");
  setCardState("rdCardMemory", memoryState);
  setCardField("rdCardMemoryState", pack?.has_rules
    ? t("home.dashboard.memory.state_active")
    : (pack?.has_registry
        ? t("home.dashboard.memory.state_building")
        : t("home.dashboard.memory.state_empty")));
  setCardField("rdCardMemoryMeta", pack?.has_rules
    ? t("home.dashboard.memory.meta_active")
    : (pack?.has_registry
        ? t("home.dashboard.memory.meta_building")
        : t("home.dashboard.memory.meta_off")));
  const memoryActions = document.getElementById("rdCardMemoryActions");
  if (memoryActions) {
    memoryActions.innerHTML = "";
    if (pack?.has_rules || pack?.has_registry) {
      memoryActions.appendChild(linkButton(`${qs}&view=md#graphe`,
        ICONS.arrowRight + " " + escapeHtml(t("home.dashboard.memory.open")),
        pack?.has_rules ? "is-primary" : ""));
    }
  }
}

// Capability banner — single ribbon at the top showing what the AI has
// access to right now. Reads as a mission-status header.
function renderCapabilities(pack) {
  const cap = document.getElementById("rdCap");
  const title = document.getElementById("rdCapTitle");
  const body = document.getElementById("rdCapBody");
  const score = document.getElementById("rdCapScore");
  const list = document.getElementById("rdCapList");
  if (!cap || !title || !body || !score || !list) return;

  const flags = {
    schematic: !!pack?.has_schematic_pdf,
    boardview: !!pack?.has_boardview,
    graph:     !!(pack && pack.has_knowledge_graph && pack.has_rules),
    memory:    !!pack?.has_rules,
  };
  const onCount = Object.values(flags).filter(Boolean).length;
  let level = "minimal";
  let label = t("home.dashboard.cap.minimal_label");
  let blurb = t("home.dashboard.cap.minimal_blurb");
  if (onCount === 4) {
    level = "full";
    label = t("home.dashboard.cap.full_label");
    blurb = t("home.dashboard.cap.full_blurb");
  } else if (onCount >= 2) {
    level = "partial";
    label = t("home.dashboard.cap.partial_label");
    blurb = t("home.dashboard.cap.partial_blurb");
  } else if (onCount === 1) {
    level = "minimal";
    label = t("home.dashboard.cap.minimal_partial_label");
    blurb = t("home.dashboard.cap.minimal_partial_blurb");
  } else {
    level = "minimal";
    label = t("home.dashboard.cap.cold_label");
    blurb = t("home.dashboard.cap.cold_blurb");
  }
  cap.dataset.level = level;
  title.textContent = label;
  body.textContent = blurb;
  score.textContent = t("home.dashboard.cap.score", { n: onCount });

  const rows = [
    { key: "schematic", label: t("home.dashboard.cap.row.schematic_label"), on: t("home.dashboard.cap.row.schematic_on"), off: t("home.dashboard.cap.row.off") },
    { key: "boardview", label: t("home.dashboard.cap.row.boardview_label"), on: t("home.dashboard.cap.row.boardview_on"), off: t("home.dashboard.cap.row.off") },
    { key: "graph",     label: t("home.dashboard.cap.row.graph_label"),     on: t("home.dashboard.cap.row.graph_on"),     off: t("home.dashboard.cap.row.off") },
    { key: "memory",    label: t("home.dashboard.cap.row.memory_label"),    on: t("home.dashboard.cap.row.memory_on"),    off: t("home.dashboard.cap.row.off") },
  ];
  list.innerHTML = rows.map(r => {
    const on = flags[r.key];
    return `<li class="rd-cap-pill ${on ? "on" : "off"}">
      <span class="rd-cap-pill-dot"></span>
      <span class="rd-cap-pill-label">${escapeHtml(r.label)}</span>
      <span class="rd-cap-pill-tag">${escapeHtml(on ? r.on : r.off)}</span>
    </li>`;
  }).join("");
}

// Render the list of uploaded versions inside a card (only when 2+ exist).
// One row per version: radio + filename · timestamp · size. Click a non-active
// row to switch the active pin via PUT /sources/{kind}.
function renderVersionList(cardId, kind, versions, slug, rid) {
  const card = document.getElementById(cardId);
  if (!card) return;
  let host = card.querySelector(".rd-versions");
  if (!host) {
    host = document.createElement("div");
    host.className = "rd-versions";
    card.appendChild(host);
  }
  if (!versions || versions.length < 2) {
    host.remove();
    return;
  }
  host.innerHTML = `<div class="rd-versions-head">
    <span class="rd-versions-tag">${escapeHtml(t("home.version.tag"))}</span>
    <span class="rd-versions-count">${versions.length}</span>
  </div>`;
  for (const v of versions) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "rd-version-row" + (v.is_active ? " is-active" : "");
    row.disabled = !!v.is_active;
    const dateLabel = formatVersionDate(v.timestamp);
    row.innerHTML = `
      <span class="rd-version-dot" aria-hidden="true"></span>
      <span class="rd-version-name">${escapeHtml(v.original_name)}</span>
      <span class="rd-version-meta">${escapeHtml(dateLabel)} · ${escapeHtml(fmtBytes(v.size_bytes))}</span>
    `;
    if (!v.is_active) {
      row.addEventListener("click", () => switchSource(slug, rid, kind, v));
    }
    host.appendChild(row);
  }
}

// Parse the ISO-like upload timestamp `20260424T130000Z` into a short
// fr-locale label `24 avr · 13:00`. Falls back to the raw string on parse fail.
function formatVersionDate(ts) {
  if (!ts) return "—";
  const m = ts.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/);
  if (!m) return ts;
  const [, y, mo, d, h, mi] = m;
  const dt = new Date(`${y}-${mo}-${d}T${h}:${mi}:00Z`);
  if (isNaN(dt)) return ts;
  const localeTag = (window.i18n && window.i18n.locale === "fr") ? "fr-FR" : "en-US";
  return dt.toLocaleString(localeTag, {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  }).replace(",", " ·");
}

// ─── Build watcher (schematic recompile spinner + ETA + polling) ───────
// One global state because at most one schematic rebuild can run on a
// device at a time (the backend serialises). Frontend keeps a 1-second
// countdown for ETA display, and a slower 8s poll on /pipeline/packs/{slug}
// to detect completion (has_electrical_graph flips back to true).
let _buildState = null;

function startBuildWatch(slug, rid, etaSeconds, pageCount) {
  stopBuildWatch();
  _buildState = {
    slug, rid,
    pageCount,
    remaining: etaSeconds || 0,
    countdownId: null,
    pollId: null,
  };
  renderBuildIndicators(_buildState);
  _buildState.countdownId = setInterval(() => {
    if (!_buildState) return;
    _buildState.remaining = Math.max(0, _buildState.remaining - 1);
    renderBuildIndicators(_buildState);
  }, 1000);
  _buildState.pollId = setInterval(async () => {
    if (!_buildState) return;
    const pack = await fetchJSON(`/pipeline/packs/${encodeURIComponent(slug)}`, null);
    if (pack?.has_electrical_graph) {
      stopBuildWatch();
      showToast("ok",
        t("home.toast.compile_done_title"),
        t("home.toast.compile_done_sub"));
      await refreshDashboardData(slug, rid);
    }
  }, 8000);
}

function stopBuildWatch() {
  if (!_buildState) return;
  if (_buildState.countdownId) clearInterval(_buildState.countdownId);
  if (_buildState.pollId) clearInterval(_buildState.pollId);
  _buildState = null;
  document.querySelectorAll(".rd-build-eta").forEach(el => el.remove());
}

function renderBuildIndicators(state) {
  for (const cardId of ["rdCardSchematic", "rdCardElectrical"]) {
    const card = document.getElementById(cardId);
    if (!card) continue;
    let eta = card.querySelector(".rd-build-eta");
    if (!eta) {
      eta = document.createElement("div");
      eta.className = "rd-build-eta";
      eta.innerHTML =
        '<span class="rd-build-spinner" aria-hidden="true">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">' +
            '<circle cx="12" cy="12" r="9" opacity=".25"/>' +
            '<path d="M21 12a9 9 0 00-9-9"/>' +
          '</svg>' +
        '</span>' +
        '<span class="rd-build-text"></span>';
      card.appendChild(eta);
    }
    const txt = eta.querySelector(".rd-build-text");
    if (state.remaining > 0) {
      txt.textContent = state.pageCount
        ? t("home.dashboard.build.vision_pipeline_pages", {
            n: state.pageCount, remaining: formatRemaining(state.remaining),
          })
        : t("home.dashboard.build.vision_pipeline", {
            remaining: formatRemaining(state.remaining),
          });
    } else if (state.remaining === 0 && state.pageCount) {
      txt.textContent = t("home.dashboard.build.vision_pipeline_finalizing_pages", {
        n: state.pageCount,
      });
    } else {
      txt.textContent = t("home.dashboard.build.vision_pipeline_running");
    }
  }
}

function formatRemaining(sec) {
  if (sec >= 60) {
    const min = Math.ceil(sec / 60);
    return t("home.dashboard.build.remaining_min", { n: min });
  }
  return t("home.dashboard.build.remaining_sec", { n: sec });
}

// Demo cache-hit animation for schematic re-imports. Fired by
// handleUpload when `has_electrical_graph` was already true BEFORE
// the POST — typical when the tech re-uploads the same PDF during a
// demo run. We fake the visible pipeline side (spinner + ETA on
// Schematic + Electrical cards) for ~12s, then refreshDashboardData
// syncs reality. Boardview is intentionally NOT covered here:
// re-import is cheap (no pipeline) and the instant flip is fine.
async function playFakeIngestTimeline(slug, rid) {
  const TOTAL_SEC = 12;
  setCardState("rdCardSchematic", "building");
  setCardState("rdCardElectrical", "building");
  // Reuse the real watcher's renderer so the visible chrome
  // (spinner + ETA text) is identical to a true rebuild.
  const fakeState = {
    slug, rid,
    pageCount: null,
    remaining: TOTAL_SEC,
    countdownId: null,
    pollId: null,
  };
  renderBuildIndicators(fakeState);
  fakeState.countdownId = setInterval(() => {
    fakeState.remaining = Math.max(0, fakeState.remaining - 1);
    renderBuildIndicators(fakeState);
  }, 1000);
  await new Promise((r) => setTimeout(r, TOTAL_SEC * 1000));
  clearInterval(fakeState.countdownId);
  document.querySelectorAll(".rd-build-eta").forEach((el) => el.remove());
  await refreshDashboardData(slug, rid);
}

// Auto-resume: if we land on the dashboard while the schematic PDF exists
// but the electrical graph is missing, a rebuild is in flight from a prior
// session — start the watcher with no countdown (just polling).
function maybeAutoResumeBuildWatch(slug, rid, pack) {
  if (_buildState) return; // already watching
  if (!pack?.has_schematic_pdf) return;
  if (pack.has_electrical_graph) return;
  startBuildWatch(slug, rid, 0, null);
}

async function switchSource(slug, rid, kind, version) {
  const label = kind === "schematic_pdf"
    ? t("home.toast.kind_schematic")
    : t("home.toast.kind_boardview");
  showToast("info",
    t("home.toast.switch_in_progress", { kind: label }),
    `${version.original_name} · ${fmtBytes(version.size_bytes)}`);

  // Pre-flight UX: flip the relevant card to building so the technician
  // sees something happening even before the PUT response lands.
  if (kind === "schematic_pdf") {
    setCardState("rdCardSchematic", "building");
    setCardState("rdCardElectrical", "building");
  }

  try {
    const res = await fetch(
      `/pipeline/packs/${encodeURIComponent(slug)}/sources/${kind}`,
      {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ filename: version.filename }),
      },
    );
    if (!res.ok) {
      let detail = "";
      try { detail = (await res.json()).detail || ""; } catch (_) { /* noop */ }
      showToast("warn",
        t("home.toast.switch_failed_title"),
        t("home.toast.switch_failed_sub", {
          status: res.status,
          detail: detail || t("home.toast.switch_failed_retry"),
        }));
      await refreshDashboardData(slug, rid);
      return;
    }
    const body = await res.json();
    if (body.status === "cached") {
      showToast("ok",
        t("home.toast.version_cached_title"),
        t("home.toast.version_cached_sub", { name: version.original_name }));
    } else if (body.status === "rebuilding") {
      const pages = body.page_count ? ` · ${body.page_count} pages` : "";
      const eta = body.eta_seconds ? ` · ~${formatRemaining(body.eta_seconds)}` : "";
      showToast("info",
        t("home.toast.rebuilding_title"),
        t("home.toast.rebuilding_sub", {
          name: version.original_name,
          pages,
          eta,
        }));
      startBuildWatch(slug, rid, body.eta_seconds || 0, body.page_count || null);
    } else {
      showToast("ok",
        t("home.toast.pin_updated_title"),
        t("home.toast.pin_updated_sub", { name: version.original_name }));
    }
    // Drop the PCB viewer's payload cache so the next #pcb visit
    // refetches /api/board/render and parses the freshly-pinned file
    // — without this, the bridge's slug cache would serve the stale
    // version because the slug itself didn't change.
    if (kind === "boardview"
        && window.Boardview
        && typeof window.Boardview.invalidate === "function") {
      window.Boardview.invalidate(slug);
    }
    await refreshDashboardData(slug, rid);
  } catch (err) {
    console.error("switchSource failed", err);
    showToast("warn",
      t("home.toast.network_title"),
      t("home.toast.network_sub"));
    await refreshDashboardData(slug, rid);
  }
}

// Helpers ───────────────────────────────────────────────────────
function setCardState(id, state) {
  const el = document.getElementById(id);
  if (el) el.dataset.state = state;
}
function setCardField(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}
function toggleEl(id, on) {
  const el = document.getElementById(id);
  if (el) el.hidden = !on;
}
function linkButton(href, html, extra = "") {
  const a = document.createElement("a");
  a.className = `rd-data-card-btn ${extra}`.trim();
  a.href = href;
  a.innerHTML = html;
  return a;
}
function actionButton(html, onclick, extra = "") {
  const b = document.createElement("button");
  b.type = "button";
  b.className = `rd-data-card-btn ${extra}`.trim();
  b.innerHTML = html;
  b.addEventListener("click", onclick);
  return b;
}

// ───────────────────────────────────────────────────────────────
// Upload wiring — POST /pipeline/packs/{slug}/documents
// Schematic = .pdf  →  kind=schematic_pdf
// Boardview = parser-supported extensions  →  kind=boardview
// ───────────────────────────────────────────────────────────────
let _uploadHandlersWired = false;
function wireUploadHandlers(slug, rid) {
  // Always re-bind the per-session slug/rid even on re-mount.
  const schemInput = document.getElementById("rdUploadSchematic");
  const bvInput = document.getElementById("rdUploadBoardview");
  if (schemInput) {
    schemInput.value = "";
    schemInput.onchange = (ev) => {
      const file = ev.target.files?.[0];
      if (file) handleUpload(slug, rid, file, "schematic_pdf");
      ev.target.value = "";
    };
  }
  if (bvInput) {
    bvInput.value = "";
    bvInput.onchange = (ev) => {
      const file = ev.target.files?.[0];
      if (file) handleUpload(slug, rid, file, "boardview");
      ev.target.value = "";
    };
  }
  if (_uploadHandlersWired) return;
  _uploadHandlersWired = true;

  // Drag-drop on the off-state cards. Visual hint via .is-dragover.
  const wireDrop = (cardId, kind) => {
    const card = document.getElementById(cardId);
    if (!card) return;
    card.addEventListener("dragenter", (ev) => {
      if (card.dataset.state !== "off") return;
      ev.preventDefault();
      card.classList.add("is-dragover");
    });
    card.addEventListener("dragover", (ev) => {
      if (card.dataset.state !== "off") return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "copy";
    });
    card.addEventListener("dragleave", () => card.classList.remove("is-dragover"));
    card.addEventListener("drop", (ev) => {
      ev.preventDefault();
      card.classList.remove("is-dragover");
      const file = ev.dataTransfer?.files?.[0];
      if (!file) return;
      const slugNow = new URLSearchParams(window.location.search).get("device");
      const ridNow = new URLSearchParams(window.location.search).get("repair");
      if (!slugNow || !ridNow) return;
      handleUpload(slugNow, ridNow, file, kind);
    });
  };
  wireDrop("rdCardSchematic", "schematic_pdf");
  wireDrop("rdCardBoardview", "boardview");
}

async function handleUpload(slug, rid, file, kind) {
  const cardId = kind === "schematic_pdf" ? "rdCardSchematic" : "rdCardBoardview";
  const card = document.getElementById(cardId);
  if (card) card.dataset.state = "building";

  // Snapshot `has_electrical_graph` BEFORE the upload — used to detect
  // a "fake import" (re-upload of a schematic PDF on a device that
  // already has the derived graph) so the demo plays an animated
  // rebuild instead of an instant flip. Boardview has no derived
  // artefact that survives a source delete, and a re-import is cheap
  // anyway (no pipeline behind it), so we skip the fake path for it.
  let preExisting = false;
  if (kind === "schematic_pdf") {
    try {
      const pre = await fetchJSON(`/pipeline/packs/${encodeURIComponent(slug)}`, null);
      if (pre) preExisting = Boolean(pre.has_electrical_graph);
    } catch (_) { /* fall through to normal flow */ }
  }

  const kindLabel = kind === "schematic_pdf"
    ? t("home.toast.kind_schematic")
    : t("home.toast.kind_boardview");
  showToast("info",
    t("home.toast.import_in_progress", { kind: kindLabel }),
    `${file.name} · ${fmtBytes(file.size)}`);

  const fd = new FormData();
  fd.append("kind", kind);
  fd.append("file", file);

  try {
    const res = await fetch(`/pipeline/packs/${encodeURIComponent(slug)}/documents`, {
      method: "POST",
      body: fd,
    });
    if (!res.ok) {
      let detail = "";
      try { detail = (await res.json()).detail || ""; } catch (_) { /* noop */ }
      showToast("warn",
        t("home.toast.import_failed"),
        t("home.toast.switch_failed_sub", {
          status: res.status,
          detail: detail || t("home.toast.import_failed_retry"),
        }));
      // Restore previous state on failure.
      await refreshDashboardData(slug, rid);
      return;
    }
    showToast("ok",
      t("home.toast.import_done"),
      `${file.name} · ${fmtBytes(file.size)}`);
    if (preExisting) {
      // Demo cache-hit path: backend short-circuits (cache-hashed PDF)
      // and we simulate ~12s of visible vision-pipeline activity on
      // the Schematic + Electrical cards before refreshing.
      await playFakeIngestTimeline(slug, rid);
    } else {
      await refreshDashboardData(slug, rid);
    }
  } catch (err) {
    console.error("upload failed", err);
    showToast("warn",
      t("home.toast.network_title"),
      t("home.toast.network_sub"));
    await refreshDashboardData(slug, rid);
  }
}

let _toastTimer = null;
function showToast(tone, title, sub) {
  const toast = document.getElementById("rdToast");
  const titleEl = document.getElementById("rdToastTitle");
  const subEl = document.getElementById("rdToastSub");
  const iconEl = document.getElementById("rdToastIcon");
  if (!toast || !titleEl || !subEl || !iconEl) return;
  toast.dataset.tone = tone;
  titleEl.textContent = title;
  subEl.textContent = sub || "";
  iconEl.innerHTML = tone === "ok"
    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"/></svg>'
    : tone === "warn"
    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>'
    : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9" opacity=".3"/><path d="M21 12a9 9 0 00-9-9"><animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="1s" repeatCount="indefinite"/></path></svg>';
  toast.classList.remove("hidden");
  if (_toastTimer) clearTimeout(_toastTimer);
  // "info" stays until the next showToast (upload-in-progress); ok/warn auto-clear.
  if (tone !== "info") {
    _toastTimer = setTimeout(() => toast.classList.add("hidden"), 3600);
  }
}

async function deleteConversation(rid, convId) {
  let res;
  try {
    res = await fetch(
      `/pipeline/repairs/${encodeURIComponent(rid)}/conversations/${encodeURIComponent(convId)}`,
      { method: "DELETE" },
    );
  } catch (_) {
    showToast("warn", t("home.dashboard.convs.delete_failed"));
    return;
  }
  if (!res.ok) {
    showToast("warn", t("home.dashboard.convs.delete_failed"));
    return;
  }
  closePanelIfConv(convId);
  const fresh = await fetchJSON(
    `/pipeline/repairs/${encodeURIComponent(rid)}/conversations`,
    { conversations: [] },
  );
  renderDashboardConvs(fresh.conversations || [], rid);
}

function renderDashboardConvs(conversations, rid) {
  const body = document.getElementById("rdConvBody");
  const count = document.getElementById("rdConvCount");
  if (!body || !count) return;
  count.textContent = String(conversations.length);
  body.innerHTML = "";
  if (conversations.length === 0) {
    body.innerHTML = `<div class="rd-block-empty">${escapeHtml(t("home.dashboard.convs.empty"))}</div>`;
  } else {
    for (const c of conversations) {
      const row = document.createElement("div");
      row.className = "rd-conv-row";
      row.dataset.convId = c.id;
      const tier = (c.tier || "fast").toLowerCase();
      const fallbackTitle = t("home.dashboard.convs.untitled", { id: c.id.slice(0, 6) });
      const title = escapeHtml((c.title || fallbackTitle).slice(0, 80));
      const ago = c.last_turn_at ? relativeTimeFr(c.last_turn_at) : "—";
      const cost = typeof c.cost_usd === "number" ? `$${c.cost_usd.toFixed(3)}` : "—";
      const meta = t("home.dashboard.convs.turns_meta", {
        turns: c.turns || 0,
        cost,
        ago,
      });

      const open = document.createElement("button");
      open.type = "button";
      open.className = "rd-conv-open";
      open.innerHTML =
        `<span class="rd-conv-tier t-${tier}">${tier.toUpperCase()}</span>` +
        `<span class="rd-conv-title">${title}</span>` +
        `<span class="rd-conv-meta">${escapeHtml(meta)}</span>`;
      open.addEventListener("click", () => {
        openPanel(c.id);  // single connect targeting the right conv
      });

      const del = document.createElement("button");
      del.type = "button";
      del.className = "rd-conv-delete";
      del.title = t("home.dashboard.convs.delete_aria");
      del.setAttribute("aria-label", t("home.dashboard.convs.delete_aria"));
      del.innerHTML =
        '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" ' +
        'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">' +
        '<path d="M3 4h10M6.5 4V2.5h3V4M5 4l.5 9a1 1 0 001 1h3a1 1 0 001-1l.5-9"/>' +
        '<path d="M7 7v5M9 7v5"/></svg>';
      del.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        if (!confirm(t("home.dashboard.convs.delete_confirm"))) return;
        await deleteConversation(rid, c.id);
      });

      row.appendChild(open);
      row.appendChild(del);
      body.appendChild(row);
    }
  }
  const newBtn = document.createElement("button");
  newBtn.type = "button";
  newBtn.className = "rd-conv-new";
  newBtn.textContent = t("home.dashboard.convs.new");
  newBtn.addEventListener("click", () => {
    openPanel("new");  // single connect; backend lazy-materializes on first message
  });
  body.appendChild(newBtn);
}

function renderDashboardFindings(findings, currentRid) {
  const body = document.getElementById("rdFindingsBody");
  const count = document.getElementById("rdFindingsCount");
  if (!body || !count) return;
  count.textContent = String(findings.length);
  if (findings.length === 0) {
    body.innerHTML = `<div class="rd-block-empty">${t("home.dashboard.findings.empty_html")}</div>`;
    return;
  }
  body.innerHTML = "";
  const currentShort = currentRid.slice(0, 8);
  for (const f of findings) {
    const row = document.createElement("div");
    row.className = "rd-finding-row";
    const isCurrent = f.session_id && f.session_id.startsWith(currentShort);
    const sessionChip = isCurrent
      ? `<span class="rd-finding-session current">${escapeHtml(t("home.dashboard.findings.session_current"))}</span>`
      : (f.session_id
          ? `<span class="rd-finding-session">${escapeHtml(f.session_id.slice(0, 8))}</span>`
          : `<span class="rd-finding-session">—</span>`);
    const notes = f.notes
      ? `<p class="rd-finding-notes">${escapeHtml(f.notes)}</p>`
      : "";
    row.innerHTML =
      `<div class="rd-finding-top">` +
        `<span class="rd-finding-refdes">${escapeHtml(f.refdes)}</span>` +
        `<span class="rd-finding-symptom">${escapeHtml(f.symptom)}</span>` +
        sessionChip +
      `</div>` +
      `<p class="rd-finding-cause">${escapeHtml(f.confirmed_cause || "—")}</p>` +
      notes;
    body.appendChild(row);
  }
}

function renderDashboardTimeline(repair, conversations, findings, pack) {
  const body = document.getElementById("rdTimelineBody");
  if (!body) return;
  const events = [];
  if (repair?.created_at) {
    events.push({ when: repair.created_at, label: t("home.dashboard.timeline.session_opened"), kind: "cyan" });
  }
  for (const c of conversations) {
    if (c.last_turn_at) {
      events.push({
        when: c.last_turn_at,
        label: t("home.dashboard.timeline.activity", {
          tier: (c.tier || "fast").toLowerCase(),
          turns: c.turns || 0,
        }),
        kind: "emerald",
      });
    }
  }
  for (const f of findings) {
    if (f.created_at) {
      events.push({
        when: f.created_at,
        label: t("home.dashboard.timeline.finding_confirmed", {
          refdes: f.refdes || "?",
        }),
        kind: "violet",
      });
    }
  }
  if (pack?.audit_verdict) {
    events.push({
      when: repair?.created_at || new Date().toISOString(),
      label: t("home.dashboard.timeline.pack_audited", { verdict: pack.audit_verdict }),
      kind: pack.audit_verdict === "APPROVED" ? "emerald" : "amber",
    });
  }
  events.sort((a, b) => (b.when || "").localeCompare(a.when || ""));
  const MAX = 8;
  const shown = events.slice(0, MAX);
  body.innerHTML = shown.map(e => (
    `<li class="rd-timeline-item">` +
      `<span class="rd-timeline-node ${e.kind}"></span>` +
      `<span class="rd-timeline-when">${escapeHtml(relativeTimeFr(e.when))}</span>` +
      `<span class="rd-timeline-label">${escapeHtml(e.label)}</span>` +
    `</li>`
  )).join("");
  if (events.length > MAX) {
    const olderLabel = t("home.dashboard.timeline.older", { n: events.length - MAX });
    body.innerHTML += `<li class="rd-timeline-item"><span class="rd-timeline-node"></span><span class="rd-timeline-label">${escapeHtml(olderLabel)}</span></li>`;
  }
  if (events.length === 0) {
    body.innerHTML = `<li class="rd-block-empty">${escapeHtml(t("home.dashboard.timeline.empty"))}</li>`;
  }
}

function renderDashboardPack(pack, slug, rid) {
  const body = document.getElementById("rdPackBody");
  if (!body) return;
  if (!pack) {
    body.innerHTML = `<div class="rd-block-empty">${escapeHtml(t("home.dashboard.pack.empty"))}</div>`;
    return;
  }
  const arts = [
    { key: "has_registry", label: "registry" },
    { key: "has_knowledge_graph", label: "knowledge_graph" },
    { key: "has_rules", label: "rules" },
    { key: "has_dictionary", label: "dictionary" },
    { key: "has_audit_verdict", label: "audit" },
  ];
  const presentCount = arts.filter(a => !!pack[a.key]).length;
  const complete = presentCount === arts.length;
  const statusLabel = complete
    ? t("home.dashboard.pack.status_approved")
    : t("home.dashboard.pack.status_building");
  const statusClass = complete ? "ok" : "warn";
  const rows = arts.map(a => {
    const on = !!pack[a.key];
    return `<li class="rd-pack-row ${on ? "on" : "off"}">` +
      `<span class="rd-pack-tick" aria-hidden="true">${on ? ICON_CHECK : "·"}</span>` +
      `<span class="rd-pack-label">${a.label}</span>` +
    `</li>`;
  }).join("");
  body.innerHTML =
    `<div class="rd-pack-status">` +
      `<span class="rd-pack-status-label ${statusClass}">${statusLabel}</span>` +
      `<span class="rd-pack-count">${presentCount}/${arts.length}</span>` +
    `</div>` +
    `<ul class="rd-pack-rows">${rows}</ul>`;
}

let _dashboardHandlersWired = false;
function wireDashboardHandlers() {
  if (_dashboardHandlersWired) return;
  _dashboardHandlersWired = true;
  document.getElementById("rdLeaveBtn")?.addEventListener("click", () => {
    leaveSession();
  });
}

function wireFixButton(slug, rid) {
  const btn = document.getElementById("dashboardFixBtn");
  if (!btn) return;
  // Expose a reset hook so llm.js can clear the pending state when the
  // validation flow fails (agent refuses, MA tool missing, error event).
  const resetBtn = () => {
    btn.disabled = false;
    btn.innerHTML = ICON_CHECK + " " + escapeHtml(t("home.dashboard.fix_btn"));
    btn.classList.remove("is-validated");
    if (btn._fixTimeoutId) { clearTimeout(btn._fixTimeoutId); btn._fixTimeoutId = null; }
  };
  window.__resetDashboardFixBtn = resetBtn;
  btn.classList.remove("hidden");
  resetBtn();
  btn.onclick = () => {
    const ws = window.__diagnosticWS;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      btn.textContent = t("home.dashboard.fix_btn_open_chat");
      setTimeout(() => {
        btn.innerHTML = ICON_CHECK + " " + escapeHtml(t("home.dashboard.fix_btn"));
      }, 1800);
      return;
    }
    ws.send(JSON.stringify({ type: "validation.start", repair_id: rid }));
    btn.disabled = true;
    btn.textContent = t("home.dashboard.fix_btn_validating");
    // Safety timeout: if the agent never fires simulation.repair_validated
    // (MA tool missing, refusal, error), reset after 25s so the button
    // isn't permanently stuck.
    btn._fixTimeoutId = setTimeout(() => {
      btn.textContent = t("home.dashboard.fix_btn_failed");
      setTimeout(resetBtn, 2200);
    }, 25000);
  };
}

/* ---------- NEW REPAIR MODAL ---------- */
const newRepairBackdrop = document.getElementById("newRepairBackdrop");
const newRepairForm     = document.getElementById("newRepairForm");
const newRepairDevice   = document.getElementById("newRepairDevice");
const newRepairSymptom  = document.getElementById("newRepairSymptom");
const newRepairSubmit   = document.getElementById("newRepairSubmit");
const newRepairError    = document.getElementById("newRepairError");
const newRepairCombo    = document.getElementById("newRepairCombo");
const newRepairPanel    = document.getElementById("newRepairComboPanel");
const newRepairHint     = document.getElementById("newRepairDeviceHint");
const newRepairRebuildRow = document.getElementById("newRepairRebuildRow");
const newRepairForceRebuild = document.getElementById("newRepairForceRebuild");
let   newRepairLastFocus = null;
let   comboEntries = [];      // flat list of known devices
let   comboActiveIndex = -1;  // keyboard-highlighted option
// When the user PICKS an existing entry from the combobox we keep the pack's
// original device_label + slug here so the submit hits the exact same slug
// server-side. Free typing resets both — we only want this mapping for clicks.
let   selectedEntryLabel = null;
let   selectedEntrySlug = null;

function openNewRepair() {
  newRepairLastFocus = document.activeElement;
  newRepairForm.reset();
  setNewRepairError(null);
  setNewRepairBusy(false);
  newRepairRebuildRow.hidden = true;
  newRepairForceRebuild.checked = false;
  selectedEntryLabel = null;
  selectedEntrySlug = null;
  newRepairBackdrop.classList.add("open");
  newRepairBackdrop.setAttribute("aria-hidden", "false");
  // Kick off the taxonomy fetch and cache it for the session — small payload.
  refreshComboEntries();
  // Let the backdrop fade-in paint, then focus first field.
  requestAnimationFrame(() => newRepairDevice.focus());
}

function closeNewRepair() {
  if (!newRepairBackdrop.classList.contains("open")) return;
  newRepairBackdrop.classList.remove("open");
  newRepairBackdrop.setAttribute("aria-hidden", "true");
  setNewRepairBusy(false);
  hideComboPanel();
  if (newRepairLastFocus && typeof newRepairLastFocus.focus === "function") {
    newRepairLastFocus.focus();
  }
}

/* ---------- Combobox — device autocomplete ---------- */

async function refreshComboEntries() {
  const tax = await loadTaxonomy();
  const entries = [];
  for (const [brand, models] of Object.entries(tax.brands || {})) {
    for (const [modelName, packs] of Object.entries(models)) {
      for (const p of packs) {
        entries.push({ ...p, brand, model: modelName });
      }
    }
  }
  for (const p of tax.uncategorized || []) {
    entries.push({ ...p, brand: null, model: null });
  }
  comboEntries = entries;
}

// Normalize: lowercase, strip accents, collapse whitespace. Used both on the
// query and on every candidate field so the match is case- and accent-agnostic.
function normalize(s) {
  return (s || "")
    .toString()
    .toLowerCase()
    .normalize("NFD").replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function scoreEntry(entry, qNorm, qTokens, qInline) {
  // Concatenate the searchable surface once, then rank.
  const haystack = normalize(
    [entry.device_label, entry.device_slug, entry.brand, entry.model, entry.version, entry.form_factor]
      .filter(Boolean).join(" ")
  );
  if (!qNorm) return 1;                         // empty query → all pass
  if (haystack === qNorm) return 1000;          // exact full-label match
  if (haystack.startsWith(qNorm)) return 500;   // prefix match
  if (haystack.includes(qNorm)) return 300;     // contiguous substring
  // Space-insensitive substring: lets "iphoneX" match "iPhone X", handy when
  // the tech omits spaces or concatenates brand+model.
  const haystackInline = haystack.replace(/ /g, "");
  if (qInline && haystackInline.includes(qInline)) return 200;
  // Token coverage: every query token must appear somewhere in the haystack.
  // Tolerates word-level reordering ("motherboard reform mnt"), and partial
  // prefix typing ("refo" matches "reform").
  let covered = 0;
  for (const t of qTokens) {
    if (!t) continue;
    if (haystack.includes(t)) covered++;
  }
  if (covered === qTokens.length) return 100 + covered;
  if (covered >= Math.ceil(qTokens.length / 2)) return 30 + covered;
  return 0;
}

function filterEntries(query) {
  const qNorm = normalize(query);
  const qTokens = qNorm.split(" ").filter(Boolean);
  const qInline = qNorm.replace(/ /g, "");
  return comboEntries
    .map(entry => ({ entry, score: scoreEntry(entry, qNorm, qTokens, qInline) }))
    .filter(x => x.score > 0)
    .sort((a, b) => b.score - a.score || a.entry.device_label.localeCompare(b.entry.device_label));
}

// Highlight every occurrence of the normalized query's substrings in the raw
// label, without stripping the original casing.
function highlight(raw, query) {
  if (!query) return escapeHtml(raw);
  const qNorm = normalize(query);
  if (!qNorm) return escapeHtml(raw);
  const rawNorm = normalize(raw);
  const idx = rawNorm.indexOf(qNorm);
  if (idx === -1) return escapeHtml(raw);
  // Map back to original-string offsets. normalize collapses whitespace and
  // strips accents 1:1 so the offsets are the same length; good enough here.
  const start = idx;
  const end = idx + qNorm.length;
  return escapeHtml(raw.slice(0, start))
       + "<mark>" + escapeHtml(raw.slice(start, end)) + "</mark>"
       + escapeHtml(raw.slice(end));
}

function renderComboPanel(query) {
  const results = filterEntries(query);
  const groups = new Map(); // brand → entries[]
  const uncatLabel = t("home.list.uncategorized");
  for (const { entry } of results) {
    const key = entry.brand || uncatLabel;
    (groups.get(key) || groups.set(key, []).get(key)).push(entry);
  }

  const parts = [];
  const trimmed = query.trim();
  const exactExists = results.some(r => normalize(r.entry.device_label) === normalize(trimmed));
  if (trimmed && !exactExists) {
    parts.push(`
      <button type="button" class="combo-option combo-create" data-action="create"
              data-label="${escapeHtml(trimmed)}" role="option">
        <span class="combo-label">${escapeHtml(t("home.modal.combo.create", { name: trimmed }))}</span>
        <span class="combo-meta"><span class="combo-badge">${escapeHtml(t("home.modal.combo.create_badge"))}</span></span>
      </button>
    `);
  }

  if (groups.size === 0 && !trimmed) {
    parts.push(`<div class="combo-empty">${escapeHtml(t("home.modal.combo.empty"))}</div>`);
  }

  const sortedBrands = Array.from(groups.keys()).sort((a, b) => a.localeCompare(b));
  for (const brand of sortedBrands) {
    const entries = groups.get(brand);
    parts.push(`
      <div class="combo-section">
        <div class="combo-section-head">
          <span>${escapeHtml(brand)}</span>
          <span class="combo-section-count">${entries.length}</span>
        </div>
    `);
    for (const e of entries) {
      // Inside a brand section the brand is already in the header — show only
      // the model/device, keep the form_factor as a separate mono chip.
      const name = deviceName(e, { includeBrand: false });
      const auditedBadge = e.complete
        ? `<span class="combo-badge ok">${escapeHtml(t("home.modal.combo.badge_audited"))}</span>`
        : `<span class="combo-badge">${escapeHtml(t("home.modal.combo.badge_partial"))}</span>`;
      const badges = [
        auditedBadge,
        e.form_factor ? `<span class="combo-badge">${escapeHtml(e.form_factor)}</span>` : '',
      ].filter(Boolean).join("");
      parts.push(`
        <button type="button" class="combo-option" role="option"
                data-action="select"
                data-slug="${escapeHtml(e.device_slug)}"
                data-label="${escapeHtml(e.device_label)}"
                data-complete="${e.complete ? "1" : "0"}">
          <span class="combo-label">${highlight(name, query)}</span>
          <span class="combo-meta">${badges}</span>
        </button>
      `);
    }
    parts.push('</div>');
  }

  newRepairPanel.innerHTML = parts.join("");
  newRepairPanel.hidden = false;
  newRepairDevice.setAttribute("aria-expanded", "true");
  comboActiveIndex = -1;
  syncComboActive();
}

function hideComboPanel() {
  newRepairPanel.hidden = true;
  newRepairDevice.setAttribute("aria-expanded", "false");
  comboActiveIndex = -1;
}

function comboOptions() {
  return Array.from(newRepairPanel.querySelectorAll(".combo-option"));
}

function syncComboActive() {
  comboOptions().forEach((el, i) => el.classList.toggle("active", i === comboActiveIndex));
}

function comboMoveActive(delta) {
  const opts = comboOptions();
  if (opts.length === 0) return;
  comboActiveIndex = (comboActiveIndex + delta + opts.length) % opts.length;
  syncComboActive();
  opts[comboActiveIndex].scrollIntoView({ block: "nearest" });
}

// Picking an existing entry from the combobox. We display the CLEAN name
// ({brand} {model}) in the input — no form_factor clutter — but we keep the
// original device_label + slug aside so the submit resolves to the exact
// same pack slug server-side.
function applyExistingEntry(entry) {
  newRepairDevice.value = deviceName(entry, { includeBrand: true });
  selectedEntryLabel = entry.device_label;
  selectedEntrySlug = entry.device_slug;
  hideComboPanel();
  applyRebuildStateForEntry(entry);
}

// Picking the "+ Créer « … »" row — the user wants a brand-new device
// with whatever string they typed.
function applyNewDeviceSelection(rawText) {
  newRepairDevice.value = rawText;
  selectedEntryLabel = null;
  selectedEntrySlug = null;
  hideComboPanel();
  applyRebuildStateForTyped();
}

function applyRebuildStateForEntry(entry) {
  if (entry.complete) {
    newRepairRebuildRow.hidden = false;
    newRepairHint.textContent = t("home.modal.hint_existing_complete");
  } else {
    newRepairRebuildRow.hidden = true;
    newRepairForceRebuild.checked = false;
    newRepairHint.textContent = t("home.modal.hint_existing_partial");
  }
}

function applyRebuildStateForTyped() {
  newRepairRebuildRow.hidden = true;
  newRepairForceRebuild.checked = false;
  newRepairHint.textContent = t("home.modal.hint_typed");
}

function commitOption(el) {
  if (!el) return;
  if (el.dataset.action === "select") {
    const slug = el.dataset.slug;
    const entry = comboEntries.find(e => e.device_slug === slug);
    if (entry) applyExistingEntry(entry);
  } else if (el.dataset.action === "create") {
    applyNewDeviceSelection(el.dataset.label);
  }
}

function initCombo() {
  newRepairDevice.addEventListener("focus", () => {
    renderComboPanel(newRepairDevice.value);
  });
  newRepairDevice.addEventListener("input", () => {
    // Free typing — the picked-entry mapping no longer applies.
    selectedEntryLabel = null;
    selectedEntrySlug = null;
    renderComboPanel(newRepairDevice.value);
    applyRebuildStateForTyped();
  });
  newRepairDevice.addEventListener("keydown", ev => {
    if (newRepairPanel.hidden) return;
    if (ev.key === "ArrowDown") { ev.preventDefault(); comboMoveActive(1); return; }
    if (ev.key === "ArrowUp")   { ev.preventDefault(); comboMoveActive(-1); return; }
    if (ev.key === "Enter" && comboActiveIndex >= 0) {
      ev.preventDefault();
      commitOption(comboOptions()[comboActiveIndex]);
      return;
    }
    if (ev.key === "Escape") {
      ev.preventDefault();
      ev.stopPropagation();
      hideComboPanel();
    }
  });
  newRepairPanel.addEventListener("mousedown", ev => {
    const opt = ev.target.closest(".combo-option");
    if (!opt) return;
    ev.preventDefault();  // keep input focus
    commitOption(opt);
  });
  // Click outside closes.
  document.addEventListener("mousedown", ev => {
    if (newRepairPanel.hidden) return;
    if (newRepairCombo.contains(ev.target)) return;
    hideComboPanel();
  });
  // Tab-away from the input closes the panel too. setTimeout lets an in-panel
  // click fire first (since mousedown on an option preventDefault'd the blur).
  newRepairDevice.addEventListener("blur", () => {
    setTimeout(() => {
      if (!newRepairCombo.contains(document.activeElement)) hideComboPanel();
    }, 120);
  });
}

function setNewRepairError(msg, opts) {
  if (!msg) {
    newRepairError.hidden = true;
    newRepairError.textContent = "";
    return;
  }
  newRepairError.hidden = false;
  newRepairError.innerHTML = "";
  if (opts && opts.title) {
    const s = document.createElement("strong");
    s.textContent = opts.title;
    newRepairError.appendChild(s);
  }
  newRepairError.appendChild(document.createTextNode(msg));
}

function setNewRepairBusy(busy) {
  newRepairSubmit.disabled  = busy;
  newRepairDevice.disabled  = busy;
  newRepairSymptom.disabled = busy;
  newRepairSubmit.setAttribute("aria-busy", busy ? "true" : "false");
  const label = newRepairSubmit.querySelector(".btn-label");
  if (label) {
    label.innerHTML = busy
      ? `<span class="modal-spinner" aria-hidden="true"></span> ${escapeHtml(t("home.modal.submit_busy"))}`
      : escapeHtml(t("home.modal.submit"));
  }
}

async function submitNewRepair(ev) {
  ev.preventDefault();
  // When the user picked an existing entry from the combobox, send its
  // ORIGINAL device_label AND the canonical device_slug so the backend
  // resolves to the exact pack on disk — regardless of any Registry-rewrite
  // drift between device_label and the directory name. Only fall back to the
  // input value for a brand-new device the user typed out.
  const typedValue = newRepairDevice.value.trim();
  const device_label = selectedEntryLabel || typedValue;
  const device_slug  = selectedEntrySlug || null;
  const symptom      = newRepairSymptom.value.trim();
  const force_rebuild = newRepairForceRebuild.checked;
  if (device_label.length < 2) {
    setNewRepairError(t("home.modal.errors.device_too_short"),
      { title: t("home.modal.errors.device_too_short_title") });
    newRepairDevice.focus();
    return;
  }
  if (symptom.length < 5) {
    setNewRepairError(t("home.modal.errors.symptom_too_short"),
      { title: t("home.modal.errors.symptom_too_short_title") });
    newRepairSymptom.focus();
    return;
  }
  setNewRepairError(null);
  setNewRepairBusy(true);
  try {
    const res = await fetch("/pipeline/repairs", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({device_label, device_slug, symptom, force_rebuild}),
    });
    if (!res.ok) {
      let detail = "";
      try { detail = (await res.json()).detail || ""; } catch (_) { /* noop */ }
      setNewRepairError(
        t("home.modal.errors.backend_response", { status: res.status, detail }).trim(),
        { title: t("home.modal.errors.backend_title") },
      );
      setNewRepairBusy(false);
      return;
    }
    const repair = await res.json();
    // Close the modal, then hand off to the pipeline progress drawer which
    // either redirects immediately (pack already built) or streams live events.
    closeNewRepair();
    openPipelineProgress(repair);
  } catch (err) {
    console.error("newRepair submit failed", err);
    setNewRepairError(t("home.modal.errors.network"),
      { title: t("home.modal.errors.network_title") });
    setNewRepairBusy(false);
  }
}

function trapNewRepairFocus(ev) {
  if (ev.key !== "Tab") return;
  const focusables = newRepairBackdrop.querySelectorAll(
    'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'
  );
  if (focusables.length === 0) return;
  const first = focusables[0];
  const last  = focusables[focusables.length - 1];
  if (ev.shiftKey && document.activeElement === first) {
    ev.preventDefault(); last.focus();
  } else if (!ev.shiftKey && document.activeElement === last) {
    ev.preventDefault(); first.focus();
  }
}

// Re-render the imperatively-built home surface (repair list + dashboard)
// when the user toggles language. Only fires when the home section is
// actually showing — otherwise the next renderHome / renderRepairDashboard
// call will pick up the new locale naturally.
async function refreshHomeOnLocaleChange() {
  const homeSection = document.getElementById("homeSection");
  if (!homeSection || homeSection.classList.contains("hidden")) return;

  const params = new URLSearchParams(window.location.search);
  const slug = params.get("device");
  const rid = params.get("repair");

  if (slug && rid) {
    // Dashboard mode — re-render the focused session view.
    try {
      await renderRepairDashboard({ device: slug, repair: rid });
    } catch (err) {
      console.warn("[home] dashboard re-render failed", err);
    }
    return;
  }

  // List mode — re-render the brand > model > card grid.
  try {
    const [tax, repairs] = await Promise.all([loadTaxonomy(), loadRepairs()]);
    renderHome(tax, repairs);
  } catch (err) {
    console.warn("[home] list re-render failed", err);
  }
}

export function initNewRepairModal() {
  document.getElementById("homeNewBtn").addEventListener("click", openNewRepair);
  document.getElementById("newRepairClose").addEventListener("click", closeNewRepair);
  document.getElementById("newRepairCancel").addEventListener("click", closeNewRepair);
  newRepairForm.addEventListener("submit", submitNewRepair);
  newRepairBackdrop.addEventListener("click", ev => {
    if (ev.target === newRepairBackdrop) closeNewRepair();
  });
  initCombo();

  // Re-render the JS-built home surface when the user toggles language.
  if (window.i18n && typeof window.i18n.onChange === "function") {
    window.i18n.onChange(() => { refreshHomeOnLocaleChange(); });
  }

  // Registered BEFORE the global ESC/Cmd+K handler, so we can intercept those
  // keys while the modal is open without closing the Inspector or stealing focus.
  document.addEventListener("keydown", ev => {
    if (!newRepairBackdrop.classList.contains("open")) return;
    if (ev.key === "Escape") {
      ev.preventDefault(); ev.stopImmediatePropagation(); closeNewRepair(); return;
    }
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "k") {
      ev.preventDefault(); ev.stopImmediatePropagation(); return;
    }
    if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") {
      ev.preventDefault(); ev.stopImmediatePropagation();
      if (!newRepairSubmit.disabled) newRepairForm.requestSubmit();
      return;
    }
    trapNewRepairFocus(ev);
  });
}
