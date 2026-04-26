// Wrench Board mascot — clones <template id="tpl-mascot"> into a target,
// applies the size + state classes, returns the mounted <svg>. Pair with
// `web/styles/mascot.css` (idle breathing, blink, plus the four state
// animations: is-thinking / is-working / is-success / is-error).

const VALID_SIZES = new Set(["xs", "sm", "md", "lg"]);
const VALID_STATES = new Set(["idle", "thinking", "working", "success", "error"]);

function getTemplate() {
  const tpl = document.getElementById("tpl-mascot");
  if (!tpl || !(tpl instanceof HTMLTemplateElement)) {
    console.warn("[mascot] <template id=\"tpl-mascot\"> not found in document");
    return null;
  }
  return tpl;
}

/**
 * Clone the mascot SVG into `target`, replacing whatever was there.
 * @param {Element} target  Mount point (div / span / etc.)
 * @param {{size?: string, state?: string}} [opts]
 *   size: xs (32px) | sm (80px) | md (160px) | lg (320px), default "sm"
 *   state: idle | thinking | working | success | error, default "idle"
 * @returns {SVGSVGElement|null} The mounted SVG, or null if mount failed.
 */
export function mountMascot(target, opts = {}) {
  if (!target) return null;
  const tpl = getTemplate();
  if (!tpl) return null;

  const size = VALID_SIZES.has(opts.size) ? opts.size : "sm";
  const state = VALID_STATES.has(opts.state) ? opts.state : "idle";

  const clone = tpl.content.firstElementChild.cloneNode(true);
  clone.classList.add("mascot", `mascot-${size}`, `is-${state}`);
  target.replaceChildren(clone);
  return clone;
}

/**
 * Toggle the state class on a mounted mascot (or any element marked .mascot).
 * Removes any existing is-* class before adding the new one. Pass null/undefined
 * state to clear all state classes (returns to idle defaults).
 */
export function setMascotState(svg, state) {
  if (!svg) return;
  for (const cls of [...svg.classList]) {
    if (cls.startsWith("is-")) svg.classList.remove(cls);
  }
  if (state && VALID_STATES.has(state)) {
    svg.classList.add(`is-${state}`);
  }
}
