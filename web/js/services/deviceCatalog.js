// Device catalogue read service — single cached source for the flattened
// device list, fed by GET /pipeline/taxonomy. Consumed by the landing
// autocomplete (features/global/landing/index.js) and the catalogue modal
// (features/global/landing/catalogue.js). Data-only: no DOM, no rendering.

import { apiGet } from "../shared/api.js";
import { prettifySlug } from "../shared/dom.js";

let _cache = null;

// Flatten a TaxonomyTree into one entry PER pack — every board revision /
// variant a model has surfaces as its own suggestion (e.g. the two iPhone X
// boards, Qualcomm vs Intel), disambiguated by `version`. Same set as
// flattenBoards (the catalogue), only re-sorted for the autocomplete: complete
// first, then alphabetical by label. Collapsing to one-per-(brand,model) used
// to hide the non-canonical variant — unreachable from search even though the
// catalogue showed it.
export function flattenTaxonomy(tree) {
  const out = flattenBoards(tree);
  out.sort((a, b) => {
    if (a.complete !== b.complete) return a.complete ? -1 : 1;
    return a.label.localeCompare(b.label);
  });
  return out;
}

// Cached for the session. `force: true` re-fetches (e.g. after a new build).
export async function loadDevices({ force = false } = {}) {
  if (_cache && !force) return _cache;
  const tree = await apiGet("/pipeline/taxonomy");
  _cache = flattenTaxonomy(tree);
  return _cache;
}

// One entry PER pack (board revision) — every variant of a model (e.g. both
// MacBook Pro 13" boards: the A1706/A1708 and the M1 A2338) disambiguated by
// `version`. The base of flattenTaxonomy too (which only re-sorts the result);
// kept separate so the catalogue's board step gets the packs in API order.
export function flattenBoards(tree) {
  const out = [];
  const push = (brand, model, p) => out.push({
    label: model || p.device_label || prettifySlug(p.device_slug),
    subtitle: brand || null,
    slug: p.device_slug,
    device_label: p.device_label || model || prettifySlug(p.device_slug),
    version: p.version || null,
    form_factor: p.form_factor || null,
    complete: Boolean(p.complete),
    has_electrical_graph: Boolean(p.has_electrical_graph),
    device_kind: p.device_kind || null,
    aliases: Array.isArray(p.aliases) ? p.aliases : [],
  });
  const brands = (tree && tree.brands) || {};
  for (const [brand, models] of Object.entries(brands)) {
    for (const [model, packs] of Object.entries(models || {})) {
      for (const p of packs || []) push(brand, model, p);
    }
  }
  for (const p of (tree && tree.uncategorized) || []) {
    if (p && p.device_slug) push(null, p.device_label || prettifySlug(p.device_slug), p);
  }
  return out;
}

let _boardsCache = null;

export async function loadBoards({ force = false } = {}) {
  if (_boardsCache && !force) return _boardsCache;
  const tree = await apiGet("/pipeline/taxonomy");
  _boardsCache = flattenBoards(tree);
  return _boardsCache;
}
