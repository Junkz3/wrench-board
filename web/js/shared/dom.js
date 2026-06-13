// web/js/shared/dom.js
// Shared DOM/string utilities. Single source of truth for the helpers that
// were duplicated across the feature modules (~10 escapeHtml variants,
// prettifySlug ×2, relative-time).

export function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[c]);
}

// COPIE VERBATIM du corps de router.js::prettifySlug (l'implémentation
// exportée, canonique).
export function prettifySlug(slug) {
  if (!slug) return "";
  return slug.replace(/-/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}

// COPIE VERBATIM du corps de home.js::relativeTimeFr (web/js/home.js).
// Renommé relativeTime (le comportement FR reste identique). Dépend des
// globaux window.t / window.i18n (câblés par i18n.js), comme dans home.js.
export function relativeTime(isoString) {
  if (!isoString) return "…";
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
