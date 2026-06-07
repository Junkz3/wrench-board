// Explanatory modal — a reusable glass dialog that explains what a feature does
// before the user commits to it. Two topics today: "knowledge" (the + Add
// knowledge action) and "stock" (the donor stock). Opened on a feature's first
// use (one-shot flag) and on demand via a persistent "?" affordance.
//
// Content is i18n (onboarding.info.<topic>.{title,intro,p1..p4} + onboarding.info.cta).
// Styling reuses the .ob-* tokens in web/styles/onboarding.css.

import { t } from "./i18n.js";

export function openInfoModal(topic, { onClose } = {}) {
  if (document.getElementById("obInfoModal")) return;
  const base = `onboarding.info.${topic}`;

  const points = ["p1", "p2", "p3", "p4"]
    .map((k) => ({ key: `${base}.${k}`, val: t(`${base}.${k}`) }))
    .filter(({ key, val }) => val && val !== key)
    .map(({ val }) => `<li>${val}</li>`)
    .join("");

  const host = document.createElement("div");
  host.className = "ob-host";
  host.id = "obInfoModal";
  host.innerHTML = `
    <div class="ob-backdrop">
      <div class="ob-panel ob-info" role="dialog" aria-modal="true" aria-labelledby="obInfoTitle">
        <button type="button" class="ob-modal-close" id="obInfoClose" aria-label="${t("onboarding.info.cta")}">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
        </button>
        <h3 class="ob-panel-title" id="obInfoTitle">${t(`${base}.title`)}</h3>
        <p class="ob-panel-intro--plain">${t(`${base}.intro`)}</p>
        <ul class="ob-info-list">${points}</ul>
        <div class="ob-actions">
          <button type="button" class="ob-btn ob-btn-primary" id="obInfoCta">${t("onboarding.info.cta")}</button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(host);

  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    host.remove();
    onClose?.();
  };
  host.querySelector("#obInfoClose").addEventListener("click", close);
  host.querySelector("#obInfoCta").addEventListener("click", close);
  host.querySelector(".ob-backdrop").addEventListener("click", (e) => {
    if (e.target.classList.contains("ob-backdrop")) close();
  });
}
