// Memory Bank section — single-page reader for one knowledge pack.
//
// Fetches /pipeline/packs (list) to populate the pack picker, then
// /pipeline/packs/{slug}/full to render registry, knowledge graph,
// rules, dictionary, and audit verdict. Missing fields render as "—"
// (hard rule #5: never fabricate).

const STATE = {
  packs: [],        // PackSummary[] from /pipeline/packs
  currentSlug: null,
  pack: null,       // Full payload for currentSlug, or null while loading
  loading: false,
};

function el(id) { return document.getElementById(id); }

function fmt(value, fallback = "—") {
  if (value === null || value === undefined) return fallback;
  if (typeof value === "string" && value.trim() === "") return fallback;
  return value;
}

function escHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function prettifySlug(slug) {
  if (!slug) return "";
  return slug.replace(/-/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}

/* ---------- fetch helpers ---------- */

async function fetchPacks() {
  try {
    const res = await fetch("/pipeline/packs");
    if (!res.ok) return [];
    return await res.json();
  } catch (err) {
    console.warn("memory-bank: /pipeline/packs failed", err);
    return [];
  }
}

async function fetchFullPack(slug) {
  try {
    const res = await fetch(`/pipeline/packs/${encodeURIComponent(slug)}/full`);
    if (!res.ok) return null;
    return await res.json();
  } catch (err) {
    console.warn("memory-bank: /full fetch failed", err);
    return null;
  }
}

/* ---------- header rendering ---------- */

function renderPackPicker() {
  const sel = el("mbPackSelect");
  sel.innerHTML = "";
  if (STATE.packs.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "— aucun pack —";
    sel.appendChild(opt);
    sel.disabled = true;
    return;
  }
  sel.disabled = false;
  for (const p of STATE.packs) {
    const opt = document.createElement("option");
    opt.value = p.device_slug;
    opt.textContent = p.device_slug;
    if (p.device_slug === STATE.currentSlug) opt.selected = true;
    sel.appendChild(opt);
  }
}

function renderVerdict() {
  const row = el("mbVerdictRow");
  const pack = STATE.pack;
  if (!pack) {
    row.innerHTML = "";
    return;
  }
  const v = pack.audit_verdict;
  let verdictHtml;
  if (!v) {
    verdictHtml = `
      <span class="mb-verdict none" title="Aucun audit n'a encore été écrit pour ce pack.">
        <span class="dot"></span>AUDIT · non exécuté
      </span>
      <span class="mb-score">cohérence <b>—</b></span>`;
  } else {
    const cls = v.overall_status === "APPROVED"       ? "approved"
              : v.overall_status === "NEEDS_REVISION" ? "needs-revision"
              : v.overall_status === "REJECTED"       ? "rejected"
              : "none";
    const label = v.overall_status === "APPROVED"       ? "AUDIT · approuvé"
                : v.overall_status === "NEEDS_REVISION" ? "AUDIT · révision"
                : v.overall_status === "REJECTED"       ? "AUDIT · rejeté"
                : "AUDIT · inconnu";
    const score = (typeof v.consistency_score === "number")
      ? v.consistency_score.toFixed(2) : "—";
    verdictHtml = `
      <span class="mb-verdict ${cls}"><span class="dot"></span>${escHtml(label)}</span>
      <span class="mb-score">cohérence <b>${score}</b></span>`;
  }

  // Counts from the pack contents.
  const reg = pack.registry || {};
  const kg = pack.knowledge_graph || {};
  const rules = pack.rules || {};
  const dict = pack.dictionary || {};
  const counts = `
    <span class="mb-counts">
      <span class="count"><b>${(reg.components || []).length}</b> composants</span>
      <span class="count"><b>${(reg.signals || []).length}</b> signaux</span>
      <span class="count"><b>${(kg.nodes || []).length}</b> nœuds</span>
      <span class="count"><b>${(kg.edges || []).length}</b> arêtes</span>
      <span class="count"><b>${(rules.rules || []).length}</b> règles</span>
      <span class="count"><b>${(dict.entries || []).length}</b> fiches</span>
    </span>`;
  row.innerHTML = verdictHtml + counts;
}

function renderDeviceLabel() {
  const h1 = el("mbDeviceLabel");
  if (!STATE.pack) {
    h1.textContent = document.body.classList.contains("guided-mode") ? "Fiche appareil" : "Memory Bank";
    return;
  }
  // Prefer a clean `{brand} {model}` from taxonomy; append the form_factor as
  // a small subordinate chip so the header reads "what we're fixing" without
  // repeating the board type inside the name.
  const tax = (STATE.pack.registry || {}).taxonomy || {};
  const nameParts = [tax.brand, tax.model].filter(Boolean);
  const deviceName = nameParts.length > 0
    ? nameParts.join(" ")
    : (STATE.pack.device_label || STATE.currentSlug);
  const form = tax.form_factor ? ` <span style="font-family:var(--mono);font-size:10.5px;color:var(--text-3);letter-spacing:.3px;text-transform:uppercase;margin-left:8px;padding:1px 7px;border:1px solid var(--border-soft);border-radius:10px">${escHtml(tax.form_factor)}</span>` : "";
  const _mbLabel = document.body.classList.contains("guided-mode") ? "Fiche appareil" : "Memory Bank";
  h1.innerHTML = `${_mbLabel} <span class="sub" style="color:var(--text-3);font-weight:400;font-size:14px;margin-left:10px">· ${escHtml(deviceName)}</span>${form}`;
}

/* ---------- blocks rendering ---------- */

function renderRegistry(registry) {
  const body = el("mbBlockRegistry");
  if (!registry) {
    body.innerHTML = `<div class="mb-missing">registry.json absent — pipeline non exécuté.</div>`;
    return;
  }
  const comps = registry.components || [];
  const sigs  = registry.signals    || [];
  body.innerHTML = `
    <h3 style="margin:0 0 8px;font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:var(--text-3);font-family:var(--mono);font-weight:500">Composants (${comps.length})</h3>
    ${comps.length === 0 ? '<div class="mb-missing">Aucun composant.</div>' : `
      <table class="mb-table" data-kind="registry-components">
        <thead><tr><th>Refdes</th><th>Type</th><th>Alias</th><th>Description</th></tr></thead>
        <tbody>
          ${comps.map(c => `
            <tr data-search="${escHtml([c.canonical_name, c.logical_alias, ...(c.aliases || []), c.description, c.kind].filter(Boolean).join(" ").toLowerCase())}">
              <td class="mono">${escHtml(c.canonical_name)}${c.logical_alias ? `<div style="font-size:10.5px;color:var(--text-3);font-family:inherit;font-style:italic">${escHtml(c.logical_alias)}</div>` : ""}</td>
              <td><span class="mb-kind ${escHtml(c.kind || "unknown")}">${escHtml(c.kind || "unknown")}</span></td>
              <td>${(c.aliases || []).map(a => `<span class="mb-alias">${escHtml(a)}</span>`).join("") || '<span class="muted">—</span>'}</td>
              <td>${escHtml(c.description) || '<span class="muted">—</span>'}</td>
            </tr>`).join("")}
        </tbody>
      </table>`}
    <h3 style="margin:16px 0 8px;font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:var(--text-3);font-family:var(--mono);font-weight:500">Signaux (${sigs.length})</h3>
    ${sigs.length === 0 ? '<div class="mb-missing">Aucun signal.</div>' : `
      <table class="mb-table" data-kind="registry-signals">
        <thead><tr><th>Nom canonique</th><th>Type</th><th>Alias</th><th>Tension nominale</th></tr></thead>
        <tbody>
          ${sigs.map(s => `
            <tr data-search="${escHtml([s.canonical_name, ...(s.aliases || []), s.kind].filter(Boolean).join(" ").toLowerCase())}">
              <td class="mono">${escHtml(s.canonical_name)}</td>
              <td><span class="mb-kind ${escHtml(s.kind || "unknown")}">${escHtml(s.kind || "unknown")}</span></td>
              <td>${(s.aliases || []).map(a => `<span class="mb-alias">${escHtml(a)}</span>`).join("") || '<span class="muted">—</span>'}</td>
              <td class="mono">${s.nominal_voltage !== null && s.nominal_voltage !== undefined ? `<span class="mb-volt">${s.nominal_voltage} V</span>` : '<span class="muted">—</span>'}</td>
            </tr>`).join("")}
        </tbody>
      </table>`}
  `;
  el("mbBlockRegistryCount").innerHTML = `<b>${comps.length}</b> composants · <b>${sigs.length}</b> signaux`;
}

function renderKnowledgeGraph(kg) {
  const body = el("mbBlockGraph");
  if (!kg) {
    body.innerHTML = `<div class="mb-missing">knowledge_graph.json absent.</div>`;
    return;
  }
  const nodes = kg.nodes || [];
  const edges = kg.edges || [];
  const byKind = {symptom: 0, component: 0, net: 0};
  for (const n of nodes) { if (n.kind in byKind) byKind[n.kind]++; }

  body.innerHTML = `
    <div class="mb-graph-stats">
      <div class="mb-stat sym"><span class="label">Symptômes</span><span class="value">${byKind.symptom}</span></div>
      <div class="mb-stat cmp"><span class="label">Composants</span><span class="value">${byKind.component}</span></div>
      <div class="mb-stat net"><span class="label">Nets</span><span class="value">${byKind.net}</span></div>
      <div class="mb-stat edge"><span class="label">Arêtes</span><span class="value">${edges.length}</span></div>
    </div>
    ${edges.length === 0 ? '<div class="mb-missing">Aucune arête dans le graphe.</div>' : `
      <div class="mb-edges">
        ${edges.map(e => `
          <div class="mb-edge-row" data-search="${escHtml([e.source_id, e.target_id, e.relation].filter(Boolean).join(" ").toLowerCase())}">
            <div class="src" title="${escHtml(e.source_id)}">${escHtml(e.source_id)}</div>
            <div class="rel ${escHtml(e.relation)}">${escHtml(e.relation)}</div>
            <div class="dst" title="${escHtml(e.target_id)}">${escHtml(e.target_id)}</div>
          </div>`).join("")}
      </div>`}
  `;
  el("mbBlockGraphCount").innerHTML = `<b>${nodes.length}</b> nœuds · <b>${edges.length}</b> arêtes`;
}

function renderRules(rules) {
  const body = el("mbBlockRules");
  if (!rules) {
    body.innerHTML = `<div class="mb-missing">rules.json absent.</div>`;
    return;
  }
  const items = rules.rules || [];
  if (items.length === 0) {
    body.innerHTML = `<div class="mb-missing">Aucune règle de diagnostic.</div>`;
    el("mbBlockRulesCount").innerHTML = `<b>0</b> règles`;
    return;
  }
  body.innerHTML = items.map((r, i) => {
    const searchText = [
      r.id,
      ...(r.symptoms || []),
      ...(r.likely_causes || []).flatMap(c => [c.refdes, c.mechanism]),
      ...(r.diagnostic_steps || []).flatMap(s => [s.action, s.expected]),
    ].filter(Boolean).join(" ").toLowerCase();
    const headSym = (r.symptoms && r.symptoms.length > 0)
      ? `<b>${escHtml(r.symptoms[0])}</b>${r.symptoms.length > 1 ? ` <span style="color:var(--text-3)">+${r.symptoms.length - 1}</span>` : ""}`
      : '<span style="color:var(--text-3)">(aucun symptôme)</span>';
    const conf = typeof r.confidence === "number" ? r.confidence.toFixed(2) : "—";
    return `
      <div class="mb-rule" data-rule-idx="${i}" data-search="${escHtml(searchText)}">
        <div class="mb-rule-head">
          <span class="caret"></span>
          <span class="mb-rule-id">${escHtml(r.id || `rule-${i}`)}</span>
          <span class="mb-rule-sym">${headSym}</span>
          <span class="mb-rule-conf">conf ${conf}</span>
        </div>
        <div class="mb-rule-body">
          <div class="mb-rule-section">
            <h4>Symptômes</h4>
            <div class="mb-rule-symptoms">
              ${(r.symptoms || []).map(s => `<span class="sym">${escHtml(s)}</span>`).join("") || '<span class="muted">—</span>'}
            </div>
          </div>
          <div class="mb-rule-section">
            <h4>Causes probables</h4>
            ${(r.likely_causes || []).length === 0 ? '<span class="muted">—</span>' :
              (r.likely_causes || []).map(c => {
                const p = typeof c.probability === "number" ? c.probability : 0;
                return `
                  <div class="mb-cause">
                    <span class="refdes">${escHtml(c.refdes)}</span>
                    <span class="mech">${escHtml(c.mechanism) || "—"}</span>
                    <div class="prob-bar"><div class="prob-fill" style="width:${(p * 100).toFixed(0)}%"></div></div>
                    <span class="prob-val">${p.toFixed(2)}</span>
                  </div>`;
              }).join("")}
          </div>
          <div class="mb-rule-section">
            <h4>Étapes de diagnostic</h4>
            ${(r.diagnostic_steps || []).length === 0 ? '<span class="muted">—</span>' :
              (r.diagnostic_steps || []).map(s => `
                <div class="mb-step">
                  <span class="act">${escHtml(s.action)}</span>
                  ${s.expected ? `<span class="exp">attendu ${escHtml(s.expected)}</span>` : ""}
                </div>`).join("")}
          </div>
          ${(r.sources || []).length > 0 ? `
            <div class="mb-rule-section">
              <h4>Sources</h4>
              <div class="mb-rule-sources">
                ${(r.sources || []).map(s => `<span class="src">${escHtml(s)}</span>`).join("")}
              </div>
            </div>` : ""}
        </div>
      </div>`;
  }).join("");

  // Accordion wire.
  body.querySelectorAll(".mb-rule-head").forEach(h => {
    h.addEventListener("click", () => {
      h.parentElement.classList.toggle("open");
    });
  });
  el("mbBlockRulesCount").innerHTML = `<b>${items.length}</b> règles`;
}

function renderDictionary(dict) {
  const body = el("mbBlockDictionary");
  if (!dict) {
    body.innerHTML = `<div class="mb-missing">dictionary.json absent.</div>`;
    return;
  }
  const entries = dict.entries || [];
  if (entries.length === 0) {
    body.innerHTML = `<div class="mb-missing">Aucune fiche composant.</div>`;
    el("mbBlockDictionaryCount").innerHTML = `<b>0</b> fiches`;
    return;
  }
  body.innerHTML = `
    <table class="mb-table" data-kind="dictionary">
      <thead><tr><th>Refdes</th><th>Rôle</th><th>Boîtier</th><th>Modes de défaillance</th><th>Notes</th></tr></thead>
      <tbody>
        ${entries.map(e => {
          const modes = e.typical_failure_modes || [];
          const searchText = [e.canonical_name, e.role, e.package, e.notes, ...modes]
            .filter(Boolean).join(" ").toLowerCase();
          return `
            <tr data-search="${escHtml(searchText)}">
              <td class="mono">${escHtml(e.canonical_name)}</td>
              <td>${escHtml(e.role) || '<span class="muted">—</span>'}</td>
              <td class="mono">${escHtml(e.package) || '<span class="muted">—</span>'}</td>
              <td>${modes.length === 0 ? '<span class="muted">—</span>' :
                modes.map(m => `<span class="mb-alias" style="color:var(--amber);background:rgba(245,158,11,.08);border-color:rgba(245,158,11,.3)">${escHtml(m)}</span>`).join("")}</td>
              <td>${escHtml(e.notes) || '<span class="muted">—</span>'}</td>
            </tr>`;
        }).join("")}
      </tbody>
    </table>`;
  el("mbBlockDictionaryCount").innerHTML = `<b>${entries.length}</b> fiches`;
}

function renderAudit(verdict) {
  const block = el("mbBlockAuditWrapper");
  const body  = el("mbBlockAudit");
  if (!verdict) {
    block.style.display = "";
    body.innerHTML = `<div class="mb-missing">audit_verdict.json absent — le pipeline n'a pas encore audité ce pack.</div>`;
    el("mbBlockAuditCount").innerHTML = `<b>—</b>`;
    return;
  }
  block.style.display = "";
  const status = verdict.overall_status || "UNKNOWN";
  const score  = typeof verdict.consistency_score === "number" ? verdict.consistency_score.toFixed(2) : "—";
  const files  = verdict.files_to_rewrite || [];
  const drift  = verdict.drift_report || [];
  const brief  = verdict.revision_brief || "";

  const headline = status === "APPROVED"
    ? "Audit approuvé — le pack est cohérent avec le registre."
    : status === "NEEDS_REVISION"
      ? "Audit demande une révision — dérive détectée, corrections attendues."
      : status === "REJECTED"
        ? "Audit rejeté — pack incohérent, non exploitable en l'état."
        : "Statut d'audit inconnu.";

  body.innerHTML = `
    <div class="mb-audit-summary">
      <div class="headline">${escHtml(headline)}</div>
      <div class="mb-score" style="margin-left:auto">cohérence <b>${score}</b></div>
    </div>
    ${brief ? `<div class="mb-audit-brief">${escHtml(brief)}</div>` : ""}
    ${files.length > 0 ? `
      <div class="mb-drift">
        <h4>Fichiers à réécrire</h4>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          ${files.map(f => `<span class="mb-alias" style="color:var(--amber);background:rgba(245,158,11,.08);border-color:rgba(245,158,11,.3)">${escHtml(f)}</span>`).join("")}
        </div>
      </div>` : ""}
    ${drift.length > 0 ? `
      <div class="mb-drift">
        <h4>Dérives détectées (${drift.length})</h4>
        ${drift.map(d => `
          <div class="mb-drift-item">
            <span class="file">${escHtml(d.file)}</span>
            <span class="reason">${escHtml(d.reason)}</span>
            ${(d.mentions || []).length > 0 ? `
              <div class="mentions">${(d.mentions || []).map(m => `<code>${escHtml(m)}</code>`).join("")}</div>
            ` : ""}
          </div>`).join("")}
      </div>` : ""}
  `;
  el("mbBlockAuditCount").innerHTML = `<b>${status}</b>`;
}

/* ---------- master rendering ---------- */

function renderPack() {
  renderDeviceLabel();
  renderVerdict();
  const p = STATE.pack;
  if (!p) return;
  renderRegistry(p.registry);
  renderKnowledgeGraph(p.knowledge_graph);
  renderRules(p.rules);
  renderDictionary(p.dictionary);
  renderAudit(p.audit_verdict);
  applySearchFilter(el("mbSearch").value || "");
}

function showEmptyState(message) {
  el("mbBody").style.display = "none";
  const empty = el("mbEmpty");
  empty.classList.remove("hidden");
  empty.querySelector("p").textContent = message || "Aucun pack de connaissances n'est disponible. Lance le pipeline pour en générer un.";
}

function hideEmptyState() {
  el("mbBody").style.display = "";
  el("mbEmpty").classList.add("hidden");
}

/* ---------- search ---------- */

function applySearchFilter(query) {
  const q = query.trim().toLowerCase();
  const root = el("memoryBank");

  // Table rows.
  root.querySelectorAll("tr[data-search]").forEach(tr => {
    tr.classList.toggle("hidden", q !== "" && !tr.dataset.search.includes(q));
  });

  // Edge rows (grid, `display:contents` so we toggle a hidden flag).
  root.querySelectorAll(".mb-edge-row[data-search]").forEach(row => {
    row.classList.toggle("hidden", q !== "" && !row.dataset.search.includes(q));
  });

  // Rules accordions.
  root.querySelectorAll(".mb-rule[data-search]").forEach(rule => {
    rule.classList.toggle("hidden", q !== "" && !rule.dataset.search.includes(q));
  });
}

/* ---------- public API ---------- */

export async function loadMemoryBank() {
  if (STATE.loading) return;
  STATE.loading = true;
  try {
    STATE.packs = await fetchPacks();
    // Prefer ?device= if present, else first available pack, else empty state.
    const params = new URLSearchParams(window.location.search);
    const deviceParam = params.get("device");
    if (deviceParam && STATE.packs.some(p => p.device_slug === deviceParam)) {
      STATE.currentSlug = deviceParam;
    } else if (STATE.packs.length > 0) {
      STATE.currentSlug = STATE.packs[0].device_slug;
    } else {
      STATE.currentSlug = null;
    }
    renderPackPicker();

    if (!STATE.currentSlug) {
      showEmptyState();
      renderDeviceLabel();
      return;
    }
    hideEmptyState();
    STATE.pack = await fetchFullPack(STATE.currentSlug);
    if (!STATE.pack) {
      showEmptyState(`Impossible de charger le pack « ${STATE.currentSlug} ».`);
      return;
    }
    renderPack();
  } finally {
    STATE.loading = false;
  }
}

export function initMemoryBank() {
  const sel = el("mbPackSelect");
  if (sel) {
    sel.addEventListener("change", async () => {
      const slug = sel.value;
      if (!slug) return;
      STATE.currentSlug = slug;
      STATE.pack = await fetchFullPack(slug);
      if (!STATE.pack) {
        showEmptyState(`Impossible de charger le pack « ${slug} ».`);
        return;
      }
      hideEmptyState();
      renderPack();
    });
  }
  const search = el("mbSearch");
  if (search) {
    search.addEventListener("input", () => applySearchFilter(search.value));
    search.addEventListener("keydown", ev => {
      if (ev.key === "Escape" && search.value !== "") {
        ev.preventDefault();
        ev.stopPropagation();
        search.value = "";
        applySearchFilter("");
      }
    });
  }
}
