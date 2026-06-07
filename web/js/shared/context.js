// web/js/shared/context.js
// Single source for "which device / repair am I in", read by every view.
// Backed by store.js. The router is the only writer (via setContext); views
// are read-only consumers. Replaces the scattered URLSearchParams(...).get("device")
// / .get("repair") reads across graph/schematic/memory_bank/llm/pcb_bridge/
// brd_viewer/home. Phase C: in C.1 the router mirrors the query string into the
// store; in C.3 the source switches to the hash route (#repair/:id/<vue>) without
// changing this read contract.
import { store } from "../store.js";

/** Active device slug, or null when outside any repair. */
export function getDeviceSlug() {
  return store.get("device") || null;
}

/** Active repair id, or null when outside any repair. */
export function getRepairId() {
  return store.get("repair") || null;
}

/**
 * Set the active context. Called by the router after it resolves the route.
 * Passing a falsy value clears the key. Subscribers on "device"/"repair" are
 * notified.
 */
export function setContext({ device, repair }) {
  store.set("device", device || null);
  store.set("repair", repair || null);
}
