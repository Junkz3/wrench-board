// Home (journal des réparations) + the "nouvelle réparation" modal.
//
// renderHome() renders the pack grid from a /pipeline/packs response, and
// initNewRepairModal() wires the modal's open/close/submit handlers plus
// its own document-level keydown interceptor. The keydown listener is
// intentionally registered before main.js adds its global Cmd+K / Esc
// handler — stopImmediatePropagation() in this handler only works if it
// runs first.

import { openPipelineProgress } from './pipeline_progress.js';
import { leaveSession } from './router.js';
import { openPanel } from './llm.js';
import { ICON_CHECK } from './icons.js';

export async function loadHomePacks() {
  try {
    const res = await fetch("/pipeline/packs");
    if (!res.ok) return [];
    return await res.json();
  } catch (err) {
    console.warn("loadHomePacks failed", err);
    return [];
  }
}

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
  if (mins < 1) return "à l'instant";
  if (mins < 60) return `il y a ${mins} min`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `il y a ${hours} h`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "hier";
  if (days < 7) return `il y a ${days} j`;
  return then.toLocaleDateString("fr-FR", { day: "numeric", month: "short", year: "numeric" });
}

const STATUS_LABEL = {
  open: "ouverte",
  in_progress: "en cours",
  closed: "clôturée",
};

function statusBadgeHTML(status) {
  const label = STATUS_LABEL[status] || status || "ouverte";
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
  return `
    <div class="home-model">
      <div class="home-model-head">
        <span class="home-model-name">${escapeHtml(modelName)}</span>
        <span class="home-model-count mono">${sorted.length} ${sorted.length > 1 ? 'réparations' : 'réparation'}</span>
      </div>
      <div class="home-grid">${cards}</div>
    </div>
  `;
}

function brandBlockHTML(brandName, devicesMap) {
  const slugs = Array.from(devicesMap.keys()).sort((a, b) => a.localeCompare(b));
  const totalRepairs = slugs.reduce((n, s) => n + devicesMap.get(s).repairs.length, 0);
  const counter = `${totalRepairs} ${totalRepairs > 1 ? 'réparations' : 'réparation'} · ${slugs.length} ${slugs.length > 1 ? 'devices' : 'device'}`;
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
        <span class="home-brand-count mono">${counter}</span>
      </header>
      <div class="home-brand-body">${body}</div>
    </section>
  `;
}

export function renderHome(_packs, taxonomy, repairs = []) {
  const container = document.getElementById("homeSections");
  const empty = document.getElementById("homeEmpty");
  container.innerHTML = "";

  if (!repairs || repairs.length === 0) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  const taxIndex = indexTaxonomyBySlug(taxonomy);
  // Group repairs by brand → device_slug → list of repairs.
  const byBrand = new Map();  // brand → Map(slug → {taxEntry, repairs})
  for (const r of repairs) {
    const tax = taxIndex.get(r.device_slug) || null;
    const brand = tax?.brand || "Non catégorisé";
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
    if (a === "Non catégorisé") return 1;
    if (b === "Non catégorisé") return -1;
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
  const [repair, convs, pack, findings, taxonomy] = await Promise.all([
    fetchJSON(`/pipeline/repairs/${encodeURIComponent(rid)}`, null),
    fetchJSON(`/pipeline/repairs/${encodeURIComponent(rid)}/conversations`, { conversations: [] }),
    fetchJSON(`/pipeline/packs/${encodeURIComponent(slug)}`, null),
    fetchJSON(`/pipeline/packs/${encodeURIComponent(slug)}/findings`, []),
    loadTaxonomy(),
  ]);

  const taxIndex = indexTaxonomyBySlug(taxonomy);
  const taxEntry = taxIndex.get(slug) || null;

  renderDashboardHeader(repair, taxEntry, slug, rid);
  renderDashboardTiles(slug, rid, pack, taxEntry);
  renderDashboardConvs(convs.conversations || [], rid);
  renderDashboardFindings(findings, rid);
  renderDashboardTimeline(repair, convs.conversations || [], findings, pack);
  renderDashboardPack(pack, slug, rid);
  wireDashboardHandlers();
  wireFixButton(slug, rid);
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
    `<span class="rd-created">créée ${escapeHtml(created)}</span>`;
}

function renderDashboardTiles(slug, rid, pack, taxEntry) {
  const qs = `?device=${encodeURIComponent(slug)}&repair=${encodeURIComponent(rid)}`;
  const pcb = document.getElementById("rdTilePcb");
  const graphe = document.getElementById("rdTileGraphe");
  const schematic = document.getElementById("rdTileSchematic");
  const memoryBank = document.getElementById("rdTileMemoryBank");
  if (pcb) pcb.href = `${qs}#pcb`;
  if (graphe) graphe.href = `${qs}#graphe`;
  if (schematic) schematic.href = `${qs}#schematic`;
  if (memoryBank) memoryBank.href = `${qs}&view=md#graphe`;

  // Tile metas — static text when we don't have richer data. Keep mono and
  // short so the tile stays scannable.
  const pcbMeta = document.getElementById("rdTilePcbMeta");
  if (pcbMeta) pcbMeta.textContent = taxEntry?.form_factor || "board";
  const grapheMeta = document.getElementById("rdTileGrapheMeta");
  if (grapheMeta) {
    const complete = pack && pack.has_registry && pack.has_knowledge_graph
      && pack.has_rules && pack.has_dictionary && pack.has_audit_verdict;
    grapheMeta.textContent = complete ? "APPROUVÉ" : (pack ? "en construction" : "aucune mémoire");
  }
  const schematicMeta = document.getElementById("rdTileSchematicMeta");
  if (schematicMeta) schematicMeta.textContent = pack?.has_schematic_graph ? "importé" : "non importé";
  const mbMeta = document.getElementById("rdTileMemoryBankMeta");
  if (mbMeta) mbMeta.textContent = pack?.has_rules ? "rules + findings" : "vide";
}

function renderDashboardConvs(conversations, rid) {
  const body = document.getElementById("rdConvBody");
  const count = document.getElementById("rdConvCount");
  if (!body || !count) return;
  count.textContent = String(conversations.length);
  body.innerHTML = "";
  if (conversations.length === 0) {
    body.innerHTML = '<div class="rd-block-empty">Aucune conversation — démarre une discussion avec l\'agent.</div>';
  } else {
    for (const c of conversations) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "rd-conv-row";
      row.dataset.convId = c.id;
      const tier = (c.tier || "fast").toLowerCase();
      const title = escapeHtml((c.title || `Conversation ${c.id.slice(0, 6)}`).slice(0, 80));
      const ago = c.last_turn_at ? relativeTimeFr(c.last_turn_at) : "—";
      const cost = typeof c.cost_usd === "number" ? `$${c.cost_usd.toFixed(3)}` : "—";
      row.innerHTML =
        `<span class="rd-conv-tier t-${tier}">${tier.toUpperCase()}</span>` +
        `<span class="rd-conv-title">${title}</span>` +
        `<span class="rd-conv-meta">${c.turns || 0} turns · ${cost} · ${escapeHtml(ago)}</span>`;
      row.addEventListener("click", () => {
        openPanel(c.id);  // single connect targeting the right conv
      });
      body.appendChild(row);
    }
  }
  const newBtn = document.createElement("button");
  newBtn.type = "button";
  newBtn.className = "rd-conv-new";
  newBtn.textContent = "+ Nouvelle conversation";
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
    body.innerHTML = '<div class="rd-block-empty">Aucun finding pour ce device. L\'agent en enregistre via <code>mb_record_finding</code> quand tu confirmes une panne.</div>';
    return;
  }
  body.innerHTML = "";
  const currentShort = currentRid.slice(0, 8);
  for (const f of findings) {
    const row = document.createElement("div");
    row.className = "rd-finding-row";
    const isCurrent = f.session_id && f.session_id.startsWith(currentShort);
    const sessionChip = isCurrent
      ? `<span class="rd-finding-session current">ce repair</span>`
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
    events.push({ when: repair.created_at, label: "Session ouverte", kind: "cyan" });
  }
  for (const c of conversations) {
    if (c.last_turn_at) {
      events.push({
        when: c.last_turn_at,
        label: `Activité · ${(c.tier || "fast").toLowerCase()} · ${c.turns || 0} turns`,
        kind: "emerald",
      });
    }
  }
  for (const f of findings) {
    if (f.created_at) {
      events.push({
        when: f.created_at,
        label: `Finding ${f.refdes || "?"} confirmé`,
        kind: "violet",
      });
    }
  }
  if (pack?.audit_verdict) {
    events.push({
      when: repair?.created_at || new Date().toISOString(),
      label: `Pack audité — ${pack.audit_verdict}`,
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
    body.innerHTML += `<li class="rd-timeline-item"><span class="rd-timeline-node"></span><span class="rd-timeline-label">+${events.length - MAX} plus anciens</span></li>`;
  }
  if (events.length === 0) {
    body.innerHTML = '<li class="rd-block-empty">Aucune activité.</li>';
  }
}

function renderDashboardPack(pack, slug, rid) {
  const body = document.getElementById("rdPackBody");
  if (!body) return;
  if (!pack) {
    body.innerHTML = '<div class="rd-block-empty">Aucun pack — la mémoire du device n\'est pas encore construite.</div>';
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
  const statusLabel = complete ? "APPROUVÉ" : "en construction";
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
    btn.innerHTML = ICON_CHECK + " Marquer fix";
    btn.classList.remove("is-validated");
    if (btn._fixTimeoutId) { clearTimeout(btn._fixTimeoutId); btn._fixTimeoutId = null; }
  };
  window.__resetDashboardFixBtn = resetBtn;
  btn.classList.remove("hidden");
  resetBtn();
  btn.onclick = () => {
    const ws = window.__diagnosticWS;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      btn.textContent = "Ouvre le chat d'abord";
      setTimeout(() => { btn.innerHTML = ICON_CHECK + " Marquer fix"; }, 1800);
      return;
    }
    ws.send(JSON.stringify({ type: "validation.start", repair_id: rid }));
    btn.disabled = true;
    btn.textContent = "… Claude valide";
    // Safety timeout: if the agent never fires simulation.repair_validated
    // (MA tool missing, refusal, error), reset after 25s so the button
    // isn't permanently stuck.
    btn._fixTimeoutId = setTimeout(() => {
      btn.textContent = "Échec — réessaie";
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
  for (const { entry } of results) {
    const key = entry.brand || "Non catégorisé";
    (groups.get(key) || groups.set(key, []).get(key)).push(entry);
  }

  const parts = [];
  const trimmed = query.trim();
  const exactExists = results.some(r => normalize(r.entry.device_label) === normalize(trimmed));
  if (trimmed && !exactExists) {
    parts.push(`
      <button type="button" class="combo-option combo-create" data-action="create"
              data-label="${escapeHtml(trimmed)}" role="option">
        <span class="combo-label">+ Créer « ${escapeHtml(trimmed)} »</span>
        <span class="combo-meta"><span class="combo-badge">nouveau</span></span>
      </button>
    `);
  }

  if (groups.size === 0 && !trimmed) {
    parts.push('<div class="combo-empty">Aucun device connu — tape un nom pour en créer un.</div>');
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
      const badges = [
        e.complete ? '<span class="combo-badge ok">audité</span>' : '<span class="combo-badge">partiel</span>',
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
    newRepairHint.textContent =
      "Pack déjà construit — la session rouvre directement. Coche pour regénérer.";
  } else {
    newRepairRebuildRow.hidden = true;
    newRepairForceRebuild.checked = false;
    newRepairHint.textContent =
      "Pack existe mais incomplet — le pipeline va compléter les artefacts manquants.";
  }
}

function applyRebuildStateForTyped() {
  newRepairRebuildRow.hidden = true;
  newRepairForceRebuild.checked = false;
  newRepairHint.textContent =
    "Tape le nom du device (marque + modèle). Le type de board est détecté automatiquement.";
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
      ? '<span class="modal-spinner" aria-hidden="true"></span> Création…'
      : "Démarrer le diagnostic";
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
    setNewRepairError("Le nom du device doit faire au moins 2 caractères.", {title:"Champ incomplet — "});
    newRepairDevice.focus();
    return;
  }
  if (symptom.length < 5) {
    setNewRepairError("Décris le symptôme — 5 caractères minimum.", {title:"Champ incomplet — "});
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
      setNewRepairError(`Le backend a répondu ${res.status}. ${detail}`.trim(), {title:"Erreur — "});
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
    setNewRepairError(
      "Impossible de joindre le serveur. Vérifie que le backend tourne.",
      {title:"Réseau — "}
    );
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

export function initNewRepairModal() {
  document.getElementById("homeNewBtn").addEventListener("click", openNewRepair);
  document.getElementById("newRepairClose").addEventListener("click", closeNewRepair);
  document.getElementById("newRepairCancel").addEventListener("click", closeNewRepair);
  newRepairForm.addEventListener("submit", submitNewRepair);
  newRepairBackdrop.addEventListener("click", ev => {
    if (ev.target === newRepairBackdrop) closeNewRepair();
  });
  initCombo();

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
