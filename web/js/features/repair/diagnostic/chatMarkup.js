// Diagnostic chat — agent markdown rendering + clickable refdes/net chips
// (Phase D.6 extraction from llm.js). Takes the agent's raw text, renders it
// through marked + DOMPurify (or a plain-text fallback when the CDN is absent),
// then walks the result and turns board-validated refdes / net tokens into
// clickable chips that drive the boardview. No module state.
//
// Permanent coupling: reads window.Boardview (the classic non-ESM renderer
// bridge — see CLAUDE.md "Permanent globals") to validate tokens and to act on
// chip clicks. marked / DOMPurify are global CDN scripts, referenced bare.

import { escapeHtml as escapeHTML } from '../../../shared/dom.js';
import { repairHash, parseRoute } from '../../../router.js';
import { getRepairId } from '../../../shared/context.js';

// Regex shapes. Kept loose — the semantic filter is the Boardview lookup.
const RE_REFDES = /\b[A-Z]{1,3}\d{1,4}\b/g;
// Nets: common naming conventions used in iPhone / Mac / Pi schematics.
// Over-matches on purpose; Boardview.hasNet is the truth gate.
const RE_NET = /\b(?:PP_[A-Z0-9_]+|[PN]P_[A-Z0-9_]+|L\d{1,3}|VCC(?:_[A-Z0-9_]+)?|VDD(?:_[A-Z0-9_]+)?|AVDD(?:_[A-Z0-9_]+)?|DVDD(?:_[A-Z0-9_]+)?|GND(?:_[A-Z0-9_]+)?|[A-Z][A-Z0-9_]{3,})\b/g;
const RE_UNKNOWN_REFDES = /⟨\?([A-Z]{1,3}\d{1,4})⟩/g;

// Parse markdown → sanitize → walk text nodes → replace validated tokens
// with clickable chips. If marked / DOMPurify aren't on the page, fall back
// to plain text (defensive: network hiccup loading the CDN).
export function renderAgentMarkup(container, text) {
  let html;
  if (typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
    const raw = marked.parse(text, { breaks: true, gfm: true });
    html = DOMPurify.sanitize(raw, {
      ALLOWED_TAGS: ["p", "br", "strong", "em", "ul", "ol", "li", "code"],
      ALLOWED_ATTR: [],
    });
  } else {
    html = escapeHTML(text).replaceAll("\n", "<br>");
  }
  container.innerHTML = html;
  decorateChipsIn(container);
}

// Walk all text nodes under `root` and replace validated refdes / net
// tokens with clickable chips, plus unknown-refdes ⟨?U999⟩ with amber
// span. Text inside <code> is skipped (agent's verbatim intent).
function decorateChipsIn(root) {
  const hasBoard = !!(window.Boardview && window.Boardview.hasBoard && window.Boardview.hasBoard());
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      if (!n.nodeValue) return NodeFilter.FILTER_REJECT;
      if (n.parentElement && n.parentElement.closest("code, .refdes-unknown, .chip-refdes, .chip-net"))
        return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const targets = [];
  while (walker.nextNode()) targets.push(walker.currentNode);
  for (const textNode of targets) decorateOneTextNode(textNode, hasBoard);
}

function decorateOneTextNode(textNode, hasBoard) {
  const original = textNode.nodeValue;
  const matches = [];
  for (const m of original.matchAll(RE_UNKNOWN_REFDES)) {
    matches.push({ kind: "unknown", start: m.index, end: m.index + m[0].length, raw: m[0], inner: m[1] });
  }
  for (const m of original.matchAll(RE_REFDES)) {
    if (hasBoard && window.Boardview.hasRefdes(m[0])) {
      matches.push({ kind: "refdes", start: m.index, end: m.index + m[0].length, raw: m[0] });
    }
  }
  for (const m of original.matchAll(RE_NET)) {
    if (hasBoard && window.Boardview.hasNet(m[0])) {
      matches.push({ kind: "net", start: m.index, end: m.index + m[0].length, raw: m[0] });
    }
  }
  if (matches.length === 0) return;
  // Resolve overlaps: earliest-start first, ties broken by longest-wins.
  matches.sort((a, b) => a.start - b.start || (b.end - b.start) - (a.end - a.start));
  const cleaned = [];
  let cursor = 0;
  for (const m of matches) {
    if (m.start < cursor) continue;
    cleaned.push(m);
    cursor = m.end;
  }
  const frag = document.createDocumentFragment();
  let i = 0;
  for (const m of cleaned) {
    if (m.start > i) frag.appendChild(document.createTextNode(original.slice(i, m.start)));
    frag.appendChild(makeChipNode(m));
    i = m.end;
  }
  if (i < original.length) frag.appendChild(document.createTextNode(original.slice(i)));
  textNode.parentNode.replaceChild(frag, textNode);
}

// Chip-click target: switch the main view to #pcb if we're not already there,
// then run the boardview action. The panel is push-mode so the board shows
// to the left while the chat stays visible on the right (420 px strip). When
// we have to navigate, wait two animation frames so the section becomes
// visible and brd_viewer's ResizeObserver sees non-zero canvas dimensions —
// otherwise the focus pan would compute against a 0×0 canvas and end up off
// screen (the ResizeObserver now flushes any pending focus on its own, but
// the nav-then-apply ordering also lets non-focus actions see the real DOM).
function gotoBoardviewThen(fn) {
  const route = parseRoute();
  if (route.level === "repair" && route.vue === "pcb") {
    fn();
    return;
  }
  // Navigate to the pcb vue of the active repair (canonical hash route).
  const id = route.level === "repair" ? route.id : getRepairId();
  if (id) window.location.hash = repairHash(id, "pcb");
  requestAnimationFrame(() => requestAnimationFrame(fn));
}

function makeChipNode(match) {
  if (match.kind === "refdes") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip-refdes";
    btn.dataset.refdes = match.raw;
    btn.textContent = match.raw;
    btn.addEventListener("click", () => {
      gotoBoardviewThen(() => window.Boardview?.focusRefdes?.(match.raw));
    });
    return btn;
  }
  if (match.kind === "net") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip-net";
    btn.dataset.net = match.raw;
    btn.textContent = match.raw;
    btn.addEventListener("click", () => {
      gotoBoardviewThen(() => window.Boardview?.highlightNet?.(match.raw));
    });
    return btn;
  }
  const span = document.createElement("span");
  span.className = "refdes-unknown";
  span.textContent = match.raw;
  return span;
}
