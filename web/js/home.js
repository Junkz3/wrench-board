// Home (journal des réparations) + the "nouvelle réparation" modal.
//
// renderHome() renders the pack grid from a /pipeline/packs response, and
// initNewRepairModal() wires the modal's open/close/submit handlers plus
// its own document-level keydown interceptor. The keydown listener is
// intentionally registered before main.js adds its global Cmd+K / Esc
// handler — stopImmediatePropagation() in this handler only works if it
// runs first.

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

export function renderHome(packs) {
  const grid = document.getElementById("homeGrid");
  const empty = document.getElementById("homeEmpty");
  grid.innerHTML = "";
  if (packs.length === 0) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  for (const p of packs) {
    const card = document.createElement("a");
    card.className = "home-card";
    card.href = `?device=${encodeURIComponent(p.device_slug)}`;
    const complete = p.has_registry && p.has_knowledge_graph && p.has_rules && p.has_dictionary;
    card.innerHTML = `
      <div class="slug">${p.device_slug}</div>
      <div class="name">${p.device_slug.replace(/-/g, " ").replace(/^./, c => c.toUpperCase())}</div>
      <div class="badges">
        <span class="badge ${complete ? 'ok' : 'warn'}">${complete ? 'pack complet' : 'incomplet'}</span>
        ${p.has_audit_verdict ? '<span class="badge">audité</span>' : ''}
      </div>
    `;
    grid.appendChild(card);
  }
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
    const res = await fetch("/repairs", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({device_label, symptom}),
    });
    if (!res.ok) {
      if (res.status === 404) {
        setNewRepairError(
          "L'endpoint /repairs n'est pas encore branché côté backend. Réessaie dès que l'intégration est en place.",
          {title:"Backend indisponible — "}
        );
      } else {
        let detail = "";
        try { detail = (await res.json()).detail || ""; } catch (_) {}
        setNewRepairError(`Le backend a répondu ${res.status}. ${detail}`.trim(), {title:"Erreur — "});
      }
      setNewRepairBusy(false);
      return;
    }
    const r = await res.json();
    const slug = encodeURIComponent(r.device_slug || "");
    const id   = encodeURIComponent(r.id || "");
    window.location.href = `?device=${slug}&repair=${id}#graphe`;
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
