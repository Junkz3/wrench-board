// Repair dashboard — the #repair/:id/diagnostic vue (Phase D.3: relocated here
// from web/js/home.js, which is now gone). Renders the focused session hub:
//   - renderRepairDashboard() : header / data grid / conversations / findings /
//     timeline / pack, mounted by the workspace shell on the diagnostic vue.
//   - initHome() : wires the dashboard's locale re-render on EN/FR toggle.
//   - loadTaxonomy() : brand>model>version index used by the dashboard header.

import { leaveSession, repairHash } from '../../../router.js';
import { openPanel, closePanelIfConv } from '../../../llm.js';
import { ICON_CHECK } from '../../../icons.js';
import { getDiagnosticWS } from '../../../services/diagnosticSocket.js';
import { store } from '../../../store.js';
import { getDeviceSlug, getRepairId } from '../../../shared/context.js';
import { escapeHtml, relativeTime as relativeTimeFr } from '../../../shared/dom.js';
import { apiGet } from '../../../shared/api.js';
import { openInfoModal } from '../../../info_modal.js';
import { maybeShowFirstDiagCoaching } from './coaching.js';
import { hideUploads } from '../../../cloud_hints.js';

export async function loadTaxonomy() {
  try {
    return await apiGet("/pipeline/taxonomy");
  } catch (err) {
    console.warn("loadTaxonomy failed", err);
    return {brands: {}, uncategorized: []};
  }
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

// Phase D.1: the dense #home journal grid (renderHome + repairCardHTML /
// deviceBlockHTML / brandBlockHTML) was removed — the landing overlay is the
// global home and lists all repairs via its sidebar. The repair dashboard below
// (renderRepairDashboard) is the #repair/:id/diagnostic vue and stays. Helpers
// kept for the dashboard: humanizeSlug / stripFormFactor / deviceName /
// indexTaxonomyBySlug / statusLabel / statusBadgeHTML.

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
  await renderBoardDeltaCard(slug, repair?.board_number || null);
  renderDashboardConvs(convs.conversations || [], rid);
  renderDashboardFindings(findings, rid);
  renderDashboardTimeline(repair, convs.conversations || [], findings, pack);
  renderDashboardPack(pack, slug, rid);
  wireDashboardHandlers();
  wireUploadHandlers(slug, rid);
  wireFixButton(slug, rid);
  maybeAutoResumeBuildWatch(slug, rid, pack);

  // Permanent "?" → the replayable "How a repair works" explainer.
  const infoBtn = document.getElementById("rdInfoHint");
  if (infoBtn) infoBtn.onclick = () => openInfoModal("repair");
  // One-shot guided tour of the workspace the first time a dashboard opens.
  // Consume the example-handoff hook one-shot (cleared on read so a later,
  // unrelated first-diag render never picks up a stale return-to-landing).
  const exampleOnDone = window.__wbExampleTourOnDone || null;
  window.__wbExampleTourOnDone = null;
  maybeShowFirstDiagCoaching(rid, { onDone: exampleOnDone, slug });
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

  // The example/demo repair carries a hard-coded FR symptom in its record, which
  // can't follow the UI locale — show a localized one instead, and flag demo mode
  // on <body> so the centered topbar DEMO badge shows across every view.
  const isDemo = !!rid && rid.startsWith("example-");
  document.body.classList.toggle("wb-demo-mode", isDemo);

  slugEl.textContent = slug;
  deviceEl.textContent = taxEntry
    ? deviceName(taxEntry, { includeBrand: true })
    : (repair?.device_label || humanizeSlug(slug));
  symptomEl.textContent = isDemo ? t("repair.demo_symptom") : (repair?.symptom || "…");

  const created = repair?.created_at ? relativeTimeFr(repair.created_at) : "…";
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
  if (!Number.isFinite(n) || n <= 0) return "…";
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
  // Intra-dashboard links navigate into the repair's vues (canonical hash route).
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
      schemActions.appendChild(linkButton(repairHash(rid, "schematic"),
        ICONS.arrowRight + " " + escapeHtml(t("home.dashboard.schematic.open")), "is-primary"));
      // Plan free (mode managé, cloud_hints) : pas d'import de fichier — le
      // serveur refuse l'upload (402), donc l'affordance disparaît.
      if (!hideUploads()) {
        schemActions.appendChild(actionButton(
          ICONS.upload + " " + escapeHtml(t("home.dashboard.schematic.import_version")), () => {
            document.getElementById("rdUploadSchematic")?.click();
          }));
      }
    } else if (!hideUploads()) {
      schemActions.appendChild(actionButton(
        ICONS.upload + " " + escapeHtml(t("home.dashboard.schematic.import_pdf")), () => {
          document.getElementById("rdUploadSchematic")?.click();
        }, "is-warn"));
    }
  }
  renderVersionList("rdCardSchematic", "schematic_pdf", schemVersions, slug, rid, {
    graphStatus: pack?.has_schematic_pdf
      ? (pack.has_electrical_graph ? "compiled" : "building")
      : null,
  });

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
      bvActions.appendChild(linkButton(repairHash(rid, "pcb"),
        ICONS.arrowRight + " " + escapeHtml(t("home.dashboard.boardview.open")), "is-primary"));
      // Plan free (mode managé, cloud_hints) : pas d'import — voir card schématique.
      if (!hideUploads()) {
        bvActions.appendChild(actionButton(
          ICONS.upload + " " + escapeHtml(t("home.dashboard.boardview.import_version")), () => {
            document.getElementById("rdUploadBoardview")?.click();
          }));
      }
    } else if (!hideUploads()) {
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
      knowledgeActions.appendChild(linkButton(repairHash(rid, "graph"),
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
      electricalActions.appendChild(linkButton(repairHash(rid, "schematic"),
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
      memoryActions.appendChild(linkButton(`?view=md${repairHash(rid, "graph")}`,
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
    // The "electrical graph" capability (simulator + hypothesize) is gated on
    // the GRAPH being resolved, NOT on a local schematic PDF: the agent's tools
    // (mb_schematic_graph / mb_hypothesize) run off has_electrical_graph
    // server-side (manifest.py:_has_electrical_graph). In the cloud the graph
    // can be available from the shared cache (resolved per-owner by PDF hash)
    // without this tenant re-uploading a PDF — so gating on has_schematic_pdf
    // wrongly showed the tools disabled while the graph was in fact live. The
    // PDF stays a SOURCE (its own data card below), not a capability.
    schematic: !!pack?.has_electrical_graph,
    boardview: !!pack?.has_boardview,
    graph:     !!(pack && pack.has_knowledge_graph && pack.has_rules),
    memory:    !!pack?.has_rules,
    stock:     !!pack?.has_parts_index,
  };
  const onCount = Object.values(flags).filter(Boolean).length;
  const totalCount = Object.keys(flags).length;
  let level = "minimal";
  let label = t("home.dashboard.cap.minimal_label");
  let blurb = t("home.dashboard.cap.minimal_blurb");
  if (onCount === totalCount) {
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
    { key: "stock",     label: t("home.dashboard.cap.row.stock_label"),     on: t("home.dashboard.cap.row.stock_on"),     off: t("home.dashboard.cap.row.off") },
  ];
  list.innerHTML = rows.map(r => {
    const on = flags[r.key];
    return `<li class="rd-cap-pill ${on ? "on" : "off"}">
      <span class="rd-cap-pill-dot"></span>
      <span class="rd-cap-pill-label">${escapeHtml(r.label)}</span>
      <span class="rd-cap-pill-tag">${escapeHtml(on ? r.on : r.off)}</span>
      <button type="button" class="rd-cap-info" data-cap="${r.key}" aria-label="${escapeHtml(t("home.dashboard.cap.info_aria", { name: r.label }))}" title="${escapeHtml(t("home.dashboard.cap.info_title"))}">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8h.01M12 12v4"/></svg>
      </button>
    </li>`;
  }).join("");
  wireCapInfoButtons(list);
}

// ── Capability tool-list popover ───────────────────────────────────────
// Maps each capability to its agent tool surface. The strings live in i18n
// (home.dashboard.cap.tools.<cap>.<idx>.{name,desc}) so they translate.
// Source of truth for the tool inventory: api/agent/manifest.py.
const CAP_TOOLS = {
  schematic: ["mb_schematic_graph", "mb_hypothesize"],
  boardview: [
    "bv_highlight", "bv_focus", "bv_scene", "bv_propose_protocol",
    "bv_draw_arrow", "bv_annotate", "bv_record_step_result",
  ],
  graph:  ["mb_get_rules_for_symptoms", "mb_get_component", "mb_expand_knowledge"],
  memory: [
    "mb_record_finding", "mb_record_session_log", "mb_record_measurement",
    "mb_observations_from_measurements", "mb_validate_finding",
  ],
  stock: [
    "stock_search", "stock_list_donors", "stock_mark_donor",
    "stock_unmark_donor", "stock_consume",
  ],
};

function wireCapInfoButtons(listEl) {
  listEl.querySelectorAll(".rd-cap-info").forEach(btn => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const cap = btn.dataset.cap;
      openCapPopover(cap, btn);
    });
  });
}

let _capPopoverAnchor = null;
function openCapPopover(cap, anchorBtn) {
  const pop = document.getElementById("rdCapPopover");
  const titleEl = document.getElementById("rdCapPopoverTitle");
  const toolsEl = document.getElementById("rdCapPopoverTools");
  if (!pop || !titleEl || !toolsEl) return;
  if (_capPopoverAnchor === anchorBtn && !pop.hidden) {
    closeCapPopover();
    return;
  }
  _capPopoverAnchor = anchorBtn;
  titleEl.textContent = t(`home.dashboard.cap.row.${cap}_label`);
  // Lead with a one-line "what this source is + what it unlocks" so the
  // grouping reads as a dependency (import → unlock), not a taxonomy.
  const descEl = document.getElementById("rdCapPopoverDesc");
  if (descEl) descEl.textContent = t(`home.dashboard.cap.desc.${cap}`);
  const tools = CAP_TOOLS[cap] || [];
  toolsEl.innerHTML = tools.map(toolName => `
    <li class="rd-cap-popover-tool">
      <code class="rd-cap-popover-tool-name">${escapeHtml(toolName)}</code>
      <span class="rd-cap-popover-tool-desc">${escapeHtml(t(`home.dashboard.cap.tool_desc.${toolName}`))}</span>
    </li>
  `).join("");
  // Position under the anchor button in viewport coords (fixed). CSS
  // `right: Xpx` measures from the right edge of the viewport, so we
  // align the popover's right edge with the button's right edge and
  // clamp to a minimum gutter. With body.llm-open the chat panel
  // occupies the rightmost 420px — the gutter jumps to 420+12 so the
  // popover never slides under the chat.
  const llmOpen = document.body.classList.contains("llm-open");
  const minRight = llmOpen ? 432 : 12;
  const btnRect = anchorBtn.getBoundingClientRect();
  const top = btnRect.bottom + 6;
  const right = Math.max(minRight, window.innerWidth - btnRect.right);
  pop.style.top = `${top}px`;
  pop.style.right = `${right}px`;
  pop.hidden = false;
  pop.dataset.open = "true";
}

function closeCapPopover() {
  const pop = document.getElementById("rdCapPopover");
  if (!pop) return;
  pop.hidden = true;
  delete pop.dataset.open;
  _capPopoverAnchor = null;
}

// One-shot wiring — close handlers don't depend on which capability is
// active, so they're attached once. Click outside or Escape dismisses;
// the [×] inside the popover routes to the same close path.
document.addEventListener("click", (ev) => {
  const pop = document.getElementById("rdCapPopover");
  if (!pop || pop.hidden) return;
  if (pop.contains(ev.target)) return;
  if (_capPopoverAnchor && _capPopoverAnchor.contains(ev.target)) return;
  closeCapPopover();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") closeCapPopover();
});
document.getElementById("rdCapPopoverClose")?.addEventListener("click", () => closeCapPopover());

// Render the list of uploaded versions inside a card (rendered as soon as
// 1 version exists — even a single version is worth surfacing so the tech
// can see which file is loaded and delete it if it's wrong).
// Each row: radio + filename · timestamp · size + status (active schematic
// only) + trash (hover). Click the row to switch the active pin via
// PUT /sources/{kind}; click the trash to drop via DELETE.
// When 5+ versions, the inner list scrolls and the header stays fixed.
// opts.graphStatus is `compiled | building | null` and only affects the
// active row of a schematic card — boardview rows ignore it.
function renderVersionList(cardId, kind, versions, slug, rid, opts = {}) {
  const card = document.getElementById(cardId);
  if (!card) return;
  let host = card.querySelector(".rd-versions");
  if (!host) {
    host = document.createElement("div");
    host.className = "rd-versions";
    card.appendChild(host);
  }
  if (!versions || versions.length < 1) {
    host.remove();
    return;
  }
  host.innerHTML = `<div class="rd-versions-head">
    <span class="rd-versions-tag">${escapeHtml(t("home.version.tag"))}</span>
    <span class="rd-versions-count">${versions.length}</span>
  </div>
  <div class="rd-versions-list" data-overflow="${versions.length > 5 ? "scroll" : "fit"}"></div>`;
  const listEl = host.querySelector(".rd-versions-list");
  for (const v of versions) {
    const row = document.createElement("div");
    row.className = "rd-version-row" + (v.is_active ? " is-active" : "");
    const dateLabel = formatVersionDate(v.timestamp);
    const switchBtn = document.createElement("button");
    switchBtn.type = "button";
    switchBtn.className = "rd-version-switch";
    switchBtn.disabled = !!v.is_active;
    const showStatus = (
      v.is_active
      && kind === "schematic_pdf"
      && (opts.graphStatus === "compiled" || opts.graphStatus === "building")
    );
    const statusKey = opts.graphStatus === "compiled"
      ? "home.version.status_compiled"
      : "home.version.status_building";
    const statusHtml = showStatus
      ? `<span class="rd-version-status" data-status="${opts.graphStatus}">
           <span class="rd-version-status-dot" aria-hidden="true"></span>
           <span class="rd-version-status-label">${escapeHtml(t(statusKey))}</span>
         </span>`
      : "";
    switchBtn.innerHTML = `
      <span class="rd-version-dot" aria-hidden="true"></span>
      <span class="rd-version-name">${escapeHtml(v.original_name)}</span>
      <span class="rd-version-meta">${escapeHtml(dateLabel)} · ${escapeHtml(fmtBytes(v.size_bytes))}</span>
      ${statusHtml}
    `;
    if (!v.is_active) {
      switchBtn.addEventListener("click", () => switchSource(slug, rid, kind, v));
    }
    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "rd-version-delete";
    deleteBtn.title = t("home.version.delete_aria");
    deleteBtn.setAttribute("aria-label", t("home.version.delete_aria"));
    deleteBtn.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>`;
    deleteBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteVersion(slug, rid, kind, v);
    });
    row.appendChild(switchBtn);
    row.appendChild(deleteBtn);
    listEl.appendChild(row);
  }
}

// Parse the ISO-like upload timestamp `20260424T130000Z` into a short
// fr-locale label `24 avr · 13:00`. Falls back to the raw string on parse fail.
function formatVersionDate(ts) {
  if (!ts) return "…";
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

async function deleteVersion(slug, rid, kind, version) {
  const confirmMsg = t("home.version.delete_confirm", { name: version.original_name });
  if (!window.confirm(confirmMsg)) return;

  // Pre-flight: if we're deleting the active schematic, the backend will
  // switch to the next newest. Either way the card flips to building until
  // the cache decision lands.
  if (version.is_active && kind === "schematic_pdf") {
    setCardState("rdCardSchematic", "building");
    setCardState("rdCardElectrical", "building");
  }

  try {
    const res = await fetch(
      `/pipeline/packs/${encodeURIComponent(slug)}`
      + `/sources/${kind}/versions/${encodeURIComponent(version.filename)}`,
      { method: "DELETE" },
    );
    if (!res.ok) {
      let detail = "";
      try { detail = (await res.json()).detail || ""; } catch (_) { /* noop */ }
      showToast("warn",
        t("home.toast.delete_failed_title"),
        t("home.toast.delete_failed_sub", {
          status: res.status,
          detail: detail || t("home.toast.delete_failed_retry"),
        }));
      await refreshDashboardData(slug, rid);
      return;
    }
    const body = await res.json();
    if (body.status === "switched_rebuilding") {
      const pages = body.page_count ? ` · ${body.page_count} pages` : "";
      const eta = body.eta_seconds ? ` · ~${formatRemaining(body.eta_seconds)}` : "";
      showToast("info",
        t("home.toast.version_deleted_rebuilding_title"),
        t("home.toast.version_deleted_rebuilding_sub", {
          name: version.original_name,
          pages,
          eta,
        }));
      startBuildWatch(slug, rid, body.eta_seconds || 0, body.page_count || null);
    } else {
      showToast("ok",
        t("home.toast.version_deleted_title"),
        t("home.toast.version_deleted_sub", { name: version.original_name }));
    }
    // For boardview deletes, the PCB viewer cache holds the previous file's
    // payload — invalidate so the next visit refetches /api/board/render.
    if (kind === "boardview"
        && version.is_active
        && window.Boardview
        && typeof window.Boardview.invalidate === "function") {
      window.Boardview.invalidate(slug);
    }
    await refreshDashboardData(slug, rid);
  } catch (err) {
    console.error("deleteVersion failed", err);
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
  // Plan free (mode managé, cloud_hints) : aucune voie d'import — ni boutons
  // (déjà non rendus) ni drag-drop. Le serveur refuse l'upload (402) pareil.
  if (hideUploads()) return;
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
      const slugNow = getDeviceSlug();
      const ridNow = getRepairId();
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

// ───────────────────────────────────────────────────────────────
// Board-delta card — revision-specific overlay surfacing what the
// agent knows about this exact board number: signature ICs, notable
// rails, repair pitfalls, kinship hints (cousin boards). Hidden when
// no board_number on the repair or when the delta endpoint 404s / returns
// coverage "none".
// ───────────────────────────────────────────────────────────────
async function renderBoardDeltaCard(slug, boardNumber) {
  const card = document.getElementById("rdCardBoardDelta");
  if (!card) return;

  // No board number on this repair — hide card and bail.
  if (!boardNumber) {
    card.classList.add("hidden");
    return;
  }

  let delta;
  try {
    delta = await apiGet(
      `/pipeline/packs/${encodeURIComponent(slug)}/board-delta/${encodeURIComponent(boardNumber)}`,
    );
  } catch (_) {
    // 404 or network error — hide the card.
    card.classList.add("hidden");
    return;
  }

  // Coverage "none" means the delta exists as a record but carries no data.
  if (!delta || delta.coverage === "none") {
    card.classList.add("hidden");
    return;
  }

  // De-duplicate ICs by (part, role) — same logic as the approved mockup.
  const seen = new Set();
  const ics = [];
  for (const ic of (delta.signature_ics || [])) {
    const k = `${ic.part}|${ic.role}`;
    if (seen.has(k)) continue;
    seen.add(k);
    ics.push(ic);
  }

  const rails   = (delta.notable_rails    || []).slice(0, 8);
  const pitfalls = delta.repair_pitfalls  || [];
  const cousins  = delta.kinship_hints    || [];
  const sources  = delta.sources          || [];

  const icRows = ics.map(ic =>
    `<div class="rd-delta-row">` +
      `<span class="rd-delta-mono rd-delta-cyan">${escapeHtml(ic.part || "?")}</span>` +
      `<span class="rd-delta-role">${escapeHtml(ic.role)}</span>` +
    `</div>`,
  ).join("");

  const railRows = rails.map(r =>
    `<div class="rd-delta-row">` +
      `<span class="rd-delta-mono rd-delta-emerald">${escapeHtml(r.name)}</span>` +
      `<span class="rd-delta-note">${escapeHtml(r.note)}</span>` +
    `</div>`,
  ).join("");

  const pitRows = pitfalls.map(p =>
    `<div class="rd-delta-pit">` +
      `<div class="rd-delta-pit-title rd-delta-amber">${escapeHtml(p.title)}</div>` +
      `<div class="rd-delta-pit-detail">${escapeHtml(p.detail)}</div>` +
    `</div>`,
  ).join("");

  const cousinRows = cousins.map(k =>
    `<div class="rd-delta-row">` +
      `<span class="rd-delta-mono">${escapeHtml(k.board_number)}</span>` +
      `<span class="rd-delta-note">${escapeHtml(k.relation)}</span>` +
    `</div>`,
  ).join("");

  const covLabel = escapeHtml(delta.coverage || "");
  const boardLabel = escapeHtml(boardNumber);
  const deviceLabel = escapeHtml(delta.device_label || "");

  card.innerHTML =
    `<header class="rd-delta-head">` +
      `<span class="rd-delta-dot" aria-hidden="true"></span>` +
      `<span class="rd-delta-title" data-i18n="home.dashboard.board_delta.title">${escapeHtml(t("home.dashboard.board_delta.title"))}</span>` +
      `<span class="rd-delta-cov">${covLabel}</span>` +
      `<span class="rd-delta-board-num">${boardLabel}</span>` +
    `</header>` +
    `<div class="rd-delta-disclaimer">` +
      escapeHtml(t("home.dashboard.board_delta.disclaimer_pre")) +
      ` <strong>${deviceLabel} · ${boardLabel}</strong>. ` +
      escapeHtml(t("home.dashboard.board_delta.disclaimer_post")) +
    `</div>` +
    (ics.length ? (
      `<div class="rd-delta-sec">` +
        `<div class="rd-delta-sec-head">${escapeHtml(t("home.dashboard.board_delta.sec_ics"))}</div>` +
        icRows +
      `</div>`
    ) : "") +
    (rails.length ? (
      `<div class="rd-delta-sec">` +
        `<div class="rd-delta-sec-head">${escapeHtml(t("home.dashboard.board_delta.sec_rails"))}</div>` +
        railRows +
      `</div>`
    ) : "") +
    (pitfalls.length ? (
      `<div class="rd-delta-sec">` +
        `<div class="rd-delta-sec-head">${escapeHtml(t("home.dashboard.board_delta.sec_pitfalls"))}</div>` +
        pitRows +
      `</div>`
    ) : "") +
    (cousins.length ? (
      `<div class="rd-delta-sec">` +
        `<div class="rd-delta-sec-head">${escapeHtml(t("home.dashboard.board_delta.sec_cousins"))}</div>` +
        cousinRows +
      `</div>`
    ) : "") +
    `<div class="rd-delta-foot">` +
      `${ics.length} ICs · ${rails.length} rails · ${pitfalls.length} ` +
      escapeHtml(t("home.dashboard.board_delta.foot_pitfalls")) +
      ` · ${cousins.length} ` +
      escapeHtml(t("home.dashboard.board_delta.foot_cousins")) +
      ` · ${sources.length} ` +
      escapeHtml(t("home.dashboard.board_delta.foot_sources")) +
    `</div>`;

  card.classList.remove("hidden");
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
      const ago = c.last_turn_at ? relativeTimeFr(c.last_turn_at) : "…";
      const cost = typeof c.cost_usd === "number" ? `$${c.cost_usd.toFixed(3)}` : "n/a";
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
          : `<span class="rd-finding-session">(none)</span>`);
    const notes = f.notes
      ? `<p class="rd-finding-notes">${escapeHtml(f.notes)}</p>`
      : "";
    row.innerHTML =
      `<div class="rd-finding-top">` +
        `<span class="rd-finding-refdes">${escapeHtml(f.refdes)}</span>` +
        `<span class="rd-finding-symptom">${escapeHtml(f.symptom)}</span>` +
        sessionChip +
      `</div>` +
      `<p class="rd-finding-cause">${escapeHtml(f.confirmed_cause || "…")}</p>` +
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

let _fixBtnResetUnsub = null;
function wireFixButton(slug, rid) {
  const btn = document.getElementById("dashboardFixBtn");
  if (!btn) return;
  // resetBtn clears the pending state when the validation flow fails (agent
  // refuses, MA tool missing, error event). Wired to llm.js via the store
  // "fixButtonReset" key below.
  const resetBtn = () => {
    btn.disabled = false;
    btn.innerHTML = ICON_CHECK + " " + escapeHtml(t("home.dashboard.fix_btn"));
    btn.classList.remove("is-validated");
    if (btn._fixTimeoutId) { clearTimeout(btn._fixTimeoutId); btn._fixTimeoutId = null; }
  };
  // Drop any prior subscription so only the latest button is wired (mirrors
  // the single-handler semantics of the former window global).
  if (_fixBtnResetUnsub) _fixBtnResetUnsub();
  _fixBtnResetUnsub = store.subscribe("fixButtonReset", resetBtn);
  btn.classList.remove("hidden");
  resetBtn();
  btn.onclick = () => {
    const ws = getDiagnosticWS();
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

// Re-render the imperatively-built repair dashboard when the user toggles
// language. Only fires when the dashboard is actually showing — otherwise the
// next renderRepairDashboard call picks up the new locale naturally.
async function refreshHomeOnLocaleChange() {
  const homeSection = document.getElementById("homeSection");
  if (!homeSection || homeSection.classList.contains("hidden")) return;

  const slug = getDeviceSlug();
  const rid = getRepairId();

  // Dashboard mode only — re-render the focused session view on locale toggle.
  // There is no journal grid anymore (the landing is the global home and handles
  // its own i18n via data-i18n); when there's no active repair, nothing here is
  // imperatively built, so we just return.
  if (slug && rid) {
    try {
      await renderRepairDashboard({ device: slug, repair: rid });
    } catch (err) {
      console.warn("[home] dashboard re-render failed", err);
    }
  }
}

// Wire the home/dashboard locale refresh. The new-repair modal was removed in
// D.2 (the landing form is the single creation entry, and it is a strict superset
// — device + symptom + device_kind + schematic-at-creation); only the dashboard's
// imperative re-render on EN/FR toggle remains here.
export function initHome() {
  if (window.i18n && typeof window.i18n.onChange === "function") {
    window.i18n.onChange(() => { refreshHomeOnLocaleChange(); });
  }
}
