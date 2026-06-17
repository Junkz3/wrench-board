// Profile config — the guided first-run wizard AND the always-available config
// modal, sharing one set of form sections.
//
// Three sections (user feedback: the first config must guide the technician
// through everything, including options they can't guess like the agent's
// "teaching" verbosity):
//   1. Identity   — name, avatar, experience level (each level explained),
//                   specialties
//   2. Workshop   — the 12 catalog tools, grouped
//   3. Posture    — verbosity (auto / concise / normal / teaching, each
//                   explained) + UI language
//
// openProfileWizard(): paginated, one section per step (Back / Next / Skip,
// progress) — used by the onboarding sequence.
// openProfileModal(): the same sections in one scrollable modal — opened from
// the cockpit's avatar pill for quick edits, never navigates away. A "full
// profile" link still reaches the rich #profile page.
//
// Persists via PUT /profile/identity|tools|preferences and broadcasts
// `wb:profile-updated` so the avatar pill re-paints.

import i18n, { t } from "../../../i18n.js";
import { apiGet, apiSend } from "../../../shared/api.js";
import { escapeHtml } from "../../../shared/dom.js";

// Mirror of api/profile/catalog.py TOOLS_CATALOG (id + group), kept in display
// order. Labels come from i18n (onboarding.profile.tool_*).
const TOOLS = [
  { id: "soldering_iron", group: "soldering" },
  { id: "hot_air", group: "rework" },
  { id: "bga_rework", group: "rework" },
  { id: "preheater", group: "rework" },
  { id: "microscope", group: "inspection" },
  { id: "thermal_camera", group: "inspection" },
  { id: "uv_lamp", group: "inspection" },
  { id: "multimeter", group: "measurement" },
  { id: "oscilloscope", group: "measurement" },
  { id: "bench_psu", group: "power" },
  { id: "reballing_kit", group: "supplies" },
  { id: "stencil_printer", group: "supplies" },
];
const GROUP_ORDER = ["soldering", "rework", "inspection", "measurement", "power", "supplies"];

// Home-made Lucide-style line icons (24×24, currentColor stroke) — one per tool,
// plus a generic wrench (_custom) for tech-declared custom tools.
const _svg = (inner) =>
  `<svg class="ob-tool-svg" viewBox="0 0 24 24" width="20" height="20" fill="none" ` +
  `stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
const TOOL_ICONS = {
  soldering_iron: _svg('<path d="M3 21l3.2-3.2"/><path d="M6.2 17.8 15 9"/><path d="M15 9l1.7-1.7a2.1 2.1 0 0 1 3 3L18 12z"/><circle cx="5.5" cy="20.5" r=".7"/>'),
  hot_air: _svg('<path d="M3 7h9a3 3 0 1 0-3-3"/><path d="M3 12h13a3 3 0 1 1-3 3"/><path d="M3 17h7a3 3 0 1 0-3 3"/>'),
  bga_rework: _svg('<rect x="6" y="6" width="12" height="12" rx="1"/><rect x="10" y="10" width="4" height="4"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>'),
  preheater: _svg('<rect x="3" y="14" width="18" height="5" rx="1"/><path d="M7 11c0-1.6-1.4-2-1.4-3.6S7 4 7 4M12 11c0-1.6-1.4-2-1.4-3.6S12 4 12 4M17 11c0-1.6-1.4-2-1.4-3.6S17 4 17 4"/>'),
  microscope: _svg('<path d="M6 18h8"/><path d="M3 22h18"/><path d="M14 22a7 7 0 1 0 0-14h-1"/><path d="M9 14h2"/><path d="M9 12a2 2 0 0 1-2-2V6h6v4a2 2 0 0 1-2 2Z"/><path d="M12 6V3a1 1 0 0 0-1-1H9a1 1 0 0 0-1 1v3"/>'),
  thermal_camera: _svg('<rect x="2" y="6" width="13" height="12" rx="2"/><path d="M15 10l6-3v10l-6-3"/><circle cx="7" cy="14.5" r="1.4"/><path d="M7 13V9a1.4 1.4 0 1 1 0 0"/>'),
  uv_lamp: _svg('<path d="M9 18h6"/><path d="M10 22h4"/><path d="M8 13a6 6 0 1 1 8 0c-.7.6-1 1.2-1 2H9c0-.8-.3-1.4-1-2Z"/>'),
  multimeter: _svg('<path d="M3 18a9 9 0 1 1 18 0"/><path d="M12 14a1.8 1.8 0 1 0 0-3.6 1.8 1.8 0 0 0 0 3.6Z"/><path d="M13.3 11.2 18 7"/>'),
  oscilloscope: _svg('<rect x="2" y="4" width="20" height="16" rx="2"/><path d="M5 13h3l2-5 2.8 8 1.8-5 1.4 2H19"/>'),
  bench_psu: _svg('<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M7 10v4M5 12h4"/><path d="M15 12h4"/>'),
  reballing_kit: _svg('<rect x="4" y="4" width="16" height="16" rx="2"/><circle cx="8.5" cy="8.5" r=".9"/><circle cx="12" cy="8.5" r=".9"/><circle cx="15.5" cy="8.5" r=".9"/><circle cx="8.5" cy="12" r=".9"/><circle cx="12" cy="12" r=".9"/><circle cx="15.5" cy="12" r=".9"/><circle cx="8.5" cy="15.5" r=".9"/><circle cx="12" cy="15.5" r=".9"/><circle cx="15.5" cy="15.5" r=".9"/>'),
  stencil_printer: _svg('<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M8 8h.01M12 8h.01M16 8h.01M8 12h.01M12 12h.01M16 12h.01M8 16h.01M12 16h.01M16 16h.01"/>'),
  _custom: _svg('<path d="M14.6 6.4a4 4 0 0 0-5.4 5.2L3 18l3 3 6.4-6.2a4 4 0 0 0 5.2-5.4l-2.6 2.6-2-2 2.6-2.6Z"/>'),
};
const VERBOSITY = ["auto", "concise", "normal", "teaching"];
const LEVELS = ["", "beginner", "intermediate", "confirmed", "expert"]; // "" = auto-derived

// ── Section builders (shared by wizard + modal) ───────────────────────────

// The four-language picker (live-switching buttons), shared by the welcome-era
// markup, the identity step (wizard) and the posture step (quick modal).
function langPickerHTML() {
  const lang = i18n.locale;
  return `
        <div class="ob-lang-opts" role="group" aria-label="${escapeHtml(t("onboarding.menu.language"))}">
          <button type="button" class="landing-lang-opt${lang === "en" ? " is-active" : ""}" data-lang="en">${t("onboarding.menu.lang_en")}</button>
          <button type="button" class="landing-lang-opt${lang === "fr" ? " is-active" : ""}" data-lang="fr">${t("onboarding.menu.lang_fr")}</button>
          <button type="button" class="landing-lang-opt${lang === "zh" ? " is-active" : ""}" data-lang="zh">${t("onboarding.menu.lang_zh")}</button>
          <button type="button" class="landing-lang-opt${lang === "hi" ? " is-active" : ""}" data-lang="hi">${t("onboarding.menu.lang_hi")}</button>
        </div>`;
}

// showLanguage: the wizard puts the picker FIRST in the identity step (language
// is chosen before anything else); the quick modal keeps it in the posture step.
function identityStepHTML(env, showLanguage = false) {
  const id = env?.profile?.identity || {};
  const level = id.level_override || "";
  const langField = showLanguage ? `
      <label class="ob-field">
        <span class="ob-field-label">${t("onboarding.menu.language")}</span>
        ${langPickerHTML()}
      </label>` : "";
  const levelRow = (v) => {
    const labelKey = v === "" ? "onboarding.profile.level_auto" : `onboarding.profile.level_${v}`;
    const descKey = v === "" ? "onboarding.level.desc_auto" : `profile.ribbon.blurbs.${v}`;
    return `
      <label class="ob-radio">
        <input type="radio" name="level" value="${v}" ${level === v ? "checked" : ""}/>
        <span class="ob-radio-main">${t(labelKey)}</span>
        <span class="ob-radio-desc">${t(descKey)}</span>
      </label>`;
  };
  return `
    <section class="ob-step" data-step="0">
      <h4 class="ob-step-title">${t("onboarding.wizard.step_identity")}</h4>
      <p class="ob-step-hint">${t("onboarding.wizard.identity_intro")}</p>
      ${langField}
      <div class="ob-field-row">
        <label class="ob-field" style="flex:1">
          <span class="ob-field-label">${t("onboarding.profile.field_name")}</span>
          <input class="ob-input" name="name" type="text" maxlength="40"
                 value="${escapeHtml(id.name || "")}"
                 placeholder="${escapeHtml(t("onboarding.profile.name_placeholder"))}" autocomplete="off"/>
        </label>
        <label class="ob-field ob-field-avatar">
          <span class="ob-field-label">${t("onboarding.profile.field_avatar")}</span>
          <input class="ob-input" name="avatar" type="text" maxlength="2"
                 value="${escapeHtml(id.avatar || "")}"
                 placeholder="${escapeHtml(t("onboarding.profile.avatar_placeholder"))}"/>
        </label>
      </div>
      <span class="ob-field-error" id="obNameError" hidden>${t("onboarding.profile.name_required")}</span>
      <fieldset class="ob-radios">
        <legend class="ob-field-label">${t("onboarding.profile.field_level")}</legend>
        ${LEVELS.map(levelRow).join("")}
      </fieldset>
      <label class="ob-field">
        <span class="ob-field-label">${t("onboarding.profile.field_specialties")}</span>
        <input class="ob-input" name="specialties" type="text"
               value="${escapeHtml((id.specialties || []).join(", "))}"
               placeholder="${escapeHtml(t("onboarding.profile.specialties_placeholder"))}"/>
      </label>
    </section>`;
}

function customChipHTML(name) {
  return `
    <span class="ob-custom-chip" data-name="${escapeHtml(name)}">
      ${TOOL_ICONS._custom}
      <span class="ob-custom-chip-name">${escapeHtml(name)}</span>
      <button type="button" class="ob-custom-rm" aria-label="${escapeHtml(t("onboarding.workshop.custom_remove"))}">
        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </span>`;
}

function workshopStepHTML(env) {
  const tools = env?.profile?.tools || {};
  const groups = GROUP_ORDER.map((g) => {
    const rows = TOOLS.filter((tl) => tl.group === g).map((tl) => `
      <label class="ob-tool">
        <input type="checkbox" name="tool_${tl.id}" ${tools[tl.id] ? "checked" : ""}/>
        <span class="ob-tool-ic">${TOOL_ICONS[tl.id] || ""}</span>
        <span class="ob-tool-name">${t(`onboarding.profile.tool_${tl.id}`)}</span>
        <span class="ob-tool-check" aria-hidden="true"><svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg></span>
      </label>`).join("");
    return `
      <div class="ob-tool-group">
        <span class="ob-group-label">${t(`onboarding.workshop.group_${g}`)}</span>
        <div class="ob-tools-grid">${rows}</div>
      </div>`;
  }).join("");
  const customChips = (env?.profile?.custom_tools || []).map(customChipHTML).join("");
  const customSection = `
      <div class="ob-tool-group ob-custom-group">
        <span class="ob-group-label">${t("onboarding.workshop.group_custom")}</span>
        <div class="ob-custom-chips" id="obCustomChips">${customChips}</div>
        <div class="ob-custom-add">
          <input type="text" class="ob-custom-input" id="obCustomInput" maxlength="40" autocomplete="off"
                 placeholder="${escapeHtml(t("onboarding.workshop.custom_placeholder"))}"/>
          <button type="button" class="ob-btn ob-btn-ghost ob-custom-addbtn" id="obCustomAdd">${t("onboarding.workshop.custom_add")}</button>
        </div>
      </div>`;
  return `
    <section class="ob-step" data-step="1">
      <h4 class="ob-step-title">${t("onboarding.wizard.step_workshop")}</h4>
      <p class="ob-step-hint">${t("onboarding.wizard.workshop_intro")}</p>
      ${groups}
      ${customSection}
    </section>`;
}

// Wire the custom-tools add/remove UI. Idempotent per root; both the wizard and
// the quick modal call it after injecting their form.
function wireCustomTools(root) {
  const chips = root.querySelector("#obCustomChips");
  const input = root.querySelector("#obCustomInput");
  const addBtn = root.querySelector("#obCustomAdd");
  if (!chips || !input || !addBtn) return;
  const add = () => {
    const name = input.value.replace(/\s+/g, " ").trim().slice(0, 40);
    if (!name) return;
    const dup = [...chips.querySelectorAll(".ob-custom-chip")]
      .some((c) => (c.dataset.name || "").toLowerCase() === name.toLowerCase());
    if (!dup) chips.insertAdjacentHTML("beforeend", customChipHTML(name));
    input.value = "";
    input.focus();
  };
  addBtn.addEventListener("click", add);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
  chips.addEventListener("click", (e) => {
    const rm = e.target.closest(".ob-custom-rm");
    if (rm) rm.closest(".ob-custom-chip")?.remove();
  });
}

// `showLanguage`: the wizard hides the picker here (language is chosen first, in
// the identity step) and only carries the chosen locale via a hidden input; the
// standalone config modal shows a live picker for later edits.
function postureStepHTML(env, showLanguage = true) {
  const prefs = env?.profile?.preferences || {};
  const verbosity = prefs.verbosity || "auto";
  const lang = i18n.locale;
  const modeRow = (v) => `
    <label class="ob-radio">
      <input type="radio" name="verbosity" value="${v}" ${verbosity === v ? "checked" : ""}/>
      <span class="ob-radio-main">${t(`onboarding.posture.mode_${v}`)}</span>
      <span class="ob-radio-desc">${t(`onboarding.posture.desc_${v}`)}</span>
    </label>`;
  const langBlock = showLanguage ? `
      <div class="ob-field">
        <span class="ob-field-label">${t("onboarding.menu.language")}</span>
        <div class="ob-lang-opts">
          <button type="button" class="landing-lang-opt${lang === "en" ? " is-active" : ""}" data-lang="en">${t("onboarding.menu.lang_en")}</button>
          <button type="button" class="landing-lang-opt${lang === "fr" ? " is-active" : ""}" data-lang="fr">${t("onboarding.menu.lang_fr")}</button>
          <button type="button" class="landing-lang-opt${lang === "zh" ? " is-active" : ""}" data-lang="zh">${t("onboarding.menu.lang_zh")}</button>
          <button type="button" class="landing-lang-opt${lang === "hi" ? " is-active" : ""}" data-lang="hi">${t("onboarding.menu.lang_hi")}</button>
        </div>
      </div>` : "";
  return `
    <section class="ob-step" data-step="2">
      <h4 class="ob-step-title">${t("onboarding.wizard.step_posture")}</h4>
      <p class="ob-step-hint">${t("onboarding.wizard.posture_intro")}</p>
      <fieldset class="ob-radios">
        <legend class="ob-field-label">${t("onboarding.posture.verbosity_label")}</legend>
        ${VERBOSITY.map(modeRow).join("")}
      </fieldset>
      <input type="hidden" name="language" value="${escapeHtml(lang)}"/>
      ${langBlock}
    </section>`;
}

// lang placement — "identity": the wizard shows the picker first (language is
// chosen before everything); "posture": the quick modal keeps it in posture.
function sectionsHTML(env, { lang = "posture" } = {}) {
  return identityStepHTML(env, lang === "identity") + workshopStepHTML(env) + postureStepHTML(env, lang === "posture");
}

// ── Read + persist ────────────────────────────────────────────────────────
export function readProfileForm(form, env) {
  const identity = {
    name: form.name.value.trim(),
    avatar: form.avatar.value.trim(),
    years_experience: env?.profile?.identity?.years_experience ?? 0,
    specialties: form.specialties.value.split(",").map((s) => s.trim()).filter(Boolean),
    level_override: form.level.value || null,
  };
  const tools = { ...(env?.profile?.tools || {}) };
  TOOLS.forEach((tl) => { tools[tl.id] = !!form[`tool_${tl.id}`]?.checked; });
  const customTools = [...form.querySelectorAll(".ob-custom-chip")]
    .map((c) => c.dataset.name).filter(Boolean);
  const prefs = {
    verbosity: form.verbosity.value || "auto",
    language: form.language.value || i18n.locale,
  };
  return { identity, tools, prefs, customTools };
}

export async function saveProfileForm(form, env) {
  const { identity, tools, prefs, customTools } = readProfileForm(form, env);
  const newEnv = await apiSend("/profile/identity", {
    method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(identity),
  });
  await apiSend("/profile/tools", {
    method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(tools),
  });
  await apiSend("/profile/custom-tools", {
    method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ custom_tools: customTools }),
  });
  await apiSend("/profile/preferences", {
    method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(prefs),
  });
  if (prefs.language && prefs.language !== i18n.locale) await i18n.setLocale(prefs.language);
  document.dispatchEvent(new CustomEvent("wb:profile-updated", { detail: { env: newEnv } }));
  return newEnv;
}

// ── First-run wizard (paginated) ──────────────────────────────────────────
// mandatory: first-connection gate — the Skip button is removed and the wizard
// cannot be left until a name is entered (the identity step blocks advancing).
// Language lives FIRST in the identity step (live-switching re-opens the wizard
// in the new locale, preserving in-progress edits).
export function openProfileWizard(env, { onComplete, onSkip, mandatory = false } = {}) {
  if (document.getElementById("obProfileConfig")) return;
  const host = document.createElement("div");
  host.className = "ob-host";
  host.id = "obProfileConfig";
  host.innerHTML = `
    <div class="ob-backdrop">
      <div class="ob-panel ob-config" role="dialog" aria-modal="true" aria-labelledby="obWizTitle">
        <header class="ob-config-head ob-config-head--wizard">
          <span class="ob-wiz-title" id="obWizTitle">${t("onboarding.wizard.config_title")}</span>
          <span class="ob-progress" id="obWizProgress"></span>
        </header>
        <form class="ob-form ob-config-body" id="obProfileForm">${sectionsHTML(env, { lang: "identity" })}</form>
        <footer class="ob-config-foot">
          ${mandatory ? "" : `<button type="button" class="mascot-bubble-skip" id="obWizSkip">${t("onboarding.skip")}</button>`}
          <div class="ob-foot-right">
            <button type="button" class="ob-btn ob-btn-ghost" id="obWizBack">${t("onboarding.back")}</button>
            <button type="button" class="ob-btn ob-btn-primary" id="obWizNext">${t("onboarding.next")}</button>
          </div>
        </footer>
      </div>
    </div>`;
  document.body.appendChild(host);
  wireCustomTools(host);

  const form = host.querySelector("#obProfileForm");
  const steps = [...host.querySelectorAll(".ob-step")];
  const total = steps.length;
  const back = host.querySelector("#obWizBack");
  const next = host.querySelector("#obWizNext");
  const progress = host.querySelector("#obWizProgress");
  const nameInput = form.querySelector('input[name="name"]');
  const nameError = host.querySelector("#obNameError");
  const close = () => host.remove();
  let step = 0;

  // Live language switch (identity step). Snapshot the in-progress form, persist
  // the locale, then re-open the wizard rendered in the new language — same as
  // the quick modal, so the tech never loses what they typed.
  host.querySelectorAll('.ob-step[data-step="0"] .ob-lang-opts .landing-lang-opt').forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (btn.dataset.lang === i18n.locale) return;
      const snap = readProfileForm(form, env);
      const merged = {
        ...env,
        profile: {
          ...(env?.profile || {}),
          identity: snap.identity, tools: snap.tools, custom_tools: snap.customTools,
          preferences: { ...(env?.profile?.preferences || {}), ...snap.prefs, language: btn.dataset.lang },
        },
      };
      await i18n.setLocale(btn.dataset.lang);
      try {
        await apiSend("/profile/preferences", {
          method: "PUT", headers: { "content-type": "application/json" },
          body: JSON.stringify({ verbosity: snap.prefs.verbosity || "auto", language: btn.dataset.lang }),
        });
      } catch (err) { console.warn("[profile_wizard] persist language failed", err); }
      host.remove();
      openProfileWizard(merged, { onComplete, onSkip, mandatory });
    });
  });

  const render = () => {
    steps.forEach((s, i) => { s.hidden = i !== step; });
    back.style.visibility = step === 0 ? "hidden" : "visible";
    next.textContent = step === total - 1 ? t("onboarding.done") : t("onboarding.next");
    progress.textContent = t("onboarding.wizard.progress", { n: step + 1, total });
    const focusable = steps[step].querySelector("input, select");
    setTimeout(() => focusable?.focus(), 40);
  };

  // Mandatory gate: a name is required before leaving the identity step (and
  // therefore before finishing). Surfaces an inline error rather than a block.
  const nameMissing = () => mandatory && step === 0 && !nameInput.value.trim();
  const flagNameMissing = () => {
    if (nameError) nameError.hidden = false;
    nameInput.classList.add("is-invalid");
    nameInput.focus();
  };
  nameInput?.addEventListener("input", () => {
    if (nameError) nameError.hidden = true;
    nameInput.classList.remove("is-invalid");
  });

  back.addEventListener("click", () => { if (step > 0) { step--; render(); } });
  next.addEventListener("click", async () => {
    if (nameMissing()) { flagNameMissing(); return; }
    if (step < total - 1) { step++; render(); return; }
    next.disabled = true;
    try {
      await saveProfileForm(form, env);
    } catch (err) {
      console.error("[profile_wizard] save failed", err);
      alert(t("onboarding.profile.save_failed", { error: err.message || err }));
      next.disabled = false;
      return;
    }
    close();
    onComplete?.();
  });
  host.querySelector("#obWizSkip")?.addEventListener("click", () => { close(); onSkip?.(); });
  render();
}

// ── Quick config modal (single scroll) ────────────────────────────────────
function _renderModal(env) {
  const host = document.createElement("div");
  host.className = "ob-host";
  host.id = "obProfileConfig";
  host.innerHTML = `
    <div class="ob-backdrop">
      <div class="ob-panel ob-config" role="dialog" aria-modal="true" aria-labelledby="obCfgTitle">
        <button type="button" class="ob-modal-close" id="obCfgClose" aria-label="${escapeHtml(t("onboarding.modal.cancel"))}">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
        </button>
        <header class="ob-config-head">
          <h3 class="ob-panel-title" id="obCfgTitle">${t("onboarding.modal.title")}</h3>
          <p class="ob-panel-intro--plain">${t("onboarding.modal.subtitle")}</p>
        </header>
        <form class="ob-form ob-config-body ob-config-body--all" id="obProfileForm">${sectionsHTML(env, { lang: "posture" })}</form>
        <footer class="ob-config-foot">
          <a class="ob-modal-fulllink" href="#profile" id="obCfgFull">${t("onboarding.modal.full_profile")}</a>
          <div class="ob-foot-right">
            <button type="button" class="ob-btn ob-btn-ghost" id="obCfgCancel">${t("onboarding.modal.cancel")}</button>
            <button type="button" class="ob-btn ob-btn-primary" id="obCfgSave">${t("onboarding.modal.save")}</button>
          </div>
        </footer>
      </div>
    </div>`;
  document.body.appendChild(host);
  wireCustomTools(host);

  // Single-scroll: every section visible (the wizard hides non-active ones).
  host.querySelectorAll(".ob-step").forEach((s) => { s.hidden = false; });

  const form = host.querySelector("#obProfileForm");
  const close = () => host.remove();

  // Live language switch: snapshot edits → switch locale → persist → re-render
  // the modal in the new locale (keeps the user's in-progress changes).
  host.querySelectorAll(".ob-lang-opts .landing-lang-opt").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (btn.dataset.lang === i18n.locale) return;
      const { identity, tools, prefs } = readProfileForm(form, env);
      const merged = {
        ...env,
        profile: {
          ...(env?.profile || {}),
          identity, tools,
          preferences: { ...(env?.profile?.preferences || {}), ...prefs, language: btn.dataset.lang },
        },
      };
      await i18n.setLocale(btn.dataset.lang);
      try {
        await apiSend("/profile/preferences", {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ verbosity: prefs.verbosity || "auto", language: btn.dataset.lang }),
        });
      } catch (err) {
        console.warn("[profile_modal] persist language failed", err);
      }
      host.remove();
      _renderModal(merged);
    });
  });
  host.querySelector("#obCfgClose").addEventListener("click", close);
  host.querySelector("#obCfgCancel").addEventListener("click", close);
  host.querySelector("#obCfgFull").addEventListener("click", close); // href navigates to #profile
  host.querySelector(".ob-backdrop").addEventListener("click", (e) => {
    if (e.target.classList.contains("ob-backdrop")) close();
  });
  const saveBtn = host.querySelector("#obCfgSave");
  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    try {
      await saveProfileForm(form, env);
    } catch (err) {
      console.error("[profile_modal] save failed", err);
      alert(t("onboarding.profile.save_failed", { error: err.message || err }));
      saveBtn.disabled = false;
      return;
    }
    close();
  });
  setTimeout(() => host.querySelector('input[name="name"]')?.focus(), 60);
}

export async function openProfileModal() {
  if (document.getElementById("obProfileConfig")) return;
  let env = null;
  try {
    env = await apiGet("/profile");
  } catch (err) {
    console.warn("[profile_modal] load profile failed", err);
  }
  _renderModal(env);
}
