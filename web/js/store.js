// web/js/store.js
// Minimal pub/sub store. Replaces the window.* globals as the inter-module
// state channel over phases B–D. ~40 lines, no deps.
//
// Usage:
//   import { store } from "./store.js";
//   const off = store.subscribe("board", (b) => render(b));
//   store.set("board", parsedBoard);   // notifies subscribers
//   const b = store.get("board");
//   off();                              // unsubscribe
//
// Phase C keys (written by the router via shared/context.js):
//   "device"  -> active device slug (string | null)
//   "repair"  -> active repair id   (string | null)

const _state = new Map();
const _subs = new Map(); // key -> Set<cb>

export const store = {
  get(key) {
    return _state.get(key);
  },
  set(key, value) {
    _state.set(key, value);
    const subs = _subs.get(key);
    if (subs) for (const cb of subs) {
      try { cb(value); } catch (e) { console.error(`[store] subscriber for "${key}" threw`, e); }
    }
  },
  subscribe(key, cb) {
    let subs = _subs.get(key);
    if (!subs) { subs = new Set(); _subs.set(key, subs); }
    subs.add(cb);
    return () => subs.delete(cb);
  },
};
