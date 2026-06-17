// i18n core — vanilla, no build step.
//
// Loads per-module JSON dictionaries from /i18n/_modules/{module}.{lang}.json
// and exposes a global `i18n` API. Default locale: English. French, Simplified
// Chinese and Hindi are offered as alternate locales. New locales = drop a
// `_modules/{module}.{lang}.json` alongside the existing ones, add the lang to
// SUPPORTED, and seed an empty bucket in `dicts`.
//
// Public API:
//   i18n.t(key, params?)       → translated string, params interpolate {name}
//   i18n.locale                → current 'en' | 'fr' | 'zh' | 'hi'
//   i18n.setLocale(lang)       → switch + persist + re-apply DOM
//   i18n.applyDom(root?)       → re-translate `[data-i18n]` / `[data-i18n-attr]`
//   i18n.ready                 → Promise resolved once first dictionary loaded
//   i18n.onReady(fn)           → run fn once dictionaries are loaded
//   i18n.onChange(fn)          → notify on locale switch (re-render hook)

const SUPPORTED = ['en', 'fr', 'zh', 'hi'];
const DEFAULT_LOCALE = 'en';
const STORAGE_KEY = 'wb.locale';

// Static module list — keep alphabetic. Each entry expects one file per
// supported locale: web/i18n/_modules/{name}.{en,fr,zh,hi}.json
const MODULES = [
  'brd',
  'camera',
  'chat',
  'common',
  'graph',
  'home',
  'intro',
  'landing',
  'mascot',
  'memory_bank',
  'onboarding',
  'pipeline',
  'profile',
  'protocol',
  'repair',
  'router',
  'schematic',
  'stock',
];

const dicts = { en: {}, fr: {}, zh: {}, hi: {} };
const changeListeners = new Set();
let currentLocale = pickInitialLocale();
let readyResolve;
const ready = new Promise((res) => { readyResolve = res; });

function pickInitialLocale() {
  const url = new URL(window.location.href);
  const fromUrl = url.searchParams.get('lang');
  if (fromUrl && SUPPORTED.includes(fromUrl)) return fromUrl;
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && SUPPORTED.includes(stored)) return stored;
  } catch {}
  // First visit with no explicit choice: match the browser's preferred
  // languages so zh-* / hi-* visitors land in their language automatically.
  // Falls through to English when nothing matches.
  try {
    const navLangs = navigator.languages && navigator.languages.length
      ? navigator.languages
      : [navigator.language];
    for (const tag of navLangs) {
      if (!tag) continue;
      const base = tag.toLowerCase().split('-')[0];
      if (SUPPORTED.includes(base)) return base;
    }
  } catch {}
  return DEFAULT_LOCALE;
}

async function loadModule(name, lang) {
  try {
    const res = await fetch(`/i18n/_modules/${name}.${lang}.json`, { cache: 'no-cache' });
    if (!res.ok) return {};
    return await res.json();
  } catch {
    return {};
  }
}

async function loadLocale(lang) {
  const merged = {};
  const results = await Promise.all(MODULES.map((m) => loadModule(m, lang)));
  for (let i = 0; i < MODULES.length; i++) {
    const ns = MODULES[i];
    merged[ns] = results[i] || {};
  }
  dicts[lang] = merged;
}

function lookup(key, lang) {
  const dict = dicts[lang];
  if (!dict) return undefined;
  const parts = key.split('.');
  let node = dict;
  for (const p of parts) {
    if (node && typeof node === 'object' && p in node) node = node[p];
    else return undefined;
  }
  return typeof node === 'string' ? node : undefined;
}

function interpolate(tpl, params) {
  if (!params) return tpl;
  return tpl.replace(/\{(\w+)\}/g, (_, k) => (k in params ? String(params[k]) : `{${k}}`));
}

function t(key, params) {
  let val = lookup(key, currentLocale);
  if (val === undefined && currentLocale !== DEFAULT_LOCALE) {
    val = lookup(key, DEFAULT_LOCALE);
  }
  if (val === undefined) return key; // visible fallback for missing keys
  return interpolate(val, params);
}

function applyDom(root) {
  const scope = root || document;
  // Text content
  scope.querySelectorAll('[data-i18n]').forEach((el) => {
    const key = el.getAttribute('data-i18n');
    if (!key) return;
    const val = t(key);
    if (el.dataset.i18nHtml === '1') el.innerHTML = val;
    else el.textContent = val;
  });
  // Attributes: data-i18n-attr="placeholder:chat.input.placeholder,title:chat.input.title"
  scope.querySelectorAll('[data-i18n-attr]').forEach((el) => {
    const spec = el.getAttribute('data-i18n-attr');
    if (!spec) return;
    spec.split(',').forEach((pair) => {
      const [attr, key] = pair.split(':').map((s) => s.trim());
      if (!attr || !key) return;
      el.setAttribute(attr, t(key));
    });
  });
  // <html lang="…">
  if (document.documentElement) document.documentElement.setAttribute('lang', currentLocale);
}

async function setLocale(lang) {
  if (!SUPPORTED.includes(lang)) return;
  if (lang === currentLocale && Object.keys(dicts[lang] || {}).length) return;
  currentLocale = lang;
  try { localStorage.setItem(STORAGE_KEY, lang); } catch {}
  if (!Object.keys(dicts[lang] || {}).length) await loadLocale(lang);
  applyDom();
  for (const fn of changeListeners) {
    try { fn(currentLocale); } catch (e) { console.error('[i18n] listener error', e); }
  }
}

function onChange(fn) { changeListeners.add(fn); return () => changeListeners.delete(fn); }
function onReady(fn) { ready.then(fn); }

async function init() {
  await loadLocale(currentLocale);
  if (currentLocale !== DEFAULT_LOCALE) await loadLocale(DEFAULT_LOCALE);
  applyDom();
  readyResolve();
}

const api = { t, applyDom, setLocale, onChange, onReady, ready, get locale() { return currentLocale; }, SUPPORTED };
window.i18n = api;
window.t = t; // global shortcut for convenience inside JS files

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init, { once: true });
} else {
  init();
}

export default api;
export { t, applyDom, setLocale, onChange, onReady };
