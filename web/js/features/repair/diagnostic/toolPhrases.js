// Diagnostic chat — tool-call paraphrases (Phase D.6 extraction from llm.js).
// Pure presentation: maps a tool name + its input object to {icon, phraseHTML}
// for the turn-rail step line. No module state. Consumed by the WS message
// dispatcher in llm.js (tool_use / memory_tool_use events).
//
// `t` is resolved through the global window.t (set by i18n.js, a classic
// non-ESM script) at CALL time so strings re-render on locale switch — mirrors
// the memory_bank.js / graph.js convention. escapeHTML guards every
// interpolated user/tool value.

import { escapeHtml as escapeHTML } from '../../../shared/dom.js';

const t = (key, params) => (window.t ? window.t(key, params) : key);

// Family icons for tool-call steps. 12×12, inline SVG, stroke currentColor.
const ICON_MB =
  '<svg class="step-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" ' +
  'aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/>' +
  '<circle cx="12" cy="12" r="3"/></svg>';
const ICON_BV =
  '<svg class="step-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" ' +
  'aria-hidden="true"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/>' +
  '<circle cx="12" cy="12" r="1.2" fill="currentColor"/></svg>';
// MEM = MA-native filesystem ops on the device's memory store (read / write /
// edit / grep / glob), surfaced via the agent_toolset_20260401 toolset. Cylinder
// = persistent storage. Distinct from MB (knowledge bank queries via mb_*).
const ICON_MEM =
  '<svg class="step-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" ' +
  'aria-hidden="true"><ellipse cx="12" cy="5" rx="8" ry="2.5"/>' +
  '<path d="M4 5v14c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5V5"/>' +
  '<path d="M4 12c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5"/></svg>';
// STOCK = donor inventory + parts harvest. Box / crate metaphor matches
// the rail icon for #stock so the chat step is recognisable as the same
// surface the technician sees in the workspace section.
const ICON_STOCK =
  '<svg class="step-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" ' +
  'aria-hidden="true"><rect x="3" y="6" width="18" height="13" rx="1.5"/>' +
  '<path d="M3 10h18M8 6v4M16 6v4"/></svg>';

// Localized paraphrase + family icon for each known tool name. Each entry
// is a function receiving the tool input object and returning
// {icon, phraseHTML}. phraseHTML may embed a <span class="refdes"> or
// <span class="net"> for typographic emphasis on the target; all user
// input is passed through escapeHTML before interpolation. Strings come
// from i18n via window.t() so they re-render on locale switch.
export const TOOL_PHRASES = {
  // --- MB (memory bank — perception / reading) ---
  mb_get_component: (i) => {
    const refdes = escapeHTML(i?.refdes || "?");
    return {
      icon: ICON_MB,
      phraseHTML: t('chat.tool.mb_get_component', { refdes }),
      // Coalesce a run of component lookups into one dimmed line (chatLog.js):
      // the target chip is what gets appended to the inline list.
      group: { key: 'tool:mb_get_component', item: `<span class="refdes">${refdes}</span>` },
    };
  },
  mb_get_rules_for_symptoms: (i) => {
    const syms = Array.isArray(i?.symptoms) ? i.symptoms.join(", ") : (i?.symptoms || "");
    return {
      icon: ICON_MB,
      phraseHTML: t('chat.tool.mb_get_rules_for_symptoms', { symptoms: escapeHTML(syms) }),
    };
  },
  mb_list_findings: (i) => ({
    icon: ICON_MB,
    phraseHTML: i?.device
      ? t('chat.tool.mb_list_findings_for', { device: escapeHTML(i.device) })
      : t('chat.tool.mb_list_findings'),
  }),
  mb_record_finding: () => ({
    icon: ICON_MB,
    phraseHTML: t('chat.tool.mb_record_finding'),
  }),
  mb_expand_knowledge: (i) => {
    const scope = [i?.component, i?.symptom].filter(Boolean).join(" / ");
    return {
      icon: ICON_MB,
      phraseHTML: scope
        ? t('chat.tool.mb_expand_knowledge_scope', { scope: escapeHTML(scope) })
        : t('chat.tool.mb_expand_knowledge'),
    };
  },
  mb_schematic_graph: () => ({
    icon: ICON_MB,
    phraseHTML: t('chat.tool.mb_schematic_graph'),
  }),

  // --- BV (boardview — action) ---
  bv_highlight: (i) => {
    const r = Array.isArray(i?.refdes) ? i.refdes.join(", ") : (i?.refdes || "?");
    return {
      icon: ICON_BV,
      phraseHTML: t('chat.tool.bv_highlight', { refdes: escapeHTML(r) }),
    };
  },
  bv_focus: (i) => ({
    icon: ICON_BV,
    phraseHTML: t('chat.tool.bv_focus', { refdes: escapeHTML(i?.refdes || "?") }),
  }),
  bv_reset_view: () => ({ icon: ICON_BV, phraseHTML: t('chat.tool.bv_reset_view') }),
  bv_highlight_net: (i) => ({
    icon: ICON_BV,
    phraseHTML: t('chat.tool.bv_highlight_net', { net: escapeHTML(i?.net || "?") }),
  }),
  bv_flip: () => ({ icon: ICON_BV, phraseHTML: t('chat.tool.bv_flip') }),
  bv_annotate: (i) => {
    let phraseHTML;
    if (i?.refdes) {
      phraseHTML = t('chat.tool.bv_annotate_near', { refdes: escapeHTML(i.refdes) });
    } else if (Number.isFinite(i?.x) && Number.isFinite(i?.y)) {
      phraseHTML = t('chat.tool.bv_annotate_at', { x: i.x, y: i.y });
    } else {
      phraseHTML = t('chat.tool.bv_annotate_blank');
    }
    return { icon: ICON_BV, phraseHTML };
  },
  bv_filter_by_type: (i) => ({
    icon: ICON_BV,
    phraseHTML: t('chat.tool.bv_filter_by_type', { prefix: escapeHTML(i?.prefix || "?") }),
  }),
  bv_draw_arrow: (i) => ({
    icon: ICON_BV,
    phraseHTML: t('chat.tool.bv_draw_arrow', {
      from: escapeHTML(i?.from_refdes || "?"),
      to: escapeHTML(i?.to_refdes || "?"),
    }),
  }),
  bv_measure: (i) => ({
    icon: ICON_BV,
    phraseHTML: t('chat.tool.bv_measure', {
      a: escapeHTML(i?.refdes_a || "?"),
      b: escapeHTML(i?.refdes_b || "?"),
    }),
  }),
  bv_show_pin: (i) => ({
    icon: ICON_BV,
    phraseHTML: t('chat.tool.bv_show_pin', {
      pin: escapeHTML(String(i?.pin ?? "?")),
      refdes: escapeHTML(i?.refdes || "?"),
    }),
  }),
  bv_dim_unrelated: () => ({ icon: ICON_BV, phraseHTML: t('chat.tool.bv_dim_unrelated') }),
  bv_layer_visibility: (i) => ({
    icon: ICON_BV,
    phraseHTML: t('chat.tool.bv_layer_visibility', { layer: escapeHTML(i?.layer || "?") }),
  }),
  bv_scene: (i) => {
    const parts = [];
    const hl = Array.isArray(i?.highlights) ? i.highlights.length : 0;
    const an = Array.isArray(i?.annotations) ? i.annotations.length : 0;
    const ar = Array.isArray(i?.arrows) ? i.arrows.length : 0;
    if (hl) parts.push(t(hl > 1 ? 'chat.tool.scene_highlight_many' : 'chat.tool.scene_highlight_one', { n: hl }));
    if (an) parts.push(t(an > 1 ? 'chat.tool.scene_annotation_many' : 'chat.tool.scene_annotation_one', { n: an }));
    if (ar) parts.push(t(ar > 1 ? 'chat.tool.scene_arrow_many' : 'chat.tool.scene_arrow_one', { n: ar }));
    if (i?.focus?.refdes) parts.push(t('chat.tool.scene_focus', { refdes: escapeHTML(i.focus.refdes) }));
    if (i?.dim_unrelated) parts.push(t('chat.tool.scene_dim'));
    if (i?.reset) parts.unshift(t('chat.tool.scene_reset'));
    return {
      icon: ICON_BV,
      phraseHTML: parts.length ? t('chat.tool.bv_scene', { parts: parts.join(", ") }) : t('chat.tool.bv_scene_empty'),
    };
  },

  // --- Stock (donor inventory + part harvest) ---
  stock_search: (i) => {
    const tp = i?.type || "";
    const v = i?.value_canonical || i?.mpn || "";
    return {
      icon: ICON_STOCK,
      phraseHTML: (tp || v)
        ? t('chat.tool.stock_search', { type: escapeHTML(tp), value: escapeHTML(v) })
        : t('chat.tool.stock_search_minimal'),
    };
  },
  stock_consume: (i) => ({
    icon: ICON_STOCK,
    phraseHTML: t('chat.tool.stock_consume', {
      refdes: escapeHTML(i?.refdes || "?"),
      donor_id: escapeHTML(i?.donor_id || "?"),
    }),
  }),
  stock_mark_donor: (i) => ({
    icon: ICON_STOCK,
    phraseHTML: t('chat.tool.stock_mark_donor', { device_slug: escapeHTML(i?.device_slug || "?") }),
  }),
  stock_unmark_donor: (i) => ({
    icon: ICON_STOCK,
    phraseHTML: t('chat.tool.stock_unmark_donor', { donor_id: escapeHTML(i?.donor_id || "?") }),
  }),
  stock_list_donors: () => ({
    icon: ICON_STOCK,
    phraseHTML: t('chat.tool.stock_list_donors'),
  }),
};

export function toolFallback(name) {
  return {
    icon: "",
    phraseHTML: `<span class="tool-name-raw">${escapeHTML(name)}</span>`,
  };
}

// Strip the `/mnt/memory/{slug}/` MA-mount prefix so memory paths read as
// short relative paths (`outcomes/abc.json`) instead of full absolute ones.
function memPath(p) {
  if (!p) return "";
  const m = String(p).match(/^\/mnt\/memory\/[^/]+\/(.+)$/);
  return m ? m[1] : String(p);
}

function memPathChip(p) {
  return `<code class="mem-path">${escapeHTML(memPath(p))}</code>`;
}

// Glob/grep patterns are shown raw (not mount-stripped) — same chip styling.
function patternChip(p) {
  return `<code class="mem-path">${escapeHTML(String(p || ""))}</code>`;
}

// Localized paraphrase + ICON_MEM for each MA-native filesystem tool. Same
// shape contract as TOOL_PHRASES — receives the tool input object and
// returns {icon, phraseHTML}.
// `group.item` is the inline target chip appended when the SAME memory op fires
// again back-to-back (chatLog.js coalesces the run into one dimmed line). The
// key is per-op so a `read` run and a `glob` run don't merge into each other.
export const MEMORY_TOOL_PHRASES = {
  read: (i) => {
    const chip = memPathChip(i?.file_path || i?.path);
    return { icon: ICON_MEM, phraseHTML: t('chat.memtool.read', { path: chip }), group: { key: 'mem:read', item: chip } };
  },
  write: (i) => {
    const chip = memPathChip(i?.file_path || i?.path);
    return { icon: ICON_MEM, phraseHTML: t('chat.memtool.write', { path: chip }), group: { key: 'mem:write', item: chip } };
  },
  edit: (i) => {
    const chip = memPathChip(i?.file_path || i?.path);
    return { icon: ICON_MEM, phraseHTML: t('chat.memtool.edit', { path: chip }), group: { key: 'mem:edit', item: chip } };
  },
  view: (i) => {
    const chip = memPathChip(i?.file_path || i?.path);
    return { icon: ICON_MEM, phraseHTML: t('chat.memtool.view', { path: chip }), group: { key: 'mem:view', item: chip } };
  },
  grep: (i) => {
    const chip = patternChip(i?.pattern);
    return {
      icon: ICON_MEM,
      phraseHTML: i?.path
        ? t('chat.memtool.grep_in', { pattern: escapeHTML(String(i?.pattern || "")), path: memPathChip(i.path) })
        : t('chat.memtool.grep', { pattern: escapeHTML(String(i?.pattern || "")) }),
      group: { key: 'mem:grep', item: chip },
    };
  },
  glob: (i) => {
    const chip = patternChip(i?.pattern);
    return { icon: ICON_MEM, phraseHTML: t('chat.memtool.glob', { pattern: escapeHTML(String(i?.pattern || "")) }), group: { key: 'mem:glob', item: chip } };
  },
  list: (i) => ({
    icon: ICON_MEM,
    phraseHTML: i?.path
      ? t('chat.memtool.list_at', { path: memPathChip(i.path) })
      : t('chat.memtool.list'),
    group: { key: 'mem:list', item: i?.path ? memPathChip(i.path) : "" },
  }),
  ls: (i) => ({
    icon: ICON_MEM,
    phraseHTML: i?.path
      ? t('chat.memtool.list_at', { path: memPathChip(i.path) })
      : t('chat.memtool.list'),
    group: { key: 'mem:list', item: i?.path ? memPathChip(i.path) : "" },
  }),
};

export function memToolFallback(name) {
  return {
    icon: ICON_MEM,
    phraseHTML: `<span class="tool-name-raw">${escapeHTML(name)}</span>`,
  };
}
