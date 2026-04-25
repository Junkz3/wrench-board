// Technician profile section.
// On first activation, fetches web/profil.html (the section's DOM partial)
// and injects it into #profileSection. Subsequent activations skip the fetch.
// Consumes GET /profile and renders identity / tools / skills / preferences.
// Tool toggles → PUT /profile/tools ; preference changes → PUT /profile/preferences
// ; skill click opens the evidence drawer. Identity modal handler lands in Task 12.

let _state = null;    // {profile, derived, catalog}
let _partialLoaded = false;

function escHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

const STATUS_LABELS = {
  mastered:  "Maîtrisées",
  practiced: "Pratiquées",
  learning:  "En apprentissage",
  unlearned: "Non pratiquées",
};
const VERBOSITIES = ["auto", "concise", "normal", "teaching"];
const LANGUAGES = ["fr", "en"];

async function ensurePartial() {
  if (_partialLoaded) return;
  const mount = document.getElementById("profileSection");
  const url = mount.dataset.partial || "/profil.html";
  const res = await fetch(url);
  if (!res.ok) throw new Error(`partial ${url} → ${res.status}`);
  mount.innerHTML = await res.text();
  _partialLoaded = true;
}

async function fetchJSON(url, init) {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`${init?.method || "GET"} ${url} → ${res.status}`);
  return res.json();
}

function fmtYears(n) {
  if (!n) return "0 an";
  return `${n} an${n > 1 ? "s" : ""} d'XP`;
}

function renderHead() {
  const id = _state.profile.identity;
  const level = _state.derived.level;
  document.getElementById("profAvatar").textContent = id.avatar || (id.name.slice(0,2).toUpperCase() || "—");
  document.getElementById("profName").textContent = id.name || "Sans nom";
  document.getElementById("profLevel").textContent = level.toUpperCase();
  document.getElementById("profYears").textContent = fmtYears(id.years_experience);
  document.getElementById("profSpecs").textContent = id.specialties.length ? id.specialties.join(" · ") : "Sans spécialité";
}

function renderTools() {
  const host = document.getElementById("profTools");
  host.innerHTML = "";
  for (const tool of _state.catalog.tools) {
    const on = !!_state.profile.tools[tool.id];
    const chip = document.createElement("div");
    chip.className = "profile-tool" + (on ? " on" : "");
    chip.innerHTML = `<span class="dot"></span><span>${escHtml(tool.label)}</span>`;
    chip.addEventListener("click", () => toggleTool(tool.id));
    host.appendChild(chip);
  }
}

async function toggleTool(toolId) {
  const nextTools = { ..._state.profile.tools };
  nextTools[toolId] = !nextTools[toolId];
  const fresh = await fetchJSON("/profile/tools", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(nextTools),
  });
  _state = fresh;
  renderTools();
}

function renderSkills() {
  const host = document.getElementById("profSkills");
  host.innerHTML = "";
  const buckets = _state.derived.skills_by_status;
  const bySkillId = new Map(_state.catalog.skills.map(s => [s.id, s]));

  for (const status of ["mastered", "practiced", "learning", "unlearned"]) {
    const col = document.createElement("div");
    col.className = "profile-skill-col";
    col.dataset.status = status;
    const ids = buckets[status] || [];
    col.innerHTML = `<h3>${STATUS_LABELS[status]} <span class="profile-skill-col-count">${ids.length}</span></h3>`;

    // Unlearned skills are rendered as compact chips (no bar, no count) — the list
    // is long and the user mostly cares about what they HAVE practiced. Other
    // status columns render full cards with progress bar + usage count.
    if (status === "unlearned") {
      const chips = document.createElement("div");
      chips.className = "profile-skill-chips";
      for (const sid of ids) {
        const entry = bySkillId.get(sid);
        if (!entry) continue;
        const chip = document.createElement("span");
        chip.className = "profile-skill-chip";
        chip.textContent = entry.label;
        chip.addEventListener("click", () => openDrawer(sid, entry, null));
        chips.appendChild(chip);
      }
      col.appendChild(chips);
      host.appendChild(col);
      continue;
    }

    for (const sid of ids) {
      const entry = bySkillId.get(sid);
      if (!entry) continue;
      const rec = _state.profile.skills[sid];
      const usages = rec ? rec.usages : 0;
      const pct = Math.min(100, (usages / 12) * 100);
      const card = document.createElement("div");
      card.className = "profile-skill";
      card.innerHTML = `
        <span class="profile-skill-label">${escHtml(entry.label)}</span>
        <div class="profile-skill-meta">
          <div class="profile-skill-bar"><span style="width:${pct}%"></span></div>
          <span class="profile-skill-count">${usages}×</span>
        </div>`;
      card.addEventListener("click", () => openDrawer(sid, entry, rec));
      col.appendChild(card);
    }
    host.appendChild(col);
  }
}

function renderPrefs() {
  const host = document.getElementById("profPrefs");
  host.innerHTML = "";
  const prefs = _state.profile.preferences;

  const makeGroup = (label, key, options) => {
    const g = document.createElement("div");
    g.className = "profile-prefs-group";
    g.innerHTML = `<label>${label}</label><div class="opts"></div>`;
    const opts = g.querySelector(".opts");
    for (const v of options) {
      const btn = document.createElement("button");
      btn.className = "profile-prefs-opt" + (prefs[key] === v ? " on" : "");
      btn.textContent = v;
      btn.addEventListener("click", () => changePref(key, v));
      opts.appendChild(btn);
    }
    return g;
  };

  host.appendChild(makeGroup("Verbosité", "verbosity", VERBOSITIES));
  host.appendChild(makeGroup("Langue", "language", LANGUAGES));
}

async function changePref(key, value) {
  const next = { ..._state.profile.preferences, [key]: value };
  const fresh = await fetchJSON("/profile/preferences", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(next),
  });
  _state = fresh;
  renderPrefs();
  renderHead();
}

function openDrawer(sid, entry, rec) {
  const drawer = document.getElementById("profDrawer");
  drawer.classList.remove("hidden");
  document.getElementById("profDrawerTitle").textContent = entry.label;
  const body = document.getElementById("profDrawerBody");
  body.innerHTML = "";
  const evidences = rec?.evidences || [];
  if (!evidences.length) {
    body.innerHTML = `<p style="color:var(--text-3);font-size:12px">Aucun historique pour cette compétence — elle sera tracée dès que l'agent détectera une utilisation confirmée.</p>`;
    return;
  }
  for (const ev of [...evidences].reverse()) {
    const card = document.createElement("div");
    card.className = "profile-evidence";
    card.innerHTML = `
      <span class="dev">${escHtml(ev.device_slug)} · ${escHtml(ev.symptom)}</span>
      <span class="sum">${escHtml(ev.action_summary)}</span>
      <span class="date">${escHtml(ev.date)}</span>`;
    body.appendChild(card);
  }
}

function wireDrawerClose() {
  document.getElementById("profDrawerClose").addEventListener("click", () => {
    document.getElementById("profDrawer").classList.add("hidden");
  });
}

// ============ Identity edit modal ============
function openIdentityModal() {
  const form = document.getElementById("profIdentityForm");
  const id = _state.profile.identity;
  form.name.value = id.name || "";
  form.avatar.value = id.avatar || "";
  form.years_experience.value = id.years_experience ?? 0;
  form.specialties.value = (id.specialties || []).join(", ");
  form.level_override.value = id.level_override || "";
  document.getElementById("profIdentityBackdrop").classList.add("open");
}

function closeIdentityModal() {
  document.getElementById("profIdentityBackdrop").classList.remove("open");
}

async function submitIdentity(evt) {
  evt.preventDefault();
  const form = evt.target;
  const payload = {
    name: form.name.value.trim(),
    avatar: form.avatar.value.trim(),
    years_experience: parseInt(form.years_experience.value || "0", 10),
    specialties: form.specialties.value.split(",").map(s => s.trim()).filter(Boolean),
    level_override: form.level_override.value || null,
  };
  try {
    _state = await fetchJSON("/profile/identity", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    renderHead();
    closeIdentityModal();
  } catch (err) {
    console.error("submitIdentity:", err);
    alert("Impossible d'enregistrer : " + err.message);
  }
}

function wireIdentityModal() {
  document.getElementById("profEditIdentityBtn").addEventListener("click", openIdentityModal);
  const backdrop = document.getElementById("profIdentityBackdrop");
  // Backdrop catch — only close if the click actually landed on the backdrop
  // itself (otherwise inner-modal clicks bubble up and would dismiss).
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) closeIdentityModal();
  });
  // Explicit dismiss buttons (close ✕, Annuler) — close unconditionally.
  // The close icon wraps an <svg>/<path> so e.target isn't always the button
  // itself; we use currentTarget via the listener binding and skip the backdrop.
  backdrop.querySelectorAll("button[data-dismiss]").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      closeIdentityModal();
    });
  });
  document.getElementById("profIdentityForm").addEventListener("submit", submitIdentity);
}

export async function initProfileSection() {
  try {
    await ensurePartial();
    _state = await fetchJSON("/profile");
  } catch (err) {
    console.error("initProfileSection:", err);
    return;
  }
  renderHead();
  renderTools();
  renderSkills();
  renderPrefs();
  wireDrawerClose();
  wireIdentityModal();
}
