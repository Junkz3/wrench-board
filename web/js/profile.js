// Technician profile section.
// On first activation, fetches web/profil.html (the section's DOM partial)
// and injects it into #profileSection. Subsequent activations skip the fetch.
// Consumes GET /profile and renders identity / tools / skills / preferences.
// Tool toggles → PUT /profile/tools ; preference changes → PUT /profile/preferences
// ; skill click opens the evidence drawer. Identity modal handler lands in Task 12.

import { escapeHtml as escHtml } from "./shared/dom.js";

let _state = null;    // {profile, derived, catalog}
let _partialLoaded = false;

const STATUS_KEYS = ["mastered", "practiced", "learning", "unlearned"];
const VERBOSITIES = ["auto", "concise", "normal", "teaching"];
const LANGUAGES = ["en", "fr", "zh", "hi"];
// Native display label per locale (the stored value stays the ISO code).
const LANGUAGE_LABELS = { en: "English", fr: "Français", zh: "中文", hi: "हिन्दी" };
// BCP-47 tag per locale for Intl date/number formatting.
const DATE_LOCALES = { en: "en-US", fr: "fr-FR", zh: "zh-CN", hi: "hi-IN" };

async function ensurePartial() {
  if (_partialLoaded) return;
  const mount = document.getElementById("profileSection");
  const url = mount.dataset.partial || "/profil.html";
  const res = await fetch(url);
  if (!res.ok) throw new Error(`partial ${url} → ${res.status}`);
  mount.innerHTML = await res.text();
  if (window.i18n?.ready) await window.i18n.ready;
  window.i18n?.applyDom(mount);
  _partialLoaded = true;
}

async function fetchJSON(url, init) {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`${init?.method || "GET"} ${url} → ${res.status}`);
  return res.json();
}

function currentLocale() {
  return (window.i18n && window.i18n.locale) || "en";
}

function fmtYears(n) {
  if (!n) return window.t("profile.head.years_zero");
  const key = n > 1 ? "profile.head.years_other" : "profile.head.years_one";
  return window.t(key, { n });
}

function fmtUpdated(iso) {
  if (!iso) return "…";
  const d = new Date(iso);
  if (isNaN(d)) return "…";
  const locale = DATE_LOCALES[currentLocale()] || "en-US";
  const date = d.toLocaleDateString(locale, { day: "numeric", month: "short" });
  return window.t("profile.head.updated", { date });
}

const LEVEL_ORDER = ["beginner", "intermediate", "confirmed", "expert"];

function renderHead() {
  const id = _state.profile.identity;
  const level = _state.derived.level;
  document.getElementById("profAvatar").textContent =
    id.avatar || (id.name?.slice(0, 2)?.toUpperCase() || "?");
  document.getElementById("profName").textContent = id.name || window.t("profile.head.no_name");
  const levelEl = document.getElementById("profLevel");
  levelEl.textContent = level.toUpperCase();
  levelEl.dataset.level = level;
  document.querySelector(".prof-head")?.setAttribute("data-level", level);
  document.getElementById("profYears").textContent = fmtYears(id.years_experience);
  document.getElementById("profSpecs").textContent = id.specialties.length
    ? id.specialties.join(" · ")
    : window.t("profile.head.no_specialty");
  document.getElementById("profUpdated").textContent = fmtUpdated(_state.profile.updated_at);
}

// Ribbon = the four-rung XP track. The active rung gets data-state="active",
// every prior rung gets data-state="done", every later rung stays empty.
function renderRibbon() {
  const ribbon = document.getElementById("profRibbon");
  const level = _state.derived.level;
  ribbon.dataset.level = level;
  document.getElementById("profRibbonTitle").textContent = window.t("profile.ribbon.title", { level });
  const idx = LEVEL_ORDER.indexOf(level);
  const total = LEVEL_ORDER.length;
  document.getElementById("profRibbonScore").textContent = `${idx + 1} / ${total}`;
  const blurbKey = `profile.ribbon.blurbs.${level}`;
  const blurb = window.t(blurbKey);
  document.getElementById("profRibbonBody").textContent =
    blurb !== blurbKey ? blurb : window.t("profile.ribbon.default_blurb");
  ribbon.querySelectorAll(".prof-rung").forEach(rung => {
    const r = rung.dataset.rung;
    const ri = LEVEL_ORDER.indexOf(r);
    rung.dataset.state = ri < idx ? "done" : ri === idx ? "active" : "empty";
  });
}

// Stat cards — counts + visual progress vs total. Pure derivations from
// _state.derived.skills_by_status + _state.profile.tools, no extra fetch.
function renderStats() {
  const buckets = _state.derived.skills_by_status;
  const totalSkills = (buckets.mastered.length + buckets.practiced.length
    + buckets.learning.length + buckets.unlearned.length) || 1;
  const setStat = (prefix, count, total, sub) => {
    const valEl = document.getElementById(`profStat${prefix}`);
    const subEl = document.getElementById(`profStat${prefix}Sub`);
    const barEl = document.getElementById(`profStat${prefix}Bar`);
    if (valEl) valEl.textContent = String(count);
    if (subEl) subEl.textContent = sub;
    if (barEl) barEl.style.width = `${Math.round((count / total) * 100)}%`;
  };
  const skillsSub = window.t("profile.stats.sub_skills", { total: totalSkills });
  setStat("Mastered",  buckets.mastered.length,  totalSkills, skillsSub);
  setStat("Practiced", buckets.practiced.length, totalSkills, skillsSub);
  setStat("Learning",  buckets.learning.length,  totalSkills, skillsSub);
  // Tools: count of "true" entries vs catalog size.
  const toolsOn = Object.values(_state.profile.tools).filter(Boolean).length;
  const toolsTotal = _state.catalog.tools.length || 1;
  setStat("Tools", toolsOn, toolsTotal, window.t("profile.stats.sub_tools", { total: toolsTotal }));

  // Block-level counts (next to the section h2s).
  const totalEl = document.getElementById("profSkillsTotal");
  if (totalEl) totalEl.textContent = window.t("profile.stats.block_skills_total", { n: totalSkills });
  const toolsTotalEl = document.getElementById("profToolsTotal");
  if (toolsTotalEl) toolsTotalEl.textContent = window.t("profile.stats.block_tools_total", { on: toolsOn, total: toolsTotal });
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
  renderStats();
}

function renderSkills() {
  const host = document.getElementById("profSkills");
  host.innerHTML = "";
  const buckets = _state.derived.skills_by_status;
  const bySkillId = new Map(_state.catalog.skills.map(s => [s.id, s]));

  for (const status of STATUS_KEYS) {
    const col = document.createElement("div");
    col.className = "profile-skill-col";
    col.dataset.status = status;
    const ids = buckets[status] || [];
    col.innerHTML = `<h3>${escHtml(window.t(`profile.status.${status}`))} <span class="profile-skill-col-count">${ids.length}</span></h3>`;

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

  const makeGroup = (label, key, options, labels) => {
    const g = document.createElement("div");
    g.className = "profile-prefs-group";
    g.innerHTML = `<label>${label}</label><div class="opts"></div>`;
    const opts = g.querySelector(".opts");
    for (const v of options) {
      const btn = document.createElement("button");
      btn.className = "profile-prefs-opt" + (prefs[key] === v ? " on" : "");
      btn.textContent = labels ? (labels[v] || v) : v;
      btn.addEventListener("click", () => changePref(key, v));
      opts.appendChild(btn);
    }
    return g;
  };

  host.appendChild(makeGroup(window.t("profile.prefs.verbosity"), "verbosity", VERBOSITIES));
  host.appendChild(makeGroup(window.t("profile.prefs.language"), "language", LANGUAGES, LANGUAGE_LABELS));
}

async function changePref(key, value) {
  const next = { ..._state.profile.preferences, [key]: value };
  const fresh = await fetchJSON("/profile/preferences", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(next),
  });
  _state = fresh;
  if (key === "language" && window.i18n && value !== window.i18n.locale) {
    await window.i18n.setLocale(value);
  }
  renderPrefs();
  renderHead();
  renderRibbon();
}

function openDrawer(sid, entry, rec) {
  const drawer = document.getElementById("profDrawer");
  drawer.classList.remove("hidden");
  document.getElementById("profDrawerTitle").textContent = entry.label;
  const body = document.getElementById("profDrawerBody");
  body.innerHTML = "";
  const evidences = rec?.evidences || [];
  if (!evidences.length) {
    body.innerHTML = `<p style="color:var(--text-3);font-size:12px">${escHtml(window.t("profile.drawer.no_history"))}</p>`;
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
    renderRibbon();
    renderStats();
    closeIdentityModal();
  } catch (err) {
    console.error("submitIdentity:", err);
    alert(window.t("profile.modal.save_failed", { error: err.message }));
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

let _localeHookWired = false;

function rerenderAll() {
  if (!_state) return;
  renderHead();
  renderRibbon();
  renderStats();
  renderTools();
  renderSkills();
  renderPrefs();
}

export async function initProfileSection() {
  try {
    await ensurePartial();
    _state = await fetchJSON("/profile");
  } catch (err) {
    console.error("initProfileSection:", err);
    return;
  }
  rerenderAll();
  wireDrawerClose();
  wireIdentityModal();
  if (!_localeHookWired) {
    window.i18n?.onChange(() => rerenderAll());
    _localeHookWired = true;
  }
}
