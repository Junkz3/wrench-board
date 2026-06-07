// Demo session replayer — plays a RECORDED diagnostic session into the live
// chat rendering path with no WebSocket and no LLM call. The onboarding demo
// uses it to show the agent "in action" for free and deterministically.
//
// Played in small NARRATED BEATS (not one autoplay dump): the orchestrator in
// coaching.js plays a slice of frames, pauses on an explainer bubble, then plays
// the next slice on "Next". Recorded server→client frames are fed through
// llm.js's `handleDiagnosticFrame` — the EXACT path a live frame takes — at a
// capped cadence. Capture tool: scripts/capture_demo_session.py; fixtures in
// web/demos/.

import { handleDiagnosticFrame, openPanelForReplay, setDemoOffline } from "../../../llm.js";

// Lifecycle / modal frames whose LIVE side-effects would break a passive replay:
// session_ready re-aligns the tier and can close+reopen the socket; the protocol
// confirmation frames pop a blocking modal; history/context frames wipe the log.
// The agent's CONTENT — messages, thinking, tool calls, boardview.* visuals, the
// protocol wizard (proposed/updated/completed) + steps, simulation overlays,
// turn costs — all replays untouched.
const SKIP = new Set([
  "session_ready", "protocol_cleared", "context_loaded", "context_lost",
  "session_resumed", "session_resumed_summary", "history_replay_start",
  "history_replay_end", "memory_store_setup_failed",
  "protocol_pending_confirmation", "protocol_confirmation_timeout",
  // `highlight` calls the viewer's selectItem, which pops the component
  // INSPECTOR panel over the board (clutter). We keep the agent's arrows +
  // labels (the capability worth showing) but drop highlight, and drive a clean
  // deliberate zoom per beat (coaching.js). Arrows only render once the board's
  // camera is ready — coaching.js waits for that before the board beat (else the
  // projection yields `translate(NaN,NaN)`).
  "boardview.highlight",
]);

// Resolve once the board viewer has a board + camera ready (arrows/annotations
// project through it). Bounded poll so the demo never wedges.
export function waitForBoard(timeoutMs = 6000) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    const tick = () => {
      if (window.Boardview && window.Boardview.hasBoard && window.Boardview.hasBoard()) return resolve(true);
      if (Date.now() - t0 > timeoutMs) return resolve(false);
      setTimeout(tick, 120);
    };
    tick();
  });
}

let _cancel = false;
const _sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Stop a running beat (e.g. the tech skips the tour mid-playback).
export function cancelDemoReplay() { _cancel = true; }

// Fetch a recorded session and return its server→client frames in order
// ({ ms, dir, frame }). Indices into this array are what coaching.js slices by.
export async function loadRecvFrames(url) {
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    return (data.frames || []).filter((f) => f.dir === "recv");
  } catch (err) {
    console.warn("[demoReplay] load failed", err);
    return [];
  }
}

// Open the chat OFFLINE (no socket, no conversation fetch) and render the
// opening user message. Call once before the first beat.
export function beginDemoReplay({ userText = null } = {}) {
  _cancel = false;
  setDemoOffline(true);
  openPanelForReplay();
  if (userText) handleDiagnosticFrame({ type: "message", role: "user", text: userText });
}

// Play one beat: a slice of recv frames, paced by their captured deltas (capped).
export async function playFrames(slice, { gapCapMs = 700 } = {}) {
  const frames = slice.filter((f) => !SKIP.has(f.frame && f.frame.type));
  let prev = frames.length ? frames[0].ms : 0;
  for (const f of frames) {
    if (_cancel) return;
    const gap = Math.min(Math.max(f.ms - prev, 0), gapCapMs);
    prev = f.ms;
    if (gap) await _sleep(gap);
    if (_cancel) return;
    try { handleDiagnosticFrame(f.frame); }
    catch (err) { console.warn("[demoReplay] frame failed", f.frame && f.frame.type, err); }
  }
}

// Leave offline mode (live chat behaves normally again).
export function endDemoReplay() { setDemoOffline(false); }
