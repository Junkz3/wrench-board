// Diagnostic chat — session cost accumulator + status-bar readout (Phase D.6
// extraction from llm.js). Owns the per-session cost state (reset on each
// reconnect; the backend emits `turn_cost` after every agent inference turn).
// The module is the single owner of these three counters — llm.js drives it
// through resetCost() / recordTurnCost() and reads formatted values via fmtUsd.

// Session cost accumulator — reset on each (re)connect.
let sessionCostUsd = 0;
let sessionTurns = 0;
let lastTurnCostUsd = 0;

export function fmtUsd(amount) {
  if (amount >= 1) return `$${amount.toFixed(2)}`;
  if (amount >= 0.01) return `$${amount.toFixed(3)}`;
  if (amount >= 0.0001) return `$${amount.toFixed(4)}`;
  return amount > 0 ? `<$0.0001` : `$0.00`;
}

// Render the running total into the status-bar chip (#llmCostTotal). Hidden
// until the first priced turn lands; "hot" once the session or last turn
// crosses a spend threshold.
export function updateCostTotal() {
  const el2 = document.getElementById("llmCostTotal");
  if (!el2) return;
  if (sessionTurns === 0) {
    el2.style.display = "none";
    return;
  }
  el2.style.display = "";
  const deltaPart = lastTurnCostUsd > 0 ? ` · +${fmtUsd(lastTurnCostUsd)} last` : "";
  el2.textContent = `${fmtUsd(sessionCostUsd)} · ${sessionTurns} turn${sessionTurns > 1 ? "s" : ""}${deltaPart}`;
  el2.classList.toggle("hot", sessionCostUsd >= 0.50 || lastTurnCostUsd >= 0.10);
}

// Zero the counters (new connection = new cost scope). Does NOT repaint — the
// caller calls updateCostTotal() once after resetting all its other state, to
// preserve the original single-repaint ordering.
export function resetCost() {
  sessionCostUsd = 0;
  sessionTurns = 0;
  lastTurnCostUsd = 0;
}

// Fold a `turn_cost` event into the running total and repaint the chip.
export function recordTurnCost(payload) {
  lastTurnCostUsd = Number(payload.cost_usd || 0);
  sessionCostUsd += lastTurnCostUsd;
  sessionTurns += 1;
  updateCostTotal();
}
