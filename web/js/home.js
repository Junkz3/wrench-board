// Home (journal des réparations) + the "nouvelle réparation" modal.
//
// renderHome() renders the pack grid from a /pipeline/packs response, and
// initNewRepairModal() wires the modal's open/close/submit handlers plus
// its own document-level keydown interceptor. The keydown listener is
// intentionally registered before main.js adds its global Cmd+K / Esc
// handler — stopImmediatePropagation() in this handler only works if it
// runs first.

import { openPipelineProgress } from './pipeline_progress.js';

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

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]
  ));
}

function humanizeSlug(slug) {
  return slug.replace(/-/g, " ").replace(/^./, c => c.toUpperCase());
}

function cardHTML(entry, packIndex) {
  const p = packIndex.get(entry.device_slug) || {};
  const complete = entry.complete;
  const badges = [
    `<span class="badge ${complete ? 'ok' : 'warn'}">${complete ? 'pack complet' : 'incomplet'}</span>`,
    p.has_audit_verdict ? '<span class="badge">audité</span>' : '',
    entry.form_factor ? `<span class="badge mono">${escapeHtml(entry.form_factor)}</span>` : '',
    entry.version ? `<span class="badge mono">${escapeHtml(entry.version)}</span>` : '',
  ].filter(Boolean).join("");
  return `
    <a class="home-card" href="?device=${encodeURIComponent(entry.device_slug)}">
      <div class="slug">${escapeHtml(entry.device_slug)}</div>
      <div class="name">${escapeHtml(entry.device_label || humanizeSlug(entry.device_slug))}</div>
      <div class="badges">${badges}</div>
    </a>
  `;
}

function brandBlockHTML(brandName, models, packIndex) {
  const modelNames = Object.keys(models).sort((a, b) => a.localeCompare(b));
  const totalPacks = modelNames.reduce((n, m) => n + models[m].length, 0);
  const counter = `${totalPacks} ${totalPacks > 1 ? 'réparations' : 'réparation'} · ${modelNames.length} ${modelNames.length > 1 ? 'modèles' : 'modèle'}`;
  const modelBlocks = modelNames.map(modelName => {
    const entries = models[modelName].slice().sort((a, b) =>
      (a.device_label || a.device_slug).localeCompare(b.device_label || b.device_slug)
    );
    const cards = entries.map(e => cardHTML(e, packIndex)).join("");
    return `
      <div class="home-model">
        <div class="home-model-head">
          <span class="home-model-name">${escapeHtml(modelName)}</span>
          <span class="home-model-count mono">${entries.length}</span>
        </div>
        <div class="home-grid">${cards}</div>
      </div>
    `;
  }).join("");
  return `
    <section class="home-brand">
      <header class="home-brand-head">
        <h2 class="home-brand-name">${escapeHtml(brandName)}</h2>
        <span class="home-brand-count mono">${counter}</span>
      </header>
      <div class="home-brand-body">${modelBlocks}</div>
    </section>
  `;
}

function uncategorizedBlockHTML(entries, packIndex) {
  const cards = entries
    .slice()
    .sort((a, b) => (a.device_label || a.device_slug).localeCompare(b.device_label || b.device_slug))
    .map(e => cardHTML(e, packIndex))
    .join("");
  const counter = `${entries.length} ${entries.length > 1 ? 'réparations' : 'réparation'}`;
  return `
    <section class="home-brand home-brand-uncategorized">
      <header class="home-brand-head">
        <h2 class="home-brand-name">Non catégorisé</h2>
        <span class="home-brand-count mono">${counter}</span>
      </header>
      <div class="home-brand-body">
        <div class="home-model">
          <div class="home-grid">${cards}</div>
        </div>
      </div>
    </section>
  `;
}

export function renderHome(packs, taxonomy) {
  const container = document.getElementById("homeSections");
  const empty = document.getElementById("homeEmpty");
  container.innerHTML = "";

  const packIndex = new Map(packs.map(p => [p.device_slug, p]));

  const brandNames = Object.keys(taxonomy.brands || {}).sort((a, b) => a.localeCompare(b));
  const uncategorized = taxonomy.uncategorized || [];

  if (brandNames.length === 0 && uncategorized.length === 0) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  const blocks = brandNames.map(brand => brandBlockHTML(brand, taxonomy.brands[brand], packIndex));
  if (uncategorized.length > 0) {
    blocks.push(uncategorizedBlockHTML(uncategorized, packIndex));
  }
  container.innerHTML = blocks.join("");
}

/* ---------- NEW REPAIR MODAL ---------- */
const newRepairBackdrop = document.getElementById("newRepairBackdrop");
const newRepairForm     = document.getElementById("newRepairForm");
const newRepairDevice   = document.getElementById("newRepairDevice");
const newRepairSymptom  = document.getElementById("newRepairSymptom");
const newRepairSubmit   = document.getElementById("newRepairSubmit");
const newRepairError    = document.getElementById("newRepairError");
let   newRepairLastFocus = null;

function openNewRepair() {
  newRepairLastFocus = document.activeElement;
  newRepairForm.reset();
  setNewRepairError(null);
  setNewRepairBusy(false);
  newRepairBackdrop.classList.add("open");
  newRepairBackdrop.setAttribute("aria-hidden", "false");
  // Let the backdrop fade-in paint, then focus first field.
  requestAnimationFrame(() => newRepairDevice.focus());
}

function closeNewRepair() {
  if (!newRepairBackdrop.classList.contains("open")) return;
  newRepairBackdrop.classList.remove("open");
  newRepairBackdrop.setAttribute("aria-hidden", "true");
  setNewRepairBusy(false);
  if (newRepairLastFocus && typeof newRepairLastFocus.focus === "function") {
    newRepairLastFocus.focus();
  }
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
  const device_label = newRepairDevice.value.trim();
  const symptom      = newRepairSymptom.value.trim();
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
      body: JSON.stringify({device_label, symptom}),
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
